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
    assert result.model == "gemini-3.8-flash"


@pytest.mark.asyncio
async def test_gemini_retries_transient_failure() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": "high demand"})
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "Дайджест після повтору"}]}}]},
        )

    service = GeminiDigestService(
        api_key="test-key",
        retry_delays=(0,),
        transport=httpx.MockTransport(handler),
    )
    result = await service.generate(
        "Example",
        [sample_post()],
        datetime(2026, 9, 10, tzinfo=UTC),
        datetime(2026, 9, 16, tzinfo=UTC),
    )

    assert result.ok is True
    assert result.content == "Дайджест після повтору"
    assert calls == 2


@pytest.mark.asyncio
async def test_gemini_uses_fallback_model_after_transient_failures() -> None:
    requested_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_models.append(request.url.path)
        if "gemini-primary" in request.url.path:
            return httpx.Response(503, json={"error": "high demand"})
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "Резервний дайджест"}]}}]},
        )

    service = GeminiDigestService(
        api_key="test-key",
        model="gemini-primary",
        fallback_model="gemini-fallback",
        retry_delays=(0,),
        transport=httpx.MockTransport(handler),
    )
    result = await service.generate(
        "Example",
        [sample_post()],
        datetime(2026, 9, 10, tzinfo=UTC),
        datetime(2026, 9, 16, tzinfo=UTC),
    )

    assert result.ok is True
    assert result.content == "Резервний дайджест"
    assert result.model == "gemini-fallback"
    assert requested_models == [
        "/v1beta/models/gemini-primary:generateContent",
        "/v1beta/models/gemini-primary:generateContent",
        "/v1beta/models/gemini-fallback:generateContent",
    ]


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
