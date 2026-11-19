# RTFM Cloud Bot (v5)

Телеграм-бот для черновиков и публикации в канал + кросспост в VK. Поддерживает:
- /draft, /queue, /approve, /approve_vk, /schedule
- Инлайн-кнопки: Approve TG, TG+VK, +1h, Delete
- /el и /brief — диалог с «Эл» (LLM) и генерация черновиков
- Фоновый планировщик отложек
- Хранение очереди в data/queue.json

## Быстрый старт (Railway/Render)

1) Создай бота у @BotFather → получи TG_BOT_TOKEN  
2) Узнай ID:
   - TG_CHANNEL_ID (канал, -100…)
   - ADMIN_CHAT_ID (приватная админ-группа, -100…)
   - ADMIN_USER_IDS (твои user id через запятую)
3) Задай переменные окружения (.env или в панели):
```
TG_BOT_TOKEN=...
TG_CHANNEL_ID=-100...
ADMIN_CHAT_ID=-100...
ADMIN_USER_IDS=123456789
OPENAI_API_KEY=sk-...   # опционально для /el и /brief
VK_ENABLED=false
VK_GROUP_ID=0
VK_ACCESS_TOKEN=
TIMEZONE=Europe/Moscow
```
4) Установи зависимости и запусти:
```
pip install -r requirements.txt
python main.py
```

## Команды

- /ping — проверка
- /draft <текст> — создать черновик
- /queue — список черновиков
- /approve <id> — TG публикация
- /approve_vk <id> — TG + VK публикация
- /schedule <id> YYYY-MM-DD HH:MM — отложить (UTC)
- /el <вопрос> — спросить «Эла»
- /brief <тема> — сгенерировать черновик

Кнопки под черновиком: Approve TG, TG+VK, +1h, Delete.

## Деплой в облаке

- Команда старта: `python main.py`
- Procfile (для Render/Railway): `worker: python main.py`

## Безопасность

- Команды доступны только user_id из ADMIN_USER_IDS.
- Токены — только в окружении/.env.
- Если токен утёк — сделай /revoke в @BotFather и обнови TG_BOT_TOKEN.
