"""Web app (M4): server-rendered Jinja2 pages with HTMX, installable as a PWA. Phone and desktop equal (SPEC §3.8).

Reached only over Tailscale (no login; Tailscale is the boundary). Listens on 127.0.0.1 unless SCRIBE_BOT_HOST is set.
Form posts from other sites are refused (Origin check), so a web page can't drive the app from Brian's browser.
"""
from __future__ import annotations

import hashlib
import os
import json
import re
import sqlite3
import subprocess
import time

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
AREAS = ("all", "work", "personal", "school", "other")   # Brian, 2026-11-20: + School, Other
from scribe_bot.web.export import AREAS as AREA_LABELS  # noqa: E402  (one list of area names for pages and exports)
templates = Jinja2Templates(directory=WEB / "templates")
templates.env.globals["area_labels"] = AREA_LABELS
from scribe_bot.ai.prompts import MEETING_TYPES  # noqa: E402  (review cards and the confirm step list them)
templates.env.globals["meeting_types"] = MEETING_TYPES


def public_demo() -> bool:
    """The public demo on the web (scribe-bot-demo's index.py sets SCRIBE_BOT_PUBLIC_DEMO=1). Nothing there is
    transcribed, sent to an AI, uploaded or sent out (D-11, Brian 2026-11-25: "what happens if someone tries to?");
    each such button ends in a short note instead. Never set on the M14."""
    return os.environ.get("SCRIBE_BOT_PUBLIC_DEMO") == "1"


# The public demo: a slim line on every page says the meetings are made up and changes reset
templates.env.globals["public_demo"] = public_demo()
# Fingerprint of the page code: base.html adds ?v=<this> to the CSS/JS links, so phones fetch the new files right after a
# deploy instead of reusing a cached copy (the installed app kept running old code otherwise).
# Every static file counts (2026-11-25: only the CSS/JS did, so a new logo alone would have kept the old cached one).
templates.env.globals["asset_v"] = hashlib.sha256(b"".join(
    f.name.encode() + f.read_bytes() for f in sorted((WEB / "static").iterdir()) if f.is_file())).hexdigest()[:10]
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


def me_label(name: str | None) -> str:
    """F38: "Me (Brian)" in pickers came from a fixed string; it now follows person 1's own name ("Me" when that is "Me")."""
    return "Me" if not name or name.strip().lower() in ("me", "myself") else f"Me ({name.strip()})"   # never "Me (me)"


templates.env.filters["me_label"] = me_label


def short_date(ts: str | None) -> str:
    """'11/17/26' in local time (Brian, 2026-11-22: the day a talk was processed, in small letters)."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%-m/%-d/%y")


templates.env.filters["short_date"] = short_date


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

_MONTHS = {m: m[:3] for m in ("January", "February", "March", "April", "May", "June", "July", "August", "September",
                              "October", "November", "December")}
_NUMS = {w: str(i) for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve".split())}


def short_due(d: str | None) -> str:
    """Q5 (Brian, 2026-11-21): a due date the AI wrote in words, made short for lists: 'before November 15th' ->
    'by Nov 15', 'for the next four to six weeks' -> '4-6 wks', 'mid-July at the earliest' -> 'mid-Jul+'.
    A real date ('2026-11-28') reads 'due Nov 28' (or today / tomorrow). Free rules, no AI."""
    if not d or re.fullmatch(r"\s*(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*", d, re.I):
        return ""   # a bare number ("due 2") says nothing (Brian, 2026-11-22)
    if re.match(r"\d{4}-\d{2}-\d{2}", d):
        return "due " + day_label(d)
    s = " " + d.strip().rstrip(".") + " "
    s = re.sub(r"(?i)^\s*due\s+", " ", s)
    for long, short in _MONTHS.items():
        s = re.sub(rf"(?i)\b{long}\b", short, s)
    s = re.sub(r"(?i)\b(\d{1,2})(st|nd|rd|th)\b", r"\1", s)
    for w, n in _NUMS.items():
        s = re.sub(rf"(?i)\b{w}\b", n, s)
    rules = [(r"(?i)\bbefore\b", "by"), (r"(?i)\bby the end of\b", "by end of"), (r"(?i)\bfor the next\b", ""),
             (r"(?i)\bthe next\b", "next"), (r"(?i)\bat the earliest\b", "+"), (r"(?i)\bat the latest\b", ""),
             (r"(?i)\b(\d+) to (\d+)\b", r"\1–\2"), (r"(?i)(\d) weeks?\b", r"\1 wks"), (r"(?i)(\d) months?\b", r"\1 mo"),
             (r"(?i)\bby the\b", "by"), (r"(?i)\bor early\b", "/ early")]
    for pat, rep in rules:
        s = re.sub(pat, rep, s)
    s = re.sub(r"\s+\+", "+", re.sub(r"\s+", " ", s)).strip(" ,")
    if s.isdigit():
        return "due " + s
    return s if re.match(r"(?i)(by|after|early|mid|late|next|this|in|within|end|\d|[A-Z][a-z]{2}\b)", s) else "due " + s


templates.env.filters["short_due"] = short_due
# "Speaker 2 from Quarterly budget review… on Nov 13" -> "Speaker 2" in short spots (task rows); the talk is beside it
templates.env.filters["who"] = lambda n: re.sub(r"^(Speaker \d+) from .*$", r"\1", n or "")
templates.env.filters["sum_chars"] = lambda s: sum(ord(ch) for ch in str(s))   # avatar colors


def date_group(ts: str | datetime | None, now: datetime | None = None) -> str:
    """Heading for a long list grouped by date (Brian, 2026-11-20: list pages get headings, one rail tick each):
    'This week', 'Last week', 'Earlier in November 2026', 'October 2026', 'October 2025'. Weeks start on Monday."""
    if not ts:
        return "No date"
    try:
        dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(ts)
    except ValueError:
        try:
            dt = datetime.strptime(ts[:10], "%Y-%m-%d")
        except ValueError:
            return "No date"
    dt = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone()
    today = (now or datetime.now().astimezone()).date()
    days = (today - dt.date()).days
    week0 = today.toordinal() - today.weekday()
    if days < 0:
        return "Coming up"
    if dt.date().toordinal() >= week0:
        return "This week"
    if dt.date().toordinal() >= week0 - 7:
        return "Last week"
    if (dt.year, dt.month) == (today.year, today.month):
        return f"Earlier in {dt:%B %Y}"
    return f"{dt:%B %Y}"   # Brian, 2026-11-23: every month shows its year (2025 did and 2026 didn't, which read as random)


templates.env.filters["date_group"] = date_group


def group_phrase(group: str, verb: str) -> str:
    """'Recorded this week', 'Added last week', 'Recorded in October', 'Added in October 2025'."""
    if group == "No date":
        return group
    first = group.split(" ")[0]
    return f"{verb} {group[0].lower()}{group[1:]}" if first in ("This", "Last", "Earlier", "Coming") else f"{verb} in {group}"


templates.env.filters["group_phrase"] = group_phrase

from scribe_bot.core.names import first_name as _first_name  # noqa: E402
templates.env.filters["first_name"] = _first_name   # B8: "Dr. Nina Okafor" -> "Nina", never "Dr."


def short_when(ts) -> str:
    """'Nov 19, 12:07 pm' (no weekday, no year this year): short enough for a row's second line."""
    if not ts:
        return ""
    try:
        dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(ts)
    except ValueError:
        return str(ts)
    dt = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone()
    out = f"{dt:%b} {dt.day}" + (f", {dt.year}" if dt.year != datetime.now().year else "")
    return out + ", " + dt.strftime("%-I:%M %p").lower()


def mins(seconds) -> str:
    """'7 s', '9 min', '1 h 4 min'."""
    s = float(seconds or 0)
    if s < 60:
        return f"{round(s)} s"
    m = round(s / 60)
    return f"{m} min" if m < 60 else f"{m // 60} h" + (f" {m % 60} min" if m % 60 else "")


templates.env.filters["short_when"] = short_when


def detail_html(text: str | None):
    """Detailed notes (## headings, - bullets) as simple HTML, every bit of text escaped."""
    from markupsafe import Markup, escape
    out, in_list = [], False
    for ln in (text or "").splitlines():
        t = ln.strip()
        if t.startswith(("- ", "* ")):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{escape(t[2:])}</li>")
            continue
        if in_list:
            out.append("</ul>"); in_list = False
        if t.startswith("## "):
            out.append(f"<h3>{escape(t[3:])}</h3>")
        elif t:
            out.append(f"<p>{escape(t)}</p>")
    if in_list:
        out.append("</ul>")
    return Markup("".join(out))


templates.env.filters["detail_html"] = detail_html
templates.env.filters["mins"] = mins
# R5-15: a Voice button or player only for a file that's really there (a missing clip made the button do nothing)
templates.env.tests["file_there"] = lambda p: bool(p) and Path(p).is_file()


def due_group(due: str | None) -> str:
    """Open tasks (sorted by due date) grouped: 'Overdue', 'Due this week', 'Due later', 'Rough due date' (words)."""
    if not due:
        return "No due date"
    try:
        day = datetime.strptime(due[:10], "%Y-%m-%d").date()
    except ValueError:
        return "Rough due date"   # said in words ("Friday", "next year"): kept as said
    today = datetime.now().date()
    if day < today:
        return "Overdue"
    return "Due this week" if (day - today).days < 7 else "Due later"


templates.env.filters["due_group"] = due_group


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


def demo_keep_alive(cfg: settings.Settings) -> None:
    """Public demo only (F22): no worker or M14 there, so made-up healthy readings stand in for them."""
    if not public_demo():
        return
    from scribe_bot import demo_data
    try:
        conn = db.connect(cfg.db_path)
        try:
            demo_data.keep_alive(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def worker_status(cfg: settings.Settings) -> dict:
    demo_keep_alive(cfg)
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
    demo_keep_alive(cfg)
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
        t0 = time.perf_counter()
        response = await call_next(request)
        # Speed log (Brian, 2026-11-22: "still not loading quickly"): the phone reads this to split M14 time from network
        response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - t0) * 1000:.0f}"
        if request.url.path.startswith("/static/") and "v=" in request.url.query and response.status_code == 200:
            # versioned file (?v= changes with every deploy): the phone keeps it, no check with the M14 on each page
            # s-maxage: the public demo's CDN (Vercel) keeps it too, so a new visitor gets it without starting Python
            response.headers["Cache-Control"] = "public, max-age=31536000, s-maxage=31536000, immutable"
        # R5-07: no other website may show the app inside its own page (a hidden frame could make Brian's clicks press
        # buttons; those clicks count as the app's own, so the cross-site check above wouldn't stop them). The
        # reading pane is the app framing itself, which "self" allows. nosniff: files are only what they say they are.
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        a = request.query_params.get("area")
        if a in AREAS:
            response.set_cookie("area", a, max_age=365 * 86400, samesite="lax")
        return response

    def status() -> dict:
        h = health.check(cfg, run) if run else health.check(cfg)
        h["worker"] = worker_status(cfg)
        if public_demo():   # F22: Vercel's own disk and power say nothing about the M14; show made-up healthy ones
            h["disk"] = {"used_pct": 38.4, "free_gb": 296.0, "ok": True}
            h["power"] = {"source": "ac", "battery_pct": 100, "state": "charged"}
            h["ok"] = h["db"]["ok"]
        return h

    @app.get("/ping")
    def ping() -> PlainTextResponse:   # keep-alive from the phone (app.js); nothing else
        return PlainTextResponse("", status_code=204, headers={"Cache-Control": "no-store"})

    @app.post("/perf")
    async def perf(request: Request) -> PlainTextResponse:
        """Speed log: each page/part the phone loads, split into M14 / network / download / phone drawing (ms).
        Paths and numbers only, never content. logs/speed.log, kept under ~1 MB."""
        try:
            rows = json.loads((await request.body())[:20000])
            log = cfg.data_dir / "logs" / "speed.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            if log.exists() and log.stat().st_size > 1_000_000:
                log.replace(log.with_suffix(".old"))
            ua = "phone" if "Mobile" in request.headers.get("user-agent", "") else "desktop"
            with log.open("a") as f:
                for r in rows[:50]:
                    path = re.sub(r"[^\w/?=&.,-]", "", str(r.get("p", "")))[:120]
                    nums = " ".join(f"{k}={int(r[k])}" for k in ("after", "tap", "pre", "total", "m14", "net", "dl", "draw", "fcp") if isinstance(r.get(k), (int, float)))
                    f.write(f"{datetime.now().isoformat(timespec='seconds')} {ua} {r.get('k', '?')[:4]} {path} {nums}\n")
        except (ValueError, TypeError, AttributeError, OSError):
            pass
        return PlainTextResponse("", status_code=204)

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
        if public_demo():   # F22: made-up live readings (there is no M14 behind the demo)
            from scribe_bot import demo_data
            live = demo_data.live_reading()
        else:
            live = machine.fast(run or subprocess.run)
        return templates.TemplateResponse(request, "_machine.html", machine_context(machine_stored(cfg), live))

    # PWA files served from the root so the service worker controls the whole app.
    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(WEB / "static" / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js")
    def service_worker():
        return FileResponse(WEB / "static" / "sw.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    # ---- Errors (R5-01..03, 2026-11-25) ----
    # A page opened in the browser (a link to a talk deleted for good, a mangled address) gets a page in the app frame,
    # never raw code text like {"detail": ...}. Requests from the page's own code (htmx) keep the short JSON message,
    # which app.js shows in its note at the bottom.
    from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    def wants_page(request: Request) -> bool:
        return (request.method == "GET" and not request.headers.get("hx-request")
                and "text/html" in request.headers.get("accept", ""))

    def error_page(request: Request, code: int, detail) -> HTMLResponse:
        title = {404: "Not here", 410: "Gone"}.get(code, "That didn't work")
        hint = detail if isinstance(detail, str) and detail and detail != "Not Found" else \
            "This page doesn't exist. It may have been deleted, or the link is wrong."
        return templates.TemplateResponse(request, "error.html", {"nav": "", "area": area_of(request), "title": title,
                                                                  "hint": hint}, status_code=code)

    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(request: Request, exc: StarletteHTTPException):
        if wants_page(request) and exc.status_code in (400, 404, 405, 409, 410):
            return error_page(request, exc.status_code, exc.detail)
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def on_bad_input(request: Request, exc: RequestValidationError):
        # a number in the address that isn't one (/conversations/abc): that page doesn't exist
        if wants_page(request) and all(e.get("loc", [""])[0] == "path" for e in exc.errors()):
            return error_page(request, 404, None)
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(OverflowError)
    async def on_huge_number(request: Request, exc: OverflowError):
        # R5-01: an id too big for the database (a mangled link) was a crash; nothing has that id
        if wants_page(request):
            return error_page(request, 404, None)
        return JSONResponse({"detail": "Not found"}, status_code=404)

    @app.exception_handler(sqlite3.IntegrityError)
    async def on_clash(request: Request, exc: sqlite3.IntegrityError):
        # R5-03: something this change points to was removed or changed meanwhile (usually on the other device)
        msg = "That changed on another device or tab. Reload the page and try again."
        if wants_page(request):
            return error_page(request, 409, msg)
        return JSONResponse({"detail": msg}, status_code=409)

    from scribe_bot.web import routes
    app.include_router(routes.router)
    return app
