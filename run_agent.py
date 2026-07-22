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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CHANNELS_FILE = ROOT / "channels.txt"
SOURCES_FILE = ROOT / "sources.json"
CONFIG_FILE = ROOT / "config.json"
OUTPUT_FILE = ROOT / "leads.csv"
RUN_LOG_FILE = ROOT / "last_run.txt"
UNAVAILABLE_CHANNELS_FILE = ROOT / "unavailable_channels.csv"

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

IRRELEVANT_PRIMARY_ROLE_KEYWORDS = [
    "поиск клиентов",
    "лидогенерация",
    "менеджер",
    "smm",
    "смм",
    "таргетолог",
    "дизайнер",
    "копирайтер",
    "продажи",
    "продавать",
    "продюсер",
    "маркетолог",
    "контент-менеджер",
    "администратор",
    "ассистент",
    "помощник",
]

PRIMARY_MONTAGE_PATTERNS = [
    r"\bvideo editor\b",
    r"\bвидеомонтаж[её]р\w*\b",
    r"\bмонтаж[её]р\w*\b",
    r"\bредактор видео\b",
    r"\bмонтаж\b.{0,80}\b(reels|shorts|tiktok|tik tok|видео|ролик)",
    r"\b(reels|shorts|tiktok|tik tok|видео|ролик).{0,80}\bмонтаж\b",
    r"\b(ищем|ищу|нужен|нужна|требуется)\b.{0,80}\bмонтаж",
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


@dataclass(frozen=True)
class ChannelSource:
    url: str
    category: str


def load_config() -> dict:
    default = {
        "portfolio_url": "https://t.me/workinonlybusiness",
        "minimum_rub_per_video": 500,
        "max_post_age_hours": 72,
        "channel_fetch_workers": 20,
        "channel_timeout_seconds": 20,
        "notify": {
            "telegram_bot_token": "",
            "telegram_chat_id": "",
            "send_when_no_new_leads": True,
        },
    }
    loaded = {}
    if CONFIG_FILE.exists():
        with CONFIG_FILE.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
    else:
        CONFIG_FILE.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
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


def normalize_source_item(item: object, category: str) -> ChannelSource | None:
    if isinstance(item, str):
        channel = normalize_channel(item)
        return ChannelSource(channel, category) if channel else None
    if not isinstance(item, dict) or item.get("enabled", True) is False:
        return None

    raw = str(item.get("url") or item.get("channel") or item.get("username") or "").strip()
    channel = normalize_channel(raw)
    if not channel:
        return None

    item_category = str(item.get("category") or category or "uncategorized").strip() or "uncategorized"
    return ChannelSource(channel, item_category)


def load_sources_file() -> list[ChannelSource]:
    if not SOURCES_FILE.exists():
        return []

    data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    sources: list[ChannelSource] = []

    if isinstance(data, list):
        for item in data:
            source = normalize_source_item(item, "sources")
            if source:
                sources.append(source)
        return sources

    if not isinstance(data, dict):
        return []

    groups = data.get("groups", data)
    if isinstance(groups, dict):
        for category, items in groups.items():
            if not isinstance(items, list):
                continue
            for item in items:
                source = normalize_source_item(item, str(category))
                if source:
                    sources.append(source)
    elif isinstance(groups, list):
        for group in groups:
            if not isinstance(group, dict):
                continue
            category = str(group.get("category") or group.get("name") or "uncategorized")
            items = group.get("channels", [])
            if not isinstance(items, list):
                continue
            for item in items:
                source = normalize_source_item(item, category)
                if source:
                    sources.append(source)

    return sources


def channel_name(channel_url: str) -> str:
    return channel_url.rstrip("/").split("/")[-1]


def fetch_channel_html(channel_url: str, timeout: int = 30) -> str:
    public_name = channel_name(channel_url)
    url = f"https://t.me/s/{public_name}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def channel_unavailable_reason(page_html: str) -> str:
    lowered = page_html.lower()
    if "tgme_widget_message" in lowered or "tgme_channel_info" in lowered:
        return ""
    if "tgme_page_title" in lowered:
        return "not_public_or_deleted"
    return "empty_or_unavailable"


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


def parse_post_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_fresh_post(post_date: str, max_age_hours: int, now: datetime) -> bool:
    parsed = parse_post_datetime(post_date)
    if not parsed:
        return False
    return now - parsed <= timedelta(hours=max_age_hours)


def has_primary_montage_role(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(pattern, lowered, flags=re.I | re.S) for pattern in PRIMARY_MONTAGE_PATTERNS)


def has_irrelevant_primary_role(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in IRRELEVANT_PRIMARY_ROLE_KEYWORDS)


def is_relevant(text: str, config: dict) -> tuple[bool, str]:
    lowered = text.lower()
    short_video = count_keywords(text, SHORT_VIDEO_KEYWORDS)
    montage = count_keywords(text, MONTAGE_KEYWORDS)
    vacancy = count_keywords(text, VACANCY_KEYWORDS)
    negative = count_keywords(text, NEGATIVE_KEYWORDS)
    candidate = count_keywords(text, CANDIDATE_KEYWORDS)
    video_context = short_video > 0 or "видео" in lowered or "video" in lowered

    if candidate:
        return False, "похоже на резюме исполнителя"
    if negative:
        return False, "есть стоп-слова: офис/выезд/smm/без оплаты/длинные видео"
    if not has_minimum_budget(text, int(config["minimum_rub_per_video"])):
        return False, "ниже минимальной оплаты"
    if montage < 1:
        return False, "нет монтажа"
    if not has_primary_montage_role(text):
        return False, "монтаж не является основной задачей"
    if has_irrelevant_primary_role(text):
        return False, "основная вакансия не про видеомонтаж"
    if vacancy < 1:
        return False, "нет признаков вакансии/заказа"
    if not video_context:
        return False, "нет Reels/Shorts/TikTok"
    return True, "видео + монтаж + вакансия"


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


def detect_niche(text: str) -> str:
    lowered = text.lower()
    niches = [
        ("эксперт", ["эксперт", "обуч", "курс", "настав", "психолог", "коуч"]),
        ("блог", ["блог", "личный бренд", "инфлюенсер", "автор"]),
        ("товарный проект", ["товар", "магазин", "бренд", "маркетплейс", "wildberries", "ozon"]),
        ("услуги", ["услуг", "салон", "клиник", "недвиж", "юрист", "стомат"]),
        ("YouTube/подкаст", ["youtube", "ютуб", "подкаст", "интервью"]),
    ]
    for niche, keywords in niches:
        if any(keyword in lowered for keyword in keywords):
            return niche
    return "проект"


def detect_content_type(text: str) -> str:
    lowered = text.lower()
    formats = [
        ("Reels", ["reels", "рилс", "рилсы"]),
        ("Shorts", ["shorts", "шортс"]),
        ("TikTok", ["tiktok", "tik tok", "тикток", "тик ток"]),
        ("короткие видео", ["короткие видео", "ролики", "видео"]),
    ]
    found = [name for name, keywords in formats if any(keyword in lowered for keyword in keywords)]
    return "/".join(dict.fromkeys(found[:3])) if found else "видео"


def detect_requirements(text: str) -> list[str]:
    lowered = text.lower()
    checks = [
        ("субтитры", ["субтитр"]),
        ("динамика", ["динамич", "темп"]),
        ("хуки", ["хук", "удержан"]),
        ("цвет/звук", ["цвет", "звук"]),
        ("CapCut", ["capcut"]),
        ("Premiere Pro", ["premiere", "премьер"]),
        ("After Effects", ["after effects", "афт"]),
        ("обложки", ["облож"]),
    ]
    return [name for name, keywords in checks if any(keyword in lowered for keyword in keywords)][:4]


def extract_project_feature(text: str) -> str:
    lines = [line.strip(" -•\t") for line in text.splitlines() if line.strip()]
    for line in lines:
        lowered = line.lower()
        if any(keyword in lowered for keyword in ["нужно", "задача", "треб", "ищем", "нужен", "нужна"]):
            return line[:120]
    return make_title(text)


def make_reply_draft(config: dict, text: str) -> str:
    niche = detect_niche(text)
    content_type = detect_content_type(text)
    requirements = detect_requirements(text)
    budget = extract_budget(text)
    feature = extract_project_feature(text)

    lines = [
        "Здравствуйте! Прочитал вакансию.",
        f"Понял, что нужен монтаж для {niche}: {content_type}.",
    ]
    if feature:
        lines.append(f"По задаче вижу главное: {feature}")
    if requirements:
        lines.append(f"Могу закрыть по монтажу: {', '.join(requirements)}.")
    else:
        lines.append("Могу взять монтаж, собрать ролик по структуре, темпу и удержанию.")
    if budget:
        lines.append(f"Бюджет увидел: {budget}.")
    lines.extend(
        [
            f"Портфолио: {config['portfolio_url']}",
            "Если формат подходит, пришлите ТЗ и пример роликов, на которые ориентироваться.",
        ]
    )
    return "\n\n".join(lines)


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


def load_channel_sources() -> list[ChannelSource]:
    sources: list[ChannelSource] = []
    if CHANNELS_FILE.exists():
        for line in CHANNELS_FILE.read_text(encoding="utf-8").splitlines():
            channel = normalize_channel(line)
            if channel:
                sources.append(ChannelSource(channel, "channels.txt"))

    sources.extend(load_sources_file())

    unique: dict[str, ChannelSource] = {}
    for source in sources:
        unique.setdefault(channel_name(source.url).lower(), source)

    if not unique:
        raise FileNotFoundError(f"Не найдены каналы: {CHANNELS_FILE} или {SOURCES_FILE}")
    return list(unique.values())


def load_channels() -> list[str]:
    return [source.url for source in load_channel_sources()]


def write_unavailable_channels(rows: list[dict[str, str]]) -> None:
    if not rows:
        if UNAVAILABLE_CHANNELS_FILE.exists():
            UNAVAILABLE_CHANNELS_FILE.unlink()
        return

    fieldnames = ["checked_at", "category", "channel", "reason"]
    with UNAVAILABLE_CHANNELS_FILE.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def fetch_channel_pages(
    sources: list[ChannelSource], timeout: int, max_workers: int
) -> list[tuple[ChannelSource, str, str]]:
    workers = max(1, min(max_workers, len(sources)))
    results: list[tuple[ChannelSource, str, str]] = []

    def fetch(source: ChannelSource) -> tuple[ChannelSource, str, str]:
        try:
            return source, fetch_channel_html(source.url, timeout=timeout), ""
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            return source, "", str(error)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(fetch, source) for source in sources]
        for future in as_completed(futures):
            results.append(future.result())

    return results


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


def filter_reason_key(reason: str) -> str:
    if reason == "нет Reels/Shorts/TikTok":
        return "no_short_video"
    if reason == "нет монтажа":
        return "no_montage"
    if reason == "нет признаков вакансии/заказа":
        return "no_vacancy"
    if reason.startswith("есть стоп-слова"):
        return "stop_words"
    return "other"


def send_run_report(config: dict, report: dict) -> None:
    notify = config["notify"]
    token = notify.get("telegram_bot_token", "").strip()
    chat_id = notify.get("telegram_chat_id", "").strip()
    if token and not chat_id:
        chat_id = discover_telegram_chat_id(config)
    if not token or not chat_id:
        raise RuntimeError("Telegram notifications are not configured: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")

    lines = [
        "Отчёт поиска лидов",
        f"Время запуска: {report['started_at']}",
        f"Каналов проверено: {report['channels_checked']}",
        f"Постов найдено: {report['posts_found']}",
        f"Пропущено как уже существующие: {report['existing_posts']}",
        f"Отфильтровано: {report['filtered_posts']}",
        "Причины фильтрации:",
        f"- нет Reels/Shorts/TikTok: {report['filter_reasons']['no_short_video']}",
        f"- нет монтажа: {report['filter_reasons']['no_montage']}",
        f"- нет вакансии/заказа: {report['filter_reasons']['no_vacancy']}",
        f"- стоп-слова: {report['filter_reasons']['stop_words']}",
        f"Новых лидов: {report['new_leads']}",
    ]
    if report["new_leads"] == 0:
        lines.append("✅ Поиск работает. Новых подходящих лидов нет.")
    if report["errors"]:
        lines.append("Ошибки:\n" + "\n".join(report["errors"][:5]))

    send_telegram_message(token, chat_id, "\n".join(lines))


def main() -> int:
    config = load_config()
    existing_links = read_existing_links()
    found_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    now_utc = datetime.now(timezone.utc)
    max_post_age_hours = int(config.get("max_post_age_hours", 72))
    new_leads: list[Lead] = []
    errors: list[str] = []
    unavailable_channels: list[dict[str, str]] = []
    notification_status = "уведомлений не было"
    started_at = datetime.now().isoformat(timespec="seconds")
    channels_checked = 0
    posts_found = 0
    existing_posts = 0
    filtered_posts = 0
    filter_reasons = {
        "no_short_video": 0,
        "no_montage": 0,
        "no_vacancy": 0,
        "stop_words": 0,
        "other": 0,
    }

    channel_sources = load_channel_sources()
    channel_pages = fetch_channel_pages(
        channel_sources,
        timeout=int(config.get("channel_timeout_seconds", 20)),
        max_workers=int(config.get("channel_fetch_workers", 20)),
    )
    for source, page_html, fetch_error in channel_pages:
        channel = source.url
        if fetch_error:
            errors.append(f"{channel}: {fetch_error}")
            unavailable_channels.append(
                {
                    "checked_at": started_at,
                    "category": source.category,
                    "channel": channel,
                    "reason": fetch_error,
                }
            )
            continue
        unavailable_reason = channel_unavailable_reason(page_html)
        if unavailable_reason:
            errors.append(f"{channel}: {unavailable_reason}")
            unavailable_channels.append(
                {
                    "checked_at": started_at,
                    "category": source.category,
                    "channel": channel,
                    "reason": unavailable_reason,
                }
            )
            continue
        channels_checked += 1

        posts = parse_posts(page_html, channel)
        posts_found += len(posts)
        for post in posts:
            if not is_fresh_post(post["date"], max_post_age_hours, now_utc):
                filtered_posts += 1
                filter_reasons["other"] += 1
                continue
            if post["link"] in existing_links:
                existing_posts += 1
                continue
            relevant, reason = is_relevant(post["text"], config)
            if not relevant:
                filtered_posts += 1
                filter_reasons[filter_reason_key(reason)] += 1
                continue
            score, status = score_post(post["text"])
            lead = Lead(
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
                reply_draft=make_reply_draft(config, post["text"]),
            )
            new_leads.append(lead)
            existing_links.add(post["link"])
            try:
                send_telegram_notification(config, [lead], [])
                notification_status = "уведомление отправлено"
            except (RuntimeError, urllib.error.URLError) as error:
                notification_status = f"уведомление не отправлено: {error}"

    write_unavailable_channels(unavailable_channels)
    new_leads.sort(key=lambda lead: lead.score, reverse=True)
    append_leads(new_leads)

    report = {
        "started_at": started_at,
        "channels_checked": channels_checked,
        "posts_found": posts_found,
        "existing_posts": existing_posts,
        "filtered_posts": filtered_posts,
        "filter_reasons": filter_reasons,
        "new_leads": len(new_leads),
        "errors": errors,
    }

    try:
        send_run_report(config, report)
        notification_status = "отчёт отправлен"
    except (RuntimeError, urllib.error.URLError) as error:
        notification_status = f"отчёт не отправлен: {error}"

    summary = [
        f"Запуск: {started_at}",
        f"Каналов проверено: {channels_checked}",
        f"Постов найдено: {posts_found}",
        f"Пропущено как уже существующие: {existing_posts}",
        f"Отфильтровано: {filtered_posts}",
        "Причины фильтрации:",
        f"- нет Reels/Shorts/TikTok: {filter_reasons['no_short_video']}",
        f"- нет монтажа: {filter_reasons['no_montage']}",
        f"- нет вакансии/заказа: {filter_reasons['no_vacancy']}",
        f"- стоп-слова: {filter_reasons['stop_words']}",
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
