from pathlib import Path

import httpx
import pytest

from app.services.telegram import (
    ChannelUnavailable,
    fetch_channel,
    normalize_username,
    parse_channel_html,
    parse_compact_number,
)

FIXTURE = Path(__file__).parent / "fixtures" / "public_channel.html"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@Durov", "durov"),
        ("@Gram", "gram"),
        (" https://t.me/s/durov?before=10 ", "durov"),
        ("t.me/durov", "durov"),
    ],
)
def test_normalize_username(raw: str, expected: str) -> None:
    assert normalize_username(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["bad!", "abc", "https://example.com/nope", "@with/slash", "durov/123", "t.me/durov/123"],
)
def test_normalize_username_rejects_invalid_input(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_username(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("757", 757), ("14.3K", 14_300), ("10.7M", 10_700_000), ("1,2K", 1_200), (None, None)],
)
def test_parse_compact_number(raw: str | None, expected: int | None) -> None:
    assert parse_compact_number(raw) == expected


def test_parser_extracts_channel_posts_and_reactions() -> None:
    parsed = parse_channel_html(FIXTURE.read_text(encoding="utf-8"), "durov")

    assert parsed.title == "Pavel Durov"
    assert parsed.subscriber_count == 10_700_000
    assert len(parsed.posts) == 2
    first = parsed.posts[0]
    assert first.telegram_post_id == 528
    assert first.text == "Hello\n\nworld"
    assert first.views == 18_800_000
    assert first.reactions_total == 146_845
    assert first.reactions["paid_star"] == 14_300
    assert first.reactions["emoji_id:5265077361648368841"] == 132_000
    assert first.reactions["unicode:❤"] == 418
    assert first.reactions["unicode:🔥"] == 127


def test_parser_selects_actual_text_not_reply_quote() -> None:
    parsed = parse_channel_html(FIXTURE.read_text(encoding="utf-8"), "durov")
    assert parsed.posts[1].text == "Actual post text"
    assert parsed.posts[1].reactions_total is None


def test_parser_fails_loudly_when_layout_is_not_telegram_preview() -> None:
    with pytest.raises(ChannelUnavailable):
        parse_channel_html("<html><h1>Generic Telegram page</h1></html>", "durov")


def test_parser_fails_loudly_when_candidate_message_is_malformed() -> None:
    html = """
    <div class="tgme_channel_info">
      <div class="tgme_channel_info_header_title"><span>Pavel Durov</span></div>
    </div>
    <div class="tgme_widget_message" data-post="durov/528">
      <div class="tgme_widget_message_text js-message_text">Post without a timestamp</div>
    </div>
    """
    with pytest.raises(ChannelUnavailable, match="не вдалося розібрати структуру"):
        parse_channel_html(html, "durov")


def telegram_page(post_ids: list[int], previous_page: str | None) -> str:
    previous = f'<link rel="prev" href="{previous_page}">' if previous_page else ""
    posts = "".join(
        f"""
        <div class="tgme_widget_message" data-post="durov/{post_id}">
          <div class="tgme_widget_message_text js-message_text">Post {post_id}</div>
          <time datetime="2026-09-18T10:00:00+00:00"></time>
        </div>
        """
        for post_id in post_ids
    )
    return f"""
    <html><head>{previous}</head><body>
      <div class="tgme_channel_info">
        <div class="tgme_channel_info_header_title"><span>Pavel Durov</span></div>
      </div>
      {posts}
    </body></html>
    """


@pytest.mark.asyncio
async def test_first_scan_saves_cursor_for_gradual_history_backfill() -> None:
    page = telegram_page([300, 299], "/s/durov?before=299")
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, text=page, headers={"content-type": "text/html"})
    )

    parsed = await fetch_channel("durov", transport=transport)

    assert parsed.history_complete is False
    assert parsed.resume_cursor == "/s/durov?before=299"
    assert {post.telegram_post_id for post in parsed.posts} == {300, 299}


@pytest.mark.asyncio
async def test_existing_single_preview_window_is_enrolled_in_backfill() -> None:
    page = telegram_page([301, 300], "/s/durov?before=300")
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, text=page, headers={"content-type": "text/html"})
    )

    parsed = await fetch_channel(
        "durov",
        known_post_ids={300, 299},
        transport=transport,
    )

    assert parsed.history_complete is False
    assert parsed.resume_cursor == "/s/durov?before=300"
    assert {post.telegram_post_id for post in parsed.posts} == {301, 300}


@pytest.mark.asyncio
async def test_pagination_returns_resumable_cursor_instead_of_silent_gap() -> None:
    pages = {
        "/s/durov": telegram_page([300, 299], "/s/durov?before=299"),
        "/s/durov?before=299": telegram_page([298, 297], "/s/durov?before=297"),
        "/s/durov?before=297": telegram_page([296, 100], "/s/durov?before=100"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        return httpx.Response(200, text=pages[key], headers={"content-type": "text/html"})

    transport = httpx.MockTransport(handler)
    first = await fetch_channel(
        "durov",
        known_post_ids={100},
        max_pages=2,
        transport=transport,
    )
    assert first.history_complete is False
    assert first.resume_cursor == "/s/durov?before=297"
    assert {post.telegram_post_id for post in first.posts} == {300, 299, 298, 297}

    resumed = await fetch_channel(
        "durov",
        known_post_ids={100},
        max_pages=2,
        start_page=first.resume_cursor,
        transport=transport,
    )
    assert resumed.history_complete is True
    assert resumed.resume_cursor is None
    assert {post.telegram_post_id for post in resumed.posts} == {296, 100}


@pytest.mark.asyncio
async def test_backfill_cursor_cannot_change_origin_or_path() -> None:
    with pytest.raises(ValueError, match="invalid Telegram backfill cursor"):
        await fetch_channel(
            "durov",
            known_post_ids={100},
            start_page="https://evil.example/steal",
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        )


@pytest.mark.asyncio
async def test_fetch_rejects_private_or_missing_channel_redirect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://t.me/s/durov")
        return httpx.Response(302, headers={"location": "https://t.me/durov"})

    with pytest.raises(ChannelUnavailable, match="приватний"):
        await fetch_channel("durov", transport=httpx.MockTransport(handler))
