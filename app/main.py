from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import Base, engine, get_db
from app.models import Channel, ChannelDigest, ChannelMetricSnapshot, Post, PostMetricSnapshot
from app.services.ai import GeminiDigestService
from app.services.analytics import annotate_view_anomalies
from app.services.collector import collect_all_channels, collect_channel_by_id
from app.services.telegram import normalize_username

BASE_DIR = Path(__file__).resolve().parent
settings = get_settings()
_digest_locks: dict[int, asyncio.Lock] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Alembic is used in production. create_all keeps a fresh local checkout one-command friendly.
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def format_number(value: int | None) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return f"{value:,}".replace(",", " ")


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%d.%m.%Y · %H:%M UTC")


templates.env.filters["compact_number"] = format_number
templates.env.filters["datetime"] = format_datetime


def get_channel_or_404(db: Session, username: str) -> Channel:
    try:
        normalized = normalize_username(username)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Channel not found") from exc
    channel = db.scalar(select(Channel).where(Channel.username == normalized))
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found")
    return channel


def dashboard_context(db: Session, *, error: str | None = None) -> dict:
    rows = db.execute(
        select(Channel, func.count(Post.id))
        .outerjoin(Post, Post.channel_id == Channel.id)
        .group_by(Channel.id)
        .order_by(Channel.created_at.desc())
    ).all()
    channels = []
    stale_before = datetime.now(UTC) - timedelta(hours=3)
    for channel, post_count in rows:
        display_status = channel.status
        collected = channel.last_collected_at
        if collected and collected.tzinfo is None:
            collected = collected.replace(tzinfo=UTC)
        if display_status == "healthy" and collected and collected < stale_before:
            display_status = "stale"
        channels.append({"channel": channel, "post_count": post_count, "display_status": display_status})
    return {"channels": channels, "error": error, "channel_limit": 10}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), error: str | None = None):
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=dashboard_context(db, error=error),
    )


@app.post("/channels")
def add_channel(channel: str = Form(...), db: Session = Depends(get_db)):
    try:
        username = normalize_username(channel)
    except ValueError as exc:
        return RedirectResponse(url=f"/?error={quote(str(exc))}", status_code=303)

    existing = db.scalar(select(Channel).where(Channel.username == username))
    if existing:
        return RedirectResponse(url=f"/channels/{existing.username}", status_code=303)
    channel_count = db.scalar(select(func.count()).select_from(Channel)) or 0
    if channel_count >= 10:
        message = "Досягнуто демонстраційного ліміту в 10 каналів."
        return RedirectResponse(url=f"/?error={quote(message)}", status_code=303)

    created = Channel(username=username, status="pending")
    db.add(created)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        created = db.scalar(select(Channel).where(Channel.username == username))
    return RedirectResponse(url=f"/channels/{created.username}?start=1", status_code=303)


@app.get("/channels/{username}", response_class=HTMLResponse)
def channel_detail(
    request: Request,
    username: str,
    days: int = Query(default=30),
    start: bool = False,
    ai_error: str | None = None,
    db: Session = Depends(get_db),
):
    channel = get_channel_or_404(db, username)
    days = days if days in {7, 30, 90, 365} else 30
    period_start = datetime.now(UTC) - timedelta(days=days)

    posts = list(
        db.scalars(
            select(Post)
            .where(Post.channel_id == channel.id, Post.published_at >= period_start)
            .order_by(Post.published_at.desc())
            .limit(100)
        )
    )
    post_rows: list[dict] = []
    for post in posts:
        metric = db.scalar(
            select(PostMetricSnapshot)
            .where(PostMetricSnapshot.post_id == post.id)
            .order_by(PostMetricSnapshot.observed_bucket.desc())
            .limit(1)
        )
        post_rows.append(
            {
                "post": post,
                "views": metric.views if metric else None,
                "reactions_total": metric.reactions_total if metric else None,
            }
        )
    annotate_view_anomalies(post_rows)

    snapshots = list(
        db.scalars(
            select(ChannelMetricSnapshot)
            .where(
                ChannelMetricSnapshot.channel_id == channel.id,
                ChannelMetricSnapshot.observed_bucket >= period_start,
            )
            .order_by(ChannelMetricSnapshot.observed_bucket)
        )
    )
    view_points = db.execute(
        select(PostMetricSnapshot.observed_bucket, func.avg(PostMetricSnapshot.views))
        .join(Post, Post.id == PostMetricSnapshot.post_id)
        .where(
            Post.channel_id == channel.id,
            PostMetricSnapshot.observed_bucket >= period_start,
            PostMetricSnapshot.views.is_not(None),
        )
        .group_by(PostMetricSnapshot.observed_bucket)
        .order_by(PostMetricSnapshot.observed_bucket)
    ).all()
    digest = db.scalar(
        select(ChannelDigest)
        .where(ChannelDigest.channel_id == channel.id)
        .order_by(ChannelDigest.created_at.desc())
        .limit(1)
    )
    chart_data = {
        "subscribers": {
            "labels": [format_datetime(item.observed_bucket) for item in snapshots],
            "values": [item.subscriber_count for item in snapshots],
        },
        "views": {
            "labels": [format_datetime(item[0]) for item in view_points],
            "values": [int(item[1] or 0) for item in view_points],
        },
    }
    return templates.TemplateResponse(
        request=request,
        name="channel.html",
        context={
            "channel": channel,
            "posts": post_rows,
            "days": days,
            "chart_data": chart_data,
            "digest": digest,
            "start_collect": start and channel.status == "pending",
            "ai_error": ai_error,
        },
    )


@app.post("/channels/{username}/collect")
async def collect_channel(username: str, db: Session = Depends(get_db)):
    channel = get_channel_or_404(db, username)
    result = await collect_channel_by_id(channel.id)
    status_code = 200 if result.status in {"healthy", "degraded"} else 422
    return JSONResponse(
        {
            "status": result.status,
            "new_posts": result.new_posts,
            "updated_posts": result.updated_posts,
            "error": result.error,
        },
        status_code=status_code,
    )


@app.post("/channels/{username}/refresh")
def refresh_channel(
    username: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    channel = get_channel_or_404(db, username)
    background_tasks.add_task(collect_channel_by_id, channel.id)
    return RedirectResponse(url=f"/channels/{channel.username}", status_code=303)


@app.post("/channels/{username}/digest")
async def generate_digest(username: str, db: Session = Depends(get_db)):
    channel = get_channel_or_404(db, username)
    lock = _digest_locks.setdefault(channel.id, asyncio.Lock())
    async with lock:
        period_end = datetime.now(UTC)
        period_start = period_end - timedelta(days=7)
        latest_digest = db.scalar(
            select(ChannelDigest)
            .where(ChannelDigest.channel_id == channel.id)
            .order_by(ChannelDigest.created_at.desc())
            .limit(1)
        )
        if latest_digest:
            created_at = latest_digest.created_at
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            if period_end - created_at < timedelta(minutes=15):
                return RedirectResponse(url=f"/channels/{channel.username}?days=7", status_code=303)
        posts = list(
            db.scalars(
                select(Post)
                .where(Post.channel_id == channel.id, Post.published_at >= period_start)
                .order_by(Post.published_at.desc())
                .limit(40)
            )
        )
        result = await GeminiDigestService().generate(
            channel.title or f"@{channel.username}", posts, period_start, period_end
        )
        if result.ok:
            db.add(
                ChannelDigest(
                    channel_id=channel.id,
                    period_start=period_start,
                    period_end=period_end,
                    content=result.content,
                    provider="google",
                    model=settings.gemini_model,
                )
            )
            db.commit()
            return RedirectResponse(url=f"/channels/{channel.username}?days=7", status_code=303)
        return RedirectResponse(
            url=f"/channels/{channel.username}?days=7&ai_error={quote(result.error or 'AI error')}",
            status_code=303,
        )


@app.get("/channels/{username}/posts/{telegram_post_id}", response_class=HTMLResponse)
def post_detail(
    request: Request,
    username: str,
    telegram_post_id: int,
    db: Session = Depends(get_db),
):
    channel = get_channel_or_404(db, username)
    post = db.scalar(
        select(Post).where(
            Post.channel_id == channel.id,
            Post.telegram_post_id == telegram_post_id,
        )
    )
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    snapshots = list(
        db.scalars(
            select(PostMetricSnapshot)
            .where(PostMetricSnapshot.post_id == post.id)
            .order_by(PostMetricSnapshot.observed_bucket)
        )
    )
    chart_data = {
        "labels": [format_datetime(item.observed_bucket) for item in snapshots],
        "views": [item.views for item in snapshots],
        "reactions": [item.reactions_total for item in snapshots],
    }
    return templates.TemplateResponse(
        request=request,
        name="post.html",
        context={"channel": channel, "post": post, "snapshots": snapshots, "chart_data": chart_data},
    )


def _authorized_cron(authorization: str | None) -> None:
    if not settings.cron_secret:
        raise HTTPException(status_code=503, detail="CRON_SECRET is not configured")
    expected = f"Bearer {settings.cron_secret}"
    if not authorization or not secrets.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.post("/internal/collect-all")
async def scheduled_collection(authorization: str | None = Header(default=None)):
    _authorized_cron(authorization)
    results = await collect_all_channels()
    return {
        "collected": len(results),
        "healthy": sum(item.status == "healthy" for item in results),
        "errors": [item.username for item in results if item.status != "healthy"],
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ready"}
