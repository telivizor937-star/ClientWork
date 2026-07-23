from __future__ import annotations

import csv
import hashlib
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
DISCOVERED_SOURCES_FILE = ROOT / "discovered_sources.csv"
RUNTIME_STATE_FILE = ROOT / "runtime_state.json"
EMPLOYER_HISTORY_FILE = ROOT / "employer_history.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)

OPENROUTER_MODELS = [
    "google/gemma-3-27b-it:free",
    "qwen/qwen3-235b-a22b:free",
    "openrouter/free",
]

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
    lead_id: str = ""
    contact_status: str = "not_contacted"
    contacted_at: str = ""
    client_answered: str = "no"
    client_answered_at: str = ""
    notes: str = ""


@dataclass(frozen=True)
class VacancyBrief:
    title: str
    bullets: list[str]
    budget: str


@dataclass(frozen=True)
class ChannelSource:
    url: str
    category: str


def load_config() -> dict:
    default = {
        "portfolio_url": "https://t.me/workinonlybusiness",
        "minimum_rub_per_video": 500,
        "max_post_age_hours": 24,
        "max_leads_per_run": 12,
        "channel_fetch_workers": 20,
        "channel_timeout_seconds": 20,
        "rotating_source_group_size": 35,
        "openrouter_model": "google/gemma-3-27b-it:free",
        "openrouter_api_key": "",
        "source_discovery": {
            "enabled": True,
            "interval_hours": 24,
            "max_new_sources_per_run": 20,
            "max_post_age_days": 120,
            "max_candidates_per_run": 160,
            "workers": 16,
        },
        "notify": {
            "telegram_bot_token": "",
            "telegram_chat_id": "",
            "send_when_no_new_leads": True,
            "send_run_report": False,
        },
    }
    loaded = {}
    if CONFIG_FILE.exists():
        with CONFIG_FILE.open("r", encoding="utf-8") as file:
            loaded = json.load(file)
    else:
        CONFIG_FILE.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
    config = {
        **default,
        **loaded,
        "notify": {**default["notify"], **loaded.get("notify", {})},
        "source_discovery": {**default["source_discovery"], **loaded.get("source_discovery", {})},
    }
    if os.getenv("TELEGRAM_BOT_TOKEN"):
        config["notify"]["telegram_bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.getenv("TELEGRAM_CHAT_ID"):
        config["notify"]["telegram_chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    if os.getenv("OPENROUTER_API_KEY"):
        config["openrouter_api_key"] = os.environ["OPENROUTER_API_KEY"]
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


def contains_any(text: str, keywords: list[str]) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in keywords)


def is_ad_or_repost(text: str) -> bool:
    lowered = text.lower()
    markers = [
        "\u0440\u0435\u043a\u043b\u0430\u043c\u0430",
        "\u0440\u0435\u043a\u043b\u0430\u043c\u043d\u044b\u0439 \u043f\u043e\u0441\u0442",
        "\u043f\u043e\u0434\u043f\u0438\u0448\u0438",
        "\u043f\u043e\u0434\u043f\u0438\u0441\u044b\u0432\u0430\u0439",
        "\u043d\u0430\u0448 \u043a\u0430\u043d\u0430\u043b",
        "\u043d\u0430\u0448\u0435\u0433\u043e \u0431\u043e\u0442\u0430",
        "\u0438\u0437 \u043d\u0430\u0448\u0435\u0433\u043e \u0431\u043e\u0442\u0430",
        "\u0432\u0430\u043a\u0430\u043d\u0441\u0438\u044f \u0432\u0437\u044f\u0442\u0430",
        "\u0432\u0437\u044f\u0442\u0430 \u0438\u0437",
        "\u0431\u043e\u0442 \u0441 \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u044f\u043c\u0438",
        "\u043a\u0443\u0440\u0441 \u043f\u043e",
        "\u043e\u0431\u0443\u0447\u0435\u043d\u0438\u0435 \u043c\u043e\u043d\u0442\u0430\u0436\u0443",
        "\u043d\u0430\u0443\u0447\u0443 \u043c\u043e\u043d\u0442\u0430\u0436\u0443",
        "\u0443\u0441\u043b\u0443\u0433\u0438 \u043c\u043e\u043d\u0442\u0430\u0436\u0430",
        "\u043f\u0440\u043e\u0434\u0430\u044e \u0443\u0441\u043b\u0443\u0433\u0438",
    ]
    return any(marker in lowered for marker in markers)


def is_candidate_offer(text: str) -> bool:
    lowered = text.lower()
    markers = [
        "\u0438\u0449\u0443 \u0440\u0430\u0431\u043e\u0442\u0443",
        "\u0438\u0449\u0443 \u0437\u0430\u043a\u0430\u0437",
        "\u0432\u043e\u0437\u044c\u043c\u0443 \u0437\u0430\u043a\u0430\u0437",
        "\u0433\u043e\u0442\u043e\u0432 \u0432\u0437\u044f\u0442\u044c",
        "\u0433\u043e\u0442\u043e\u0432\u0430 \u0432\u0437\u044f\u0442\u044c",
        "\u044f \u043c\u043e\u043d\u0442\u0430\u0436\u0435\u0440",
        "\u044f \u043c\u043e\u043d\u0442\u0430\u0436\u0451\u0440",
        "\u044f \u0432\u0438\u0434\u0435\u043e\u043c\u043e\u043d\u0442\u0430\u0436\u0435\u0440",
        "\u044f \u0432\u0438\u0434\u0435\u043e\u043c\u043e\u043d\u0442\u0430\u0436\u0451\u0440",
        "\u043c\u043e\u0438 \u0440\u0430\u0431\u043e\u0442\u044b",
        "\u043c\u043e\u0435 \u043f\u043e\u0440\u0442\u0444\u043e\u043b\u0438\u043e",
        "\u043c\u043e\u0451 \u043f\u043e\u0440\u0442\u0444\u043e\u043b\u0438\u043e",
        "#\u043f\u043e\u0440\u0442\u0444\u043e\u043b\u0438\u043e",
    ]
    return any(marker in lowered for marker in markers)


def has_real_montage_request(text: str) -> bool:
    request_markers = [
        "\u0438\u0449\u0443",
        "\u0438\u0449\u0435\u043c",
        "\u043d\u0443\u0436\u0435\u043d",
        "\u043d\u0443\u0436\u043d\u0430",
        "\u043d\u0443\u0436\u043d\u044b",
        "\u0442\u0440\u0435\u0431\u0443\u0435\u0442\u0441\u044f",
        "\u0432\u0430\u043a\u0430\u043d\u0441\u0438\u044f",
        "\u0437\u0430\u0434\u0430\u0447\u0430",
        "\u043e\u0431\u044f\u0437\u0430\u043d\u043d\u043e\u0441\u0442\u0438",
        "\u043e\u043f\u043b\u0430\u0442\u0430",
        "\u0431\u044e\u0434\u0436\u0435\u0442",
    ]
    montage_markers = [
        "\u043c\u043e\u043d\u0442\u0430\u0436",
        "\u043c\u043e\u043d\u0442\u0430\u0436\u0435\u0440",
        "\u043c\u043e\u043d\u0442\u0430\u0436\u0451\u0440",
        "\u0432\u0438\u0434\u0435\u043e\u043c\u043e\u043d\u0442\u0430\u0436",
        "video editor",
        "reels",
        "shorts",
        "tiktok",
    ]
    return contains_any(text, request_markers) and contains_any(text, montage_markers)


def is_relevant(text: str, config: dict) -> tuple[bool, str]:
    lowered = text.lower()
    short_video = count_keywords(text, SHORT_VIDEO_KEYWORDS)
    montage = count_keywords(text, MONTAGE_KEYWORDS)
    vacancy = count_keywords(text, VACANCY_KEYWORDS)
    negative = count_keywords(text, NEGATIVE_KEYWORDS)
    candidate = count_keywords(text, CANDIDATE_KEYWORDS)
    video_context = short_video > 0 or "\u0432\u0438\u0434\u0435\u043e" in lowered or "video" in lowered

    if is_ad_or_repost(text):
        return False, "\u0440\u0435\u043a\u043b\u0430\u043c\u0430/\u043f\u0435\u0440\u0435\u043f\u0435\u0447\u0430\u0442\u043a\u0430"
    if is_candidate_offer(text):
        return False, "\u043f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u0435 \u0438\u0441\u043f\u043e\u043b\u043d\u0438\u0442\u0435\u043b\u044f"
    if not has_real_montage_request(text):
        return False, "\u043d\u0435\u0442 \u0440\u0435\u0430\u043b\u044c\u043d\u043e\u0433\u043e \u0437\u0430\u043f\u0440\u043e\u0441\u0430 \u043d\u0430 \u0432\u0438\u0434\u0435\u043e\u043c\u043e\u043d\u0442\u0430\u0436"
    if candidate:
        return False, "\u043f\u043e\u0445\u043e\u0436\u0435 \u043d\u0430 \u0440\u0435\u0437\u044e\u043c\u0435 \u0438\u0441\u043f\u043e\u043b\u043d\u0438\u0442\u0435\u043b\u044f"
    if negative:
        return False, "\u0435\u0441\u0442\u044c \u0441\u0442\u043e\u043f-\u0441\u043b\u043e\u0432\u0430: \u043e\u0444\u0438\u0441/\u0432\u044b\u0435\u0437\u0434/smm/\u0431\u0435\u0437 \u043e\u043f\u043b\u0430\u0442\u044b/\u0434\u043b\u0438\u043d\u043d\u044b\u0435 \u0432\u0438\u0434\u0435\u043e"
    if not has_minimum_budget(text, int(config["minimum_rub_per_video"])):
        return False, "\u043d\u0438\u0436\u0435 \u043c\u0438\u043d\u0438\u043c\u0430\u043b\u044c\u043d\u043e\u0439 \u043e\u043f\u043b\u0430\u0442\u044b"
    if montage < 1:
        return False, "\u043d\u0435\u0442 \u043c\u043e\u043d\u0442\u0430\u0436\u0430"
    if not has_primary_montage_role(text):
        return False, "\u043c\u043e\u043d\u0442\u0430\u0436 \u043d\u0435 \u044f\u0432\u043b\u044f\u0435\u0442\u0441\u044f \u043e\u0441\u043d\u043e\u0432\u043d\u043e\u0439 \u0437\u0430\u0434\u0430\u0447\u0435\u0439"
    if has_irrelevant_primary_role(text):
        return False, "\u043e\u0441\u043d\u043e\u0432\u043d\u0430\u044f \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u044f \u043d\u0435 \u043f\u0440\u043e \u0432\u0438\u0434\u0435\u043e\u043c\u043e\u043d\u0442\u0430\u0436"
    if vacancy < 1:
        return False, "\u043d\u0435\u0442 \u043f\u0440\u0438\u0437\u043d\u0430\u043a\u043e\u0432 \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0438/\u0437\u0430\u043a\u0430\u0437\u0430"
    if not video_context:
        return False, "\u043d\u0435\u0442 Reels/Shorts/TikTok"
    return True, "\u0432\u0438\u0434\u0435\u043e + \u043c\u043e\u043d\u0442\u0430\u0436 + \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u044f"


def score_post(text: str) -> tuple[int, str]:
    score = 0
    score += count_keywords(text, SHORT_VIDEO_KEYWORDS) * 3
    score += count_keywords(text, MONTAGE_KEYWORDS) * 2
    score += count_keywords(text, VACANCY_KEYWORDS)
    score += count_keywords(text, GOOD_FORMAT_KEYWORDS) * 2
    if extract_budget(text):
        score += 2
    if contains_any(text, ["@", "t.me/", "\u043f\u0438\u0448\u0438\u0442\u0435", "\u0441\u0432\u044f\u0437\u044c", "\u043b\u0441", "\u0434\u0438\u0440\u0435\u043a\u0442"]):
        score += 2
    if contains_any(text, ["\u0441\u0440\u043e\u043a", "\u0434\u0435\u0434\u043b\u0430\u0439\u043d", "\u0441\u0435\u0433\u043e\u0434\u043d\u044f", "\u0437\u0430\u0432\u0442\u0440\u0430"]):
        score += 2

    has_task = (
        (count_keywords(text, MONTAGE_KEYWORDS) > 0 or contains_any(text, ["\u043c\u043e\u043d\u0442\u0430\u0436", "\u043c\u043e\u043d\u0442\u0430\u0436\u0435\u0440", "\u043c\u043e\u043d\u0442\u0430\u0436\u0451\u0440", "\u0432\u0438\u0434\u0435\u043e\u043c\u043e\u043d\u0442\u0430\u0436"]))
        and (count_keywords(text, SHORT_VIDEO_KEYWORDS) > 0 or contains_any(text, ["reels", "\u0440\u0438\u043b\u0441", "shorts", "\u0448\u043e\u0440\u0442\u0441", "tiktok", "\u0442\u0438\u043a\u0442\u043e\u043a"]))
    )
    has_requirements = contains_any(
        text,
        [
            "\u0441\u0443\u0431\u0442\u0438\u0442\u0440",
            "\u0434\u0438\u043d\u0430\u043c\u0438\u0447",
            "\u0433\u0440\u0430\u0444\u0438\u0447",
            "\u044d\u043b\u0435\u043c\u0435\u043d\u0442",
            "\u0442\u0440\u0435\u0431",
        ],
    )
    has_regular_volume = contains_any(
        text,
        [
            "\u0432 \u0434\u0435\u043d\u044c",
            "\u0435\u0436\u0435\u0434\u043d",
            "\u0440\u0435\u0433\u0443\u043b\u044f\u0440",
            "\u043f\u043e\u0441\u0442\u043e\u044f\u043d",
            "\u043e\u0442 1",
            "\u043e\u0442 \u043e\u0434\u043d\u043e\u0433\u043e",
        ],
    )
    has_contact = contains_any(text, ["@", "t.me/", "\u043f\u0438\u0448\u0438\u0442\u0435", "\u0441\u0432\u044f\u0437\u044c", "\u043b\u0441", "\u0434\u0438\u0440\u0435\u043a\u0442", "\u043e\u0442\u043a\u043b\u0438\u043a"])

    if score >= 14 or (has_task and has_requirements and has_regular_volume and has_contact):
        status = "\u0432\u044b\u0441\u043e\u043a\u0430\u044f"
    elif score >= 9:
        status = "\u0441\u0440\u0435\u0434\u043d\u044f\u044f"
    else:
        status = "\u043d\u0438\u0437\u043a\u0430\u044f"
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


REPLY_BEHAVIOR_VARIANTS = [
    "\u043d\u0430\u0447\u043d\u0438 \u0441 \u043a\u043e\u0440\u043e\u0442\u043a\u043e\u0433\u043e \u043f\u0440\u0438\u0432\u0435\u0442\u0441\u0442\u0432\u0438\u044f, \u0437\u0430\u0442\u0435\u043c \u0441\u0440\u0430\u0437\u0443 \u043a \u0441\u0443\u0442\u0438",
    "\u043d\u0430\u0447\u043d\u0438 \u0441\u0440\u0430\u0437\u0443 \u0441 \u0438\u043d\u0442\u0435\u0440\u0435\u0441\u0430 \u043a \u0437\u0430\u0434\u0430\u0447\u0435, \u0431\u0435\u0437 \u043f\u0440\u0438\u0432\u0435\u0442\u0441\u0442\u0432\u0438\u044f",
    "\u043d\u0430\u0447\u043d\u0438 \u0436\u0438\u0432\u043e \u0438 \u0432 \u043a\u043e\u043d\u0446\u0435 \u0437\u0430\u0434\u0430\u0439 \u043e\u0434\u0438\u043d \u0443\u043c\u0435\u0441\u0442\u043d\u044b\u0439 \u0432\u043e\u043f\u0440\u043e\u0441, \u0435\u0441\u043b\u0438 \u043d\u0435 \u0445\u0432\u0430\u0442\u0430\u0435\u0442 \u0431\u044e\u0434\u0436\u0435\u0442\u0430, \u0441\u0440\u043e\u043a\u043e\u0432 \u0438\u043b\u0438 \u043e\u0431\u044a\u0435\u043c\u0430",
    "\u043d\u0430\u0447\u043d\u0438 \u0441 \u043a\u0440\u0430\u0442\u043a\u043e\u0433\u043e \u0441\u043e\u0433\u043b\u0430\u0441\u0438\u044f \u043f\u043e \u0443\u043a\u0430\u0437\u0430\u043d\u043d\u043e\u043c\u0443 \u0444\u043e\u0440\u043c\u0430\u0442\u0443",
    "\u043d\u0430\u0447\u043d\u0438 \u0441 \u043e\u0434\u043d\u043e\u0439 \u043a\u043e\u0440\u043e\u0442\u043a\u043e\u0439 \u0444\u0440\u0430\u0437\u044b \u043f\u043e \u0441\u0430\u043c\u043e\u0439 \u043a\u043e\u043d\u043a\u0440\u0435\u0442\u043d\u043e\u0439 \u0434\u0435\u0442\u0430\u043b\u0438",
]


def reply_behavior_variant(text: str) -> str:
    digest = hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()
    return REPLY_BEHAVIOR_VARIANTS[int(digest[:8], 16) % len(REPLY_BEHAVIOR_VARIANTS)]


def reply_fact_snippets(text: str, budget: str = "") -> list[str]:
    facts: list[str] = []
    checks = [
        ("Reels", ["reels", "\u0440\u0438\u043b\u0441"]),
        ("Shorts", ["shorts", "\u0448\u043e\u0440\u0442\u0441"]),
        ("TikTok", ["tiktok", "tik tok", "\u0442\u0438\u043a\u0442\u043e\u043a"]),
        ("YouTube", ["youtube", "\u044e\u0442\u0443\u0431"]),
        ("\u0438\u043d\u0442\u0435\u0440\u0432\u044c\u044e", ["\u0438\u043d\u0442\u0435\u0440\u0432\u044c\u044e"]),
        ("\u043f\u043e\u0434\u043a\u0430\u0441\u0442", ["\u043f\u043e\u0434\u043a\u0430\u0441\u0442"]),
        ("\u0440\u0435\u043a\u043b\u0430\u043c\u043d\u044b\u0435 \u043a\u0440\u0435\u0430\u0442\u0438\u0432\u044b", ["\u0440\u0435\u043a\u043b\u0430\u043c", "\u043a\u0440\u0435\u0430\u0442\u0438\u0432"]),
        ("\u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b", ["\u0441\u0443\u0431\u0442\u0438\u0442\u0440"]),
        ("\u0434\u0438\u043d\u0430\u043c\u0438\u0447\u043d\u044b\u0439 \u043c\u043e\u043d\u0442\u0430\u0436", ["\u0434\u0438\u043d\u0430\u043c\u0438\u0447"]),
        ("\u0433\u0440\u0430\u0444\u0438\u0447\u0435\u0441\u043a\u0438\u0435 \u044d\u043b\u0435\u043c\u0435\u043d\u0442\u044b", ["\u0433\u0440\u0430\u0444\u0438\u0447", "\u044d\u043b\u0435\u043c\u0435\u043d\u0442"]),
        ("\u0440\u0435\u0433\u0443\u043b\u044f\u0440\u043d\u044b\u0439 \u043e\u0431\u044a\u0435\u043c", ["\u0432 \u0434\u0435\u043d\u044c", "\u0435\u0436\u0435\u0434\u043d", "\u043e\u0431\u044a\u0435\u043c", "\u043e\u0431\u044a\u0451\u043c"]),
    ]
    for label, markers in checks:
        if contains_any(text, markers):
            facts.append(label)
    if budget:
        facts.append(f"\u0431\u044e\u0434\u0436\u0435\u0442: {budget}")
    return facts[:5]


def make_reply_draft(config: dict, text: str) -> str:
    api_key = str(config.get("openrouter_api_key", "")).strip()
    if not api_key:
        return make_fallback_reply(config, text)

    facts = reply_fact_snippets(text, extract_budget(text))
    facts_text = ", ".join(facts) if facts else "\u0442\u043e\u043b\u044c\u043a\u043e \u043e\u0431\u0449\u0438\u0439 \u0437\u0430\u043f\u0440\u043e\u0441 \u043d\u0430 \u043c\u043e\u043d\u0442\u0430\u0436"
    system_prompt = (
        "\u0422\u044b \u043f\u0438\u0448\u0435\u0448\u044c \u043a\u043e\u0440\u043e\u0442\u043a\u0438\u0439 \u043e\u0442\u043a\u043b\u0438\u043a \u043d\u0430 \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u044e \u0432\u0438\u0434\u0435\u043e\u043c\u043e\u043d\u0442\u0430\u0436\u0435\u0440\u0430.\n\n"
        "\u041f\u0438\u0448\u0438 \u043a\u0430\u043a \u043e\u0431\u044b\u0447\u043d\u044b\u0439 \u0447\u0435\u043b\u043e\u0432\u0435\u043a, \u0430 \u043d\u0435 \u043a\u0430\u043a \u043d\u0435\u0439\u0440\u043e\u0441\u0435\u0442\u044c.\n"
        "\u041d\u0435 \u043f\u043e\u0432\u0442\u043e\u0440\u044f\u0439 \u043e\u0434\u043d\u0443 \u0438 \u0442\u0443 \u0436\u0435 \u0444\u0440\u0430\u0437\u0443 \u0432 \u0440\u0430\u0437\u043d\u044b\u0445 \u043e\u0442\u043a\u043b\u0438\u043a\u0430\u0445.\n"
        f"\u0412\u0430\u0440\u0438\u0430\u043d\u0442 \u043f\u043e\u0432\u0435\u0434\u0435\u043d\u0438\u044f: {reply_behavior_variant(text)}.\n"
        f"\u0424\u0430\u043a\u0442\u044b, \u043a\u043e\u0442\u043e\u0440\u044b\u0435 \u043c\u043e\u0436\u043d\u043e \u0443\u043f\u043e\u043c\u044f\u043d\u0443\u0442\u044c: {facts_text}.\n"
        "\u0418\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439 \u0442\u043e\u043b\u044c\u043a\u043e \u0442\u0435 \u0434\u0435\u0442\u0430\u043b\u0438, \u043a\u043e\u0442\u043e\u0440\u044b\u0435 \u043f\u0440\u044f\u043c\u043e \u0435\u0441\u0442\u044c \u0432 \u0442\u0435\u043a\u0441\u0442\u0435 \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0438.\n"
        "\u0415\u0441\u043b\u0438 \u0432 \u0442\u0435\u043a\u0441\u0442\u0435 \u0435\u0441\u0442\u044c Reels, \u0434\u0438\u043d\u0430\u043c\u0438\u0447\u043d\u044b\u0435 \u0440\u043e\u043b\u0438\u043a\u0438, \u0441\u0443\u0431\u0442\u0438\u0442\u0440\u044b, \u0433\u0440\u0430\u0444\u0438\u0447\u0435\u0441\u043a\u0438\u0435 \u044d\u043b\u0435\u043c\u0435\u043d\u0442\u044b \u0438\u043b\u0438 \u043e\u0431\u044a\u0435\u043c, \u043c\u043e\u0436\u043d\u043e \u0435\u0441\u0442\u0435\u0441\u0442\u0432\u0435\u043d\u043d\u043e \u0443\u043f\u043e\u043c\u044f\u043d\u0443\u0442\u044c \u0442\u043e\u043b\u044c\u043a\u043e \u044d\u0442\u0438 \u0444\u0430\u043a\u0442\u044b.\n"
        "\u041d\u0435 \u043f\u0440\u0438\u0434\u0443\u043c\u044b\u0432\u0430\u0439 \u043e\u0431\u044f\u0437\u0430\u043d\u043d\u043e\u0441\u0442\u0438, \u0444\u043e\u0440\u043c\u0430\u0442 \u0432\u0438\u0434\u0435\u043e, \u043d\u0438\u0448\u0443, \u0442\u0440\u0435\u0431\u043e\u0432\u0430\u043d\u0438\u044f, \u0431\u044e\u0434\u0436\u0435\u0442, \u043e\u043f\u044b\u0442, \u043d\u0430\u0432\u044b\u043a\u0438 \u0438 \u043f\u0440\u043e\u0435\u043a\u0442\u044b.\n"
        "\u0415\u0441\u043b\u0438 \u0434\u0435\u0442\u0430\u043b\u0435\u0439 \u043c\u0430\u043b\u043e, \u043d\u0430\u043f\u0438\u0448\u0438 \u043a\u043e\u0440\u043e\u0442\u043a\u043e \u0438 \u043d\u0435\u0439\u0442\u0440\u0430\u043b\u044c\u043d\u043e, \u0431\u0435\u0437 \u043a\u043e\u043d\u043a\u0440\u0435\u0442\u0438\u043a\u0438 \u043e\u0442 \u0441\u0435\u0431\u044f.\n"
        "\u041d\u0435 \u0430\u043d\u0430\u043b\u0438\u0437\u0438\u0440\u0443\u0439 \u0438 \u043d\u0435 \u043f\u0435\u0440\u0435\u0441\u043a\u0430\u0437\u044b\u0432\u0430\u0439 \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u044e.\n"
        "\u041d\u0435 \u0443\u043f\u043e\u043c\u0438\u043d\u0430\u0439 \u043f\u0440\u043e\u0433\u0440\u0430\u043c\u043c\u044b \u043c\u043e\u043d\u0442\u0430\u0436\u0430, \u0435\u0441\u043b\u0438 \u044d\u0442\u043e \u043d\u0435 \u0441\u043a\u0430\u0437\u0430\u043d\u043e \u0432 \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0438.\n"
        "\u0414\u043b\u0438\u043d\u0430: 2-5 \u043a\u043e\u0440\u043e\u0442\u043a\u0438\u0445 \u043f\u0440\u0435\u0434\u043b\u043e\u0436\u0435\u043d\u0438\u0439.\n"
        "\u0418\u043d\u043e\u0433\u0434\u0430 \u043d\u0430\u0447\u0438\u043d\u0430\u0439 \u0441 \u043f\u0440\u0438\u0432\u0435\u0442\u0441\u0442\u0432\u0438\u044f, \u0438\u043d\u043e\u0433\u0434\u0430 \u0441\u0440\u0430\u0437\u0443 \u0441 \u0441\u0443\u0442\u0438.\n"
        "\u041c\u043e\u0436\u043d\u043e \u0437\u0430\u0434\u0430\u0442\u044c \u0440\u043e\u0432\u043d\u043e \u043e\u0434\u0438\u043d \u0443\u043c\u0435\u0441\u0442\u043d\u044b\u0439 \u0432\u043e\u043f\u0440\u043e\u0441, \u0435\u0441\u043b\u0438 \u0432 \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0438 \u043d\u0435 \u0445\u0432\u0430\u0442\u0430\u0435\u0442 \u0432\u0430\u0436\u043d\u043e\u0439 \u0438\u043d\u0444\u043e\u0440\u043c\u0430\u0446\u0438\u0438.\n"
        f"\u041f\u043e\u0440\u0442\u0444\u043e\u043b\u0438\u043e \u0432\u0441\u0435\u0433\u0434\u0430 \u0432 \u0441\u0430\u043c\u043e\u043c \u043a\u043e\u043d\u0446\u0435: \u041f\u043e\u0440\u0442\u0444\u043e\u043b\u0438\u043e: {config['portfolio_url']}.\n\n"
        "\u0412\u0435\u0440\u043d\u0438 \u0442\u043e\u043b\u044c\u043a\u043e \u0433\u043e\u0442\u043e\u0432\u044b\u0439 \u043e\u0442\u043a\u043b\u0438\u043a \u043d\u0430 \u0440\u0443\u0441\u0441\u043a\u043e\u043c \u044f\u0437\u044b\u043a\u0435.\n"
        "\u041d\u0435 \u043f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u0439 \u0440\u0430\u0441\u0441\u0443\u0436\u0434\u0435\u043d\u0438\u044f, \u0438\u043d\u0441\u0442\u0440\u0443\u043a\u0446\u0438\u0438, \u043f\u043b\u0430\u043d \u0438\u043b\u0438 \u0430\u043d\u0430\u043b\u0438\u0437.\n"
        "\u041d\u0435 \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439 \u0430\u043d\u0433\u043b\u0438\u0439\u0441\u043a\u0438\u0439 \u044f\u0437\u044b\u043a.\n"
        "\u041d\u0435 \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u0443\u0439 Markdown.\n"
        "\u041f\u0435\u0440\u0432\u044b\u0439 \u0441\u0438\u043c\u0432\u043e\u043b \u043e\u0442\u0432\u0435\u0442\u0430 \u0434\u043e\u043b\u0436\u0435\u043d \u0431\u044b\u0442\u044c \u0447\u0430\u0441\u0442\u044c\u044e \u0433\u043e\u0442\u043e\u0432\u043e\u0433\u043e \u043e\u0442\u043a\u043b\u0438\u043a\u0430.\n"
        "\u0415\u0441\u043b\u0438 \u043d\u0435 \u043c\u043e\u0436\u0435\u0448\u044c \u0441\u043e\u0441\u0442\u0430\u0432\u0438\u0442\u044c \u043d\u043e\u0440\u043c\u0430\u043b\u044c\u043d\u044b\u0439 \u043e\u0442\u043a\u043b\u0438\u043a, \u0432\u0435\u0440\u043d\u0438 \u043f\u0443\u0441\u0442\u0443\u044e \u0441\u0442\u0440\u043e\u043a\u0443."
    )
    for model in OPENROUTER_MODELS:
        reply = request_openrouter_reply(api_key, model, system_prompt, text)
        cleaned = clean_reply_draft(reply, config, text)
        if cleaned:
            return cleaned
    return make_fallback_reply(config, text)

def request_openrouter_reply(api_key: str, model: str, system_prompt: str, vacancy_text: str) -> str:
    data = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": vacancy_text},
            ],
            "temperature": 0.7,
            "max_tokens": 220,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/telivizor937-star/ClientWork",
            "X-Title": "ClientWork Lead Agent",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"].get("content") or ""
        return str(content).strip()
    except (KeyError, IndexError, AttributeError, json.JSONDecodeError, urllib.error.URLError, TimeoutError, OSError):
        return ""


def clean_reply_draft(reply: str, config: dict, vacancy_text: str = "") -> str:
    reply = reply.strip()
    if is_invalid_model_output(reply):
        return ""
    if has_unsupported_reply_details(reply, vacancy_text):
        return ""
    portfolio_url = config["portfolio_url"]
    if portfolio_url not in reply:
        reply = f"{reply.rstrip()}\n\n\u041f\u043e\u0440\u0442\u0444\u043e\u043b\u0438\u043e: {portfolio_url}"
    else:
        before, _, after = reply.partition(portfolio_url)
        reply = f"{before.rstrip()}\n\n\u041f\u043e\u0440\u0442\u0444\u043e\u043b\u0438\u043e: {portfolio_url}{after.strip()}"
    reply = limit_reply_sentences(reply, portfolio_url)
    if is_invalid_model_output(reply) or has_unsupported_reply_details(reply, vacancy_text):
        return ""
    return reply.strip()


def limit_reply_sentences(reply: str, portfolio_url: str) -> str:
    before, _, _ = reply.partition("\u041f\u043e\u0440\u0442\u0444\u043e\u043b\u0438\u043e:")
    sentences = re.findall(r"[^.!?\n]+[.!?]", before.strip())
    if not sentences:
        return reply
    kept = " ".join(sentence.strip() for sentence in sentences[:4]).strip()
    return f"{kept}\n\n\u041f\u043e\u0440\u0442\u0444\u043e\u043b\u0438\u043e: {portfolio_url}"


def is_invalid_model_output(
    value: str,
    extra_forbidden: list[str] | None = None,
    require_human_markers: bool = True,
) -> bool:
    forbidden = [
        "user safety",
        "safe",
        "unsafe",
        "we need",
        "let's craft",
        "i need",
        "the user",
        "\u043d\u0443\u0436\u043d\u043e \u043d\u0430\u043f\u0438\u0441\u0430\u0442\u044c",
        "\u0433\u043e\u0442\u043e\u0432\u044b\u0439 \u043e\u0442\u043a\u043b\u0438\u043a",
        "reasoning",
        "analysis",
        "\u0440\u0430\u0441\u0441\u0443\u0436\u0434\u0435\u043d\u0438\u0435",
        "\u0440\u0430\u0441\u0441\u0443\u0436\u0434\u0435\u043d\u0438\u044f",
        "\u0430\u043d\u0430\u043b\u0438\u0437",
        "\u043f\u043b\u0430\u043d",
        "\u0438\u043d\u0441\u0442\u0440\u0443\u043a\u0446\u0438\u044f",
        "\u0438\u043d\u0441\u0442\u0440\u0443\u043a\u0446\u0438\u0438",
    ]
    human_markers = [
        "\u0437\u0434\u0440\u0430\u0432\u0441\u0442\u0432\u0443\u0439\u0442\u0435",
        "\u0434\u043e\u0431\u0440\u044b\u0439",
        "\u043f\u0440\u0438\u0432\u0435\u0442",
        "\u0433\u043e\u0442\u043e\u0432",
        "\u043c\u043e\u0433\u0443",
        "\u0441\u0434\u0435\u043b\u0430\u044e",
        "\u0438\u043d\u0442\u0435\u0440\u0435\u0441\u043d",
        "\u043e\u0431\u0441\u0443\u0434",
    ]
    if extra_forbidden:
        forbidden.extend(extra_forbidden)
    normalized = value.strip().lower()
    if not normalized or len(normalized) < 40:
        return True
    if normalized in {"safe", "unsafe", "user safety: safe", "user safety: unsafe"}:
        return True
    if any(phrase in normalized for phrase in forbidden):
        return True
    if normalized.startswith("{") or normalized.startswith("[") or '"role"' in normalized or '"content"' in normalized:
        return True
    if re.fullmatch(r"https?://\S+", normalized):
        return True
    if require_human_markers and not re.search(r"[.!?]\s|[.!?]$", value):
        return True
    if require_human_markers and not any(marker in normalized for marker in human_markers):
        return True
    return False


def make_fallback_reply(config: dict, vacancy_text: str = "") -> str:
    portfolio = config["portfolio_url"]
    facts = reply_fact_snippets(vacancy_text, extract_budget(vacancy_text))
    variant = int(hashlib.sha1(vacancy_text.encode("utf-8", errors="ignore")).hexdigest()[:8], 16) % 5
    fact_text = ", ".join(facts[:2])
    has_budget = bool(extract_budget(vacancy_text))
    has_volume = contains_any(vacancy_text, ["\u043e\u0431\u044a\u0435\u043c", "\u043e\u0431\u044a\u0451\u043c", "\u0432 \u0434\u0435\u043d\u044c", "\u0435\u0436\u0435\u0434\u043d"])

    if facts and variant == 0:
        body = f"\u0417\u0434\u0440\u0430\u0432\u0441\u0442\u0432\u0443\u0439\u0442\u0435! \u0418\u043d\u0442\u0435\u0440\u0435\u0441\u043d\u0430 \u0437\u0430\u0434\u0430\u0447\u0430: {fact_text}. \u0413\u043e\u0442\u043e\u0432 \u043e\u0431\u0441\u0443\u0434\u0438\u0442\u044c \u0434\u0435\u0442\u0430\u043b\u0438."
    elif facts and variant == 1:
        body = f"\u0418\u043d\u0442\u0435\u0440\u0435\u0441\u0435\u043d {fact_text}. \u0415\u0441\u043b\u0438 \u0430\u043a\u0442\u0443\u0430\u043b\u044c\u043d\u043e, \u043d\u0430\u043f\u0438\u0448\u0438\u0442\u0435, \u0438 \u043e\u0431\u0441\u0443\u0434\u0438\u043c \u0437\u0430\u0434\u0430\u0447\u0443."
    elif facts and variant == 2 and not (has_budget and has_volume):
        question = "\u041c\u043e\u0436\u0435\u0442\u0435 \u0443\u0442\u043e\u0447\u043d\u0438\u0442\u044c \u0432\u0430\u0436\u043d\u044b\u0435 \u0443\u0441\u043b\u043e\u0432\u0438\u044f \u043f\u043e \u0437\u0430\u0434\u0430\u0447\u0435?"
        body = f"\u0414\u043e\u0431\u0440\u044b\u0439 \u0434\u0435\u043d\u044c! \u041f\u043e\u0434\u0445\u043e\u0434\u0438\u0442 \u0444\u043e\u0440\u043c\u0430\u0442: {fact_text}. {question}"
    elif facts:
        body = f"\u041c\u043e\u0433\u0443 \u0432\u0437\u044f\u0442\u044c\u0441\u044f \u0437\u0430 {fact_text}. \u041d\u0430\u043f\u0438\u0448\u0438\u0442\u0435, \u0435\u0441\u043b\u0438 \u043d\u0443\u0436\u043d\u043e \u0431\u044b\u0441\u0442\u0440\u043e \u043e\u0431\u0441\u0443\u0434\u0438\u0442\u044c."
    else:
        body = "\u0417\u0434\u0440\u0430\u0432\u0441\u0442\u0432\u0443\u0439\u0442\u0435! \u0418\u043d\u0442\u0435\u0440\u0435\u0441\u043d\u043e \u043e\u0431\u0441\u0443\u0434\u0438\u0442\u044c \u0437\u0430\u0434\u0430\u0447\u0443. \u041c\u043e\u0436\u0435\u0442\u0435 \u0441\u043a\u0438\u043d\u0443\u0442\u044c \u0434\u0435\u0442\u0430\u043b\u0438?"
    return f"{body}\n\n\u041f\u043e\u0440\u0442\u0444\u043e\u043b\u0438\u043e: {portfolio}"


def source_contains_any(source_text: str, variants: list[str]) -> bool:
    lowered = source_text.lower()
    return any(variant in lowered for variant in variants)


def has_unsupported_reply_details(reply: str, vacancy_text: str) -> bool:
    if not vacancy_text:
        return False
    checks = [
        (["reels", "\u0440\u0438\u043b\u0441"], ["reels", "\u0440\u0438\u043b\u0441"]),
        (["shorts", "\u0448\u043e\u0440\u0442\u0441"], ["shorts", "\u0448\u043e\u0440\u0442\u0441"]),
        (["tiktok", "tik tok", "\u0442\u0438\u043a\u0442\u043e\u043a"], ["tiktok", "tik tok", "\u0442\u0438\u043a\u0442\u043e\u043a"]),
        (["youtube", "\u044e\u0442\u0443\u0431"], ["youtube", "\u044e\u0442\u0443\u0431"]),
        (["capcut", "vn", "premiere", "after effects"], ["capcut", "vn", "premiere", "after effects"]),
        (["\u0448\u0443\u043c"], ["\u0448\u0443\u043c"]),
        (["\u043c\u0443\u0437\u044b\u043a"], ["\u043c\u0443\u0437\u044b\u043a"]),
        (["\u0441\u0443\u0431\u0442\u0438\u0442\u0440"], ["\u0441\u0443\u0431\u0442\u0438\u0442\u0440"]),
        (["\u0434\u0438\u043d\u0430\u043c\u0438\u0447"], ["\u0434\u0438\u043d\u0430\u043c\u0438\u0447"]),
        (["\u0433\u0440\u0430\u0444\u0438\u0447"], ["\u0433\u0440\u0430\u0444\u0438\u0447"]),
        (["\u044d\u043b\u0435\u043c\u0435\u043d\u0442"], ["\u044d\u043b\u0435\u043c\u0435\u043d\u0442"]),
        (["\u043e\u0431\u044a\u0435\u043c", "\u043e\u0431\u044a\u0451\u043c", "\u0432 \u0434\u0435\u043d\u044c"], ["\u043e\u0431\u044a\u0435\u043c", "\u043e\u0431\u044a\u0451\u043c", "\u0432 \u0434\u0435\u043d\u044c"]),
        (["\u0445\u0443\u043a"], ["\u0445\u0443\u043a"]),
        (["\u0443\u0434\u0435\u0440\u0436\u0430\u043d"], ["\u0443\u0434\u0435\u0440\u0436\u0430\u043d"]),
        (["\u0446\u0432\u0435\u0442"], ["\u0446\u0432\u0435\u0442"]),
        (["\u043e\u0431\u043b\u043e\u0436"], ["\u043e\u0431\u043b\u043e\u0436"]),
        (["\u0430\u043d\u0438\u043c\u0430\u0446"], ["\u0430\u043d\u0438\u043c\u0430\u0446"]),
        (["\u044d\u0444\u0444\u0435\u043a\u0442"], ["\u044d\u0444\u0444\u0435\u043a\u0442"]),
        (["\u043f\u0435\u0440\u0435\u0445\u043e\u0434"], ["\u043f\u0435\u0440\u0435\u0445\u043e\u0434"]),
        (["\u043e\u0437\u0432\u0443\u0447"], ["\u043e\u0437\u0432\u0443\u0447"]),
        (["\u0441\u0446\u0435\u043d\u0430\u0440"], ["\u0441\u0446\u0435\u043d\u0430\u0440"]),
    ]
    lowered_reply = reply.lower()
    return any(
        any(marker in lowered_reply for marker in reply_markers)
        and not source_contains_any(vacancy_text, source_markers)
        for reply_markers, source_markers in checks
    )


def enough_fact_overlap(value: str, source_text: str) -> bool:
    normalized = value.strip().lower()
    if not normalized or normalized == "\u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e":
        return True
    if normalized in source_text.lower():
        return True
    words = re.findall(r"[a-zA-Z\u0400-\u04ff0-9]{4,}", normalized)
    if not words:
        return True
    source_lowered = source_text.lower()
    return sum(1 for word in words if word in source_lowered) >= min(2, len(words))


def is_missing_summary_value(value: str) -> bool:
    normalized = value.strip().lower().strip(".")
    return normalized in {"", "\u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d", "\u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e", "-", "\u2014"}


def trim_summary_text(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", value.strip())
    if len(value) <= limit:
        return value
    sentence = re.search(r"^(.{20,%d}?[.!?])\s" % max(20, limit - 1), value)
    if sentence:
        return sentence.group(1).strip()
    shortened = value[:limit].rstrip()
    if " " in shortened:
        shortened = shortened.rsplit(" ", 1)[0]
    return shortened.rstrip(" ,;:-")


def make_vacancy_brief(config: dict, text: str, budget: str) -> VacancyBrief:
    api_key = str(config.get("openrouter_api_key", "")).strip()
    if not api_key:
        return make_fallback_vacancy_brief(text, budget)

    system_prompt = (
        "\u0422\u044b \u0434\u0435\u043b\u0430\u0435\u0448\u044c \u043a\u0440\u0430\u0442\u043a\u0443\u044e \u0432\u044b\u0436\u0438\u043c\u043a\u0443 \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0438 \u0434\u043b\u044f Telegram.\n"
        "\u0412\u0435\u0440\u043d\u0438 \u0442\u043e\u043b\u044c\u043a\u043e \u0433\u043e\u0442\u043e\u0432\u044b\u0439 \u0440\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442 \u043d\u0430 \u0440\u0443\u0441\u0441\u043a\u043e\u043c \u044f\u0437\u044b\u043a\u0435.\n"
        "\u041d\u0438\u043a\u0430\u043a\u0438\u0445 \u0440\u0430\u0441\u0441\u0443\u0436\u0434\u0435\u043d\u0438\u0439, \u043e\u0431\u044a\u044f\u0441\u043d\u0435\u043d\u0438\u0439, \u043c\u044b\u0441\u043b\u0435\u0439 \u043c\u043e\u0434\u0435\u043b\u0438, \u0438\u043d\u0441\u0442\u0440\u0443\u043a\u0446\u0438\u0439 \u0438\u043b\u0438 Markdown.\n"
        "\u041d\u0435 \u0432\u044b\u0432\u043e\u0434\u0438 \u043f\u043e\u043b\u043d\u044b\u0439 \u0442\u0435\u043a\u0441\u0442 \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0438.\n"
        "\u0412\u044b\u0436\u0438\u043c\u043a\u0430 \u0434\u043e\u043b\u0436\u043d\u0430 \u0441\u043e\u0434\u0435\u0440\u0436\u0430\u0442\u044c \u0442\u043e\u043b\u044c\u043a\u043e \u0444\u0430\u043a\u0442\u044b, \u043f\u0440\u044f\u043c\u043e \u0443\u043a\u0430\u0437\u0430\u043d\u043d\u044b\u0435 \u0432 \u0438\u0441\u0445\u043e\u0434\u043d\u043e\u043c \u0442\u0435\u043a\u0441\u0442\u0435.\n"
        "\u041d\u0435 \u043f\u0440\u0438\u0434\u0443\u043c\u044b\u0432\u0430\u0439 \u043e\u0431\u044f\u0437\u0430\u043d\u043d\u043e\u0441\u0442\u0438, \u0444\u043e\u0440\u043c\u0430\u0442 \u0432\u0438\u0434\u0435\u043e, \u043d\u0438\u0448\u0443, \u0442\u0440\u0435\u0431\u043e\u0432\u0430\u043d\u0438\u044f, \u0431\u044e\u0434\u0436\u0435\u0442 \u0438 \u0434\u0440\u0443\u0433\u0438\u0435 \u0434\u0435\u0442\u0430\u043b\u0438.\n"
        "\u0415\u0441\u043b\u0438 \u0444\u0430\u043a\u0442\u0430 \u0434\u043b\u044f \u043f\u0443\u043d\u043a\u0442\u0430 \u043d\u0435\u0442, \u043e\u0441\u0442\u0430\u0432\u044c \u0441\u0442\u0440\u043e\u043a\u0443 \u043f\u0443\u043d\u043a\u0442\u0430 \u043f\u0443\u0441\u0442\u043e\u0439.\n"
        "\u0424\u043e\u0440\u043c\u0430\u0442 \u0441\u0442\u0440\u043e\u0433\u043e 5 \u0441\u0442\u0440\u043e\u043a:\n"
        "\u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435: \u043a\u043e\u0440\u043e\u0442\u043a\u043e\u0435 \u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435 \u0438\u0437 \u0442\u0435\u043a\u0441\u0442\u0430 \u0438\u043b\u0438 \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e\n"
        "\u041f\u0443\u043d\u043a\u0442 1: \u0444\u0430\u043a\u0442 \u0438\u0437 \u0442\u0435\u043a\u0441\u0442\u0430 \u0438\u043b\u0438 \u043f\u0443\u0441\u0442\u043e\n"
        "\u041f\u0443\u043d\u043a\u0442 2: \u0444\u0430\u043a\u0442 \u0438\u0437 \u0442\u0435\u043a\u0441\u0442\u0430 \u0438\u043b\u0438 \u043f\u0443\u0441\u0442\u043e\n"
        "\u041f\u0443\u043d\u043a\u0442 3: \u0444\u0430\u043a\u0442 \u0438\u0437 \u0442\u0435\u043a\u0441\u0442\u0430 \u0438\u043b\u0438 \u043f\u0443\u0441\u0442\u043e\n"
        "\u0411\u044e\u0434\u0436\u0435\u0442: \u0431\u044e\u0434\u0436\u0435\u0442 \u0438\u0437 \u0442\u0435\u043a\u0441\u0442\u0430 \u0438\u043b\u0438 \u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\n"
        "\u0415\u0441\u043b\u0438 \u043d\u0435\u0432\u043e\u0437\u043c\u043e\u0436\u043d\u043e \u0441\u043e\u0441\u0442\u0430\u0432\u0438\u0442\u044c \u043d\u043e\u0440\u043c\u0430\u043b\u044c\u043d\u0443\u044e \u0432\u044b\u0436\u0438\u043c\u043a\u0443, \u0432\u0435\u0440\u043d\u0438 \u043f\u0443\u0441\u0442\u0443\u044e \u0441\u0442\u0440\u043e\u043a\u0443."
    )
    for model in OPENROUTER_MODELS:
        raw = request_openrouter_reply(api_key, model, system_prompt, text)
        brief = clean_vacancy_brief(raw, text, budget)
        if brief:
            return brief
    return make_fallback_vacancy_brief(text, budget)

def clean_vacancy_brief(raw: str, text: str, budget: str) -> VacancyBrief | None:
    if is_invalid_model_output(raw, require_human_markers=False):
        return None

    values: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip().lower()] = value.strip(" -\u2022\t")

    title = values.get("\u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435", "") or "\u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e"
    bullets = [
        values.get("\u043f\u0443\u043d\u043a\u0442 1", ""),
        values.get("\u043f\u0443\u043d\u043a\u0442 2", ""),
        values.get("\u043f\u0443\u043d\u043a\u0442 3", ""),
    ]
    summary_budget = values.get("\u0431\u044e\u0434\u0436\u0435\u0442", "") or budget or "\u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e"
    if not enough_fact_overlap(title, text):
        title = "\u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e"
    bullets = [
        trim_summary_text(item, 120)
        for item in bullets
        if not is_missing_summary_value(item) and enough_fact_overlap(item, text)
    ]
    if summary_budget != "\u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e" and budget and summary_budget != budget and not enough_fact_overlap(summary_budget, text):
        summary_budget = budget
    return VacancyBrief(title=trim_summary_text(title, 80), bullets=bullets[:3], budget=trim_summary_text(summary_budget, 80))


def factual_summary_lines(text: str) -> list[str]:
    priority_lines: list[str] = []
    other_lines: list[str] = []
    useful_markers = [
        "\u0438\u0449\u0443",
        "\u0438\u0449\u0435\u043c",
        "\u043d\u0443\u0436",
        "\u043c\u043e\u043d\u0442\u0430\u0436",
        "\u043c\u043e\u043d\u0442\u0430\u0436\u0435\u0440",
        "reels",
        "\u0440\u0438\u043b\u0441",
        "shorts",
        "youtube",
        "tiktok",
        "\u043e\u043f\u044b\u0442",
        "\u0443\u0434\u0430\u043b",
        "\u043e\u043f\u043b\u0430\u0442",
        "\u0431\u044e\u0434\u0436\u0435\u0442",
        "\u043e\u0431\u044a\u0435\u043c",
        "\u0433\u0440\u0430\u0444\u0438\u043a",
        "\u0442\u0440\u0435\u0431",
    ]
    for raw_line in text.splitlines():
        line = re.sub(r"\s+", " ", raw_line.strip(" -\u2022\t"))
        if not line or line.startswith("#"):
            continue
        lowered = line.lower()
        service_markers = [
            "\u043e\u0431\u044f\u0437\u0430\u043d\u043d\u043e\u0441\u0442\u0438",
            "\u0442\u0440\u0435\u0431\u043e\u0432\u0430\u043d\u0438\u044f",
            "\u0443\u0441\u043b\u043e\u0432\u0438",
            "\u0431\u044e\u0434\u0436\u0435\u0442",
            "\u0441\u0441\u044b\u043b\u043a\u0430 \u043d\u0430 \u043f\u0440\u043e\u0435\u043a\u0442",
            "\u043d\u0435 \u043d\u0443\u0436\u043d\u044b",
        ]
        if any(marker in lowered for marker in service_markers) and len(line.split()) <= 4:
            continue
        if any(marker in lowered for marker in ["\u043a\u0442\u043e \u0443\u0447\u0438\u0442\u0441\u044f", "\u043a\u0442\u043e \u043f\u0440\u043e\u043f\u0430\u0434\u0430\u0435\u0442", "\u043d\u0435 \u0440\u0430\u0441\u0441\u043c\u0430\u0442\u0440"]):
            continue
        if len(line) < 8 or (len(line.split()) <= 2 and not any(marker in lowered for marker in useful_markers)):
            continue
        target = priority_lines if any(marker in lowered for marker in useful_markers) else other_lines
        target.append(trim_summary_text(line, 120))
    unique: list[str] = []
    seen: set[str] = set()
    for line in priority_lines + other_lines:
        key = line.lower()
        if key not in seen:
            seen.add(key)
            unique.append(line)
    return unique


def make_fallback_vacancy_brief(text: str, budget: str) -> VacancyBrief:
    lines = factual_summary_lines(text)
    generated_title = make_title(text)
    title = lines[0] if generated_title.startswith("#") and lines else generated_title or (lines[0] if lines else "\u0412\u0430\u043a\u0430\u043d\u0441\u0438\u044f")
    bullets = lines[1:4] if lines and lines[0] == title else lines[:3]
    return VacancyBrief(
        title=trim_summary_text(title, 80),
        bullets=[trim_summary_text(item, 120) for item in bullets if not is_missing_summary_value(item)],
        budget=budget or "\u043d\u0435 \u0443\u043a\u0430\u0437\u0430\u043d\u043e",
    )


LEADS_FIELDNAMES = [
    "lead_id",
    "found_at",
    "title",
    "source",
    "vacancy_url",
    "budget",
    "summary",
    "generated_reply",
    "contact_status",
    "contacted_at",
    "client_answered",
    "client_answered_at",
    "notes",
    "post_date",
    "score",
    "status",
    "reason",
    "message",
]


def make_lead_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def lead_to_row(lead: Lead) -> dict[str, str]:
    return {
        "lead_id": lead.lead_id or make_lead_id(lead.link),
        "found_at": lead.found_at,
        "title": lead.title,
        "source": lead.channel,
        "vacancy_url": lead.link,
        "budget": lead.budget,
        "summary": make_title(lead.message),
        "generated_reply": lead.reply_draft,
        "contact_status": lead.contact_status or "not_contacted",
        "contacted_at": lead.contacted_at,
        "client_answered": lead.client_answered or "no",
        "client_answered_at": lead.client_answered_at,
        "notes": lead.notes,
        "post_date": lead.post_date,
        "score": str(lead.score),
        "status": lead.status,
        "reason": lead.reason,
        "message": lead.message,
    }


def normalize_lead_row(row: dict[str, str]) -> dict[str, str]:
    vacancy_url = row.get("vacancy_url") or row.get("link") or ""
    message = row.get("message", "")
    normalized = {field: row.get(field, "") for field in LEADS_FIELDNAMES}
    normalized["lead_id"] = row.get("lead_id") or make_lead_id(vacancy_url)
    normalized["found_at"] = row.get("found_at", "")
    normalized["title"] = row.get("title", "")
    normalized["source"] = row.get("source") or row.get("channel", "")
    normalized["vacancy_url"] = vacancy_url
    normalized["budget"] = row.get("budget", "")
    normalized["summary"] = row.get("summary") or make_title(message)
    normalized["generated_reply"] = row.get("generated_reply") or row.get("reply_draft", "")
    normalized["contact_status"] = row.get("contact_status") or "not_contacted"
    normalized["contacted_at"] = row.get("contacted_at", "")
    normalized["client_answered"] = row.get("client_answered") or "no"
    normalized["client_answered_at"] = row.get("client_answered_at", "")
    normalized["notes"] = row.get("notes", "")
    normalized["post_date"] = row.get("post_date", "")
    normalized["score"] = row.get("score", "")
    normalized["status"] = row.get("status", "")
    normalized["reason"] = row.get("reason", "")
    normalized["message"] = message
    return normalized


def read_lead_rows() -> list[dict[str, str]]:
    if not OUTPUT_FILE.exists():
        return []
    with OUTPUT_FILE.open("r", encoding="utf-8-sig", newline="") as file:
        return [normalize_lead_row(row) for row in csv.DictReader(file)]


def write_lead_rows(rows: list[dict[str, str]]) -> None:
    with OUTPUT_FILE.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=LEADS_FIELDNAMES)
        writer.writeheader()
        writer.writerows([{field: row.get(field, "") for field in LEADS_FIELDNAMES} for row in rows])


def read_existing_links() -> set[str]:
    return {row["vacancy_url"] for row in read_lead_rows() if row.get("vacancy_url")}


def normalize_employer_id(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"^https?://", "", value)
    value = re.sub(r"^t\.me/", "@", value)
    value = value.strip(" /.,;)")
    if value.startswith("@"):
        return "tg:" + value[1:]
    if "@" in value and "." in value:
        return "email:" + value
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 10:
        return "phone:" + digits[-11:]
    return value


def extract_employer_id(text: str) -> str:
    skip_usernames = {
        "youtube",
        "reels",
        "shorts",
        "tiktok",
        "telegram",
        "workinonlybusiness",
    }
    patterns = [
        r"(?:контакт|связ[ьи]|писать|пишите|лс|директ|отклик)[^\n@]*(@[a-zA-Z0-9_]{5,32})",
        r"(?:контакт|связ[ьи]|писать|пишите|лс|директ|отклик)[^\n]*(?:https?://)?t\.me/([a-zA-Z0-9_]{5,32})",
        r"@[a-zA-Z0-9_]{5,32}",
        r"(?:https?://)?t\.me/[a-zA-Z0-9_]{5,32}",
        r"[\w.+-]+@[\w.-]+\.\w+",
        r"(?:\+?\d[\d\s().-]{9,}\d)",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.I):
            value = match if isinstance(match, str) else next((item for item in match if item), "")
            if not value:
                continue
            if re.fullmatch(r"[a-zA-Z0-9_]{5,32}", value):
                value = "@" + value
            normalized = normalize_employer_id(value)
            username = normalized.removeprefix("tg:")
            if username and username in skip_usernames:
                continue
            return normalized
    return ""


def lead_history_date(lead: Lead) -> str:
    return (lead.post_date or lead.found_at or datetime.now(timezone.utc).isoformat(timespec="seconds"))[:10]


def load_employer_history() -> dict[str, dict]:
    if not EMPLOYER_HISTORY_FILE.exists():
        return {}
    try:
        data = json.loads(EMPLOYER_HISTORY_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save_employer_history(history: dict[str, dict]) -> None:
    EMPLOYER_HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_employer_history_update(leads: list[Lead]) -> tuple[dict[str, dict], dict[str, dict]]:
    history = load_employer_history()
    stats_by_lead_id: dict[str, dict] = {}
    for lead in leads:
        employer_id = extract_employer_id(lead.message)
        if not employer_id:
            continue
        lead_id = lead.lead_id or make_lead_id(lead.link)
        date = lead_history_date(lead)
        entry = history.setdefault(
            employer_id,
            {
                "employer_id": employer_id,
                "first_seen": date,
                "last_seen": date,
                "lead_ids": [],
            },
        )
        lead_ids = entry.setdefault("lead_ids", [])
        if lead_id not in lead_ids:
            lead_ids.append(lead_id)
        entry["first_seen"] = min(str(entry.get("first_seen") or date), date)
        entry["last_seen"] = max(str(entry.get("last_seen") or date), date)
        entry["count"] = len(set(lead_ids))
        stats_by_lead_id[lead_id] = {
            "count": entry["count"],
            "first_seen": entry["first_seen"],
            "last_seen": entry["last_seen"],
        }
    return history, stats_by_lead_id


def append_leads(leads: list[Lead]) -> None:
    if not leads:
        return
    rows = read_lead_rows()
    existing_ids = {row["lead_id"] for row in rows}
    for lead in leads:
        row = lead_to_row(lead)
        if row["lead_id"] not in existing_ids:
            rows.append(row)
            existing_ids.add(row["lead_id"])
    write_lead_rows(rows)


def update_lead_status(lead_id: str, contact_status: str, now: datetime) -> tuple[bool, dict[str, str] | None]:
    rows = read_lead_rows()
    changed = False
    updated_row: dict[str, str] | None = None
    for row in rows:
        if row.get("lead_id") != lead_id:
            continue
        updated_row = row
        if row.get("contact_status") == contact_status:
            break
        row["contact_status"] = contact_status
        if contact_status == "contacted" and not row.get("contacted_at"):
            row["contacted_at"] = now.isoformat(timespec="seconds")
        changed = True
        break
    if changed:
        write_lead_rows(rows)
    return changed, updated_row


def update_lead_action(lead_id: str, action: str, now: datetime) -> tuple[bool, dict[str, str] | None]:
    if action == "replied":
        return update_lead_status(lead_id, "contacted", now)
    if action == "rejected":
        return update_lead_status(lead_id, "skipped", now)
    if action != "client_answered":
        return False, None

    rows = read_lead_rows()
    changed = False
    updated_row: dict[str, str] | None = None
    for row in rows:
        if row.get("lead_id") != lead_id:
            continue
        updated_row = row
        if row.get("client_answered") == "yes":
            break
        row["client_answered"] = "yes"
        if not row.get("client_answered_at"):
            row["client_answered_at"] = now.isoformat(timespec="seconds")
        changed = True
        break
    if changed:
        write_lead_rows(rows)
    return changed, updated_row


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


def default_runtime_state() -> dict:
    return {
        "next_source_group_index": 0,
        "last_message_ids": {},
        "sent_urls": [],
        "sent_message_ids": [],
        "sent_text_hashes": [],
        "sent_text_fingerprints": [],
        "last_checked_at": "",
        "telegram_update_offset": 0,
        "last_daily_table_sent_date": "",
    }


def load_runtime_state() -> dict:
    if not RUNTIME_STATE_FILE.exists():
        return default_runtime_state()
    try:
        loaded = json.loads(RUNTIME_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_runtime_state()
    state = default_runtime_state()
    for key, value in loaded.items():
        if key in state:
            state[key] = value
    return state


def save_runtime_state(state: dict) -> None:
    RUNTIME_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def post_message_id(post: dict[str, str]) -> int:
    match = re.search(r"/(\d+)(?:\?|$)", post.get("link", ""))
    return int(match.group(1)) if match else 0


def post_message_key(post: dict[str, str]) -> str:
    return f"{channel_name(post.get('channel', ''))}:{post_message_id(post)}"


def normalize_text_for_dedupe(text: str) -> str:
    lowered = text.lower()
    lowered = re.sub(r"https?://\S+", " ", lowered)
    lowered = re.sub(r"@\w+", " ", lowered)
    lowered = re.sub(r"[^a-zа-яё0-9]+", " ", lowered, flags=re.I)
    return re.sub(r"\s+", " ", lowered).strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize_text_for_dedupe(text).encode("utf-8")).hexdigest()


def text_fingerprint(text: str) -> str:
    tokens = [token for token in normalize_text_for_dedupe(text).split() if len(token) > 3]
    return " ".join(sorted(set(tokens))[:160])


def is_near_duplicate_text(fingerprint: str, existing_fingerprints: list[str]) -> bool:
    current = set(fingerprint.split())
    if len(current) < 8:
        return False
    for item in existing_fingerprints[-800:]:
        other = set(str(item).split())
        if not other:
            continue
        overlap = len(current & other) / max(len(current), len(other))
        if overlap >= 0.88:
            return True
    return False


def trim_state_lists(state: dict, limit: int = 5000) -> None:
    for key in ["sent_urls", "sent_message_ids", "sent_text_hashes", "sent_text_fingerprints"]:
        values = list(dict.fromkeys(state.get(key, [])))
        state[key] = values[-limit:]


def mark_lead_sent(state: dict, lead: Lead) -> None:
    post = {"channel": lead.channel, "link": lead.link, "text": lead.message}
    state.setdefault("sent_urls", []).append(lead.link)
    state.setdefault("sent_message_ids", []).append(post_message_key(post))
    state.setdefault("sent_text_hashes", []).append(text_hash(lead.message))
    state.setdefault("sent_text_fingerprints", []).append(text_fingerprint(lead.message))
    update_last_message_id(state, post)
    trim_state_lists(state)


def is_priority_source(source: ChannelSource) -> bool:
    name = channel_name(source.url).lower()
    category = source.category.lower()
    profile_markers = [
        "video",
        "montage",
        "reels",
        "shorts",
        "tiktok",
        "motion",
        "youtube",
        "editor",
        "videographer",
    ]
    return category in {"channels.txt", "video_editing", "media_production"} or any(
        marker in name or marker in category for marker in profile_markers
    )


def select_sources_for_run(sources: list[ChannelSource], state: dict, config: dict) -> list[ChannelSource]:
    priority = [source for source in sources if is_priority_source(source)]
    rotating = [source for source in sources if not is_priority_source(source)]
    group_size = max(1, int(config.get("rotating_source_group_size", 35)))
    groups = [rotating[index : index + group_size] for index in range(0, len(rotating), group_size)]
    group_index = int(state.get("next_source_group_index", 0))
    current_group = groups[group_index % len(groups)] if groups else []
    state["next_source_group_index"] = (group_index + 1) % max(1, len(groups))

    selected: dict[str, ChannelSource] = {}
    for source in priority + current_group:
        selected.setdefault(channel_name(source.url).lower(), source)
    return list(selected.values())


def is_duplicate_post(post: dict[str, str], state: dict, existing_links: set[str]) -> bool:
    message_id = post_message_id(post)
    source_name = channel_name(post["channel"])
    if message_id and message_id <= int(state.get("last_message_ids", {}).get(source_name, 0)):
        return True
    if post["link"] in existing_links or post["link"] in set(state.get("sent_urls", [])):
        return True
    if post_message_key(post) in set(state.get("sent_message_ids", [])):
        return True
    current_hash = text_hash(post["text"])
    if current_hash in set(state.get("sent_text_hashes", [])):
        return True
    return is_near_duplicate_text(text_fingerprint(post["text"]), list(state.get("sent_text_fingerprints", [])))


def update_last_message_id(state: dict, post: dict[str, str]) -> None:
    message_id = post_message_id(post)
    if not message_id:
        return
    source_name = channel_name(post["channel"])
    values = state.setdefault("last_message_ids", {})
    values[source_name] = max(int(values.get(source_name, 0)), message_id)


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


DISCOVERY_QUERIES = [
    "\u043c\u043e\u043d\u0442\u0430\u0436", "\u0432\u0438\u0434\u0435\u043e\u043c\u043e\u043d\u0442\u0430\u0436", "\u043c\u043e\u043d\u0442\u0430\u0436\u0435\u0440", "\u043c\u043e\u043d\u0442\u0430\u0436\u0451\u0440", "reels", "shorts",
    "video editor", "motion", "motion designer", "youtube", "youtube editor",
    "tiktok", "\u043a\u043e\u043d\u0442\u0435\u043d\u0442", "digital", "smm", "freelance", "\u0443\u0434\u0430\u043b\u0435\u043d\u043d\u0430\u044f \u0440\u0430\u0431\u043e\u0442\u0430",
    "\u0443\u0434\u0430\u043b\u0451\u043d\u043d\u0430\u044f \u0440\u0430\u0431\u043e\u0442\u0430", "\u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0438", "\u0440\u0430\u0431\u043e\u0442\u0430", "\u043a\u0440\u0435\u0430\u0442\u0438\u0432", "videographer",
    "content creator", "digital jobs", "marketing jobs", "creative jobs",
    "remote jobs", "freelance jobs", "video editing jobs", "shorts editor",
]

DISCOVERY_INDEX_URLS = [
    ("search-t", "https://search-t.me/search?query={query}"),
    ("tgstat", "https://tgstat.ru/search?query={query}"),
    ("tgstat", "https://tgstat.org/search?query={query}"),
    ("tgstat", "https://tgstat.org/top100/683/career/"),
    ("telemetr", "https://telemetr.me/channels/?q={query}"),
    ("telemetr", "https://telemetr.me/catalog/jobs"),
    ("semagram", "https://semagram.ru/search?query={query}"),
    ("telegram_directory", "https://telegramchannels.me/search?search={query}"),
    ("telegram_directory", "https://tlgrm.eu/channels?search={query}"),
    ("google", "https://www.google.com/search?q={query}+site%3At.me%2Fs"),
    ("google", "https://www.google.com/search?q={query}+Telegram+channel"),
]

DISCOVERY_RELEVANCE_KEYWORDS = [
    "\u043c\u043e\u043d\u0442\u0430\u0436", "\u0432\u0438\u0434\u0435\u043e\u043c\u043e\u043d\u0442\u0430\u0436", "\u043c\u043e\u043d\u0442\u0430\u0436\u0435\u0440", "\u043c\u043e\u043d\u0442\u0430\u0436\u0451\u0440", "reels", "shorts",
    "video editor", "motion designer", "youtube editor", "tiktok", "tik tok",
]

DISCOVERY_VACANCY_KEYWORDS = [
    "\u0438\u0449\u0435\u043c", "\u0438\u0449\u0443", "\u043d\u0443\u0436\u0435\u043d", "\u043d\u0443\u0436\u043d\u0430", "\u0442\u0440\u0435\u0431\u0443\u0435\u0442\u0441\u044f", "\u0432\u0430\u043a\u0430\u043d\u0441\u0438\u044f", "\u0440\u0430\u0431\u043e\u0442\u0430",
    "\u0437\u0430\u043a\u0430\u0437", "\u043f\u0440\u043e\u0435\u043a\u0442", "\u043e\u043f\u043b\u0430\u0442\u0430", "\u0431\u044e\u0434\u0436\u0435\u0442", "hiring", "job", "vacancy",
    "looking for", "remote", "freelance",
]

TELEGRAM_USERNAME_RE = re.compile(r"(?:https?://t\.me/(?:s/)?|@)([A-Za-z0-9_]{4,32})", re.I)
RESERVED_TELEGRAM_NAMES = {"joinchat", "addstickers", "share", "iv", "s", "c", "telegram"}


def source_discovery_due(config: dict, now: datetime) -> bool:
    discovery = config.get("source_discovery", {})
    if not discovery.get("enabled", True):
        return False
    if not DISCOVERED_SOURCES_FILE.exists():
        return True

    latest: datetime | None = None
    try:
        with DISCOVERED_SOURCES_FILE.open("r", encoding="utf-8-sig", newline="") as file:
            for row in csv.DictReader(file):
                value = row.get("discovered_at", "")
                parsed = parse_post_datetime(value)
                if parsed and (latest is None or parsed > latest):
                    latest = parsed
    except (OSError, csv.Error):
        return True
    if latest is None:
        return True
    return now - latest >= timedelta(hours=int(discovery.get("interval_hours", 24)))


def fetch_url(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def discover_candidate_usernames(timeout: int = 20) -> dict[str, set[str]]:
    candidates: dict[str, set[str]] = {}
    tasks: list[tuple[str, str]] = []
    for provider, template in DISCOVERY_INDEX_URLS:
        if "{query}" not in template:
            tasks.append((provider, template))
            continue
        for query in DISCOVERY_QUERIES:
            tasks.append((provider, template.format(query=urllib.parse.quote_plus(query))))

    def fetch_index(task: tuple[str, str]) -> tuple[str, str]:
        provider, url = task
        try:
            return provider, fetch_url(url, timeout=timeout)
        except (urllib.error.URLError, TimeoutError, OSError):
            return provider, ""

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(fetch_index, task) for task in tasks]
        for future in as_completed(futures):
            provider, page_html = future.result()
            if not page_html:
                continue
            for match in TELEGRAM_USERNAME_RE.findall(page_html):
                username = match.strip("/_")
                lowered = username.lower()
                if lowered in RESERVED_TELEGRAM_NAMES:
                    continue
                if re.fullmatch(r"[A-Za-z0-9_]{4,32}", username):
                    candidates.setdefault(lowered, set()).add(provider)
    return candidates


def load_existing_source_names() -> set[str]:
    names = {channel_name(source.url).lower() for source in load_channel_sources()}
    if DISCOVERED_SOURCES_FILE.exists():
        try:
            with DISCOVERED_SOURCES_FILE.open("r", encoding="utf-8-sig", newline="") as file:
                for row in csv.DictReader(file):
                    if row.get("status") == "accepted":
                        names.add(str(row.get("username", "")).lower())
        except (OSError, csv.Error):
            pass
    return names


def discovery_relevance(text: str) -> bool:
    lowered = text.lower()
    return any(item in lowered for item in DISCOVERY_RELEVANCE_KEYWORDS) and any(
        item in lowered for item in DISCOVERY_VACANCY_KEYWORDS
    )


def verify_discovered_source(username: str, max_age_days: int, timeout: int = 15) -> tuple[bool, str]:
    channel_url = f"https://t.me/{username}"
    try:
        page_html = fetch_channel_html(channel_url, timeout=timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return False, f"unavailable: {error}"

    unavailable_reason = channel_unavailable_reason(page_html)
    if unavailable_reason:
        return False, unavailable_reason

    now = datetime.now(timezone.utc)
    posts = parse_posts(page_html, channel_url)
    post_dates = [parse_post_datetime(post["date"]) for post in posts]
    latest_post = max((date for date in post_dates if date is not None), default=None)
    if latest_post is None or now - latest_post > timedelta(days=max_age_days):
        return False, "no_recent_posts"

    for query in DISCOVERY_RELEVANCE_KEYWORDS:
        try:
            query_url = f"https://t.me/s/{username}?q={urllib.parse.quote(query)}"
            search_html = fetch_url(query_url, timeout=timeout)
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
        for post in parse_posts(search_html, channel_url):
            parsed = parse_post_datetime(post["date"])
            if not parsed or now - parsed > timedelta(days=max_age_days):
                continue
            if discovery_relevance(post["text"]):
                return True, f"accepted: {post['link']}"
    return False, "no_verified_video_vacancy"


def append_discovery_history(rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    fieldnames = ["discovered_at", "username", "url", "status", "reason"]
    exists = DISCOVERED_SOURCES_FILE.exists()
    with DISCOVERED_SOURCES_FILE.open("a", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def add_sources_to_sources_json(usernames: list[str]) -> None:
    if not usernames:
        return
    data: dict = {"groups": {}}
    if SOURCES_FILE.exists():
        data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    groups = data.setdefault("groups", {})
    target = groups.setdefault("auto_discovered_sources", [])
    seen = {str(item).lower() for values in groups.values() if isinstance(values, list) for item in values}
    for username in usernames:
        lowered = username.lower()
        if lowered not in seen:
            target.append(username)
            seen.add(lowered)
    SOURCES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_source_discovery(config: dict, now: datetime) -> dict[str, int | str]:
    if not source_discovery_due(config, now):
        return {"status": "skipped", "candidates": 0, "checked": 0, "added": 0}

    discovery = config.get("source_discovery", {})
    max_new = int(discovery.get("max_new_sources_per_run", 20))
    max_age_days = int(discovery.get("max_post_age_days", 120))
    max_candidates = int(discovery.get("max_candidates_per_run", 160))
    workers = max(1, int(discovery.get("workers", 16)))
    found_at = now.isoformat(timespec="seconds")
    candidates = discover_candidate_usernames()
    existing = load_existing_source_names()
    history_rows: list[dict[str, str]] = []
    accepted: list[str] = []
    checked = 0

    pending = [name for name in sorted(candidates) if name not in existing][:max_candidates]
    for name in sorted(candidates):
        if name in existing:
            history_rows.append(
                {
                    "discovered_at": found_at,
                    "username": name,
                    "url": f"https://t.me/{name}",
                    "status": "duplicate",
                    "reason": "already_in_sources",
                }
            )

    def verify(name: str) -> tuple[str, bool, str]:
        ok, reason = verify_discovered_source(name, max_age_days=max_age_days)
        return name, ok, reason

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(verify, name) for name in pending]
        for future in as_completed(futures):
            try:
                username, ok, reason = future.result()
            except Exception as error:
                username, ok, reason = "unknown", False, f"verify_error: {error}"
            checked += 1
            status = "accepted" if ok and len(accepted) < max_new else "rejected"
            if ok and len(accepted) < max_new:
                accepted.append(username)
            elif ok:
                reason = "accepted_limit_reached"
            history_rows.append(
                {
                    "discovered_at": found_at,
                    "username": username,
                    "url": f"https://t.me/{username}",
                    "status": status,
                    "reason": reason,
                }
            )
    add_sources_to_sources_json(accepted)
    if not history_rows:
        history_rows.append(
            {
                "discovered_at": found_at,
                "username": "",
                "url": "",
                "status": "done",
                "reason": "no_candidates",
            }
        )
    append_discovery_history(history_rows)
    return {"status": "done", "candidates": len(candidates), "checked": checked, "added": len(accepted)}


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


def send_telegram_message(
    token: str,
    chat_id: str,
    text: str,
    reply_markup: dict | None = None,
    parse_mode: str | None = None,
) -> None:
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(api_url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def escape_telegram_html(value: str) -> str:
    return html.escape(value or "", quote=False)


def format_lead_telegram_message(lead: Lead, brief: VacancyBrief, employer_stats: dict | None = None) -> str:
    bullets = [item for item in brief.bullets[:3] if not is_missing_summary_value(item)]

    title = escape_telegram_html(brief.title)
    budget = escape_telegram_html(brief.budget or "не указан")
    relevance = escape_telegram_html(lead.status)
    link = escape_telegram_html(lead.link)
    reply = escape_telegram_html(lead.reply_draft)

    employer_block = ""
    if employer_stats:
        employer_block = (
            "\U0001f464 \u0420\u0430\u0431\u043e\u0442\u043e\u0434\u0430\u0442\u0435\u043b\u044c:\n"
            f"\u2022 \u0432\u0441\u0442\u0440\u0435\u0447\u0430\u043b\u0441\u044f: {int(employer_stats.get('count', 0) or 0)} \u0440\u0430\u0437\n"
            f"\u2022 \u043f\u0435\u0440\u0432\u0430\u044f \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u044f: {escape_telegram_html(str(employer_stats.get('first_seen', '')))}\n"
            f"\u2022 \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u044f\u044f \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u044f: {escape_telegram_html(str(employer_stats.get('last_seen', '')))}\n\n"
        )

    prefix = (
        "\U0001f525 \u0412\u0430\u043a\u0430\u043d\u0441\u0438\u044f\n\n"
        f"\U0001f3ac {title}\n\n"
        "\U0001f4cc \u041a\u0440\u0430\u0442\u043a\u043e:\n"
        + "".join(f"\u2022 {escape_telegram_html(item)}\n" for item in bullets)
        + "\n"
        f"\U0001f4b0 \u0411\u044e\u0434\u0436\u0435\u0442: {budget}\n\n"
        f"\u2b50 \u0420\u0435\u043b\u0435\u0432\u0430\u043d\u0442\u043d\u043e\u0441\u0442\u044c: {relevance}\n\n"
        f"{employer_block}"
        f"\U0001f517 \u0421\u0441\u044b\u043b\u043a\u0430: {link}\n\n"
        "\U0001f4ac \u0413\u043e\u0442\u043e\u0432\u044b\u0439 \u043e\u0442\u043a\u043b\u0438\u043a:\n"
    )
    max_reply_length = max(0, 3900 - len(prefix) - len("<pre></pre>"))
    if len(reply) > max_reply_length:
        reply = reply[:max_reply_length].rstrip()
    return f"{prefix}<pre>{reply}</pre>"


def send_telegram_document(token: str, chat_id: str, file_path: Path, caption: str) -> None:
    boundary = "----ClientWorkBoundary" + hashlib.sha1(str(file_path).encode("utf-8")).hexdigest()
    body = bytearray()
    fields = {"chat_id": chat_id, "caption": caption}
    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8"))
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        f'Content-Disposition: form-data; name="document"; filename="{file_path.name}"\r\n'
        'Content-Type: text/csv\r\n\r\n'.encode("utf-8")
    )
    body.extend(file_path.read_bytes())
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=bytes(body),
        headers={"User-Agent": USER_AGENT, "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        response.read()


def answer_callback_query(token: str, callback_query_id: str, text: str) -> None:
    data = urllib.parse.urlencode({"callback_query_id": callback_query_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/answerCallbackQuery",
        data=data,
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def edit_message_reply_markup(token: str, chat_id: str, message_id: int, reply_markup: dict) -> None:
    data = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "message_id": str(message_id),
            "reply_markup": json.dumps(reply_markup, ensure_ascii=False),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/editMessageReplyMarkup",
        data=data,
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


def status_buttons(lead_id: str, contact_status: str = "not_contacted", client_answered: str = "no") -> dict:
    replied_label = "\u2705 \u042f \u043e\u0442\u043a\u043b\u0438\u043a\u043d\u0443\u043b\u0441\u044f"
    answered_label = "\U0001f4ac \u041a\u043b\u0438\u0435\u043d\u0442 \u043e\u0442\u0432\u0435\u0442\u0438\u043b"
    rejected_label = "\u274c \u041d\u0435 \u043f\u043e\u0434\u0445\u043e\u0434\u0438\u0442"
    if contact_status == "contacted":
        replied_label += " \u2713"
    if client_answered == "yes":
        answered_label += " \u2713"
    if contact_status == "skipped":
        rejected_label += " \u2713"
    return {
        "inline_keyboard": [
            [
                {"text": replied_label, "callback_data": f"lead:replied:{lead_id}"},
                {"text": answered_label, "callback_data": f"lead:client_answered:{lead_id}"},
            ],
            [{"text": rejected_label, "callback_data": f"lead:rejected:{lead_id}"}],
        ]
    }


def telegram_credentials(config: dict) -> tuple[str, str]:
    notify = config["notify"]
    token = notify.get("telegram_bot_token", "").strip()
    chat_id = notify.get("telegram_chat_id", "").strip()
    if token and not chat_id:
        chat_id = discover_telegram_chat_id(config)
    return token, chat_id


def daily_table_date(config: dict, now: datetime) -> datetime:
    table_config = config.get("daily_table", {})
    offset = int(table_config.get("timezone_offset_hours", 4))
    return now.astimezone(timezone(timedelta(hours=offset)))


def daily_table_stats(rows: list[dict[str, str]], day: str) -> dict[str, int]:
    today_rows = [row for row in rows if row.get("found_at", "")[:10] == day]
    return {
        "found": len(today_rows),
        "contacted": sum(1 for row in today_rows if row.get("contact_status") == "contacted"),
        "not_contacted": sum(1 for row in today_rows if row.get("contact_status") == "not_contacted"),
        "skipped": sum(1 for row in today_rows if row.get("contact_status") == "skipped"),
        "answered": sum(1 for row in today_rows if row.get("client_answered") == "yes"),
    }


def build_daily_table(config: dict, now: datetime) -> tuple[Path, str]:
    local_now = daily_table_date(config, now)
    day = local_now.date().isoformat()
    rows = [row for row in read_lead_rows() if row.get("found_at", "")[:10] == day]
    file_path = ROOT / f"daily_leads_{day}.csv"
    with file_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=LEADS_FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    stats = daily_table_stats(rows, day)
    caption = (
        f"\U0001f4ca \u041e\u0442\u0447\u0435\u0442 \u0437\u0430 {local_now.strftime('%d.%m.%Y')}\n\n"
        f"\u041d\u0430\u0439\u0434\u0435\u043d\u043e: {stats['found']}\n"
        f"\u041e\u0442\u0432\u0435\u0447\u0435\u043d\u043e: {stats['contacted']}\n"
        f"\u041d\u0435 \u043e\u0442\u0432\u0435\u0442\u0438\u043b\u0438: {stats['not_contacted']}\n"
        f"\u041d\u0435 \u043f\u043e\u0434\u0445\u043e\u0434\u044f\u0442: {stats['skipped']}"
    )
    return file_path, caption


def send_daily_table(config: dict, now: datetime) -> None:
    token, chat_id = telegram_credentials(config)
    if not token or not chat_id:
        return
    file_path, caption = build_daily_table(config, now)
    send_telegram_document(token, chat_id, file_path, caption)


def maybe_send_daily_table(config: dict, state: dict, now: datetime) -> None:
    table_config = config.get("daily_table", {})
    if not table_config.get("enabled", True):
        return
    local_now = daily_table_date(config, now)
    day = local_now.date().isoformat()
    if state.get("last_daily_table_sent_date") == day:
        return
    send_time = str(table_config.get("send_time", "20:00"))
    current_time = local_now.strftime("%H:%M")
    if current_time >= send_time:
        try:
            send_daily_table(config, now)
            state["last_daily_table_sent_date"] = day
        except (RuntimeError, urllib.error.URLError, TimeoutError, OSError):
            return


def get_telegram_updates(config: dict, state: dict) -> list[dict]:
    token, _ = telegram_credentials(config)
    if not token:
        return []
    offset = int(state.get("telegram_update_offset", 0) or 0)
    params = {"timeout": 0}
    if offset:
        params["offset"] = offset
    url = f"https://api.telegram.org/bot{token}/getUpdates?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload.get("result", [])


def process_telegram_updates(config: dict, state: dict, now: datetime) -> None:
    token, chat_id = telegram_credentials(config)
    if not token or not chat_id:
        return
    try:
        updates = get_telegram_updates(config, state)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return
    max_update_id = int(state.get("telegram_update_offset", 0) or 0) - 1
    for update in updates:
        update_id = int(update.get("update_id", 0))
        max_update_id = max(max_update_id, update_id)
        callback = update.get("callback_query") or {}
        message = update.get("message") or {}
        if callback:
            data = str(callback.get("data", ""))
            match = re.fullmatch(
                r"lead:(replied|client_answered|rejected|contacted|not_contacted|skipped):([a-f0-9]{12})",
                data,
            )
            if match:
                action, lead_id = match.groups()
                legacy_actions = {
                    "contacted": "replied",
                    "not_contacted": "not_contacted",
                    "skipped": "rejected",
                }
                action = legacy_actions.get(action, action)
                if action == "not_contacted":
                    changed, row = update_lead_status(lead_id, "not_contacted", now)
                else:
                    changed, row = update_lead_action(lead_id, action, now)
                text = {
                    "replied": "\u2705 \u041e\u0442\u043c\u0435\u0447\u0435\u043d\u043e: \u0432\u044b \u043e\u0442\u043a\u043b\u0438\u043a\u043d\u0443\u043b\u0438\u0441\u044c",
                    "client_answered": "\U0001f4ac \u041e\u0442\u043c\u0435\u0447\u0435\u043d\u043e: \u043a\u043b\u0438\u0435\u043d\u0442 \u043e\u0442\u0432\u0435\u0442\u0438\u043b",
                    "rejected": "\u274c \u041e\u0442\u043c\u0435\u0447\u0435\u043d\u043e: \u043d\u0435 \u043f\u043e\u0434\u0445\u043e\u0434\u0438\u0442",
                    "not_contacted": "\u274c \u041e\u0442\u043c\u0435\u0447\u0435\u043d\u043e: \u043f\u043e\u043a\u0430 \u043d\u0435 \u043e\u0442\u0432\u0435\u0442\u0438\u043b\u0438",
                }[action]
                try:
                    answer_callback_query(token, callback.get("id", ""), text)
                    callback_message = callback.get("message") or {}
                    message_id = int(callback_message.get("message_id", 0) or 0)
                    if row and message_id:
                        edit_message_reply_markup(
                            token,
                            chat_id,
                            message_id,
                            status_buttons(
                                lead_id,
                                row.get("contact_status", "not_contacted"),
                                row.get("client_answered", "no"),
                            ),
                        )
                except (urllib.error.URLError, TimeoutError, OSError):
                    pass
            continue
        if str(message.get("text", "")).strip().lower() == "/table":
            try:
                send_daily_table(config, now)
            except (RuntimeError, urllib.error.URLError, TimeoutError, OSError):
                pass
    if max_update_id >= 0:
        state["telegram_update_offset"] = max_update_id + 1


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


def send_telegram_notification(
    config: dict,
    leads: list[Lead],
    errors: list[str],
    employer_stats_by_lead_id: dict[str, dict] | None = None,
) -> None:
    token, chat_id = telegram_credentials(config)
    if not token or not chat_id:
        raise RuntimeError("Telegram notifications are not configured: missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    if not leads and not config["notify"].get("send_when_no_new_leads", False):
        return

    if not leads:
        send_telegram_message(token, chat_id, "\u041d\u043e\u0432\u044b\u0445 \u043b\u0438\u0434\u043e\u0432 \u043f\u043e Reels-\u043c\u043e\u043d\u0442\u0430\u0436\u0443 \u043d\u0435\u0442.")
        return

    send_telegram_message(token, chat_id, f"\u041d\u043e\u0432\u044b\u0445 \u0432\u0430\u043a\u0430\u043d\u0441\u0438\u0439: {len(leads)}")

    for lead in leads[:12]:
        if not lead.lead_id:
            lead.lead_id = make_lead_id(lead.link)
        brief = make_vacancy_brief(config, lead.message, lead.budget)
        text = format_lead_telegram_message(
            lead,
            brief,
            (employer_stats_by_lead_id or {}).get(lead.lead_id),
        )
        chunks = split_telegram_text(text)
        for index, chunk in enumerate(chunks):
            reply_markup = status_buttons(lead.lead_id) if index == len(chunks) - 1 else None
            send_telegram_message(token, chat_id, chunk, reply_markup=reply_markup, parse_mode="HTML")

    if len(leads) > 12:
        send_telegram_message(token, chat_id, f"\u0415\u0449\u0435 {len(leads) - 12} \u043b\u0438\u0434\u043e\u0432 \u043e\u0441\u0442\u0430\u043b\u0438\u0441\u044c \u0432 \u0442\u0430\u0431\u043b\u0438\u0446\u0435.")
    if errors:
        send_telegram_message(token, chat_id, "\u041e\u0448\u0438\u0431\u043a\u0438 \u043f\u043e \u043a\u0430\u043d\u0430\u043b\u0430\u043c:\n" + "\n".join(errors[:5]))


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
    runtime_state = load_runtime_state()
    existing_links = read_existing_links()
    found_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    now_utc = datetime.now(timezone.utc)
    max_post_age_hours = int(config.get("max_post_age_hours", 24))
    max_leads_per_run = int(config.get("max_leads_per_run", 12))
    new_leads: list[Lead] = []
    current_run_fingerprints: list[str] = []
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

    process_telegram_updates(config, runtime_state, now_utc)
    discovery_report = run_source_discovery(config, now_utc)

    all_channel_sources = load_channel_sources()
    channel_sources = select_sources_for_run(all_channel_sources, runtime_state, config)
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

        posts = sorted(parse_posts(page_html, channel), key=post_message_id)
        posts_found += len(posts)
        for post in posts:
            if is_duplicate_post(post, runtime_state, existing_links):
                existing_posts += 1
                update_last_message_id(runtime_state, post)
                continue
            if not is_fresh_post(post["date"], max_post_age_hours, now_utc):
                filtered_posts += 1
                filter_reasons["other"] += 1
                update_last_message_id(runtime_state, post)
                continue
            relevant, reason = is_relevant(post["text"], config)
            if not relevant:
                filtered_posts += 1
                filter_reasons[filter_reason_key(reason)] += 1
                update_last_message_id(runtime_state, post)
                continue
            fingerprint = text_fingerprint(post["text"])
            if is_near_duplicate_text(fingerprint, current_run_fingerprints):
                existing_posts += 1
                update_last_message_id(runtime_state, post)
                continue
            if len(new_leads) >= max_leads_per_run:
                filtered_posts += 1
                filter_reasons["other"] += 1
                update_last_message_id(runtime_state, post)
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
                lead_id=make_lead_id(post["link"]),
            )
            new_leads.append(lead)
            current_run_fingerprints.append(fingerprint)
            existing_links.add(post["link"])

    write_unavailable_channels(unavailable_channels)
    new_leads.sort(key=lambda lead: lead.score, reverse=True)

    if new_leads:
        try:
            employer_history, employer_stats = build_employer_history_update(new_leads)
            send_telegram_notification(config, new_leads, [], employer_stats)
            notification_status = "уведомление отправлено"
            for lead in new_leads:
                mark_lead_sent(runtime_state, lead)
            append_leads(new_leads)
            save_employer_history(employer_history)
        except (RuntimeError, urllib.error.URLError) as error:
            notification_status = f"уведомление не отправлено: {error}"

    runtime_state["last_checked_at"] = now_utc.isoformat(timespec="seconds")
    maybe_send_daily_table(config, runtime_state, now_utc)
    save_runtime_state(runtime_state)

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

    if config["notify"].get("send_run_report", False):
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
        f"Источников в запуске: {len(channel_sources)} из {len(all_channel_sources)}",
        f"Discovery: {discovery_report['status']}, candidates={discovery_report['candidates']}, checked={discovery_report['checked']}, added={discovery_report['added']}",
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
