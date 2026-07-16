# PROJECT_CONTEXT

## Что делает бот

Telegram-бот ищет лиды по вакансиям/заказам на reels, shorts, TikTok и видеомонтаж в Telegram-каналах. Он читает список каналов, парсит публичные посты, фильтрует релевантные сообщения по ключевым словам и бюджету, сохраняет новые лиды в CSV и отправляет уведомления в Telegram.

## Как запускается бот

- Локально: `python run_agent.py`
- В GitHub Actions: workflow `.github/workflows/reels-leads.yml`
- Для уведомлений нужны `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` в переменных или secrets.

## Файлы

- `run_agent.py` — основная логика: загрузка настроек, чтение каналов, парсинг постов, фильтрация, запись лидов, Telegram-уведомления.
- `channels.txt` — список Telegram-каналов-источников.
- `leads.csv` — таблица найденных лидов.
- `last_run.txt` — лог последнего запуска.
- `config.example.json` — пример конфигурации.
- `.github/workflows/reels-leads.yml` — автоматический запуск в GitHub Actions.
- `README.md` и `CLOUD_SETUP.md` — пользовательская документация по запуску и облачной настройке.

## Расписание

Расписание находится в `.github/workflows/reels-leads.yml`, блок `on.schedule`. Сейчас cron: `*/5 * * * *`.

## Поиск лидов

Поиск лидов находится в `run_agent.py`. Основные зоны:

- ключевые слова и критерии — списки `SHORT_VIDEO_KEYWORDS`, `MONTAGE_KEYWORDS`, `VACANCY_KEYWORDS`, `GOOD_FORMAT_KEYWORDS`;
- загрузка каналов — `load_channels`;
- парсинг постов — `fetch_channel_html` и `parse_posts`;
- проверка релевантности — `is_relevant`;
- оценка лида — `score_post`;
- сохранение — `append_leads`;
- отправка уведомления — `send_telegram_notification`.

## Безопасные отдельные изменения

- Добавлять или удалять каналы в `channels.txt`.
- Менять cron-расписание в `.github/workflows/reels-leads.yml`.
- Корректировать ключевые слова и пороги фильтрации в `run_agent.py`.
- Менять текст шаблона ответа через конфиг.
- Улучшать формат уведомлений отдельно от логики поиска.
- Добавлять новые колонки в `leads.csv` только вместе с обновлением `append_leads`.
