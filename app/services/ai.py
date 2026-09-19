from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

import httpx

from app.config import get_settings
from app.models import Post

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DigestResult:
    ok: bool
    content: str = ""
    error: str | None = None
    model: str | None = None


class GeminiDigestService:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
        retry_delays: tuple[float, ...] = (1.0,),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.gemini_api_key
        self.model = model or settings.gemini_model
        self.fallback_model = (
            fallback_model if fallback_model is not None else settings.gemini_fallback_model
        )
        self.retry_delays = retry_delays
        self.transport = transport

    @property
    def models(self) -> tuple[str, ...]:
        candidates = (self.model, self.fallback_model)
        return tuple(
            candidate
            for index, candidate in enumerate(candidates)
            if candidate and candidate not in candidates[:index]
        )

    def _prompt(
        self,
        channel_title: str,
        posts: list[Post],
        period_start: datetime,
        period_end: datetime,
    ) -> str:
        excerpts = []
        for post in posts[:40]:
            clean_text = " ".join(post.text.split())[:800] or f"[{post.media_kind or 'post without text'}]"
            excerpts.append(f"- {post.published_at.date().isoformat()}: {clean_text}")
        joined = "\n".join(excerpts)
        return (
            "Ти аналітик контенту. Створи корисний українськомовний дайджест Telegram-каналу. "
            "Не вигадуй фактів поза постами. Дай: 1) 2–3 речення підсумку; "
            "2) 3–5 головних тем маркерами; 3) коротке спостереження про зміну фокусу або тональності.\n\n"
            f"Канал: {channel_title}\nПеріод: {period_start.date()} — {period_end.date()}\n"
            f"Пости:\n{joined}"
        )

    async def generate(
        self,
        channel_title: str,
        posts: list[Post],
        period_start: datetime,
        period_end: datetime,
    ) -> DigestResult:
        if not self.api_key:
            return DigestResult(False, error="AI тимчасово недоступний: ключ провайдера не налаштовано.")
        if not posts:
            return DigestResult(False, error="За вибраний період немає постів для дайджесту.")

        payload = {
            "contents": [{"parts": [{"text": self._prompt(channel_title, posts, period_start, period_end)}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 700},
        }
        transient_statuses = {408, 429, 500, 502, 503, 504}
        last_status: int | None = None

        async with httpx.AsyncClient(transport=self.transport, timeout=35.0) as client:
            for model in self.models:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
                for attempt in range(len(self.retry_delays) + 1):
                    try:
                        response = await client.post(
                            url,
                            headers={"x-goog-api-key": self.api_key},
                            json=payload,
                        )
                        last_status = response.status_code
                        if not response.is_success:
                            provider_message = response.text.replace(self.api_key, "[redacted]")[:1000]
                            logger.warning(
                                "Gemini model %s failed with status %s (attempt %s): %s",
                                model,
                                response.status_code,
                                attempt + 1,
                                provider_message,
                            )
                            if response.status_code not in transient_statuses:
                                return DigestResult(
                                    False,
                                    error=(
                                        "AI не відповів, але дашборд продовжує працювати. "
                                        "Спробуйте пізніше."
                                    ),
                                )
                        else:
                            data = response.json()
                            parts = data["candidates"][0]["content"]["parts"]
                            content = "\n".join(part.get("text", "") for part in parts).strip()
                            if not content:
                                raise ValueError("empty AI response")
                            return DigestResult(True, content=content, model=model)
                    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
                        logger.warning(
                            "Gemini model %s failed on attempt %s: %s",
                            model,
                            attempt + 1,
                            type(exc).__name__,
                        )

                    if attempt < len(self.retry_delays):
                        await asyncio.sleep(self.retry_delays[attempt])

        if last_status == 429:
            return DigestResult(
                False,
                error="AI досяг ліміту запитів. Спробуйте пізніше; інші дані доступні.",
            )
        return DigestResult(
            False,
            error="AI не відповів, але дашборд продовжує працювати. Спробуйте пізніше.",
        )
