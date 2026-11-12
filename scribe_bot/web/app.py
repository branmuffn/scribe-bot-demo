"""Web app (M4): server-rendered Jinja2 pages with HTMX, installable as a PWA. Phone and desktop equal (SPEC §3.8).

Reached only over Tailscale (no login; Tailscale is the boundary). Listens on 127.0.0.1 unless SCRIBE_BOT_HOST is set.
Form posts from other sites are refused (Origin check), so a web page can't drive the app from Brian's browser.
"""
from __future__ import annotations

import hashlib
import sqlite3
import subprocess

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from scribe_bot.core import db, health, machine, progress, settings
from scribe_bot.core.secrets import KeychainStore

WEB = Path(__file__).parent
WORKER_STALE_SECONDS = 180  # the worker beats every ~30 s
AREAS = ("all", "work", "personal")
templates = Jinja2Templates(directory=WEB / "templates")
from scribe_bot.ai.prompts import MEETING_TYPES  # noqa: E402  (review cards and the confirm step list them)
templates.env.globals["meeting_types"] = MEETING_TYPES
# Fingerprint of the page code: base.html adds ?v=<this> to the CSS/JS links, so phones fetch the new files right after a
# deploy instead of reusing a cached copy (the installed app kept running old code otherwise).
templates.env.globals["asset_v"] = hashlib.sha256(b"".join(
    (WEB / "static" / f).read_bytes() for f in ("app.css", "app.js", "htmx.min.js"))).hexdigest()[:10]
templates.env.filters["local_time"] = progress.local_time
templates.env.filters["duration"] = progress.duration_label


def local_datetime(ts: str | None) -> str:
    """'Thu Oct 16, 9:29 am' in the M14's local time zone (stored times are UTC ISO)."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%a %b %-d, %-I:%M %p").replace("AM", "am").replace("PM", "pm")


templates.env.filters["local_datetime"] = local_datetime


def local_date(ts: str | None) -> str:
    """'Oct 16' (or 'Oct 16, 2025' outside this year) in local time."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts[:10]
    dt = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone()
    return dt.strftime("%b %-d" if dt.year == datetime.now().year else "%b %-d, %Y")


templates.env.filters["local_date"] = local_date


def day_label(d: str | None) -> str:
    """A calendar date (due dates, note dates; no time zone to shift): 'today', 'tomorrow', 'yesterday', 'Oct 18',
    or 'Oct 18, 2025' outside this year. Anything unreadable is shown as it is."""
    if not d:
        return ""
    try:
        day = datetime.strptime(d[:10], "%Y-%m-%d").date()
    except ValueError:
        return d
    today = datetime.now().date()
    named = {0: "today", 1: "tomorrow", -1: "yesterday"}.get((day - today).days)
    return named or (f"{day:%b} {day.day}" if day.year == today.year else f"{day:%b} {day.day}, {day.year}")


templates.env.filters["day"] = day_label


def when_back(ts: str | None) -> str:
    """When a snoozed item returns: 'at 6:00 pm' (today), 'tomorrow 8:00 am', 'Mon Oct 26, 8:00 am'."""
    if not ts:
        return ""
    dt = datetime.fromisoformat(ts)
    dt = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone()
    clock = dt.strftime("%-I:%M %p").lower()
    days = (dt.date() - datetime.now().astimezone().date()).days
    return f"at {clock}" if days == 0 else f"tomorrow {clock}" if days == 1 else f"{dt:%a %b} {dt.day}, {clock}"


templates.env.filters["when_back"] = when_back


def worker_status(cfg: settings.Settings) -> dict:
    try:
        conn = db.connect(cfg.db_path)
        try:
            row = conn.execute("SELECT value FROM setting WHERE key = 'worker_heartbeat'").fetchone()
        finally:
            conn.close()
    except Exception:
        row = None
    if row is None:
        return {"alive": False, "last_seen_s": None}
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds()
    return {"alive": age <= WORKER_STALE_SECONDS, "last_seen_s": int(age)}


def active_progress(cfg: settings.Settings) -> list[dict]:
    try:
        conn = db.connect(cfg.db_path)
        try:
            return progress.active_jobs(conn)
        finally:
            conn.close()
    except Exception:
        return []


def pipeline_paused(cfg: settings.Settings) -> bool:
    try:
        conn = db.connect(cfg.db_path)
        try:
            row = conn.execute("SELECT value FROM setting WHERE key = 'pipeline_paused'").fetchone()
        finally:
            conn.close()
    except Exception:
        return False
    return bool(row) and row[0] == "1"


def machine_stored(cfg: settings.Settings) -> dict:
    """Latest M14 sample, 24-hour history, reasons needing attention, last recording time. DB reads only (fast)."""
    try:
        conn = db.connect(cfg.db_path)
        try:
            return {"latest": machine.latest(conn), "history": machine.history(conn),
                    "attention": machine.needs_attention(conn),
                    "last_recording": conn.execute("SELECT max(received_at) FROM recording").fetchone()[0]}
        finally:
            conn.close()
    except Exception:
        return {"latest": None, "history": [], "attention": [], "last_recording": None}


HISTORY_RANGES = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}


def machine_charts(cfg: settings.Settings, hours: int) -> list[dict]:
    """History charts for the Status page (DB read only; samples are taken every 5 minutes by the worker)."""
    try:
        conn = db.connect(cfg.db_path)
        try:
            rows = machine.history(conn, hours=hours)
        finally:
            conn.close()
    except Exception:
        return []
    return [c for spec in machine.CHARTS if (c := machine.chart(rows, *spec, hours=hours))]


def area_of(request: Request) -> str:
    """Work/Personal toggle: ?area= wins (and is remembered in a cookie by the middleware), else the cookie."""
    a = request.query_params.get("area") or request.cookies.get("area") or "all"
    return a if a in AREAS else "all"


SHARE_TARGET = "/capture/share"   # Android's share sheet posts here itself: no page of ours sent it


def own_post(request: Request) -> bool:
    """A post one of Scribe Bot's own pages sent (the check against other websites, DECISIONS 2026-10-19). Browsers say
    where a request came from: Sec-Fetch-Site, or the Origin header on older ones. An Origin of "null" is refused (any
    web page can send one: a no-referrer page or a sandboxed frame), except on the share target."""
    site = request.headers.get("sec-fetch-site")
    if site:
        return site in ("same-origin", "none")   # none = the browser itself (the share sheet), not a web page
    origin = request.headers.get("origin")
    if not origin:
        return True   # not a browser (curl, the tests): browsers send Origin with every post
    if origin == "null":
        return request.url.path == SHARE_TARGET
    return urlparse(origin).netloc == request.headers.get("host")


class DropEmptyQuery:
    """A blank filter (Anyone, an emptied date) means "no filter": drop `person=` style empty values before routing,
    so a number or date parameter never rejects it (Brian 2026-10-22: picking Anyone again gave an error)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("query_string"):
            from urllib.parse import parse_qsl, urlencode
            pairs = parse_qsl(scope["query_string"].decode("latin-1"), keep_blank_values=True)
            kept = [(k, v) for k, v in pairs if v.strip()]
            if len(kept) != len(pairs):
                scope = {**scope, "query_string": urlencode(kept).encode("latin-1")}
        return await self.app(scope, receive, send)


class TextGZip:
    """Compress pages, CSS and JS (HTML shrinks ~8x; Tailscale serve doesn't compress). Audio is left alone so
    seeking (Range requests) keeps working."""

    def __init__(self, app):
        from starlette.middleware.gzip import GZipMiddleware
        self.app, self.gz = app, GZipMiddleware(app, minimum_size=1000)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope["path"].startswith("/audio"):
            return await self.gz(scope, receive, send)
        return await self.app(scope, receive, send)


def create_app(cfg: settings.Settings | None = None, run=None, secrets_store: KeychainStore | None = None) -> FastAPI:
    cfg = cfg or settings.load()
    app = FastAPI(title="Scribe Bot", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.cfg = cfg
    app.state.secrets = secrets_store or KeychainStore()
    app.mount("/static", StaticFiles(directory=WEB / "static"), name="static")
    app.add_middleware(TextGZip)
    app.add_middleware(DropEmptyQuery)

    @app.middleware("http")
    async def guard_and_area(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS") and not own_post(request):
            return PlainTextResponse("Cross-site request refused", status_code=403)
        response = await call_next(request)
        response.headers["X-Asset-V"] = templates.env.globals["asset_v"]   # app.js: a newer page code means a full load
        a = request.query_params.get("area")
        if a in AREAS:
            response.set_cookie("area", a, max_age=365 * 86400, samesite="lax")
        return response

    def status() -> dict:
        h = health.check(cfg, run) if run else health.check(cfg)
        h["worker"] = worker_status(cfg)
        return h

    @app.get("/health")
    def get_health() -> JSONResponse:
        h = status()
        stored = machine_stored(cfg)   # from the 5-minute sample: no slow commands here (deploy/health watch call this)
        h["machine"] = {"sample": stored["latest"], "needs_attention": stored["attention"]}
        return JSONResponse(h, status_code=200 if h["ok"] else 503)

    def machine_context(stored: dict, live: dict | None) -> dict:
        latest = stored["latest"] or {}
        data = {**latest, **live, "wifi_dbm": latest.get("wifi_dbm")} if live is not None else latest
        card = machine.card(data)
        return {"mc": card, "tiles": machine.tiles(card), "mc_live": live is not None, "mc_ts": latest.get("ts"),
                "mc_live_at": datetime.now().astimezone().strftime("%-I:%M:%S %p").lower(),
                "mc_attention": stored["attention"], "mc_last_recording": stored["last_recording"],
                "uptime_label": machine.uptime_label, "words": machine.LEVEL_WORDS,
                "ranges": {k: machine.ranges_text(k) for k in machine.RANGES}, "ranges_full": list(machine.RANGES.items())}

    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request):
        # The page shows the last 5-minute sample at once; the machine card then fetches live readings (~1 s).
        stored = machine_stored(cfg)
        rng = request.query_params.get("range", "24h")
        rng = rng if rng in HISTORY_RANGES else "24h"
        ctx = {"h": status(), "now": db.now(), "paused": pipeline_paused(cfg), "jobs": active_progress(cfg),
               "charts": machine_charts(cfg, HISTORY_RANGES[rng]), "range": rng, **machine_context(stored, None)}
        try:   # the same page frame as everywhere (Today count, ⋯ red dot, Undo bar), unless the database itself is broken
            conn = db.connect(cfg.db_path)
            try:
                return routes.render(request, "status.html", "status", conn, **ctx)
            finally:
                conn.close()
        except sqlite3.Error:
            return templates.TemplateResponse(request, "status.html", {"nav": "status", "area": area_of(request), **ctx})

    @app.get("/status/machine", response_class=HTMLResponse)
    def status_machine(request: Request):
        """Live M14 readings for the Status page card, fetched once when the page opens (no polling). Wi-Fi is cached."""
        live = machine.fast(run or subprocess.run)
        return templates.TemplateResponse(request, "_machine.html", machine_context(machine_stored(cfg), live))

    # PWA files served from the root so the service worker controls the whole app.
    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(WEB / "static" / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js")
    def service_worker():
        return FileResponse(WEB / "static" / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    from scribe_bot.web import routes
    app.include_router(routes.router)
    return app
