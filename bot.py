from __future__ import annotations

import csv
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

import run_agent


TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV = "TELEGRAM_CHAT_ID"

MENU = {
    "keyboard": [
        ["🤖 Статус", "🔍 Проверить поиск"],
        ["📊 Статистика", "📩 Тест Telegram"],
    ],
    "resize_keyboard": True,
}


def api_request(token: str, method: str, payload: dict) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(
        {"reply_markup": json.dumps(payload.pop("reply_markup"), ensure_ascii=False), **payload}
        if "reply_markup" in payload
        else payload
    ).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"User-Agent": run_agent.USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def send_message(token: str, chat_id: str, text: str, with_menu: bool = True) -> None:
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }
    if with_menu:
        payload["reply_markup"] = MENU
    api_request(token, "sendMessage", payload)


def load_bot_config() -> tuple[str, str]:
    config = run_agent.load_config()
    notify = config.get("notify", {})
    token = os.getenv(TOKEN_ENV, notify.get("telegram_bot_token", "")).strip()
    chat_id = os.getenv(CHAT_ID_ENV, notify.get("telegram_chat_id", "")).strip()
    return token, chat_id


def parse_last_run() -> str:
    if not run_agent.RUN_LOG_FILE.exists():
        return "Последний запуск: нет данных"

    lines = run_agent.RUN_LOG_FILE.read_text(encoding="utf-8").splitlines()
    started = next((line for line in lines if "Запуск:" in line), "Запуск: нет данных")
    leads = next((line for line in lines if "Новых лидов:" in line), "Новых лидов: нет данных")
    notify = next((line for line in lines if "Уведомления:" in line), "Уведомления: нет данных")
    error_index = next((index for index, line in enumerate(lines) if "Ошибки:" in line), None)
    errors = "\n".join(lines[error_index:]) if error_index is not None else "Ошибки: нет"
    return "\n".join([started, leads, notify, errors])


def build_stats() -> str:
    if not run_agent.OUTPUT_FILE.exists():
        return "leads.csv не найден"

    today = date.today().isoformat()
    total = 0
    today_count = 0
    hot_count = 0

    with run_agent.OUTPUT_FILE.open("r", encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            total += 1
            if row.get("found_at", "").startswith(today):
                today_count += 1
            if "горяч" in row.get("status", "").lower():
                hot_count += 1

    return "\n".join(
        [
            f"Всего лидов: {total}",
            f"Лидов за сегодня: {today_count}",
            f"Горячих лидов: {hot_count}",
        ]
    )


def run_manual_check() -> str:
    config = run_agent.load_config()
    existing_links = run_agent.read_existing_links()
    channels = run_agent.load_channels()
    checked_channels = 0
    posts_found = 0
    new_leads = 0
    errors: list[str] = []

    for channel in channels:
        try:
            html = run_agent.fetch_channel_html(channel)
            checked_channels += 1
            posts = run_agent.parse_posts(html, channel)
            posts_found += len(posts)
        except urllib.error.URLError as error:
            errors.append(f"{channel}: {error}")
            continue

        for post in posts:
            if post["link"] in existing_links:
                continue
            relevant, _reason = run_agent.is_relevant(post["text"], config)
            if relevant:
                new_leads += 1

    return "\n".join(
        [
            "Проверка поиска завершена",
            f"Каналов проверено: {checked_channels}",
            f"Постов найдено: {posts_found}",
            f"Новых лидов: {new_leads}",
            f"Ошибок: {len(errors)}",
            *errors[:5],
        ]
    )


def handle_text(text: str) -> str:
    normalized = text.strip()
    if normalized in {"/start", "/help"}:
        return "Выберите действие."
    if normalized in {"/status", "🤖 Статус"}:
        return parse_last_run()
    if normalized in {"/stats", "📊 Статистика"}:
        return build_stats()
    if normalized in {"/test", "📩 Тест Telegram"}:
        return "✅ Telegram управление работает"
    if normalized in {"/check", "🔍 Проверить поиск"}:
        return run_manual_check()
    return "Неизвестная команда. Используйте меню или /status /check /stats /test."


def poll(token: str, allowed_chat_id: str) -> None:
    offset = 0
    while True:
        try:
            data = api_request(token, "getUpdates", {"offset": offset, "timeout": 50})
            for update in data.get("result", []):
                offset = max(offset, update["update_id"] + 1)
                message = update.get("message") or {}
                chat = message.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                text = message.get("text", "")
                if not chat_id or not text:
                    continue
                if allowed_chat_id and chat_id != allowed_chat_id:
                    continue
                send_message(token, chat_id, handle_text(text))
        except (urllib.error.URLError, TimeoutError) as error:
            print(f"Polling error: {error}")
            time.sleep(5)


def main() -> int:
    token, chat_id = load_bot_config()
    if not token:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or notify.telegram_bot_token")
    print("Telegram control bot started")
    poll(token, chat_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
