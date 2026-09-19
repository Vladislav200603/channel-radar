from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup, Tag

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")
POST_KEY_RE = re.compile(r"^(?P<username>[A-Za-z0-9_]+)/(?P<post_id>\d+)$")


class ChannelUnavailable(ValueError):
    """Raised when a public Telegram preview cannot be read."""


@dataclass(slots=True)
class ParsedPost:
    telegram_post_id: int
    url: str
    published_at: datetime
    text: str = ""
    author: str | None = None
    media_kind: str | None = None
    views: int | None = None
    forwards: int | None = None
    reactions: dict[str, int] = field(default_factory=dict)
    reactions_available: bool = False

    @property
    def reactions_total(self) -> int | None:
        return sum(self.reactions.values()) if self.reactions_available else None


@dataclass(slots=True)
class ParsedChannel:
    username: str
    title: str
    description: str | None
    subscriber_count: int | None
    posts: list[ParsedPost]
    previous_page: str | None = None
    history_complete: bool = True
    resume_cursor: str | None = None


def normalize_username(raw: str) -> str:
    value = raw.strip()
    if value.startswith("@"):
        value = value[1:]
    elif re.match(r"^(?:https?://)?(?:www\.)?t\.me(?:/|$)", value, flags=re.IGNORECASE):
        parsed = urlsplit(value if "://" in value else f"https://{value}")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError(
                "Вкажіть коректний публічний username Telegram (4–32 символи)."
            ) from exc
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or (parsed.hostname or "").lower() not in {"t.me", "www.t.me"}
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
        ):
            raise ValueError("Вкажіть коректний публічний username Telegram (4–32 символи).")
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[0].lower() == "s":
            parts = parts[1:]
        if len(parts) != 1:
            raise ValueError("Вкажіть коректний публічний username Telegram (4–32 символи).")
        value = parts[0]
    if not USERNAME_RE.fullmatch(value):
        raise ValueError("Вкажіть коректний публічний username Telegram (4–32 символи).")
    return value.lower()


def parse_compact_number(raw: str | None) -> int | None:
    if not raw:
        return None
    value = raw.strip().upper().replace("\u00a0", "").replace(" ", "")
    value = value.replace(",", ".")
    match = re.search(r"([\d.]+)\s*([KMBКММЛН]*)", value)
    if not match:
        return None
    try:
        number = float(match.group(1))
    except ValueError:
        return None
    suffix = match.group(2)
    if suffix in {"K", "К"}:
        number *= 1_000
    elif suffix in {"M", "М", "МЛН"}:
        number *= 1_000_000
    elif suffix == "B":
        number *= 1_000_000_000
    return int(number)


def _text(node: Tag | None, separator: str = " ") -> str | None:
    if node is None:
        return None
    value = node.get_text(separator, strip=True)
    return value or None


def _media_kind(message: Tag) -> str | None:
    selectors = (
        (".tgme_widget_message_video_player", "video"),
        (".tgme_widget_message_photo_wrap", "photo"),
        (".tgme_widget_message_voice_player", "voice"),
        (".tgme_widget_message_audio_player", "audio"),
        (".tgme_widget_message_poll", "poll"),
        (".tgme_widget_message_document", "document"),
    )
    for selector, kind in selectors:
        if message.select_one(selector):
            return kind
    return None


def _message_text(message: Tag) -> str:
    node = message.select_one(".tgme_widget_message_text.js-message_text")
    if node is None:
        return ""
    for line_break in node.select("br"):
        line_break.replace_with("\n")
    value = node.get_text("", strip=False).replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n[ \t]+", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _parse_reactions(message: Tag) -> dict[str, int]:
    reactions: dict[str, int] = {}
    for node in message.select(".tgme_widget_message_reactions .tgme_reaction"):
        telegram_emoji = node.select_one("tg-emoji[emoji-id]")
        unicode_emoji = node.select_one("i.emoji b")
        if "tgme_reaction_paid" in node.get("class", []):
            key = "paid_star"
        elif telegram_emoji is not None:
            key = f"emoji_id:{telegram_emoji.get('emoji-id')}"
        elif unicode_emoji is not None and _text(unicode_emoji, separator=""):
            key = f"unicode:{_text(unicode_emoji, separator='')}"
        else:
            key = "reaction"
        count = parse_compact_number(_text(node, separator=""))
        if count is not None:
            reactions[key] = reactions.get(key, 0) + count
    return reactions


def parse_channel_html(html: str, requested_username: str) -> ParsedChannel:
    soup = BeautifulSoup(html, "html.parser")
    canonical_username = normalize_username(requested_username)
    if soup.select_one(".tgme_channel_info") is None:
        raise ChannelUnavailable("структура Telegram web-preview змінилася або канал недоступний")
    title = _text(soup.select_one(".tgme_channel_info_header_title span, .tgme_channel_info_header_title"))
    description = _text(soup.select_one(".tgme_channel_info_description"), separator="\n")

    subscriber_count: int | None = None
    for counter in soup.select(".tgme_channel_info_counter"):
        counter_type = (_text(counter.select_one(".counter_type")) or "").lower()
        if any(word in counter_type for word in ("subscriber", "member", "підпис", "подпис")):
            subscriber_count = parse_compact_number(_text(counter.select_one(".counter_value")))
            break

    posts: list[ParsedPost] = []
    candidate_messages = soup.select(".tgme_widget_message[data-post]")
    malformed_message = False
    for message in candidate_messages:
        key = str(message.get("data-post", ""))
        match = POST_KEY_RE.fullmatch(key)
        if not match:
            malformed_message = True
            continue
        time_node = message.select_one("time[datetime]")
        if time_node is None:
            malformed_message = True
            continue
        try:
            published_at = datetime.fromisoformat(str(time_node["datetime"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            malformed_message = True
            continue
        if published_at.tzinfo is None or published_at.utcoffset() is None:
            malformed_message = True
            continue
        post_id = int(match.group("post_id"))
        post_username = match.group("username")
        text = _message_text(message)
        views = parse_compact_number(_text(message.select_one(".tgme_widget_message_views")))
        forwards = parse_compact_number(_text(message.select_one(".tgme_widget_message_forwards")))
        posts.append(
            ParsedPost(
                telegram_post_id=post_id,
                url=f"https://t.me/{post_username}/{post_id}",
                published_at=published_at,
                text=text,
                author=_text(message.select_one(".tgme_widget_message_author")),
                media_kind=_media_kind(message),
                views=views,
                forwards=forwards,
                reactions=_parse_reactions(message),
                reactions_available=message.select_one(".tgme_widget_message_reactions") is not None,
            )
        )

    if malformed_message:
        raise ChannelUnavailable("не вдалося розібрати структуру одного або кількох дописів Telegram")

    if not title and not posts:
        page_title = _text(soup.select_one(".tgme_page_title"))
        detail = page_title or "канал не існує, приватний або web-preview недоступний"
        raise ChannelUnavailable(detail)

    previous_link = soup.select_one('link[rel="prev"]')
    previous_page = str(previous_link.get("href")) if previous_link and previous_link.get("href") else None

    return ParsedChannel(
        username=canonical_username,
        title=title or f"@{canonical_username}",
        description=description,
        subscriber_count=subscriber_count,
        posts=posts,
        previous_page=previous_page,
    )


async def _fetch_page(client: httpx.AsyncClient, url: str) -> httpx.Response:
    response: httpx.Response | None = None
    for attempt in range(3):
        try:
            response = await client.get(url)
        except httpx.TransportError:
            if attempt == 2:
                raise
            await asyncio.sleep(0.4 * (2**attempt))
            continue
        if response.status_code == 429 or response.status_code >= 500:
            if attempt == 2:
                return response
            retry_after = response.headers.get("retry-after")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 0.4 * (2**attempt)
            await asyncio.sleep(min(delay, 3.0))
            continue
        return response
    assert response is not None
    return response


def _validate_page_path(path: str, username: str) -> bool:
    return re.fullmatch(rf"/s/{re.escape(username)}\?before=\d+", path) is not None


def _parse_response(response: httpx.Response, username: str) -> ParsedChannel:
    if response.status_code == 404 or response.is_redirect:
        raise ChannelUnavailable("канал не знайдено, він приватний або web-preview недоступний")
    response.raise_for_status()
    if "text/html" not in response.headers.get("content-type", ""):
        raise ChannelUnavailable("Telegram повернув неочікуваний формат відповіді")
    if len(response.content) > 2_500_000:
        raise ChannelUnavailable("відповідь Telegram перевищила безпечний ліміт")
    return parse_channel_html(response.text, username)


async def fetch_channel(
    username: str,
    *,
    timeout_seconds: float = 15.0,
    known_post_ids: set[int] | None = None,
    max_pages: int = 5,
    start_page: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ParsedChannel:
    normalized = normalize_username(username)
    if start_page is not None and not _validate_page_path(start_page, normalized):
        raise ValueError("invalid Telegram backfill cursor")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.8",
    }
    timeout = httpx.Timeout(timeout_seconds, connect=min(5.0, timeout_seconds))
    async with httpx.AsyncClient(
        headers=headers,
        follow_redirects=False,
        timeout=timeout,
        transport=transport,
    ) as client:
        first_path = start_page or f"/s/{normalized}"
        response = await _fetch_page(client, f"https://t.me{first_path}")
        parsed = _parse_response(response, normalized)

        latest_post_ids = {post.telegram_post_id for post in parsed.posts}
        overlaps_database = bool(known_post_ids and latest_post_ids.intersection(known_post_ids))

        # Keep the first scan fast, but remember Telegram's previous-page cursor so
        # later scheduled runs can grow the local history beyond the ~20-post preview.
        # The second branch also bootstraps channels created before this behaviour was
        # introduced, while their database still contains only one preview window.
        initial_window_only = not known_post_ids or (
            start_page is None
            and overlaps_database
            and len(known_post_ids) <= len(parsed.posts)
        )
        if start_page is None and initial_window_only and parsed.previous_page is not None:
            if not _validate_page_path(parsed.previous_page, normalized):
                raise ChannelUnavailable("Telegram повернув небезпечний cursor пагінації")
            parsed.history_complete = False
            parsed.resume_cursor = parsed.previous_page

        # Normally the latest page already overlaps our database. If more than ~20
        # messages appeared between runs, follow Telegram's own cursor until overlap.
        if not known_post_ids or overlaps_database:
            return parsed
        all_posts = list(parsed.posts)
        previous_page = parsed.previous_page
        overlap_found = False
        for _ in range(max_pages - 1):
            if not previous_page:
                break
            if not _validate_page_path(previous_page, normalized):
                raise ChannelUnavailable("Telegram повернув небезпечний cursor пагінації")
            response = await _fetch_page(client, f"https://t.me{previous_page}")
            older = _parse_response(response, normalized)
            all_posts.extend(older.posts)
            if any(post.telegram_post_id in known_post_ids for post in older.posts):
                overlap_found = True
                previous_page = older.previous_page
                break
            previous_page = older.previous_page
        parsed.posts = all_posts
        if not overlap_found and previous_page is not None:
            if not _validate_page_path(previous_page, normalized):
                raise ChannelUnavailable("Telegram повернув небезпечний cursor пагінації")
            parsed.history_complete = False
            parsed.resume_cursor = previous_page
        return parsed
