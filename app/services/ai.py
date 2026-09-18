from __future__ import annotations

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


class GeminiDigestService:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.gemini_api_key
        self.model = model or settings.gemini_model
        self.transport = transport

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

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": self._prompt(channel_title, posts, period_start, period_end)}]}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 700},
        }
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=35.0) as client:
                response = await client.post(
                    url,
                    headers={"x-goog-api-key": self.api_key},
                    json=payload,
                )
            if response.status_code == 429:
                return DigestResult(
                    False,
                    error="AI досяг ліміту запитів. Спробуйте пізніше; інші дані доступні.",
                )
            if not response.is_success:
                provider_message = response.text.replace(self.api_key, "[redacted]")[:1000]
                logger.warning(
                    "Gemini request failed with status %s: %s",
                    response.status_code,
                    provider_message,
                )
            response.raise_for_status()
            data = response.json()
            content = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            if not content:
                raise ValueError("empty AI response")
            return DigestResult(True, content=content)
        except (httpx.HTTPError, KeyError, IndexError, ValueError):
            return DigestResult(
                False,
                error="AI не відповів, але дашборд продовжує працювати. Спробуйте пізніше.",
            )
