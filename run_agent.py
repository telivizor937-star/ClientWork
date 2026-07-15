from __future__ import annotations

import csv
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CHANNELS_FILE = ROOT / "channels.txt"
CONFIG_FILE = ROOT / "config.json"
OUTPUT_FILE = ROOT / "leads.csv"
RUN_LOG_FILE = ROOT / "last_run.txt"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)

SHORT_VIDEO_KEYWORDS = [
    "рилс",
    "reels",
    "shorts",
    "шортс",
    "tiktok",
    "tik tok",
    "тик ток",
    "тикток",
    "короткие видео",
    "short-form",
]

MONTAGE_KEYWORDS = [
    "монтаж",
    "монтажер",
    "монтажёр",
    "видеомонтаж",
    "видео монтаж",
    "редактор видео",
    "video editor",
    "capcut",
    "premiere",
    "adobe premiere",
    "after effects",
]

VACANCY_KEYWORDS = [
    "ищем",
    "ищу",
    "нужен",
    "нужна",
    "требуется",
    "вакансия",
    "работа",
    "проект",
    "заказ",
    "оплата",
    "бюджет",
    "постоян",
    "удален",
    "удалён",
]

GOOD_FORMAT_KEYWORDS = [
    "постоян",
    "долгоср",
    "разов",
    "проект",
    "тестовое оплач",
    "оплачиваемое тест",
]

NEGATIVE_KEYWORDS = [
    "офис",
    "выезд",
    "съемка",
    "съёмка",
    "снимать",
    "полный день",
    "5/2",
    "без оплаты",
    "не оплачивается",
    "бартер",
    "smm",
    "смм",
    "ведение соцсетей",
    "контент-план",
    "длинные видео",
    "youtube видео",
]

CANDIDATE_KEYWORDS = [
    "резюме",
    "#портфолио",
    "портфолио:",
    "ищу работу",
    "ищу проект",
    "ищу заказы",
    "ищу вакансию",
    "мое портфолио",
    "моё портфолио",
    "готов взять",
    "готова взять",
    "меня зовут",
    "я монтажер",
    "я монтажёр",
    "я видеомонтажер",
    "я видеомонтажёр",
    "работаю с динамикой",
]


@dataclass
class Lead:
    found_at: str
    channel: str
    post_date: str
    title: str
    budget: str
    score: int
    status: str
    link: str
    reason: str
    message: str
    reply_draft: str


def load_config() -> dict:
    default = {
        "portfolio_url": "https://t.me/workinonlybusiness",
        "minimum_rub_per_video": 500,
        "notify": {
            "telegram_bot_token": "",
            "telegram_chat_id": "",
            "send_when_no_new_leads": True,
        },
    }
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
        return default
    with CONFIG_FILE.open("r", encoding="utf-8") as file:
        loaded = json.load(file)
    config = {**default, **loaded, "notify": {**default["notify"], **loaded.get("notify", {})}}
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        config["notify"]["telegram_bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.getenv("TELEGRAM_CHAT_ID"):
        config["notify"]["telegram_chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    return config


def normalize_channel(raw: str) -> str:
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return ""
    raw = raw.removesuffix("/")
    if raw.startswith("@"):
        return f"https://t.me/{raw[1:]}"
    if raw.startswith("https://t.me/"):
        return raw
    return f"https://t.me/{raw}"


def channel_name(channel_url: str) -> str:
    return channel_url.rstrip("/").split("/")[-1]


def fetch_channel_html(channel_url: str) -> str:
    public_name = channel_name(channel_url)
    url = f"https://t.me/s/{public_name}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def strip_tags(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def parse_posts(page_html: str, channel_url: str) -> list[dict[str, str]]:
    posts: list[dict[str, str]] = []
    blocks = re.findall(
        r'<div class="tgme_widget_message_wrap[^"]*".*?</div>\s*</div>\s*</div>',
        page_html,
        flags=re.S,
    )
    public_name = channel_name(channel_url)

    for block in blocks:
        link_match = re.search(r'href="(https://t\.me/%s/\d+)"' % re.escape(public_name), block)
        text_match = re.search(
            r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
            block,
            flags=re.S,
        )
        date_match = re.search(r'<time datetime="([^"]+)"', block)
        if not link_match or not text_match:
            continue
        text = strip_tags(text_match.group(1))
        if text:
            posts.append(
                {
                    "channel": channel_url,
                    "link": link_match.group(1),
                    "date": date_match.group(1) if date_match else "",
                    "text": text,
                }
            )
    return posts


def count_keywords(text: str, keywords: list[str]) -> int:
    lowered = text.lower()
    return sum(1 for keyword in keywords if keyword in lowered)


def extract_budget_values(text: str) -> list[int]:
    values: list[int] = []
    patterns = [
        r"(?<!\d)(?:от\s*)?(\d[\d .]{0,10})\s*(?:₽|руб\.?|р\b)",
        r"(?:₽|руб\.?)\s*(\d[\d .]{0,10})",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.I):
            digits = re.sub(r"\D", "", match)
            if digits:
                values.append(int(digits))
    return values


def extract_budget(text: str) -> str:
    rub_values = extract_budget_values(text)
    dollar_values = re.findall(r"\$\s?\d{1,4}(?:[ .]\d{3})*", text, flags=re.I)
    parts = [f"{value} ₽" for value in rub_values] + [re.sub(r"\s+", " ", item).strip() for item in dollar_values]
    return "; ".join(dict.fromkeys(parts))[:120]


def has_minimum_budget(text: str, minimum_rub: int) -> bool:
    rub_values = extract_budget_values(text)
    if not rub_values:
        return True
    return max(rub_values) >= minimum_rub


def make_title(text: str) -> str:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first_line[:87].rstrip() + "..." if len(first_line) > 90 else first_line


def is_relevant(text: str, config: dict) -> tuple[bool, str]:
    short_video = count_keywords(text, SHORT_VIDEO_KEYWORDS)
    montage = count_keywords(text, MONTAGE_KEYWORDS)
    vacancy = count_keywords(text, VACANCY_KEYWORDS)
    negative = count_keywords(text, NEGATIVE_KEYWORDS)
    candidate = count_keywords(text, CANDIDATE_KEYWORDS)

    if candidate:
        return False, "похоже на резюме исполнителя"
    if short_video < 1:
        return False, "нет Reels/Shorts/TikTok"
    if montage < 1:
        return False, "нет монтажа"
    if vacancy < 1:
        return False, "нет признаков вакансии/заказа"
    if negative:
        return False, "есть стоп-слова: офис/выезд/smm/без оплаты/длинные видео"
    if not has_minimum_budget(text, int(config["minimum_rub_per_video"])):
        return False, "ниже минимальной оплаты"
    return True, "короткие видео + монтаж + вакансия"


def score_post(text: str) -> tuple[int, str]:
    score = 0
    score += count_keywords(text, SHORT_VIDEO_KEYWORDS) * 3
    score += count_keywords(text, MONTAGE_KEYWORDS) * 2
    score += count_keywords(text, VACANCY_KEYWORDS)
    score += count_keywords(text, GOOD_FORMAT_KEYWORDS) * 2
    if extract_budget(text):
        score += 2

    if score >= 12:
        status = "горячий"
    elif score >= 7:
        status = "норм"
    else:
        status = "слабый"
    return score, status


def make_reply_draft(config: dict) -> str:
    return (
        "Здравствуйте! Готов взять монтаж Reels/Shorts/TikTok.\n\n"
        "Делаю динамичный монтаж, субтитры, акценты, темп и упаковку под удержание. "
        "Особенно хорошо захожу в экспертные рилсы.\n\n"
        f"Портфолио: {config['portfolio_url']}\n\n"
        "Могу сделать платный тестовый ролик, чтобы вы сразу увидели уровень. "
        "Куда удобнее прислать ТЗ?"
    )


def read_existing_links() -> set[str]:
    if not OUTPUT_FILE.exists():
        return set()
    with OUTPUT_FILE.open("r", encoding="utf-8-sig", newline="") as file:
        return {row["link"] for row in csv.DictReader(file) if row.get("link")}


def append_leads(leads: list[Lead]) -> None:
    fieldnames = [
        "found_at",
        "channel",
        "post_date",
        "title",
        "budget",
        "score",
        "status",
        "link",
        "reason",
        "message",
        "reply_draft",
    ]
    exists = OUTPUT_FILE.exists()
    with OUTPUT_FILE.open("a", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        for lead in leads:
            writer.writerow(lead.__dict__)


def load_channels() -> list[str]:
    if not CHANNELS_FILE.exists():
        raise FileNotFoundError(f"Не найден файл каналов: {CHANNELS_FILE}")
    channels = [
        channel
        for channel in (normalize_channel(line) for line in CHANNELS_FILE.read_text(encoding="utf-8").splitlines())
        if channel
    ]
    return list(dict.fromkeys(channels))


def split_telegram_text(text: str, limit: int = 3900) -> list[str]:
    chunks: list[str] = []
    current = ""
    for part in text.split("\n\n"):
        candidate = part if not current else f"{current}\n\n{part}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        current = part[:limit]
    if current:
        chunks.append(current)
    return chunks


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(api_url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def save_config(config: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def discover_telegram_chat_id(config: dict) -> str:
    notify = config["notify"]
    token = notify.get("telegram_bot_token", "").strip()
    if not token:
        return ""
    api_url = f"https://api.telegram.org/bot{token}/getUpdates"
    request = urllib.request.Request(api_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
    for update in reversed(data.get("result", [])):
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id:
            notify["telegram_chat_id"] = str(chat_id)
            save_config(config)
            return str(chat_id)
    return ""


def send_telegram_notification(config: dict, leads: list[Lead], errors: list[str]) -> None:
    notify = config["notify"]
    token = notify.get("telegram_bot_token", "").strip()
    chat_id = notify.get("telegram_chat_id", "").strip()
    if token and not chat_id:
        chat_id = discover_telegram_chat_id(config)
    if not token or not chat_id:
        raise RuntimeError("Telegram notifications are not configured: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    if not leads and not notify.get("send_when_no_new_leads", False):
        return

    if not leads:
        send_telegram_message(token, chat_id, "Новых лидов на Reels-монтаж нет.")
        return

    header = (
        f"Новых лидов на Reels-монтаж: {len(leads)}\n"
        f"Таблица обновлена: {OUTPUT_FILE}\n\n"
        "Ниже контакты и готовые сообщения для ответа с телефона."
    )
    send_telegram_message(token, chat_id, header)

    for index, lead in enumerate(leads[:12], start=1):
        text = (
            f"Лид {index}/{len(leads)}\n"
            f"Статус: {lead.status} | score {lead.score}\n"
            f"Канал: {lead.channel}\n"
            f"Пост/контакт: {lead.link}\n"
            f"Бюджет: {lead.budget or 'не указан'}\n"
            f"Заголовок: {lead.title}\n\n"
            "Готовый отклик:\n"
            f"{lead.reply_draft}"
        )
        for chunk in split_telegram_text(text):
            send_telegram_message(token, chat_id, chunk)

    if len(leads) > 12:
        send_telegram_message(token, chat_id, f"Еще {len(leads) - 12} лидов лежат в таблице: {OUTPUT_FILE}")
    if errors:
        send_telegram_message(token, chat_id, "Ошибки по каналам:\n" + "\n".join(errors[:5]))


def main() -> int:
    config = load_config()
    existing_links = read_existing_links()
    found_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_leads: list[Lead] = []
    errors: list[str] = []

    for channel in load_channels():
        try:
            page_html = fetch_channel_html(channel)
        except urllib.error.URLError as error:
            errors.append(f"{channel}: {error}")
            continue

        for post in parse_posts(page_html, channel):
            if post["link"] in existing_links:
                continue
            relevant, reason = is_relevant(post["text"], config)
            if not relevant:
                continue
            score, status = score_post(post["text"])
            new_leads.append(
                Lead(
                    found_at=found_at,
                    channel=channel,
                    post_date=post["date"],
                    title=make_title(post["text"]),
                    budget=extract_budget(post["text"]),
                    score=score,
                    status=status,
                    link=post["link"],
                    reason=reason,
                    message=post["text"],
                    reply_draft=make_reply_draft(config),
                )
            )
            existing_links.add(post["link"])

    new_leads.sort(key=lambda lead: lead.score, reverse=True)
    append_leads(new_leads)

    try:
        send_telegram_notification(config, new_leads, errors)
        notification_status = "уведомление отправлено"
    except (RuntimeError, urllib.error.URLError) as error:
        notification_status = f"уведомление не отправлено: {error}"

    summary = [
        f"Запуск: {datetime.now().isoformat(timespec='seconds')}",
        f"Новых лидов: {len(new_leads)}",
        f"Таблица: {OUTPUT_FILE}",
        f"Уведомления: {notification_status}",
    ]
    if errors:
        summary.append("Ошибки:\n" + "\n".join(errors))
    RUN_LOG_FILE.write_text("\n".join(summary), encoding="utf-8")
    print("\n".join(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
