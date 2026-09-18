from datetime import UTC, datetime

import httpx
import pytest

from app.models import Post
from app.services.ai import GeminiDigestService


def sample_post() -> Post:
    return Post(
        channel_id=1,
        telegram_post_id=1,
        url="https://t.me/example/1",
        text="A product update",
        published_at=datetime(2026, 9, 15, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_gemini_success_is_mocked_without_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "test-key"
        assert "test-key" not in str(request.url)
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "Короткий дайджест"}]}}]},
        )

    service = GeminiDigestService(api_key="test-key", transport=httpx.MockTransport(handler))
    result = await service.generate(
        "Example",
        [sample_post()],
        datetime(2026, 9, 10, tzinfo=UTC),
        datetime(2026, 9, 16, tzinfo=UTC),
    )
    assert result.ok is True
    assert result.content == "Короткий дайджест"


@pytest.mark.asyncio
async def test_gemini_quota_failure_degrades_gracefully() -> None:
    service = GeminiDigestService(
        api_key="test-key",
        transport=httpx.MockTransport(lambda _: httpx.Response(429, json={"error": "quota"})),
    )
    result = await service.generate(
        "Example",
        [sample_post()],
        datetime(2026, 9, 10, tzinfo=UTC),
        datetime(2026, 9, 16, tzinfo=UTC),
    )
    assert result.ok is False
    assert "ліміту" in (result.error or "")


@pytest.mark.asyncio
async def test_gemini_http_error_never_exposes_secret() -> None:
    secret = "very-sensitive-test-key"
    service = GeminiDigestService(
        api_key=secret,
        transport=httpx.MockTransport(lambda _: httpx.Response(403, json={"error": "forbidden"})),
    )
    result = await service.generate(
        "Example",
        [sample_post()],
        datetime(2026, 9, 10, tzinfo=UTC),
        datetime(2026, 9, 16, tzinfo=UTC),
    )
    assert result.ok is False
    assert secret not in (result.error or "")
