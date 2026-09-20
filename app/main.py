from __future__ import annotations

import asyncio
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import Base, engine, get_db
from app.models import Channel, ChannelDigest, ChannelMetricSnapshot, Post, PostMetricSnapshot
from app.services.ai import GeminiDigestService, is_usable_digest
from app.services.analytics import annotate_view_anomalies
from app.services.channel_management import deletion_token, valid_deletion_token
from app.services.collector import collect_channel_by_id, get_collection_lock
from app.services.scheduler import collect_due_channels, run_collection_scheduler
from app.services.telegram import normalize_username

BASE_DIR = Path(__file__).resolve().parent
settings = get_settings()
_digest_locks: dict[int, asyncio.Lock] = {}
_channel_creation_lock = threading.Lock()
_deletion_key = (settings.cron_secret or secrets.token_urlsafe(32)).encode()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Alembic is used in production. create_all keeps a fresh local checkout one-command friendly.
    Base.metadata.create_all(bind=engine)
    stop_event = asyncio.Event()
    scheduler = (
        asyncio.create_task(run_collection_scheduler(stop_event)) if settings.scheduler_enabled else None
    )
    try:
        yield
    finally:
        stop_event.set()
        if scheduler:
            scheduler.cancel()
            await asyncio.gather(scheduler, return_exceptions=True)


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


def channel_display_status(channel: Channel) -> str:
    collected = channel.last_collected_at
    if collected and collected.tzinfo is None:
        collected = collected.replace(tzinfo=UTC)
    if channel.status in {"healthy", "degraded"} and collected:
        if collected < datetime.now(UTC) - timedelta(hours=3):
            return "stale"
    return channel.status


def latest_usable_digest(db: Session, channel_id: int) -> ChannelDigest | None:
    candidates = db.scalars(
        select(ChannelDigest)
        .where(ChannelDigest.channel_id == channel_id)
        .order_by(ChannelDigest.created_at.desc())
        .limit(20)
    )
    return next((item for item in candidates if is_usable_digest(item.content)), None)


def dashboard_context(db: Session, *, error: str | None = None, removed: str | None = None) -> dict:
    rows = db.execute(
        select(Channel, func.count(Post.id))
        .outerjoin(Post, Post.channel_id == Channel.id)
        .group_by(Channel.id)
        .order_by(Channel.created_at.desc())
    ).all()
    channels = []
    for channel, post_count in rows:
        channels.append(
            {"channel": channel, "post_count": post_count, "display_status": channel_display_status(channel)}
        )
    return {"channels": channels, "error": error, "removed": removed, "channel_limit": 10}


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request, db: Session = Depends(get_db), error: str | None = None, removed: str | None = None
):
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context=dashboard_context(db, error=error, removed=removed),
    )


@app.post("/channels")
def add_channel(channel: str = Form(...), db: Session = Depends(get_db)):
    # The public demo has one web worker. Count+insert must be one serialized action.
    with _channel_creation_lock:
        return _add_channel(channel, db)


def _add_channel(channel: str, db: Session):
    try:
        username = normalize_username(channel)
    except ValueError as exc:
        return RedirectResponse(url=f"/?error={quote(str(exc))}", status_code=303)

    existing = db.scalar(select(Channel).where(Channel.username == username))
    if existing:
        return RedirectResponse(url=f"/channels/{existing.username}", status_code=303)
    channel_count = db.scalar(select(func.count()).select_from(Channel)) or 0
    if channel_count >= 10:
        message = "Досягнуто ліміту в 10 каналів. Видаліть непотрібний канал, щоб звільнити місце."
        return RedirectResponse(url=f"/?error={quote(message)}", status_code=303)

    created = Channel(username=username, status="pending")
    db.add(created)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        created = db.scalar(select(Channel).where(Channel.username == username))
    return RedirectResponse(url=f"/channels/{created.username}?start=1", status_code=303)


@app.get("/channels/{username}/delete", response_class=HTMLResponse)
def confirm_channel_deletion(username: str, request: Request, db: Session = Depends(get_db)):
    channel = get_channel_or_404(db, username)
    post_count = db.scalar(select(func.count(Post.id)).where(Post.channel_id == channel.id)) or 0
    token = deletion_token(channel, _deletion_key)
    response = templates.TemplateResponse(
        request=request,
        name="delete_channel.html",
        context={"channel": channel, "post_count": post_count, "confirmation_token": token},
    )
    response.set_cookie(
        "delete_confirmation",
        token,
        max_age=600,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/channels/{username}/delete")
async def delete_channel(
    username: str, request: Request, confirmation_token: str = Form(...), db: Session = Depends(get_db)
):
    channel = get_channel_or_404(db, username)
    channel_id = channel.id
    cookie = request.cookies.get("delete_confirmation")
    if not valid_deletion_token(channel, confirmation_token, cookie, _deletion_key):
        raise HTTPException(
            status_code=403, detail="Підтвердження застаріло. Відкрийте сторінку видалення знову."
        )
    db.rollback()
    # Serialise deletion with in-flight writes. Lock order is consistent for all deletes.
    async with _digest_locks.setdefault(channel_id, asyncio.Lock()), get_collection_lock(channel_id):
        channel = get_channel_or_404(db, username)
        if channel.id != channel_id or not valid_deletion_token(
            channel, confirmation_token, cookie, _deletion_key
        ):
            raise HTTPException(status_code=409, detail="Канал змінився. Повторіть підтвердження.")
        post_ids = select(Post.id).where(Post.channel_id == channel_id)
        db.execute(delete(PostMetricSnapshot).where(PostMetricSnapshot.post_id.in_(post_ids)))
        db.execute(delete(ChannelDigest).where(ChannelDigest.channel_id == channel_id))
        db.execute(delete(ChannelMetricSnapshot).where(ChannelMetricSnapshot.channel_id == channel_id))
        db.execute(delete(Post).where(Post.channel_id == channel_id))
        db.execute(delete(Channel).where(Channel.id == channel_id))
        db.commit()
    response = RedirectResponse(url=f"/?removed={quote(username)}", status_code=303)
    response.delete_cookie("delete_confirmation")
    return response


@app.get("/channels/{username}", response_class=HTMLResponse)
def channel_detail(
    request: Request,
    username: str,
    days: int = Query(default=30),
    page: int = Query(default=1, ge=1),
    start: bool = False,
    ai_error: str | None = None,
    db: Session = Depends(get_db),
):
    channel = get_channel_or_404(db, username)
    days = days if days in {7, 30, 90, 365} else 30
    period_start = datetime.now(UTC) - timedelta(days=days)
    total_posts = (
        db.scalar(
            select(func.count(Post.id)).where(
                Post.channel_id == channel.id, Post.published_at >= period_start
            )
        )
        or 0
    )
    page_size = 50
    total_pages = max(1, (total_posts + page_size - 1) // page_size)
    page = min(page, total_pages)

    posts = list(
        db.scalars(
            select(Post)
            .where(Post.channel_id == channel.id, Post.published_at >= period_start)
            .order_by(Post.published_at.desc(), Post.telegram_post_id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    post_rows: list[dict] = []
    post_ids = [post.id for post in posts]
    ranked_metrics = (
        select(
            PostMetricSnapshot.id,
            func.row_number()
            .over(
                partition_by=PostMetricSnapshot.post_id,
                order_by=PostMetricSnapshot.observed_bucket.desc(),
            )
            .label("position"),
        )
        .where(PostMetricSnapshot.post_id.in_(post_ids))
        .subquery()
    )
    metrics = (
        {
            metric.post_id: metric
            for metric in db.scalars(
                select(PostMetricSnapshot)
                .join(ranked_metrics, PostMetricSnapshot.id == ranked_metrics.c.id)
                .where(ranked_metrics.c.position == 1)
            )
        }
        if post_ids
        else {}
    )
    for post in posts:
        metric = metrics.get(post.id)
        post_rows.append(
            {
                "post": post,
                "views": metric.views if metric else None,
                "reactions_total": metric.reactions_total if metric else None,
            }
        )
    period_metrics = (
        select(
            PostMetricSnapshot.views,
            func.row_number()
            .over(
                partition_by=PostMetricSnapshot.post_id,
                order_by=PostMetricSnapshot.observed_bucket.desc(),
            )
            .label("position"),
        )
        .join(Post, Post.id == PostMetricSnapshot.post_id)
        .where(Post.channel_id == channel.id, Post.published_at >= period_start)
        .subquery()
    )
    baseline_values = (
        list(db.scalars(select(period_metrics.c.views).where(period_metrics.c.position == 1)))
        if post_rows
        else []
    )
    annotate_view_anomalies(post_rows, baseline_values=baseline_values)

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
    digest = latest_usable_digest(db, channel.id)
    chart_data = {
        "subscribers": {
            "labels": [format_datetime(item.observed_bucket) for item in snapshots],
            "values": [item.subscriber_count for item in snapshots],
        },
        "views": {
            "labels": [format_datetime(item.observed_bucket) for item in snapshots],
            "values": [item.fresh_average_views for item in snapshots],
        },
    }
    return templates.TemplateResponse(
        request=request,
        name="channel.html",
        context={
            "channel": channel,
            "posts": post_rows,
            "days": days,
            "page": page,
            "total_pages": total_pages,
            "total_posts": total_posts,
            "display_status": channel_display_status(channel),
            "chart_data": chart_data,
            "digest": digest,
            "start_collect": start and channel.status == "pending",
            "ai_error": ai_error,
        },
    )


@app.post("/channels/{username}/collect")
async def collect_channel(username: str, db: Session = Depends(get_db)):
    channel = get_channel_or_404(db, username)
    channel_id = channel.id
    db.rollback()
    result = await collect_channel_by_id(channel_id)
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


@app.get("/channels/{username}/status")
def collection_status(username: str, db: Session = Depends(get_db)):
    channel = get_channel_or_404(db, username)
    return {"status": channel.status, "display_status": channel_display_status(channel)}


@app.post("/channels/{username}/refresh")
async def refresh_channel(
    username: str,
    days: int = Form(default=30),
    db: Session = Depends(get_db),
):
    channel = get_channel_or_404(db, username)
    channel_id, canonical_username = channel.id, channel.username
    db.rollback()
    await collect_channel_by_id(channel_id)
    days = days if days in {7, 30, 90, 365} else 30
    return RedirectResponse(url=f"/channels/{canonical_username}?days={days}", status_code=303)


@app.post("/channels/{username}/digest")
async def generate_digest(username: str, db: Session = Depends(get_db)):
    channel = get_channel_or_404(db, username)
    channel_id = channel.id
    db.rollback()
    lock = _digest_locks.setdefault(channel_id, asyncio.Lock())
    async with lock:
        channel = get_channel_or_404(db, username)
        if channel.id != channel_id:
            raise HTTPException(status_code=409, detail="Канал змінився. Повторіть запит.")
        period_end = datetime.now(UTC)
        period_start = period_end - timedelta(days=7)
        latest_digest = latest_usable_digest(db, channel.id)
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
        title, canonical_username = channel.title or f"@{channel.username}", channel.username
        # Release the DB connection while the external AI request is in progress.
        for post in posts:
            db.expunge(post)
        db.rollback()
        result = await GeminiDigestService().generate(title, posts, period_start, period_end)
        if result.ok and is_usable_digest(result.content):
            db.add(
                ChannelDigest(
                    channel_id=channel_id,
                    period_start=period_start,
                    period_end=period_end,
                    content=result.content,
                    provider="google",
                    model=result.model or settings.gemini_model,
                )
            )
            db.commit()
            return RedirectResponse(url=f"/channels/{canonical_username}?days=7", status_code=303)
        return RedirectResponse(
            url=f"/channels/{canonical_username}?days=7&ai_error={quote(result.error or 'AI error')}",
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
async def scheduled_collection(
    authorization: str | None = Header(default=None), db: Session = Depends(get_db)
):
    _authorized_cron(authorization)
    try:
        async with asyncio.timeout(180):
            results = await collect_due_channels()
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504, detail="Collection deadline exceeded; saved data retained"
        ) from exc
    sources = list(db.scalars(select(Channel)))
    return {
        "collected": len(results),
        "healthy": sum(item.status == "healthy" for item in results),
        "backfilling": sum(item.status == "degraded" for item in results),
        "errors": [item.username for item in results if item.status not in {"healthy", "degraded"}],
        "source_healthy": sum(channel.status in {"healthy", "degraded"} for channel in sources),
        "source_errors": [channel.username for channel in sources if channel.status == "error"],
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/readyz")
def readyz(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ready"}
