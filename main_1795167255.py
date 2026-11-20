#!/usr/bin/env python3
import os, re, json, asyncio, logging, time, pathlib
from datetime import datetime, timezone

import aiohttp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command, CommandObject

# Optional: dotenv for local dev (not required on Railway)
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ------------------ ENV helpers ------------------
def getenv_str(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return ("" if v is None else str(v)).strip()

def _to_int_safe(s: str, default: int = 0) -> int:
    # keep only digits and minus, e.g. 'ID: -100123...' -> '-100123...'
    cleaned = re.sub(r"[^\d\-]", "", s or "")
    try:
        return int(cleaned) if cleaned else int(default)
    except ValueError:
        return int(default)

def getenv_int(name: str, default: int = 0) -> int:
    return _to_int_safe(getenv_str(name, str(default)), default)

def getenv_bool(name: str, default: bool = False) -> bool:
    val = getenv_str(name, "true" if default else "false").lower()
    return val in {"1", "true", "yes", "y", "on"}

def getenv_ids_list(name: str):
    raw = getenv_str(name, "")
    if not raw:
        return []
    parts = re.split(r"[,\s]+", raw)
    out = []
    for p in parts:
        p = re.sub(r"[^\d\-]", "", p)
        if p:
            try:
                out.append(int(p))
            except ValueError:
                pass
    return out

# ------------------ Config ------------------
TG_BOT_TOKEN    = getenv_str("TG_BOT_TOKEN")
TG_CHANNEL_ID   = getenv_int("TG_CHANNEL_ID", 0)
ADMIN_CHAT_ID   = getenv_int("ADMIN_CHAT_ID", 0)
ADMIN_USER_IDS  = getenv_ids_list("ADMIN_USER_IDS")
OPENAI_API_KEY  = getenv_str("OPENAI_API_KEY", "")
OPENAI_MODEL    = getenv_str("OPENAI_MODEL", "gpt-4o-mini")

VK_ENABLED      = getenv_bool("VK_ENABLED", False)
VK_GROUP_ID     = getenv_int("VK_GROUP_ID", 0)
VK_ACCESS_TOKEN = getenv_str("VK_ACCESS_TOKEN", "")
VK_API_VERSION  = "5.199"

TIMEZONE_NAME   = getenv_str("TIMEZONE", "Europe/Moscow")
QUEUE_PATH      = getenv_str("QUEUE_PATH", "data/queue.json")

# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("rtfm-bot")

# validate token
if not TG_BOT_TOKEN:
    raise SystemExit("ERROR: TG_BOT_TOKEN is not set. Put it into Railway → Variables")

# ensure queue file exists
QUEUE_FILE = pathlib.Path(QUEUE_PATH)
QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
if not QUEUE_FILE.exists():
    QUEUE_FILE.write_text("[]", encoding="utf-8")

# ------------------ Utils ------------------
def now_utc():
    return datetime.now(timezone.utc)

def load_queue():
    try:
        return json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

def save_queue(data):
    QUEUE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def next_id(items):
    mx = 0
    for it in items:
        try:
            mx = max(mx, int(it.get("id", 0)))
        except Exception:
            pass
    return mx + 1

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS

def human_time(ts: float | None):
    if not ts:
        return "—"
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")

# ------------------ VK ------------------
async def vk_post(session: aiohttp.ClientSession, text: str):
    if not (VK_ENABLED and VK_ACCESS_TOKEN and VK_GROUP_ID):
        return {"ok": False, "reason": "VK disabled or not configured"}
    url = "https://api.vk.com/method/wall.post"
    payload = {
        "owner_id": -VK_GROUP_ID,
        "from_group": 1,
        "message": text,
        "access_token": VK_ACCESS_TOKEN,
        "v": VK_API_VERSION
    }
    async with session.post(url, data=payload, timeout=30) as resp:
        try:
            data = await resp.json()
        except Exception:
            data = {"error": f"bad response {resp.status}"}
    return data

# ------------------ LLM (/el, /brief) ------------------
EL_SYSTEM_PROMPT = (
    "Ты — Эл, дружеский альфа-агент RTFM (Read The Friendly Manual).\n"
    "Пиши по делу, коротко, в стиле RTFM: 3–5 буллетов, честные минусы, без воды.\n"
    "Если просят черновик поста — дай заголовок, 3 плюса, 2 минуса, CTA, UTM-плейсхолдеры.\n"
)

async def llm_chat(session: aiohttp.ClientSession, user_prompt: str) -> str:
    if not OPENAI_API_KEY:
        return "LLM не подключён. Добавь OPENAI_API_KEY в Variables, чтобы /el и /brief работали."
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": EL_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt}
        ],
        "temperature": 0.6,
    }
    async with session.post(url, json=payload, headers=headers, timeout=60) as resp:
        data = await resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except Exception:
            return f"LLM error: {data}"

# ------------------ Bot wiring ------------------
bot = Bot(
    token=TG_BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML", link_preview_is_disabled=True)
)
dp = Dispatcher()
router = Router()
dp.include_router(router)

queue = load_queue()

def draft_kb(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Approve TG", callback_data=f"appr:{draft_id}:tg"),
            InlineKeyboardButton(text="✅ TG+VK",   callback_data=f"appr:{draft_id}:both"),
        ],
        [
            InlineKeyboardButton(text="⏰ +1h",      callback_data=f"sched:{draft_id}:1h"),
            InlineKeyboardButton(text="🗑️ Delete",  callback_data=f"del:{draft_id}"),
        ]
    ])

def short_preview(text: str, n: int = 80) -> str:
    t = text.strip().replace("\n", " ")
    return (t[:n] + "…") if len(t) > n else t

async def publish_to_tg(text: str):
    if TG_CHANNEL_ID == 0:
        raise RuntimeError("TG_CHANNEL_ID is not set")
    return await bot.send_message(chat_id=TG_CHANNEL_ID, text=text)

# ------------- Handlers -------------
@router.message(Command("ping"))
async def cmd_ping(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    await msg.reply("pong")

@router.message(Command("help"))
async def cmd_help(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    await msg.reply(
        "/ping — проверка\n"
        "/draft <текст> — создать черновик\n"
        "/queue — список черновиков\n"
        "/approve <id> — опубликовать в TG\n"
        "/approve_vk <id> — опубликовать TG+VK\n"
        "/schedule <id> YYYY-MM-DD HH:MM — отложить (UTC)\n"
        "/el <вопрос> — спросить Эла\n"
        "/brief <тема> — сгенерить черновик"
    )

@router.message(Command("draft"))
async def cmd_draft(msg: Message, command: CommandObject):
    if not is_admin(msg.from_user.id):
        return
    text = (command.args or "").strip()
    if not text:
        await msg.reply("Дай текст: /draft <текст>")
        return
    d = {
        "id": next_id(queue),
        "text": text,
        "created_ts": time.time(),
        "scheduled_ts": None,
        "author_id": msg.from_user.id,
    }
    queue.append(d)
    save_queue(queue)
    await msg.reply(f"Черновик #{d['id']} сохранён:\n\n{text}", reply_markup=draft_kb(d["id"]))

@router.message(Command("queue"))
async def cmd_queue(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    if not queue:
        await msg.reply("Очередь пуста.")
        return
    lines = [f"#{d['id']} • {human_time(d.get('scheduled_ts'))} • {short_preview(d['text'])}" for d in queue]
    await msg.reply("Черновики:\n" + "\n".join(lines))

@router.message(Command("approve"))
async def cmd_approve(msg: Message, command: CommandObject):
    if not is_admin(msg.from_user.id):
        return
    if not command.args or not command.args.strip().isdigit():
        await msg.reply("Используй: /approve <id>")
        return
    did = int(command.args.strip())
    d = next((x for x in queue if x["id"] == did), None)
    if not d:
        await msg.reply("Нет такого черновика")
        return
    await publish_to_tg(d["text"])
    queue.remove(d)
    save_queue(queue)
    await msg.reply(f"Опубликовано в TG: #{did}")

@router.message(Command("approve_vk"))
async def cmd_approve_vk(msg: Message, command: CommandObject):
    if not is_admin(msg.from_user.id):
        return
    if not command.args or not command.args.strip().isdigit():
        await msg.reply("Используй: /approve_vk <id>")
        return
    did = int(command.args.strip())
    d = next((x for x in queue if x["id"] == did), None)
    if not d:
        await msg.reply("Нет такого черновика")
        return
    await publish_to_tg(d["text"])
    async with aiohttp.ClientSession() as session:
        vk_res = await vk_post(session, d["text"])
    queue.remove(d)
    save_queue(queue)
    await msg.reply(f"Опубликовано TG+VK: #{did}\nVK ответ: {vk_res}")

@router.message(Command("schedule"))
async def cmd_schedule(msg: Message, command: CommandObject):
    if not is_admin(msg.from_user.id):
        return
    args = (command.args or "").strip().split()
    if len(args) < 3:
        await msg.reply("Формат: /schedule <id> YYYY-MM-DD HH:MM (UTC)")
        return
    if not args[0].isdigit():
        await msg.reply("Первый параметр должен быть id черновика")
        return
    did = int(args[0])
    d = next((x for x in queue if x["id"] == did), None)
    if not d:
        await msg.reply("Нет такого черновика")
        return
    try:
        ts = datetime.strptime(args[1] + " " + args[2], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        await msg.reply("Не разобрал дату. Пример: /schedule 3 2026-11-20 12:30")
        return
    d["scheduled_ts"] = ts
    save_queue(queue)
    await msg.reply(f"Черновик #{did} отложен на {human_time(ts)}")

@router.message(Command("el"))
async def cmd_el(msg: Message, command: CommandObject):
    if not is_admin(msg.from_user.id):
        return
    question = (command.args or "").strip()
    if not question:
        await msg.reply("Спроси что-нибудь: /el <вопрос>")
        return
    async with aiohttp.ClientSession() as session:
        answer = await llm_chat(session, question)
    await msg.reply(answer)

@router.message(Command("brief"))
async def cmd_brief(msg: Message, command: CommandObject):
    if not is_admin(msg.from_user.id):
        return
    topic = (command.args or "").strip()
    if not topic:
        await msg.reply("Дай тему: /brief <тема поста>")
        return
    prompt = (
        f"Сделай черновик поста RTFM по теме: {topic}. "
        "Формат: Заголовок; 3 плюса; 2 минуса; Где купить (UTM плейсхолдеры); Как встроить (3 шага). Коротко."
    )
    async with aiohttp.ClientSession() as session:
        text = await llm_chat(session, prompt)
    d = {"id": next_id(queue), "text": text, "created_ts": time.time(), "scheduled_ts": None, "author_id": msg.from_user.id}
    queue.append(d)
    save_queue(queue)
    await msg.reply(f"Черновик #{d['id']} по теме «{topic}»:", reply_markup=draft_kb(d["id"]))
    await msg.answer(text)

# ---------- Callbacks ----------
@router.callback_query(F.data.startswith("appr:"))
async def cb_approve(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Недоступно", show_alert=True); return
    _, sid, mode = cb.data.split(":")
    did = int(sid)
    d = next((x for x in queue if x["id"] == did), None)
    if not d:
        await cb.answer("Черновика нет", show_alert=True); return
    await publish_to_tg(d["text"])
    if mode == "both":
        async with aiohttp.ClientSession() as session:
            _ = await vk_post(session, d["text"])
    queue.remove(d); save_queue(queue)
    await cb.message.edit_text(f"✅ Опубликовано: #{did}")
    await cb.answer("Готово")

@router.callback_query(F.data.startswith("sched:"))
async def cb_schedule(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Недоступно", show_alert=True); return
    _, sid, how = cb.data.split(":")
    did = int(sid)
    d = next((x for x in queue if x["id"] == did), None)
    if not d:
        await cb.answer("Черновика нет", show_alert=True); return
    delay = 3600 if how == "1h" else 600
    d["scheduled_ts"] = time.time() + delay
    save_queue(queue)
    await cb.answer("Отложено на +1 час" if delay == 3600 else "Отложено")
    await cb.message.edit_reply_markup(reply_markup=draft_kb(did))

@router.callback_query(F.data.startswith("del:"))
async def cb_delete(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Недоступно", show_alert=True); return
    _, sid = cb.data.split(":")
    did = int(sid)
    d = next((x for x in queue if x["id"] == did), None)
    if not d:
        await cb.answer("Уже удалено", show_alert=False); return
    queue.remove(d)
    save_queue(queue)
    await cb.message.edit_text(f"🗑️ Удалено: #{did}")
    await cb.answer("Удалено")

# ---------- Background scheduler ----------
async def scheduler_loop():
    while True:
        try:
            now = time.time()
            to_publish = [d for d in list(queue) if d.get("scheduled_ts") and d["scheduled_ts"] <= now]
            for d in to_publish:
                try:
                    await publish_to_tg(d["text"])
                    queue.remove(d); save_queue(queue)
                    logger.info("Published scheduled draft #%s", d["id"])
                except Exception as e:
                    logger.exception("Failed to publish scheduled draft #%s: %s", d["id"], e)
            await asyncio.sleep(15)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Scheduler error: %s", e)
            await asyncio.sleep(5)

# ---------- Startup ----------
async def main():
    logger.info("Starting bot...")
    asyncio.create_task(scheduler_loop())
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
