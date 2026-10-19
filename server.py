#!/usr/bin/env python3
"""Single-file personal CalDAV server backed by Radicale.

The FastAPI management API writes standard iCalendar resources into
Radicale's documented filesystem storage. Radicale serves those same files
over CalDAV for Apple Calendar, macOS Calendar, and native Android CalDAV
providers where the device/OEM includes one.
"""

from __future__ import annotations

import asyncio
import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import zipfile
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from email.utils import formatdate
from pathlib import Path
from typing import Any, Iterable, Literal
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import bcrypt
import recurring_ical_events
from dateutil import rrule as dateutil_rrule
from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.wsgi import WSGIMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from icalendar import Calendar
from pydantic import BaseModel, Field


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", APP_DIR / "data")).resolve()
COLLECTIONS_DIR = DATA_DIR / "collections"
COLLECTION_ROOT_DIR = COLLECTIONS_DIR / "collection-root"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
CONFIG_DIR = DATA_DIR / "config"
RADICALE_CONFIG_FILE = CONFIG_DIR / "radicale.conf"
HTPASSWD_FILE = CONFIG_DIR / "users"
USERS_JSON_FILE = DATA_DIR / "users.json"
API_KEY_FILE = DATA_DIR / "api_key.txt"
ATTACHMENT_INDEX_FILE = DATA_DIR / "attachments.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
JOURNAL_FILE = DATA_DIR / "journal.sqlite3"
SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,199}$")
CONTENT_TYPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,99}/[A-Za-z0-9][A-Za-z0-9.+-]{0,99}$")
BCRYPT_HASH_RE = re.compile(r"^\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}$")
MIN_API_KEY_LENGTH = 32
DEFAULT_MAX_ICS_BYTES = 1024 * 1024
DEFAULT_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_REQUEST_BYTES = 30 * 1024 * 1024
DEFAULT_SETTINGS = {
    "attachment_ttl_days": 365,
    "cleanup_interval_seconds": 3600,
}
DEFAULT_LIST_LIMIT = 200
MAX_LIST_LIMIT = 1000
MAX_BATCH_OPERATIONS = 500
MAX_EXPANDED_INSTANCES = 50000
RADICALE_MAX_RRULE_OCCURRENCES = 10000
# Floating and all-day values are compared in a caller-chosen zone, which can
# differ from the library's notion by up to a day; widen scans by this margin.
RECURRENCE_SCAN_MARGIN = timedelta(days=2)
EARLIEST_SCAN_START = datetime(1900, 1, 1, tzinfo=timezone.utc)
OCCURRENCE_CHUNK = timedelta(days=31)
CALENDAR_COMPONENTS = ("VEVENT", "VTODO", "VJOURNAL")

storage_mutex = threading.RLock()
storage_lock_state = threading.local()


class CalendarCreate(BaseModel):
    owner: str = Field(..., examples=["work", "main"])
    calendar: str = Field("default", examples=["default", "personal"])
    display_name: str | None = Field(None, examples=["Work"])


class CalendarUpdate(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=200, examples=["Household"])


class CalendarInfo(BaseModel):
    owner: str
    calendar: str
    display_name: str
    path: str


class CalendarDeleteResult(BaseModel):
    deleted: bool
    owner: str
    calendar: str
    events_deleted: int
    attachments_deleted: int


class EventSummary(BaseModel):
    uid: str
    href: str = Field(..., description="File name of the event inside the calendar collection.")
    summary: str | None = None
    location: str | None = None
    status: str | None = None
    dtstart: str | None = Field(None, description="DTSTART as stored: ISO date for all-day, ISO date-time with offset, or naive ISO for floating times.")
    dtend: str | None = Field(None, description="Effective end (DTEND, or DTSTART + DURATION, or DTSTART + 1 day for all-day).")
    start_utc: str | None = Field(None, description="Start in UTC; floating and all-day values are interpreted in the query `tz`.")
    end_utc: str | None = None
    all_day: bool = False
    tzid: str | None = None
    rrule: str | None = Field(None, description="RRULE with parts in canonical order: FREQ first, then alphabetical.")
    is_recurring: bool = False
    recurrence_id: str | None = Field(None, description="Original start of this instance; only set for expanded instances and override components.")
    is_override: bool = Field(False, description="True when the instance comes from a RECURRENCE-ID override component.")
    etag: str = Field(..., description="ETag of the whole event resource (shared by every instance of a series).")
    last_modified: str
    attachment_count: int = 0


class InvalidItem(BaseModel):
    href: str
    error: str


class EventListResponse(BaseModel):
    items: list[EventSummary]
    next_cursor: str | None = Field(None, description="Opaque cursor for the next page; null on the last page.")
    truncated: bool = Field(..., description="True exactly when more matching items exist after this page.")
    total: int = Field(..., description="Number of matching items across all pages at the time of this request.")
    timezone: str = Field(..., description="Zone used to interpret floating and all-day values.")
    invalid_items: list[InvalidItem] = Field(default_factory=list, description="Files in the collection that could not be parsed.")


class ChangeEntry(BaseModel):
    seq: int
    owner: str
    calendar: str
    uid: str | None
    href: str
    type: Literal["created", "updated", "deleted"]
    etag: str | None
    modified_at: str
    source: str = Field(..., description="api, batch, import, attachment, cleanup, calendar-delete, external (DAV clients, CLI, manual edits) or baseline (first scan).")


class ChangesResponse(BaseModel):
    changes: list[ChangeEntry]
    next_token: str = Field(..., description="Pass as `since` to receive only later changes.")
    truncated: bool = Field(..., description="True when more changes are available right away with next_token.")


class HistoryResponse(BaseModel):
    owner: str
    calendar: str
    uid: str
    items: list[ChangeEntry]
    next_cursor: str | None
    truncated: bool


class AttachmentInfo(BaseModel):
    id: str | None = Field(None, description="Managed attachment id; null for references this server did not upload.")
    filename: str | None = None
    content_type: str | None = None
    size: int | None = None
    created_at: str | None = None
    uri: str | None = None
    managed: bool
    inline: bool = False
    in_event: bool = Field(..., description="Whether the event's ATTACH properties reference this attachment.")
    file_present: bool | None = None


class AttachmentListResponse(BaseModel):
    owner: str
    calendar: str
    uid: str
    etag: str
    attachments: list[AttachmentInfo]


class AttachmentDeleteResult(BaseModel):
    deleted: bool
    id: str
    etag: str


class BatchOperation(BaseModel):
    op: Literal["put", "delete"]
    owner: str
    calendar: str
    uid: str
    ics: str | None = Field(None, description="Full text/calendar body for put operations.")
    if_match: str | None = Field(None, description="Entity tag(s) the current event must match, or *.")
    if_none_match: str | None = Field(None, description="Use * to require that the event does not exist yet.")


class BatchRequest(BaseModel):
    operations: list[BatchOperation] = Field(..., min_length=1)
    atomic: bool = Field(True, description="Only atomic batches are supported: every operation is applied or none is.")
    dry_run: bool = Field(False, description="Validate and evaluate preconditions without writing anything.")


class BatchOperationResult(BaseModel):
    index: int
    op: str
    owner: str
    calendar: str
    uid: str
    status: int | None = Field(None, description="HTTP-style status of this operation; null when it was never evaluated.")
    outcome: Literal["applied", "would_apply", "skipped", "failed", "not_applied"]
    etag: str | None = None
    created: bool | None = None
    deleted: bool | None = None
    error: str | None = None


class BatchResponse(BaseModel):
    atomic: bool = True
    applied: bool
    dry_run: bool
    failed_index: int | None = None
    results: list[BatchOperationResult]
    skipped_components: list[dict[str, Any]] = Field(default_factory=list)


class ValidateRequest(BaseModel):
    ics: str
    uid: str | None = Field(None, description="Optional UID the event must carry, as when it is PUT to a URL.")


class ValidationIssue(BaseModel):
    code: str
    message: str
    line: int | None = None


class ValidateResponse(BaseModel):
    valid: bool
    uid: str | None = None
    errors: list[ValidationIssue]
    warnings: list[ValidationIssue]


class PutDryRunResponse(BaseModel):
    dry_run: Literal[True] = True
    would_create: bool
    would_change: bool
    etag: str = Field(..., description="ETag the event would have after the write.")
    path: str
    ics: str = Field(..., description="The normalised event exactly as it would be stored.")
    validation: ValidateResponse = Field(..., description="Full validation report; PUT itself only enforces what it cannot store faithfully.")


class CleanupResult(BaseModel):
    dry_run: bool = False
    deleted: int
    remaining: int
    attachments: list[dict[str, Any]] | None = Field(None, description="Attachments that would be deleted (dry run only).")


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"description": "Invalid input."},
    401: {"description": "Missing or invalid X-API-Key."},
    404: {"description": "Not found."},
    409: {"description": "Conflict."},
    412: {"description": "Precondition failed (If-Match / If-None-Match); nothing was written."},
    410: {"description": "Token belongs to a different change journal; resync from scratch."},
    413: {"description": "Request body too large."},
    422: {"description": "Request is valid but too expensive (e.g. too many recurrence instances)."},
}


def error_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    return {code: ERROR_RESPONSES[code] for code in (401, *codes)}


ICS_REQUEST_BODY = {
    "requestBody": {
        "required": True,
        "content": {"text/calendar": {"schema": {"type": "string"}}},
    }
}


class RequestBodyTooLarge(Exception):
    def __init__(self, limit: int) -> None:
        self.limit = limit


class RequestBodyLimitMiddleware:
    def __init__(self, app: Any, limits: dict[str, int]) -> None:
        self.app = app
        self.limits = limits

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = request_body_limit(scope, self.limits)
        content_length = header_value(scope, "content-length")
        if content_length:
            try:
                if int(content_length) > limit:
                    await send_payload_too_large(send, limit)
                    return
            except ValueError:
                await send_bad_request(send, "Invalid Content-Length")
                return

        received = 0
        response_started = False

        async def limited_receive() -> dict[str, Any]:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise RequestBodyTooLarge(limit)
            return message

        async def tracking_send(message: dict[str, Any]) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except RequestBodyTooLarge as exc:
            if response_started:
                raise
            await send_payload_too_large(send, exc.limit)


class AttachmentStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: dict[str, Any]) -> Any:
        response = await super().get_response(path, scope)
        response.headers["Content-Disposition"] = "attachment"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
        response.headers["Cache-Control"] = "private, no-store"
        return response


class Cleaner:
    def __init__(self, interval_seconds: int, ttl_days: int) -> None:
        self.interval_seconds = max(60, interval_seconds)
        self.ttl_days = max(1, ttl_days)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="attachment-cleaner",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                cleanup_attachments(self.ttl_days)
            except Exception as exc:  # pragma: no cover - logged in runtime
                print(f"[attachment-cleaner] cleanup failed: {exc}", flush=True)
            self._stop.wait(self.interval_seconds)


def ensure_directories() -> None:
    for path in (DATA_DIR, COLLECTIONS_DIR, COLLECTION_ROOT_DIR, ATTACHMENTS_DIR, CONFIG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def validate_segment(value: str, name: str) -> str:
    if not SEGMENT_RE.fullmatch(value):
        raise HTTPException(
            status_code=400,
            detail=f"{name} must match {SEGMENT_RE.pattern}",
        )
    return value


def safe_filename(filename: str) -> str:
    name = Path(filename or "attachment.bin").name
    name = re.sub(r"[^A-Za-z0-9._@+-]+", "_", name).strip("._")
    return name[:120] or "attachment.bin"


def safe_content_type(content_type: str | None) -> str:
    value = (content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    if not CONTENT_TYPE_RE.fullmatch(value):
        return "application/octet-stream"
    return value


def parse_positive_int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def runtime_request_limits() -> dict[str, int]:
    attachment_limit = parse_positive_int_env(
        "MAX_ATTACHMENT_BYTES",
        DEFAULT_MAX_ATTACHMENT_BYTES,
    )
    ics_limit = parse_positive_int_env("MAX_ICS_BYTES", DEFAULT_MAX_ICS_BYTES)
    request_limit = parse_positive_int_env(
        "MAX_REQUEST_BYTES",
        max(DEFAULT_MAX_REQUEST_BYTES, attachment_limit + 1024 * 1024),
    )
    return {
        "attachment": attachment_limit,
        "attachment_request": max(request_limit, attachment_limit + 1024 * 1024),
        "ics": ics_limit,
        "request": request_limit,
    }


def request_body_limit(scope: dict[str, Any], limits: dict[str, int]) -> int:
    path = str(scope.get("path", ""))
    method = str(scope.get("method", "")).upper()
    if method == "POST" and re.fullmatch(r"/api/calendars/[^/]+/[^/]+/[^/]+/attachments", path):
        return limits["attachment_request"]
    if method == "PUT" and re.fullmatch(r"/api/calendars/[^/]+/[^/]+/[^/]+", path):
        return limits["ics"]
    if method == "POST" and (path == "/api/batch" or re.fullmatch(r"/api/calendars/[^/]+/[^/]+/import", path)):
        return limits["request"]
    if method == "POST" and path == "/api/validate":
        # JSON string escaping can double the size of an ICS body.
        return limits["ics"] * 2 + 4096
    if path.startswith("/api/"):
        return limits["ics"]
    return limits["request"]


def header_value(scope: dict[str, Any], name: str) -> str | None:
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


async def send_plain_response(send: Any, status: int, body: str) -> None:
    payload = body.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(payload)).encode("ascii")),
                (b"x-content-type-options", b"nosniff"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


async def send_payload_too_large(send: Any, limit: int) -> None:
    await send_plain_response(send, 413, f"Request body too large. Limit is {limit} bytes.")


async def send_bad_request(send: Any, detail: str) -> None:
    await send_plain_response(send, 400, detail)


def is_bcrypt_hash(value: str) -> bool:
    return bool(BCRYPT_HASH_RE.fullmatch(value))


def hash_password(password: str) -> str:
    if len(password.encode("utf-8")) > 72:
        raise ValueError("CalDAV bcrypt passwords must be at most 72 UTF-8 bytes")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def normalize_password_secret(secret: str) -> str:
    if is_bcrypt_hash(secret):
        return secret
    return hash_password(secret)


def validate_api_key_strength(api_key: str, source: str) -> None:
    if len(api_key) < MIN_API_KEY_LENGTH:
        raise ValueError(f"{source} must be at least {MIN_API_KEY_LENGTH} characters")


def public_base_url_from_env() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")


def public_base_hostname() -> str | None:
    configured = public_base_url_from_env()
    if not configured:
        return None
    parsed = urlparse(configured)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("PUBLIC_BASE_URL must be an absolute http(s) origin")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError("PUBLIC_BASE_URL must not include a path, query, or fragment")
    if parsed.scheme != "https" and os.environ.get("ALLOW_INSECURE_PUBLIC_BASE_URL", "").lower() not in {"1", "true", "yes"}:
        raise ValueError("PUBLIC_BASE_URL must use https unless ALLOW_INSECURE_PUBLIC_BASE_URL=true")
    return parsed.hostname


def allowed_hosts() -> list[str]:
    configured = os.environ.get("ALLOWED_HOSTS", "").strip()
    if configured:
        hosts = [item.strip() for item in configured.split(",") if item.strip()]
    else:
        hosts = []
        hostname = public_base_hostname()
        if hostname:
            hosts.append(hostname)
    hosts.extend(["127.0.0.1", "localhost", "::1"])
    return sorted(set(hosts))


def validate_public_origin_policy() -> None:
    if public_base_url_from_env():
        public_base_hostname()
        return
    host = os.environ.get("HOST", "127.0.0.1").strip()
    if host in {"0.0.0.0", "::"} and os.environ.get("ALLOW_REQUEST_BASE_URL", "").lower() not in {"1", "true", "yes"}:
        raise ValueError("Set PUBLIC_BASE_URL when binding to a non-loopback host")


def parse_users_from_env(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    value = value.strip()
    if not value:
        return {}
    if value.startswith("{"):
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("CALDAV_USERS JSON must be an object")
        return {str(k): str(v) for k, v in parsed.items()}
    users: dict[str, str] = {}
    for entry in value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" not in entry:
            raise ValueError("CALDAV_USERS entries must use user:password")
        username, password = entry.split(":", 1)
        username = username.strip()
        if not username or not password:
            raise ValueError("CALDAV_USERS entries require both user and password")
        users[username] = password
    return users


def load_or_create_users() -> dict[str, str]:
    users: dict[str, str] = {}
    if USERS_JSON_FILE.exists():
        users = json.loads(USERS_JSON_FILE.read_text(encoding="utf-8"))

    env_users = parse_users_from_env(os.environ.get("CALDAV_USERS"))
    if env_users:
        users.update(env_users)

    write_users(users)
    return users


def write_users(users: dict[str, str]) -> None:
    normalized: dict[str, str] = {}
    for user, password in users.items():
        validate_segment_for_startup(user, "username")
        if not isinstance(password, str) or not password:
            raise ValueError(f"password for {user!r} must be a non-empty string")
        normalized[user] = normalize_password_secret(password)

    atomic_write_text(USERS_JSON_FILE, json.dumps(normalized, indent=2, sort_keys=True) + "\n")
    atomic_write_text(
        HTPASSWD_FILE,
        "".join(f"{username}:{password}\n" for username, password in sorted(normalized.items())),
    )


def load_settings() -> dict[str, int]:
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if not isinstance(stored, dict):
            raise ValueError("settings.json must contain a JSON object")
        settings.update(stored)
    normalized = {
        "attachment_ttl_days": max(1, int(settings["attachment_ttl_days"])),
        "cleanup_interval_seconds": max(60, int(settings["cleanup_interval_seconds"])),
    }
    atomic_write_text(SETTINGS_FILE, json.dumps(normalized, indent=2, sort_keys=True) + "\n")
    return normalized


def runtime_cleanup_settings() -> dict[str, int]:
    settings = load_settings()
    return {
        "attachment_ttl_days": int(os.environ.get("ATTACHMENT_TTL_DAYS", settings["attachment_ttl_days"])),
        "cleanup_interval_seconds": int(
            os.environ.get("CLEANUP_INTERVAL_SECONDS", settings["cleanup_interval_seconds"])
        ),
    }


def validate_segment_for_startup(value: str, name: str) -> None:
    if not SEGMENT_RE.fullmatch(value):
        raise ValueError(f"{name} {value!r} must match {SEGMENT_RE.pattern}")


def load_or_create_api_key() -> str:
    env_key = os.environ.get("CALDAV_API_KEY", "").strip()
    if env_key:
        validate_api_key_strength(env_key, "CALDAV_API_KEY")
        atomic_write_text(API_KEY_FILE, env_key + "\n")
        return env_key
    if API_KEY_FILE.exists():
        stored_key = API_KEY_FILE.read_text(encoding="utf-8").strip()
        validate_api_key_strength(stored_key, str(API_KEY_FILE))
        return stored_key
    key = secrets.token_urlsafe(32)
    atomic_write_text(API_KEY_FILE, key + "\n")
    return key


def write_radicale_config() -> None:
    config = f"""
[auth]
type = htpasswd
htpasswd_filename = {HTPASSWD_FILE}
htpasswd_encryption = bcrypt
delay = 1

[rights]
type = owner_only

[storage]
type = multifilesystem
filesystem_folder = {COLLECTIONS_DIR}

[web]
type = none

[logging]
level = info
mask_passwords = True
""".strip()
    atomic_write_text(RADICALE_CONFIG_FILE, config + "\n")
    os.environ["RADICALE_CONFIG"] = str(RADICALE_CONFIG_FILE)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    if os.name == "nt":
        path.write_text(content, encoding="utf-8", newline="\n")
        return
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as tmp:
            tmp.write(content)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except PermissionError:
                pass


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == content:
        return
    if os.name == "nt":
        path.write_bytes(content)
        return
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(content)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except PermissionError:
                pass


@contextmanager
def storage_lock() -> Iterable[None]:
    COLLECTIONS_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = COLLECTIONS_DIR / ".Radicale.lock"
    with storage_mutex:
        # flock is per open file description, so a nested acquire through a
        # second handle would deadlock; only the outermost level takes it.
        depth = getattr(storage_lock_state, "depth", 0)
        storage_lock_state.depth = depth + 1
        try:
            if depth:
                yield
                return
            with lock_file.open("a+b") as handle:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    try:
                        yield
                    finally:
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    try:
                        yield
                    finally:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            storage_lock_state.depth = depth


def collection_path(owner: str, calendar: str) -> Path:
    return COLLECTION_ROOT_DIR / owner / calendar


def event_path(owner: str, calendar: str, uid: str) -> Path:
    return collection_path(owner, calendar) / f"{uid}.ics"


def read_calendar_props(path: Path) -> dict[str, Any]:
    try:
        props = json.loads((path / ".Radicale.props").read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return props if isinstance(props, dict) else {}


def calendar_exists(owner: str, calendar: str) -> bool:
    return (collection_path(owner, calendar) / ".Radicale.props").is_file()


def create_calendar(owner: str, calendar: str, display_name: str | None = None) -> Path:
    validate_segment_for_startup(owner, "owner")
    validate_segment_for_startup(calendar, "calendar")
    path = collection_path(owner, calendar)
    with storage_lock():
        path.mkdir(parents=True, exist_ok=True)
        # Merge so properties set by CalDAV clients (colour, description) survive.
        props = read_calendar_props(path)
        props["tag"] = "VCALENDAR"
        if display_name:
            props["D:displayname"] = display_name
        atomic_write_text(path / ".Radicale.props", json.dumps(props, sort_keys=True))
    return path


def ensure_calendar(owner: str, calendar: str) -> bool:
    """Create the calendar if it is missing. Returns True when it was created."""
    with storage_lock():
        if calendar_exists(owner, calendar):
            return False
        create_calendar(owner, calendar, calendar)
        return True


def list_calendars() -> list[dict[str, str]]:
    calendars: list[dict[str, str]] = []
    if not COLLECTION_ROOT_DIR.exists():
        return calendars
    for props_file in COLLECTION_ROOT_DIR.glob("*/*/.Radicale.props"):
        try:
            props = json.loads(props_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            props = {}
        owner = props_file.parent.parent.name
        calendar_name = props_file.parent.name
        calendars.append(
            {
                "owner": owner,
                "calendar": calendar_name,
                "display_name": str(props.get("D:displayname") or calendar_name),
                "path": f"/{owner}/{calendar_name}/",
            }
        )
    return sorted(calendars, key=lambda item: (item["owner"], item["calendar"]))


def path_is_within(path: Path, base: Path) -> bool:
    resolved_path = path.resolve()
    resolved_base = base.resolve()
    return resolved_path == resolved_base or resolved_base in resolved_path.parents


def remove_tree_under(base: Path, path: Path) -> None:
    if not path.exists():
        return
    if not path_is_within(path, base):
        raise ValueError(f"Refusing to delete path outside {base}: {path}")
    shutil.rmtree(path)


def delete_managed_attachments_for_owner(owner: str) -> int:
    records = load_attachment_index()
    kept: list[dict[str, Any]] = []
    deleted = 0
    for record in records:
        if record.get("owner") == owner:
            path = (ATTACHMENTS_DIR / str(record.get("relative_path", ""))).resolve()
            if path_is_within(path, ATTACHMENTS_DIR):
                delete_file_quietly(path)
                delete_empty_parents(path)
            deleted += 1
        else:
            kept.append(record)
    save_attachment_index(kept)
    return deleted


def rewrite_attachment_uri_owner(uri: str, source_owner: str, target_owner: str) -> str:
    marker = "/attachments/"
    if marker not in uri:
        return uri
    prefix, rest = uri.split(marker, 1)
    parts = rest.split("/", 1)
    if not parts or parts[0] != quote(source_owner, safe=""):
        return uri
    suffix = parts[1] if len(parts) > 1 else ""
    return f"{prefix}{marker}{quote(target_owner, safe='')}/{suffix}"


def validate_attachment_migration_targets(source_owner: str, target_owner: str) -> None:
    for record in load_attachment_index():
        if record.get("owner") != source_owner:
            continue
        old_relative = Path(str(record.get("relative_path", "")))
        if not old_relative.parts:
            continue
        old_path = (ATTACHMENTS_DIR / old_relative).resolve()
        new_relative = Path(target_owner).joinpath(*old_relative.parts[1:])
        new_path = (ATTACHMENTS_DIR / new_relative).resolve()
        if not path_is_within(old_path, ATTACHMENTS_DIR):
            raise ValueError(f"Refusing to move attachment outside {ATTACHMENTS_DIR}: {old_path}")
        if not path_is_within(new_path, ATTACHMENTS_DIR):
            raise ValueError(f"Refusing to move attachment outside {ATTACHMENTS_DIR}: {new_path}")
        if old_path.exists() and new_path.exists():
            raise ValueError(f"Target attachment path already exists: {new_path}")


def migrate_managed_attachments_owner(source_owner: str, target_owner: str) -> dict[str, int]:
    records = load_attachment_index()
    migrated = 0
    rewritten_uris = 0
    for record in records:
        if record.get("owner") != source_owner:
            continue
        old_relative = Path(str(record.get("relative_path", "")))
        if not old_relative.parts:
            continue
        old_path = (ATTACHMENTS_DIR / old_relative).resolve()
        new_relative = Path(target_owner).joinpath(*old_relative.parts[1:])
        new_path = (ATTACHMENTS_DIR / new_relative).resolve()
        if path_is_within(old_path, ATTACHMENTS_DIR) and old_path.exists():
            if not path_is_within(new_path, ATTACHMENTS_DIR):
                raise ValueError(f"Refusing to move attachment outside {ATTACHMENTS_DIR}: {new_path}")
            if new_path.exists():
                raise ValueError(f"Target attachment path already exists: {new_path}")
            new_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old_path), str(new_path))
            delete_empty_parents(old_path)
        record["owner"] = target_owner
        record["relative_path"] = new_relative.as_posix()
        old_uri = str(record.get("uri", ""))
        new_uri = rewrite_attachment_uri_owner(old_uri, source_owner, target_owner)
        if new_uri != old_uri:
            record["uri"] = new_uri
            rewritten_uris += 1
        migrated += 1
    save_attachment_index(records)
    return {"attachments_migrated": migrated, "attachment_uris_rewritten": rewritten_uris}


def rewrite_calendar_attachment_uris(source_owner: str, target_owner: str) -> int:
    rewritten = 0
    owner_root = COLLECTION_ROOT_DIR / target_owner
    if not owner_root.exists():
        return 0
    for event_file in owner_root.glob("*/*.ics"):
        try:
            calendar = Calendar.from_ical(event_file.read_bytes())
        except Exception:
            continue
        file_changed = False
        for event in calendar.walk("VEVENT"):
            attachments = attach_values(event)
            if not attachments:
                continue
            changed = False
            for item in attachments:
                uri = str(item)
                new_uri = rewrite_attachment_uri_owner(uri, source_owner, target_owner)
                if new_uri != uri:
                    changed = True
                    rewritten += 1
            if changed:
                file_changed = True
                event.pop("ATTACH", None)
                for item in attachments:
                    uri = str(item)
                    new_uri = rewrite_attachment_uri_owner(uri, source_owner, target_owner)
                    if new_uri != uri:
                        params = dict(getattr(item, "params", {}))
                        event.add("ATTACH", new_uri, parameters=params)
                    else:
                        event.add("ATTACH", item, encode=0)
        if file_changed:
            atomic_write_bytes(event_file, calendar.to_ical())
    return rewritten


def migrate_user_data(source_owner: str, target_owner: str) -> dict[str, int]:
    validate_segment_for_startup(source_owner, "source owner")
    validate_segment_for_startup(target_owner, "target owner")
    if source_owner == target_owner:
        raise ValueError("source and target users must be different")

    migrated_calendars = 0
    with storage_lock():
        source_root = COLLECTION_ROOT_DIR / source_owner
        target_root = COLLECTION_ROOT_DIR / target_owner
        legacy_source_root = COLLECTIONS_DIR / source_owner
        legacy_target_root = COLLECTIONS_DIR / target_owner
        if source_root.exists():
            for calendar_dir in source_root.iterdir():
                if not calendar_dir.is_dir():
                    continue
                target_dir = target_root / calendar_dir.name
                if target_dir.exists():
                    raise ValueError(
                        f"Target user already has calendar {calendar_dir.name!r}; rename or remove it first"
                    )
        if legacy_source_root.exists() and legacy_target_root.exists():
            raise ValueError(f"Target legacy collection path already exists: {legacy_target_root}")
        validate_attachment_migration_targets(source_owner, target_owner)

        if source_root.exists():
            target_root.mkdir(parents=True, exist_ok=True)
            for calendar_dir in source_root.iterdir():
                if calendar_dir.is_dir():
                    shutil.move(str(calendar_dir), str(target_root / calendar_dir.name))
                    migrated_calendars += 1
            remove_tree_under(COLLECTION_ROOT_DIR, source_root)

        if legacy_source_root.exists():
            legacy_target_root.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_source_root), str(legacy_target_root))

        attachment_migration = migrate_managed_attachments_owner(source_owner, target_owner)
        event_uris_rewritten = rewrite_calendar_attachment_uris(source_owner, target_owner)

    return {
        "calendars_migrated": migrated_calendars,
        **attachment_migration,
        "event_attachment_uris_rewritten": event_uris_rewritten,
    }


def delete_user(username: str, mode: str, target_user: str | None = None) -> dict[str, Any]:
    validate_segment_for_startup(username, "username")
    if mode not in {"delete", "migrate"}:
        raise ValueError("mode must be 'delete' or 'migrate'")
    users = load_or_create_users()
    if mode == "migrate":
        if not target_user:
            raise ValueError("target_user is required for migration")
        validate_segment_for_startup(target_user, "target username")
        if target_user not in users:
            raise ValueError(f"target user does not exist: {target_user}")
        migration = migrate_user_data(username, target_user)
    else:
        migration = {
            "calendars_migrated": 0,
            "attachments_migrated": 0,
            "attachment_uris_rewritten": 0,
            "event_attachment_uris_rewritten": 0,
        }

    existed = username in users
    if existed:
        del users[username]
        write_users(users)

    result: dict[str, Any] = {
        "user_deleted": existed,
        "mode": mode,
        "data_deleted": False,
        "attachments_deleted": 0,
        **migration,
    }
    if mode == "delete":
        with storage_lock():
            result["attachments_deleted"] = delete_managed_attachments_for_owner(username)
            remove_tree_under(COLLECTION_ROOT_DIR, COLLECTION_ROOT_DIR / username)
            remove_tree_under(COLLECTIONS_DIR, COLLECTIONS_DIR / username)
        result["data_deleted"] = True
    return result


def parse_calendar(data: bytes) -> Calendar:
    try:
        calendar = Calendar.from_ical(data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid iCalendar data: {exc}") from exc
    events = list(calendar.walk("VEVENT"))
    if not events:
        raise HTTPException(status_code=400, detail="ICS must contain exactly one VEVENT")
    uids = {str(event.get("UID", "")).strip() for event in events}
    if "" in uids:
        raise HTTPException(status_code=400, detail="VEVENT must contain UID")
    if len(events) > 1:
        # A recurring series may carry RECURRENCE-ID overrides in the same resource.
        masters = [event for event in events if event.get("RECURRENCE-ID") is None]
        if len(uids) != 1 or len(masters) > 1:
            raise HTTPException(
                status_code=400,
                detail="ICS must contain exactly one VEVENT, or one series plus RECURRENCE-ID overrides sharing its UID",
            )
        recurrence_ids = [event["RECURRENCE-ID"].to_ical() for event in events if event.get("RECURRENCE-ID") is not None]
        if len(set(recurrence_ids)) != len(recurrence_ids):
            raise HTTPException(status_code=400, detail="ICS contains duplicate RECURRENCE-ID overrides")
    return calendar


def property_parse_errors(calendar: Calendar) -> list[str]:
    problems: list[str] = []
    for component in calendar.walk():
        for name, message in getattr(component, "errors", None) or []:
            label = f"{component.name}.{name}" if name else str(component.name)
            problems.append(f"{label}: {message}")
    return problems


def event_uid(calendar: Calendar) -> str:
    return str(list(calendar.walk("VEVENT"))[0].get("UID"))


def master_event(calendar: Calendar) -> Any:
    events = list(calendar.walk("VEVENT"))
    for event in events:
        if event.get("RECURRENCE-ID") is None:
            return event
    return events[0]


def attach_values(component: Any) -> list[Any]:
    attachments = component.get("ATTACH")
    if attachments is None:
        return []
    return attachments if isinstance(attachments, list) else [attachments]


def normalize_calendar(data: bytes, expected_uid: str) -> bytes:
    calendar = parse_calendar(data)
    problems = property_parse_errors(calendar)
    if problems:
        # The parser would silently drop these properties; refuse instead of losing data.
        raise HTTPException(status_code=400, detail="Invalid iCalendar data: " + "; ".join(problems[:5]))
    uid = event_uid(calendar)
    if uid != expected_uid:
        raise HTTPException(
            status_code=409,
            detail=f"VEVENT UID {uid!r} does not match URL uid {expected_uid!r}",
        )
    return calendar.to_ical()


def caldav_load_error(data: bytes) -> str | None:
    """Run Radicale's own loader; items it rejects are invisible to CalDAV clients."""
    from radicale import item as radicale_item

    try:
        vobject_items = radicale_item.read_components(data.decode("utf-8"))
        radicale_item.check_and_sanitize_items(
            vobject_items,
            max_vevent_rrule_occurrence=RADICALE_MAX_RRULE_OCCURRENCES,
            tag="VCALENDAR",
        )
        (vobject_item,) = vobject_items
        item = radicale_item.Item(collection_path="validate", vobject_item=vobject_item)
        item.etag, item.uid, item.time_range  # noqa: B018 - force Radicale to compute these
    except Exception as exc:
        return str(exc) or exc.__class__.__name__
    return None


def prepare_event_body(data: bytes, uid: str) -> bytes:
    normalized = normalize_calendar(data, uid)
    error = caldav_load_error(normalized)
    if error:
        raise HTTPException(status_code=400, detail=f"CalDAV clients could not load this event: {error}")
    return normalized


# ---------------------------------------------------------------------------
# Stored items: tolerant parsing with an in-memory cache keyed by file stat.
# ---------------------------------------------------------------------------


@dataclass
class StoredItem:
    href: str
    path: Path
    etag: str
    size: int
    mtime_ns: int
    inode: int
    calendar: Calendar | None
    uid: str | None
    component: str | None
    error: str | None

    @property
    def last_modified(self) -> datetime:
        return datetime.fromtimestamp(self.mtime_ns / 1_000_000_000, tz=timezone.utc)


_stored_item_cache: dict[str, StoredItem] = {}
_stored_item_cache_lock = threading.Lock()


def compute_etag(data: bytes) -> str:
    return '"' + hashlib.sha256(data).hexdigest()[:32] + '"'


def http_date(timestamp: float) -> str:
    return formatdate(timestamp, usegmt=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_item_filename(name: str) -> bool:
    # Mirrors Radicale's is_safe_filesystem_path_component() for item files.
    return bool(name) and not name.startswith(".") and not name.endswith("~") and "/" not in name and "\\" not in name


def iter_collection_files(owner: str, calendar: str) -> list[Path]:
    try:
        with os.scandir(collection_path(owner, calendar)) as entries:
            names = sorted(entry.name for entry in entries if entry.is_file() and is_item_filename(entry.name))
    except (FileNotFoundError, NotADirectoryError):
        return []
    directory = collection_path(owner, calendar)
    return [directory / name for name in names]


def read_stored_item(path: Path) -> StoredItem | None:
    key = str(path)
    try:
        with path.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            with _stored_item_cache_lock:
                cached = _stored_item_cache.get(key)
            if cached and (cached.mtime_ns, cached.size, cached.inode) == (stat.st_mtime_ns, stat.st_size, stat.st_ino):
                return cached
            data = handle.read()
    except (FileNotFoundError, IsADirectoryError):
        with _stored_item_cache_lock:
            _stored_item_cache.pop(key, None)
        return None
    calendar: Calendar | None = None
    error: str | None = None
    uid: str | None = None
    component: str | None = None
    try:
        calendar = Calendar.from_ical(data)
    except Exception as exc:
        error = f"Invalid iCalendar data: {exc}"[:300]
    if calendar is not None:
        for sub in calendar.subcomponents:
            if sub.name in CALENDAR_COMPONENTS:
                uid = str(sub.get("UID", "")).strip() or None
                component = sub.name
                break
    item = StoredItem(
        href=path.name,
        path=path,
        etag=compute_etag(data),
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        inode=stat.st_ino,
        calendar=calendar,
        uid=uid,
        component=component,
        error=error,
    )
    with _stored_item_cache_lock:
        _stored_item_cache[key] = item
    return item


def find_event_path(owner: str, calendar: str, uid: str) -> Path | None:
    """Locate an event by UID, including files whose name differs from the UID.

    CalDAV clients choose their own resource names, so `<uid>.ics` is only the
    convention for events created through this API.
    """
    direct = event_path(owner, calendar, uid)
    if direct.is_file():
        return direct
    for path in iter_collection_files(owner, calendar):
        item = read_stored_item(path)
        if item and item.uid == uid:
            return path
    return None


def read_event(owner: str, calendar: str, uid: str) -> tuple[Path, bytes, os.stat_result] | None:
    path = find_event_path(owner, calendar, uid)
    if path is None:
        return None
    try:
        with path.open("rb") as handle:
            return path, handle.read(), os.fstat(handle.fileno())
    except FileNotFoundError:
        return None


def load_event(owner: str, calendar: str, uid: str) -> bytes:
    found = read_event(owner, calendar, uid)
    if found is None:
        raise HTTPException(status_code=404, detail="Event not found")
    return found[1]


# ---------------------------------------------------------------------------
# Conditional requests
# ---------------------------------------------------------------------------


def parse_entity_tags(value: str) -> list[str] | None:
    """Parse an If-Match / If-None-Match header. None means `*`."""
    value = value.strip()
    if value == "*":
        return None
    tags = re.findall(r'(?:W/)?"[^"]*"', value)
    if not tags:
        raise HTTPException(status_code=400, detail="Malformed entity-tag list; ETags must be quoted")
    return tags


def precondition_failed(detail: str, current_etag: str | None) -> HTTPException:
    headers = {"ETag": current_etag} if current_etag else None
    return HTTPException(status_code=412, detail=detail, headers=headers)


def check_write_preconditions(current_etag: str | None, if_match: str | None, if_none_match: str | None) -> None:
    if if_match is not None:
        tags = parse_entity_tags(if_match)
        if current_etag is None:
            raise precondition_failed("If-Match failed: the event does not exist", None)
        # Strong comparison: weak tags never match for writes.
        if tags is not None and current_etag not in tags:
            raise precondition_failed(f"If-Match failed: the event has changed (current ETag {current_etag})", current_etag)
    if if_none_match is not None:
        tags = parse_entity_tags(if_none_match)
        if current_etag is not None:
            if tags is None:
                raise precondition_failed("If-None-Match: * failed: the event already exists", current_etag)
            if current_etag in {tag.removeprefix("W/") for tag in tags}:
                raise precondition_failed("If-None-Match failed: the event matches the given ETag", current_etag)


def if_none_match_hits(header: str | None, current_etag: str) -> bool:
    if header is None:
        return False
    tags = parse_entity_tags(header)
    return tags is None or current_etag in {tag.removeprefix("W/") for tag in tags}


# ---------------------------------------------------------------------------
# Writes: every mutation of event files goes through commit_writes().
# ---------------------------------------------------------------------------


@dataclass
class PlannedWrite:
    owner: str
    calendar: str
    uid: str
    path: Path
    data: bytes | None  # None deletes the file


def commit_writes(writes: list[PlannedWrite], source: str) -> None:
    """Apply event writes all-or-nothing. The caller must hold storage_lock()."""
    if not writes:
        return
    scopes = sorted({(write.owner, write.calendar) for write in writes})
    journal_sync(scopes, "external")
    created_calendars: list[Path] = []
    snapshots: list[tuple[Path, bytes | None]] = []
    try:
        for owner, calendar in scopes:
            needs_calendar = any(w.data is not None for w in writes if (w.owner, w.calendar) == (owner, calendar))
            if needs_calendar and ensure_calendar(owner, calendar):
                created_calendars.append(collection_path(owner, calendar))
        for write in writes:
            previous = write.path.read_bytes() if write.path.exists() else None
            snapshots.append((write.path, previous))
            if write.data is None:
                if previous is not None:
                    write.path.unlink()
            else:
                atomic_write_bytes(write.path, write.data)
    except Exception:
        for path, previous in reversed(snapshots):
            try:
                if previous is None:
                    delete_file_quietly(path)
                else:
                    atomic_write_bytes(path, previous)
            except Exception as exc:  # pragma: no cover - best effort
                print(f"[storage] rollback failed for one file: {exc.__class__.__name__}", flush=True)
        for directory in created_calendars:
            try:
                remove_tree_under(COLLECTION_ROOT_DIR, directory)
            except Exception:  # pragma: no cover - best effort
                pass
        raise
    for write in writes:
        try:
            if write.data is None:
                delete_managed_attachments(write.owner, write.calendar, write.uid)
            else:
                allowed = attachment_uris_from_ics(write.data)
                if allowed is not None:
                    reconcile_managed_attachments(write.owner, write.calendar, write.uid, allowed)
        except Exception as exc:  # pragma: no cover - attachments must not undo an applied write
            print(f"[attachments] reconcile failed: {exc.__class__.__name__}", flush=True)
    journal_sync(scopes, source)


def put_event(
    owner: str,
    calendar: str,
    uid: str,
    data: bytes,
    if_match: str | None = None,
    if_none_match: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    validate_segment_for_startup(owner, "owner")
    validate_segment_for_startup(calendar, "calendar")
    validate_segment_for_startup(uid, "uid")
    normalized = prepare_event_body(data, uid)
    with storage_lock():
        found = read_event(owner, calendar, uid)
        current = found[1] if found else None
        check_write_preconditions(compute_etag(current) if current is not None else None, if_match, if_none_match)
        path = found[0] if found else event_path(owner, calendar, uid)
        result: dict[str, Any] = {
            "created": current is None,
            "changed": current != normalized,
            "path": path,
            "data": normalized,
            "etag": compute_etag(normalized),
            "last_modified": None,
        }
        if not dry_run:
            commit_writes([PlannedWrite(owner, calendar, uid, path, normalized)], "api")
            result["last_modified"] = path.stat().st_mtime
    return result


def upsert_event(owner: str, calendar: str, uid: str, data: bytes) -> Path:
    return put_event(owner, calendar, uid, data)["path"]


def delete_event(owner: str, calendar: str, uid: str, if_match: str | None = None) -> bool:
    validate_segment_for_startup(owner, "owner")
    validate_segment_for_startup(calendar, "calendar")
    validate_segment_for_startup(uid, "uid")
    with storage_lock():
        found = read_event(owner, calendar, uid)
        check_write_preconditions(compute_etag(found[1]) if found else None, if_match, None)
        if found is None:
            delete_managed_attachments(owner, calendar, uid)
            return False
        commit_writes([PlannedWrite(owner, calendar, uid, found[0], None)], "api")
    return True


def delete_event_and_attachments(owner: str, calendar: str, uid: str) -> bool:
    return delete_event(owner, calendar, uid)


def attachment_uris_from_ics(data: bytes) -> set[str] | None:
    """URIs referenced by ATTACH properties, or None when the data cannot be parsed."""
    try:
        calendar = Calendar.from_ical(data)
    except Exception:
        return None
    uris: set[str] = set()
    for event in calendar.walk("VEVENT"):
        for item in attach_values(event):
            if str(item).startswith(("http://", "https://", "/")):
                uris.add(str(item))
    return uris


def add_attachment_to_ics(data: bytes, public_uri: str, content_type: str, filename: str) -> bytes:
    calendar = parse_calendar(data)
    master_event(calendar).add(
        "ATTACH",
        public_uri,
        parameters={
            "VALUE": "URI",
            "FMTTYPE": content_type or "application/octet-stream",
            "FILENAME": filename,
        },
    )
    return calendar.to_ical()


def remove_attachment_uris_from_ics(data: bytes, stale_uris: set[str]) -> bytes | None:
    """Return the event without the given ATTACH URIs, or None if nothing changed."""
    try:
        calendar = Calendar.from_ical(data)
    except Exception:
        return None
    changed = False
    for event in calendar.walk("VEVENT"):
        attachments = attach_values(event)
        kept = [item for item in attachments if str(item) not in stale_uris]
        if len(kept) == len(attachments):
            continue
        changed = True
        event.pop("ATTACH", None)
        for item in kept:
            event.add("ATTACH", item, encode=0)
    return calendar.to_ical() if changed else None


# ---------------------------------------------------------------------------
# Time handling
# ---------------------------------------------------------------------------


@dataclass
class Bounds:
    start: date | None
    end: date | None
    start_utc: datetime | None
    end_utc: datetime | None
    all_day: bool


def default_timezone_name() -> str:
    return os.environ.get("DEFAULT_TIMEZONE", "").strip() or "UTC"


def resolve_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Unknown timezone {name!r}") from exc


def default_timezone() -> Any:
    try:
        return ZoneInfo(default_timezone_name())
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def to_utc(value: Any, tz: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=tz)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, datetime_time.min, tzinfo=tz).astimezone(timezone.utc)
    return None


def decoded_or_none(component: Any, name: str) -> Any:
    try:
        return component.decoded(name, None)
    except Exception:
        return None


def component_bounds(component: Any, tz: Any) -> Bounds:
    start = decoded_or_none(component, "DTSTART")
    if not isinstance(start, date):
        return Bounds(None, None, None, None, False)
    all_day = not isinstance(start, datetime)
    end = decoded_or_none(component, "DTEND")
    if not isinstance(end, date):
        duration = decoded_or_none(component, "DURATION")
        if isinstance(duration, timedelta):
            end = start + duration
        elif all_day:
            end = start + timedelta(days=1)
        else:
            end = start
    start_utc = to_utc(start, tz)
    end_utc = to_utc(end, tz)
    if end_utc is None or (start_utc is not None and end_utc < start_utc):
        end_utc = start_utc
    return Bounds(start, end, start_utc, end_utc, all_day)


def overlaps(bounds: Bounds, range_from: datetime | None, range_to: datetime | None) -> bool:
    """RFC 4791 time-range semantics: start < to AND end > from; instants need from <= start."""
    if range_from is None and range_to is None:
        return True
    if bounds.start_utc is None or bounds.end_utc is None:
        return False
    if range_to is not None and bounds.start_utc >= range_to:
        return False
    if range_from is None:
        return True
    if bounds.end_utc > bounds.start_utc:
        return bounds.end_utc > range_from
    return bounds.start_utc >= range_from


def is_recurring_calendar(calendar: Calendar) -> bool:
    return any(event.get("RRULE") is not None or event.get("RDATE") is not None for event in calendar.walk("VEVENT"))


def has_unbounded_rrule(calendar: Calendar) -> bool:
    for event in calendar.walk("VEVENT"):
        rules = event.get("RRULE")
        if rules is None:
            continue
        for rule in rules if isinstance(rules, list) else [rules]:
            if "COUNT" not in rule and "UNTIL" not in rule:
                return True
    return False


def earliest_start(calendar: Calendar, tz: Any) -> datetime | None:
    starts = [component_bounds(event, tz).start_utc for event in calendar.walk("VEVENT")]
    starts = [start for start in starts if start is not None]
    return min(starts) if starts else None


def iter_occurrences(
    calendar: Calendar,
    range_from: datetime | None,
    range_to: datetime | None,
    tz: Any,
    budget: list[int],
) -> Iterable[tuple[Any, Bounds]]:
    """Yield (instance component, bounds) for occurrences overlapping the range.

    Bounded ranges are expanded in fixed-size chunks with between(), which is far
    cheaper per instance than after(); after() is only used to find the first
    instance of an open-ended range.
    """
    query = recurring_ical_events.of(calendar)
    scan_from = (range_from or EARLIEST_SCAN_START) - RECURRENCE_SCAN_MARGIN
    first = earliest_start(calendar, tz)
    if first is not None:
        scan_from = max(scan_from, first - RECURRENCE_SCAN_MARGIN)
    if range_to is None:
        for occurrence in query.after(scan_from):
            bounds = component_bounds(occurrence, tz)
            budget[0] -= 1
            if budget[0] < 0:
                raise too_many_instances()
            if overlaps(bounds, range_from, range_to):
                yield occurrence, bounds
        return
    scan_to = range_to + RECURRENCE_SCAN_MARGIN
    seen: set[tuple[int, int]] = set()
    chunk_start = scan_from
    while chunk_start < scan_to:
        chunk_end = min(chunk_start + OCCURRENCE_CHUNK, scan_to)
        found: list[tuple[Any, Bounds]] = []
        for occurrence in query.between(chunk_start, chunk_end):
            bounds = component_bounds(occurrence, tz)
            # Instances spanning a chunk boundary are returned by both chunks.
            key = (
                epoch_microseconds(to_utc(decoded_or_none(occurrence, "RECURRENCE-ID"), tz)),
                epoch_microseconds(bounds.start_utc),
            )
            if key in seen:
                continue
            seen.add(key)
            found.append((occurrence, bounds))
        budget[0] -= len(found)
        if budget[0] < 0:
            raise too_many_instances()
        found.sort(key=lambda pair: epoch_microseconds(pair[1].start_utc))
        for occurrence, bounds in found:
            if overlaps(bounds, range_from, range_to):
                yield occurrence, bounds
        chunk_start = chunk_end


def series_overlaps(
    calendar: Calendar,
    range_from: datetime | None,
    range_to: datetime | None,
    tz: Any,
    budget: list[int],
) -> bool:
    # Instances come in start order, so the first one ending after `from` has the
    # earliest start of all candidates; the series matches iff it starts before `to`.
    for _occurrence, bounds in iter_occurrences(calendar, range_from, None, tz, budget):
        return range_to is None or (bounds.start_utc is not None and bounds.start_utc < range_to)
    return False


def too_many_instances() -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=f"More than {MAX_EXPANDED_INSTANCES} recurrence instances in this window; narrow from/to",
    )


def series_last_end(calendar: Calendar, tz: Any) -> datetime | None:
    """End of the last instance of a finite series (dateutil is much faster than full expansion)."""
    events = list(calendar.walk("VEVENT"))
    ends = [component_bounds(event, tz).end_utc for event in events if event.get("RECURRENCE-ID") is not None]
    master = next((event for event in events if event.get("RECURRENCE-ID") is None), None)
    if master is not None:
        start = decoded_or_none(master, "DTSTART")
        bounds = component_bounds(master, tz)
        if isinstance(start, date) and bounds.start_utc and bounds.end_utc:
            reference = as_naive_or_aware(start, start)
            ruleset = dateutil_rrule.rruleset()
            rules = master.get("RRULE")
            for rule in [] if rules is None else rules if isinstance(rules, list) else [rules]:
                ruleset.rrule(dateutil_rrule.rrulestr(rule.to_ical().decode("utf-8"), dtstart=reference))
            for name, add in (("RDATE", ruleset.rdate), ("EXDATE", ruleset.exdate)):
                props = master.get(name)
                for prop in [] if props is None else props if isinstance(props, list) else [props]:
                    for item in prop.dts:
                        if isinstance(item.dt, date):
                            add(as_naive_or_aware(item.dt, start))
            last = None
            for count, occurrence in enumerate(ruleset):
                if count >= 100_000:
                    return None
                last = occurrence
            if last is not None:
                last_start = to_utc(last, tz)
                if last_start is not None:
                    ends.append(last_start + (bounds.end_utc - bounds.start_utc))
    ends = [end for end in ends if end is not None]
    return max(ends) if ends else None


def event_end_from_ics(data: bytes) -> datetime | None:
    """Time after which the event (or the last instance of a series) is over.

    Returns None when it cannot be determined or the series never ends.
    """
    try:
        calendar = Calendar.from_ical(data)
    except Exception:
        return None
    if not list(calendar.walk("VEVENT")):
        return None
    tz = default_timezone()
    if not is_recurring_calendar(calendar):
        return component_bounds(master_event(calendar), tz).end_utc
    if has_unbounded_rrule(calendar):
        return None
    try:
        return series_last_end(calendar, tz)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Change journal
# ---------------------------------------------------------------------------


class ChangeJournal:
    """Append-only change log reconciled against the files on disk.

    Writes made through the management API are recorded with their source.
    Anything else (CalDAV clients, the CLI, manual edits) is detected by
    comparing each file with the last known state and recorded as `external`.
    Callers must hold storage_lock().
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS changes (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        owner TEXT NOT NULL,
        calendar TEXT NOT NULL,
        href TEXT NOT NULL,
        uid TEXT,
        type TEXT NOT NULL,
        etag TEXT,
        modified_at TEXT NOT NULL,
        source TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS changes_by_calendar ON changes(owner, calendar, seq);
    CREATE INDEX IF NOT EXISTS changes_by_uid ON changes(owner, calendar, uid, seq);
    CREATE TABLE IF NOT EXISTS state (
        owner TEXT NOT NULL,
        calendar TEXT NOT NULL,
        href TEXT NOT NULL,
        uid TEXT,
        etag TEXT NOT NULL,
        mtime_ns INTEGER NOT NULL,
        size INTEGER NOT NULL,
        inode INTEGER NOT NULL,
        PRIMARY KEY (owner, calendar, href)
    );
    CREATE TABLE IF NOT EXISTS known_calendars (
        owner TEXT NOT NULL,
        calendar TEXT NOT NULL,
        PRIMARY KEY (owner, calendar)
    );
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterable[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(self.SCHEMA)
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('journal_id', ?)", (secrets.token_hex(8),))
            conn.commit()
            yield conn
        finally:
            conn.close()

    @staticmethod
    def journal_id(conn: sqlite3.Connection) -> str:
        return str(conn.execute("SELECT value FROM meta WHERE key = 'journal_id'").fetchone()[0])

    @staticmethod
    def current_seq(conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT COALESCE(MAX(seq), 0) FROM changes").fetchone()[0])

    def all_scopes(self, conn: sqlite3.Connection, owner: str | None = None) -> list[tuple[str, str]]:
        scopes: set[tuple[str, str]] = set()
        if COLLECTION_ROOT_DIR.exists():
            for owner_dir in COLLECTION_ROOT_DIR.iterdir():
                if not owner_dir.is_dir() or (owner and owner_dir.name != owner):
                    continue
                for calendar_dir in owner_dir.iterdir():
                    if calendar_dir.is_dir() and is_item_filename(calendar_dir.name):
                        scopes.add((owner_dir.name, calendar_dir.name))
        for row in conn.execute("SELECT DISTINCT owner, calendar FROM state"):
            if not owner or row["owner"] == owner:
                scopes.add((row["owner"], row["calendar"]))
        return sorted(scopes)

    def sync(self, scopes: Iterable[tuple[str, str]] | None, source: str) -> None:
        with self.connect() as conn:
            with conn:
                for owner, calendar in self.all_scopes(conn) if scopes is None else scopes:
                    self._sync_calendar(conn, owner, calendar, source)

    def _record(
        self,
        conn: sqlite3.Connection,
        owner: str,
        calendar: str,
        href: str,
        uid: str | None,
        change_type: str,
        etag: str | None,
        modified_at: str,
        source: str,
    ) -> None:
        conn.execute(
            "INSERT INTO changes(owner, calendar, href, uid, type, etag, modified_at, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (owner, calendar, href, uid, change_type, etag, modified_at, source),
        )

    def _sync_calendar(self, conn: sqlite3.Connection, owner: str, calendar: str, source: str) -> None:
        known = {
            row["href"]: row
            for row in conn.execute("SELECT * FROM state WHERE owner = ? AND calendar = ?", (owner, calendar))
        }
        if not known and not collection_path(owner, calendar).is_dir():
            return
        first_seen = (
            conn.execute("INSERT OR IGNORE INTO known_calendars(owner, calendar) VALUES (?, ?)", (owner, calendar)).rowcount
            == 1
        )
        label = "baseline" if first_seen else source
        now = utc_now_iso()
        seen: set[str] = set()
        for path in iter_collection_files(owner, calendar):
            href = path.name
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            row = known.get(href)
            if row is not None and (row["mtime_ns"], row["size"], row["inode"]) == (
                stat.st_mtime_ns,
                stat.st_size,
                stat.st_ino,
            ):
                seen.add(href)
                continue
            item = read_stored_item(path)
            if item is None:
                continue
            seen.add(href)
            if row is None:
                change_type: str | None = "created"
            elif row["etag"] != item.etag:
                change_type = "updated"
            else:
                change_type = None
            conn.execute(
                "INSERT OR REPLACE INTO state(owner, calendar, href, uid, etag, mtime_ns, size, inode) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (owner, calendar, href, item.uid, item.etag, item.mtime_ns, item.size, item.inode),
            )
            if change_type:
                modified_at = item.last_modified.isoformat() if label in {"external", "baseline"} else now
                self._record(conn, owner, calendar, href, item.uid, change_type, item.etag, modified_at, label)
        for href, row in known.items():
            if href in seen:
                continue
            conn.execute("DELETE FROM state WHERE owner = ? AND calendar = ? AND href = ?", (owner, calendar, href))
            self._record(conn, owner, calendar, href, row["uid"], "deleted", None, now, source)

    def changes(
        self,
        after_seq: int,
        owner: str | None,
        calendar: str | None,
        uid: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], bool, int, str]:
        clauses = ["seq > ?"]
        params: list[Any] = [after_seq]
        for column, value in (("owner", owner), ("calendar", calendar), ("uid", uid)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM changes WHERE {' AND '.join(clauses)} ORDER BY seq LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
            current = self.current_seq(conn)
            journal_id = self.journal_id(conn)
        has_more = len(rows) > limit
        return [dict(row) for row in rows[:limit]], has_more, current, journal_id

    def journal_id_value(self) -> str:
        with self.connect() as conn:
            return self.journal_id(conn)


journal = ChangeJournal(JOURNAL_FILE)


def journal_sync(scopes: Iterable[tuple[str, str]] | None, source: str) -> None:
    try:
        journal.sync(scopes, source)
    except Exception as exc:  # pragma: no cover - the journal must never block a write
        print(f"[journal] sync failed: {exc.__class__.__name__}: {exc}", flush=True)


def encode_token(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_token(token: str, name: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Malformed {name}") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise HTTPException(status_code=400, detail=f"Malformed {name}")
    return payload


def encode_change_token(journal_id: str, seq: int) -> str:
    return encode_token({"v": 1, "j": journal_id, "s": seq})


def decode_change_token(token: str | None, journal_id: str, name: str = "token") -> int:
    if not token:
        return 0
    payload = decode_token(token, name)
    seq = payload.get("s")
    if not isinstance(seq, int) or seq < 0:
        raise HTTPException(status_code=400, detail=f"Malformed {name}")
    if payload.get("j") != journal_id:
        raise HTTPException(status_code=410, detail=f"{name} belongs to a different change journal; start a full resync")
    return seq


def change_entry(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in ("seq", "owner", "calendar", "uid", "href", "type", "etag", "modified_at", "source")}


# ---------------------------------------------------------------------------
# Event listing
# ---------------------------------------------------------------------------


def parse_query_datetime(value: str | None, name: str) -> datetime | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    # An unencoded "+" in a query string arrives as a space.
    text = re.sub(r"^(\S+T[\d:.]+) (\d{2}:?\d{2})$", r"\1+\2", text)
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{name} must be an ISO 8601 date-time") from exc
    if parsed.tzinfo is None:
        raise HTTPException(status_code=400, detail=f"{name} must include a timezone offset, e.g. Z or +01:00")
    return parsed.astimezone(timezone.utc)


def ical_value_iso(value: Any) -> str | None:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return None


def canonical_rrule(component: Any) -> str | None:
    rules = component.get("RRULE")
    if rules is None:
        return None
    rule = rules[0] if isinstance(rules, list) else rules
    parts = [part for part in rule.to_ical().decode("utf-8").split(";") if part]
    freq = [part for part in parts if part.upper().startswith("FREQ=")]
    rest = sorted((part for part in parts if not part.upper().startswith("FREQ=")), key=lambda part: part.split("=", 1)[0])
    return ";".join(freq + rest)


def component_tzid(component: Any) -> str | None:
    prop = component.get("DTSTART")
    if prop is None:
        return None
    tzid = getattr(prop, "params", {}).get("TZID")
    if tzid:
        return str(tzid)
    value = getattr(prop, "dt", None)
    if isinstance(value, datetime) and value.tzinfo is not None:
        return getattr(value.tzinfo, "key", None) or value.tzname()
    return None


def component_text(component: Any, name: str) -> str | None:
    value = component.get(name)
    if value is None:
        return None
    if isinstance(value, list):
        value = value[0]
    return str(value)


def component_matches(component: Any, needle: str) -> bool:
    for name in ("SUMMARY", "LOCATION", "DESCRIPTION"):
        text = component_text(component, name)
        if text and needle in text.casefold():
            return True
    return False


def epoch_microseconds(value: datetime | None) -> int:
    if value is None:
        return -(2**62)
    return (value - datetime(1970, 1, 1, tzinfo=timezone.utc)) // timedelta(microseconds=1)


def event_summary(
    item: StoredItem,
    component: Any,
    bounds: Bounds,
    *,
    uid: str,
    rrule: str | None,
    is_recurring: bool,
    recurrence_id: str | None,
    is_override: bool,
) -> dict[str, Any]:
    status = component_text(component, "STATUS")
    return {
        "uid": uid,
        "href": item.href,
        "summary": component_text(component, "SUMMARY"),
        "location": component_text(component, "LOCATION"),
        "status": status.upper() if status else None,
        "dtstart": ical_value_iso(bounds.start),
        "dtend": ical_value_iso(bounds.end),
        "start_utc": bounds.start_utc.isoformat() if bounds.start_utc else None,
        "end_utc": bounds.end_utc.isoformat() if bounds.end_utc else None,
        "all_day": bounds.all_day,
        "tzid": component_tzid(component),
        "rrule": rrule,
        "is_recurring": is_recurring,
        "recurrence_id": recurrence_id,
        "is_override": is_override,
        "etag": item.etag,
        "last_modified": item.last_modified.isoformat(),
        "attachment_count": len(attach_values(component)),
    }


def collect_event_entries(
    owner: str,
    calendar: str,
    *,
    range_from: datetime | None,
    range_to: datetime | None,
    expand: bool,
    query: str | None,
    tz: Any,
) -> tuple[list[tuple[tuple[Any, ...], dict[str, Any]]], list[dict[str, str]]]:
    entries: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    invalid: list[dict[str, str]] = []
    budget = [MAX_EXPANDED_INSTANCES]
    needle = query.casefold() if query else None
    windowed = range_from is not None or range_to is not None
    for path in iter_collection_files(owner, calendar):
        item = read_stored_item(path)
        if item is None:
            continue
        if item.calendar is None:
            invalid.append({"href": item.href, "error": item.error or "Invalid iCalendar data"})
            continue
        events = list(item.calendar.walk("VEVENT"))
        if not events:
            continue
        master = master_event(item.calendar)
        uid = str(master.get("UID", "")).strip() or Path(item.href).stem
        recurring = is_recurring_calendar(item.calendar)
        rrule = canonical_rrule(master)
        try:
            if expand and (recurring or len(events) > 1):
                overrides = {
                    epoch_microseconds(to_utc(decoded_or_none(event, "RECURRENCE-ID"), tz))
                    for event in events
                    if event.get("RECURRENCE-ID") is not None
                }
                for occurrence, bounds in iter_occurrences(item.calendar, range_from, range_to, tz, budget):
                    if needle and not component_matches(occurrence, needle):
                        continue
                    recurrence_value = decoded_or_none(occurrence, "RECURRENCE-ID")
                    recurrence_utc = to_utc(recurrence_value, tz)
                    summary = event_summary(
                        item,
                        occurrence,
                        bounds,
                        uid=uid,
                        rrule=rrule,
                        is_recurring=recurring,
                        recurrence_id=ical_value_iso(recurrence_value),
                        is_override=epoch_microseconds(recurrence_utc) in overrides,
                    )
                    key = (epoch_microseconds(bounds.start_utc), uid, epoch_microseconds(recurrence_utc), item.href)
                    entries.append((key, summary))
                continue
            if needle and not any(component_matches(event, needle) for event in events):
                continue
            bounds = component_bounds(master, tz)
            if windowed:
                if recurring or len(events) > 1:
                    if not series_overlaps(item.calendar, range_from, range_to, tz, budget):
                        continue
                elif not overlaps(bounds, range_from, range_to):
                    continue
            recurrence_value = decoded_or_none(master, "RECURRENCE-ID")
            summary = event_summary(
                item,
                master,
                bounds,
                uid=uid,
                rrule=rrule,
                is_recurring=recurring,
                recurrence_id=ical_value_iso(recurrence_value),
                is_override=recurrence_value is not None,
            )
            key = (epoch_microseconds(bounds.start_utc), uid, epoch_microseconds(None), item.href)
            entries.append((key, summary))
        except HTTPException:
            raise
        except Exception as exc:
            invalid.append({"href": item.href, "error": f"Recurrence expansion failed: {exc}"[:300]})
    entries.sort(key=lambda entry: entry[0])
    return entries, invalid


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


def load_attachment_index() -> list[dict[str, Any]]:
    if not ATTACHMENT_INDEX_FILE.exists():
        return []
    return json.loads(ATTACHMENT_INDEX_FILE.read_text(encoding="utf-8"))


def save_attachment_index(records: list[dict[str, Any]]) -> None:
    atomic_write_text(
        ATTACHMENT_INDEX_FILE,
        json.dumps(records, indent=2, sort_keys=True) + "\n",
    )


def public_base_url(request: Request) -> str:
    configured = public_base_url_from_env()
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def attachment_static_path(owner: str, calendar: str, uid: str, stored_name: str) -> str:
    parts = [quote(part, safe="") for part in (owner, calendar, uid, stored_name)]
    return "/attachments/" + "/".join(parts)


def attachment_file(record: dict[str, Any]) -> Path | None:
    path = (ATTACHMENTS_DIR / str(record.get("relative_path", ""))).resolve()
    if not path.is_relative_to(ATTACHMENTS_DIR.resolve()) or path == ATTACHMENTS_DIR.resolve():
        return None
    return path


def event_attachment_records(owner: str, calendar: str, uid: str) -> list[dict[str, Any]]:
    return [
        record
        for record in load_attachment_index()
        if record.get("owner") == owner and record.get("calendar") == calendar and record.get("uid") == uid
    ]


def list_event_attachments(owner: str, calendar: str, uid: str) -> dict[str, Any]:
    found = read_event(owner, calendar, uid)
    if found is None:
        raise HTTPException(status_code=404, detail="Event not found")
    _path, data, _stat = found
    try:
        parsed = Calendar.from_ical(data)
        attach_items = [item for event in parsed.walk("VEVENT") for item in attach_values(event)]
    except Exception:
        attach_items = []
    referenced = {str(item) for item in attach_items}
    attachments: list[dict[str, Any]] = []
    managed_uris: set[str] = set()
    for record in event_attachment_records(owner, calendar, uid):
        path = attachment_file(record)
        present = bool(path and path.is_file())
        managed_uris.add(str(record.get("uri", "")))
        attachments.append(
            {
                "id": record.get("id"),
                "filename": record.get("filename"),
                "content_type": record.get("content_type"),
                "size": path.stat().st_size if present and path else None,
                "created_at": record.get("created_at"),
                "uri": record.get("uri"),
                "managed": True,
                "inline": False,
                "in_event": str(record.get("uri", "")) in referenced,
                "file_present": present,
            }
        )
    for item in attach_items:
        params = getattr(item, "params", {}) or {}
        inline = str(params.get("VALUE", "")).upper() == "BINARY" or str(params.get("ENCODING", "")).upper() == "BASE64"
        if not inline and str(item) in managed_uris:
            continue
        size: int | None = None
        if inline:
            try:
                size = len(base64.b64decode(str(item), validate=False))
            except Exception:
                size = None
        attachments.append(
            {
                "id": None,
                "filename": params.get("FILENAME") or params.get("X-FILENAME"),
                "content_type": params.get("FMTTYPE"),
                "size": size,
                "created_at": None,
                "uri": None if inline else str(item),
                "managed": False,
                "inline": inline,
                "in_event": True,
                "file_present": None,
            }
        )
    return {"owner": owner, "calendar": calendar, "uid": uid, "etag": compute_etag(data), "attachments": attachments}


def find_attachment_record(owner: str, calendar: str, uid: str, attachment_id: str) -> dict[str, Any]:
    for record in event_attachment_records(owner, calendar, uid):
        if record.get("id") == attachment_id:
            return record
    raise HTTPException(status_code=404, detail="Attachment not found")


def delete_event_attachment(owner: str, calendar: str, uid: str, attachment_id: str, if_match: str | None) -> str:
    """Remove a managed attachment and its ATTACH reference. Returns the new event ETag."""
    with storage_lock():
        found = read_event(owner, calendar, uid)
        if found is None:
            raise HTTPException(status_code=404, detail="Event not found")
        path, data, _stat = found
        check_write_preconditions(compute_etag(data), if_match, None)
        record = find_attachment_record(owner, calendar, uid, attachment_id)
        updated = remove_attachment_uris_from_ics(data, {str(record.get("uri", ""))})
        if updated is not None:
            # commit_writes() reconciles the index and removes the now unreferenced file.
            commit_writes([PlannedWrite(owner, calendar, uid, path, updated)], "attachment")
            data = updated
        records = load_attachment_index()
        remaining = [item for item in records if item.get("id") != attachment_id]
        if len(remaining) != len(records):
            target = attachment_file(record)
            if target:
                delete_file_quietly(target)
                delete_empty_parents(target)
            save_attachment_index(remaining)
    return compute_etag(data)


async def store_uploaded_attachment(
    request: Request,
    owner: str,
    calendar_name: str,
    uid: str,
    upload: UploadFile,
) -> tuple[dict[str, Any], str]:
    """Store an upload and reference it from the event. Returns (record, new event ETag)."""
    validate_segment_for_startup(owner, "owner")
    validate_segment_for_startup(calendar_name, "calendar")
    validate_segment_for_startup(uid, "uid")
    load_event(owner, calendar_name, uid)

    original_name = safe_filename(upload.filename or "attachment.bin")
    attachment_id = secrets.token_urlsafe(18)
    stored_name = f"{attachment_id}-{original_name}"
    relative_path = Path(owner) / calendar_name / uid / stored_name
    target = ATTACHMENTS_DIR / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)

    max_attachment_bytes = parse_positive_int_env("MAX_ATTACHMENT_BYTES", DEFAULT_MAX_ATTACHMENT_BYTES)
    written = 0
    with target.open("wb") as handle:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > max_attachment_bytes:
                handle.close()
                delete_file_quietly(target)
                raise HTTPException(
                    status_code=413,
                    detail=f"Attachment too large. Limit is {max_attachment_bytes} bytes.",
                )
            handle.write(chunk)

    static_path = attachment_static_path(owner, calendar_name, uid, stored_name)
    public_uri = f"{public_base_url(request)}{static_path}"
    now = datetime.now(timezone.utc).isoformat()
    content_type = safe_content_type(upload.content_type)
    try:
        with storage_lock():
            found = read_event(owner, calendar_name, uid)
            if found is None:
                raise HTTPException(status_code=404, detail="Event not found")
            updated = add_attachment_to_ics(found[1], public_uri, content_type, original_name)
            commit_writes([PlannedWrite(owner, calendar_name, uid, found[0], updated)], "attachment")
            event_end = event_end_from_ics(updated)
            records = load_attachment_index()
            record = {
                "id": attachment_id,
                "owner": owner,
                "calendar": calendar_name,
                "uid": uid,
                "filename": original_name,
                "content_type": content_type,
                "relative_path": relative_path.as_posix(),
                "uri": public_uri,
                "created_at": now,
                "event_end": event_end.isoformat() if event_end else None,
            }
            records.append(record)
            save_attachment_index(records)
    except BaseException:
        delete_file_quietly(target)
        delete_empty_parents(target)
        raise
    return record, compute_etag(updated)


def delete_file_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def delete_empty_parents(path: Path) -> None:
    current = path.parent
    while current != ATTACHMENTS_DIR and ATTACHMENTS_DIR in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def delete_managed_attachments(owner: str, calendar: str, uid: str) -> None:
    records = load_attachment_index()
    kept: list[dict[str, Any]] = []
    for record in records:
        if record.get("owner") == owner and record.get("calendar") == calendar and record.get("uid") == uid:
            path = (ATTACHMENTS_DIR / str(record.get("relative_path", ""))).resolve()
            if path.is_relative_to(ATTACHMENTS_DIR):
                delete_file_quietly(path)
                delete_empty_parents(path)
        else:
            kept.append(record)
    save_attachment_index(kept)


def delete_managed_attachments_for_calendar(owner: str, calendar: str) -> int:
    records = load_attachment_index()
    kept: list[dict[str, Any]] = []
    deleted = 0
    for record in records:
        if record.get("owner") == owner and record.get("calendar") == calendar:
            path = attachment_file(record)
            if path:
                delete_file_quietly(path)
                delete_empty_parents(path)
            deleted += 1
        else:
            kept.append(record)
    save_attachment_index(kept)
    return deleted


def reconcile_managed_attachments(owner: str, calendar: str, uid: str, allowed_uris: set[str]) -> None:
    records = load_attachment_index()
    kept: list[dict[str, Any]] = []
    for record in records:
        is_target = (
            record.get("owner") == owner
            and record.get("calendar") == calendar
            and record.get("uid") == uid
        )
        if is_target and record.get("uri") not in allowed_uris:
            path = (ATTACHMENTS_DIR / str(record.get("relative_path", ""))).resolve()
            if path.is_relative_to(ATTACHMENTS_DIR):
                delete_file_quietly(path)
                delete_empty_parents(path)
            continue
        kept.append(record)
    save_attachment_index(kept)


def cleanup_attachments(ttl_days: int, dry_run: bool = False) -> dict[str, Any]:
    """Delete managed attachments whose event is over or that are older than the TTL.

    An attachment expires when the event (for a series: its last instance) has
    ended, when its event no longer exists, or when it is older than ttl_days.
    The file, its index record, and the ATTACH reference in the event are
    removed together. Running it again is a no-op until more attachments expire.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=ttl_days)
    with storage_lock():
        records = load_attachment_index()
        kept: list[dict[str, Any]] = []
        expired: list[tuple[dict[str, Any], str]] = []
        event_state: dict[tuple[str, str, str], tuple[Path | None, datetime | None]] = {}
        for record in records:
            owner = str(record.get("owner", ""))
            calendar_name = str(record.get("calendar", ""))
            uid = str(record.get("uid", ""))
            created_at = datetime.fromisoformat(str(record["created_at"]))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            key = (owner, calendar_name, uid)
            if key not in event_state:
                segments_ok = all(SEGMENT_RE.fullmatch(part) for part in key)
                event_file = find_event_path(owner, calendar_name, uid) if segments_ok else None
                event_state[key] = (
                    event_file,
                    event_end_from_ics(event_file.read_bytes()) if event_file else None,
                )
            event_file, event_end = event_state[key]
            if event_file is None:
                reason = "event_missing"
            elif event_end is not None and event_end <= now:
                reason = "event_ended"
            elif created_at <= cutoff:
                reason = "ttl_expired"
            else:
                reason = ""
            if reason:
                expired.append((record, reason))
            else:
                if event_end:
                    record["event_end"] = event_end.isoformat()
                kept.append(record)
        if dry_run:
            return {
                "dry_run": True,
                "deleted": len(expired),
                "remaining": len(kept),
                "attachments": [
                    {
                        "id": record.get("id"),
                        "owner": record.get("owner"),
                        "calendar": record.get("calendar"),
                        "uid": record.get("uid"),
                        "filename": record.get("filename"),
                        "reason": reason,
                    }
                    for record, reason in expired
                ],
            }
        stale_by_event: dict[tuple[str, str, str], set[str]] = {}
        for record, _reason in expired:
            path = attachment_file(record)
            if path:
                delete_file_quietly(path)
                delete_empty_parents(path)
            key = (str(record.get("owner", "")), str(record.get("calendar", "")), str(record.get("uid", "")))
            stale_by_event.setdefault(key, set()).add(str(record.get("uri", "")))
        save_attachment_index(kept)
        writes: list[PlannedWrite] = []
        for (owner, calendar_name, uid), stale_uris in stale_by_event.items():
            event_file = event_state[(owner, calendar_name, uid)][0]
            if event_file is None or not event_file.exists():
                continue
            updated = remove_attachment_uris_from_ics(event_file.read_bytes(), stale_uris)
            if updated is not None:
                writes.append(PlannedWrite(owner, calendar_name, uid, event_file, updated))
        commit_writes(writes, "cleanup")
    return {"deleted": len(expired), "remaining": len(kept)}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

CONTENT_LINE_RE = re.compile(
    r'^[A-Za-z0-9-]+'
    r'(?:;[A-Za-z0-9-]+=(?:"[^"]*"|[^";:,]*)(?:,(?:"[^"]*"|[^";:,]*))*)*'
    r':'
)


def unfold_content_lines(raw_lines: list[str]) -> list[tuple[int, str]]:
    logical: list[tuple[int, str]] = []
    for number, line in enumerate(raw_lines, start=1):
        if line[:1] in (" ", "\t") and logical:
            logical[-1] = (logical[-1][0], logical[-1][1] + line[1:])
        else:
            logical.append((number, line))
    return logical


def as_naive_or_aware(value: Any, reference: Any) -> Any:
    """Bring a DATE/DATE-TIME into the shape dateutil expects for `reference`."""
    if not isinstance(value, datetime):
        value = datetime.combine(value, datetime_time.min)
    if isinstance(reference, datetime) and reference.tzinfo is not None and value.tzinfo is None:
        value = value.replace(tzinfo=reference.tzinfo)
    return value


def tzid_references(component: Any) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for sub in component.walk():
        for name, value in sub.items():
            for item in value if isinstance(value, list) else [value]:
                tzid = (getattr(item, "params", None) or {}).get("TZID")
                if tzid:
                    found.append((name, str(tzid)))
    return found


def validate_ics(data: bytes, expected_uid: str | None = None) -> dict[str, Any]:
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    def error(code: str, message: str, line: int | None = None) -> None:
        errors.append({"code": code, "message": message, "line": line})

    def warning(code: str, message: str, line: int | None = None) -> None:
        warnings.append({"code": code, "message": message, "line": line})

    def result(uid: str | None = None) -> dict[str, Any]:
        return {"valid": not errors, "uid": uid, "errors": errors, "warnings": warnings}

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        error("encoding", f"ICS must be UTF-8 ({exc.reason} at byte {exc.start})")
        return result()
    if text.startswith("﻿"):
        warning("byte_order_mark", "ICS starts with a byte order mark")
        text = text[1:]
    raw_lines = re.split(r"\r\n|\n|\r", text)
    if re.search(r"(?<!\r)\n|\r(?!\n)", text):
        warning("line_endings", "Lines should end with CRLF")
    long_lines = sum(1 for line in raw_lines if len(line.encode("utf-8")) > 75)
    if long_lines:
        warning("line_length", f"{long_lines} line(s) exceed 75 octets and should be folded")

    stack: list[tuple[str, int]] = []
    seen_root = False
    for number, line in unfold_content_lines(raw_lines):
        if not line.strip():
            continue
        if not CONTENT_LINE_RE.match(line):
            error("malformed_line", f"Not a valid content line: {line[:60]!r}", number)
            continue
        name, value = line.split(":", 1)
        name = name.split(";", 1)[0].upper()
        if name == "BEGIN":
            component = value.strip().upper()
            if not stack and (seen_root or component != "VCALENDAR"):
                error("structure", "The ICS must contain exactly one top-level VCALENDAR", number)
            seen_root = True
            stack.append((component, number))
        elif name == "END":
            component = value.strip().upper()
            if not stack or stack[-1][0] != component:
                opened = stack[-1][0] if stack else "nothing"
                error("unbalanced_component", f"END:{component} does not close {opened}", number)
            else:
                stack.pop()
        elif not stack:
            error("structure", f"{name} appears outside of any component", number)
    for component, number in stack:
        error("unbalanced_component", f"BEGIN:{component} is never closed", number)
    if not seen_root:
        error("structure", "The ICS must start with BEGIN:VCALENDAR")

    try:
        calendar = Calendar.from_ical(text)
    except Exception as exc:
        error("parse_error", f"Invalid iCalendar data: {exc}")
        return result()
    for problem in property_parse_errors(calendar):
        error("invalid_property", problem)
    if "VERSION" not in calendar:
        warning("missing_version", "VCALENDAR has no VERSION")
    if "PRODID" not in calendar:
        warning("missing_prodid", "VCALENDAR has no PRODID")
    if "METHOD" in calendar:
        warning("method_present", "CalDAV resources must not contain METHOD (RFC 4791 section 4.1)")

    events = list(calendar.walk("VEVENT"))
    if not events:
        error("missing_vevent", "ICS must contain a VEVENT")
        return result()
    for other in calendar.subcomponents:
        if other.name in ("VTODO", "VJOURNAL"):
            error("mixed_components", f"{other.name} cannot be stored together with a VEVENT")
    uids = {str(event.get("UID", "")).strip() for event in events} - {""}
    uid = next(iter(uids)) if len(uids) == 1 else None
    if len(uids) > 1:
        error("multiple_uids", "All VEVENTs in one resource must share a UID")
    masters = [event for event in events if event.get("RECURRENCE-ID") is None]
    if len(masters) > 1:
        error("multiple_masters", "Only one VEVENT may omit RECURRENCE-ID")
    recurrence_ids = [event["RECURRENCE-ID"].to_ical() for event in events if event.get("RECURRENCE-ID") is not None]
    if len(set(recurrence_ids)) != len(recurrence_ids):
        error("duplicate_recurrence_id", "RECURRENCE-ID values must be unique")
    if uid and not SEGMENT_RE.fullmatch(uid):
        error("uid_not_addressable", f"UID must match {SEGMENT_RE.pattern} to be stored through the management API")
    if expected_uid is not None and uid != expected_uid:
        error("uid_mismatch", f"VEVENT UID {uid!r} does not match expected uid {expected_uid!r}")

    defined_tzids = {str(tz.get("TZID")) for tz in calendar.walk("VTIMEZONE")}
    tz = default_timezone()
    master_rule = None
    for event in events:
        rid = decoded_or_none(event, "RECURRENCE-ID")
        label = "VEVENT" + (f" (RECURRENCE-ID {ical_value_iso(rid)})" if rid is not None else "")
        if not str(event.get("UID", "")).strip():
            error("missing_uid", f"{label} has no UID")
        if event.get("DTSTAMP") is None:
            error("missing_dtstamp", f"{label} has no DTSTAMP")
        for prop, tzid in tzid_references(event):
            if tzid not in defined_tzids:
                error("missing_vtimezone", f"{label} {prop} references TZID {tzid!r} but no VTIMEZONE defines it")
        dtstart = decoded_or_none(event, "DTSTART")
        if not isinstance(dtstart, date):
            error("missing_dtstart", f"{label} has no valid DTSTART")
            continue
        dtend = decoded_or_none(event, "DTEND")
        if event.get("DTEND") is not None and event.get("DURATION") is not None:
            error("dtend_and_duration", f"{label} must not contain both DTEND and DURATION")
        if isinstance(dtend, date):
            if isinstance(dtend, datetime) != isinstance(dtstart, datetime):
                error("dtend_type_mismatch", f"{label} DTEND and DTSTART must both be DATE or both be DATE-TIME")
            else:
                start_utc, end_utc = to_utc(dtstart, tz), to_utc(dtend, tz)
                if start_utc and end_utc and end_utc < start_utc:
                    error("dtend_before_dtstart", f"{label} DTEND is before DTSTART")
                elif start_utc == end_utc:
                    warning("zero_duration", f"{label} DTEND equals DTSTART")

        rules = event.get("RRULE")
        rules = [] if rules is None else rules if isinstance(rules, list) else [rules]
        if len(rules) > 1:
            warning("multiple_rrule", f"{label} has more than one RRULE")
        rule_set = None
        for rule in rules:
            raw_rule = rule.to_ical().decode("utf-8")
            if "COUNT" in rule and "UNTIL" in rule:
                error("invalid_rrule", f"{label} RRULE must not contain both COUNT and UNTIL")
            for until in rule.get("UNTIL", []):
                if isinstance(until, datetime) != isinstance(dtstart, datetime):
                    error("invalid_rrule", f"{label} RRULE UNTIL must have the same value type (DATE or DATE-TIME) as DTSTART")
            try:
                rule_set = dateutil_rrule.rrulestr(raw_rule, dtstart=as_naive_or_aware(dtstart, dtstart), forceset=True)
                if next(iter(rule_set), None) is None:
                    warning("rrule_no_occurrences", f"{label} RRULE produces no occurrences")
            except Exception as exc:
                error("invalid_rrule", f"{label} RRULE cannot be parsed: {exc}")
                rule_set = None
        if event.get("RECURRENCE-ID") is None:
            master_rule = rule_set

        rdates: set[Any] = set()
        for rdate in event.get("RDATE") if isinstance(event.get("RDATE"), list) else [event.get("RDATE")]:
            if rdate is not None:
                rdates.update(as_naive_or_aware(item.dt, dtstart) for item in rdate.dts if isinstance(item.dt, date))
        exdates_props = event.get("EXDATE")
        exdates = [
            item.dt
            for prop in (exdates_props if isinstance(exdates_props, list) else [exdates_props])
            if prop is not None
            for item in prop.dts
        ]
        if exdates and not rules and not rdates:
            warning("exdate_without_recurrence", f"{label} has EXDATE but no RRULE or RDATE")
        frequent = any(str(rule.get("FREQ", [""])[0]).upper() in {"SECONDLY", "MINUTELY"} for rule in rules)
        for value in exdates:
            if not isinstance(value, date):
                continue
            if isinstance(value, datetime) != isinstance(dtstart, datetime):
                error(
                    "exdate_type_mismatch",
                    f"{label} EXDATE {ical_value_iso(value)} is a {'DATE-TIME' if isinstance(value, datetime) else 'DATE'} "
                    f"but DTSTART is a {'DATE-TIME' if isinstance(dtstart, datetime) else 'DATE'}",
                )
                continue
            if rule_set is None or frequent:
                continue
            candidate = as_naive_or_aware(value, dtstart)
            try:
                if (isinstance(candidate, datetime) and isinstance(dtstart, datetime)
                        and (candidate.tzinfo is None) != (dtstart.tzinfo is None)):
                    raise TypeError("floating/zoned mismatch")
                matched = candidate in rdates or bool(
                    rule_set.between(candidate - timedelta(seconds=1), candidate + timedelta(seconds=1), inc=True)
                )
            except TypeError:
                matched = False
            if not matched:
                error(
                    "exdate_not_in_recurrence",
                    f"{label} EXDATE {ical_value_iso(value)} does not match any occurrence; check its time and TZID against DTSTART",
                )

    if master_rule is not None:
        master_start = decoded_or_none(masters[0], "DTSTART") if masters else None
        for event in events:
            rid = decoded_or_none(event, "RECURRENCE-ID")
            if rid is None or not isinstance(master_start, date):
                continue
            candidate = as_naive_or_aware(rid, master_start)
            try:
                matched = bool(master_rule.between(candidate - timedelta(seconds=1), candidate + timedelta(seconds=1), inc=True))
            except TypeError:
                matched = True
            if not matched:
                warning("recurrence_id_not_in_series", f"RECURRENCE-ID {ical_value_iso(rid)} does not match an occurrence of the series")

    if not errors:
        load_error = caldav_load_error(text.encode("utf-8"))
        if load_error:
            error("caldav_incompatible", f"CalDAV clients could not load this event: {load_error}")
    return result(uid)


# ---------------------------------------------------------------------------
# Batch and import
# ---------------------------------------------------------------------------


@dataclass
class BatchOp:
    op: str
    owner: str
    calendar: str
    uid: str
    ics: bytes | None = None
    if_match: str | None = None
    if_none_match: str | None = None
    skip_existing: bool = False


def execute_batch(ops: list[BatchOp], *, dry_run: bool, source: str, max_ics_bytes: int) -> tuple[int, dict[str, Any]]:
    """Validate, check preconditions, then apply every operation or none of them."""
    results: list[dict[str, Any]] = []
    prepared: list[bytes | None] = []
    failed_index: int | None = None

    def fail(index: int, exc: HTTPException) -> None:
        nonlocal failed_index
        results[index].update(status=exc.status_code, outcome="failed", error=str(exc.detail))
        if failed_index is None:
            failed_index = index

    for index, op in enumerate(ops):
        results.append(
            {"index": index, "op": op.op, "owner": op.owner, "calendar": op.calendar, "uid": op.uid,
             "status": None, "outcome": "not_applied"}
        )
        prepared.append(None)
        try:
            validate_segment(op.owner, "owner")
            validate_segment(op.calendar, "calendar")
            validate_segment(op.uid, "uid")
            if op.op == "put":
                if not op.ics:
                    raise HTTPException(status_code=400, detail="put operations require ics")
                if len(op.ics) > max_ics_bytes:
                    raise HTTPException(status_code=413, detail=f"ICS too large. Limit is {max_ics_bytes} bytes.")
                prepared[index] = prepare_event_body(op.ics, op.uid)
            elif op.ics is not None:
                raise HTTPException(status_code=400, detail="delete operations must not include ics")
        except HTTPException as exc:
            fail(index, exc)

    if failed_index is None:
        with storage_lock():
            state: dict[tuple[str, str, str], dict[str, Any]] = {}
            for index, op in enumerate(ops):
                key = (op.owner, op.calendar, op.uid)
                if key not in state:
                    found = read_event(*key)
                    current = found[1] if found else None
                    state[key] = {
                        "path": found[0] if found else event_path(*key),
                        "original": current,
                        "data": current,
                    }
                entry = state[key]
                current_etag = compute_etag(entry["data"]) if entry["data"] is not None else None
                if op.skip_existing and entry["data"] is not None:
                    results[index].update(status=200, outcome="skipped", etag=current_etag, created=False)
                    continue
                try:
                    check_write_preconditions(current_etag, op.if_match, op.if_none_match)
                except HTTPException as exc:
                    fail(index, exc)
                    break
                outcome = "would_apply" if dry_run else "applied"
                if op.op == "put":
                    new_data = prepared[index]
                    results[index].update(
                        status=200 if entry["data"] is not None else 201,
                        outcome=outcome,
                        etag=compute_etag(new_data),
                        created=entry["data"] is None,
                    )
                    entry["data"] = new_data
                else:
                    results[index].update(status=200, outcome=outcome, deleted=entry["data"] is not None)
                    entry["data"] = None
            if failed_index is None and not dry_run:
                writes = [
                    PlannedWrite(*key, path=entry["path"], data=entry["data"])
                    for key, entry in state.items()
                    if entry["data"] != entry["original"]
                ]
                try:
                    commit_writes(writes, source)
                except HTTPException:
                    raise
                except Exception as exc:
                    print(f"[batch] write failed and was rolled back: {exc.__class__.__name__}", flush=True)
                    for result in results:
                        if result["outcome"] == "applied":
                            result.update(outcome="not_applied")
                    body = {
                        "atomic": True,
                        "applied": False,
                        "dry_run": dry_run,
                        "failed_index": None,
                        "results": results,
                        "error": "Storage write failed; all changes were rolled back",
                    }
                    return 500, body

    if failed_index is not None:
        for result in results:
            if result["outcome"] in {"applied", "would_apply", "skipped"}:
                result["outcome"] = "not_applied"
        status = int(results[failed_index]["status"] or 400)
        return status, {"atomic": True, "applied": False, "dry_run": dry_run, "failed_index": failed_index, "results": results}
    return 200, {"atomic": True, "applied": not dry_run, "dry_run": dry_run, "failed_index": None, "results": results}


def split_calendar_bundle(data: bytes) -> tuple[list[tuple[str, bytes]], list[dict[str, Any]]]:
    """Split a multi-event VCALENDAR into one resource per UID, with the VTIMEZONEs each needs."""
    try:
        bundle = Calendar.from_ical(data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid iCalendar data: {exc}") from exc
    timezones = {str(sub.get("TZID")): sub for sub in bundle.subcomponents if sub.name == "VTIMEZONE"}
    groups: dict[str, list[Any]] = {}
    skipped: list[dict[str, Any]] = []
    for sub in bundle.subcomponents:
        if sub.name == "VTIMEZONE":
            continue
        uid = str(sub.get("UID", "")).strip()
        if sub.name != "VEVENT":
            skipped.append({"component": sub.name, "uid": uid or None, "reason": "only VEVENT components are imported"})
            continue
        if not uid:
            raise HTTPException(status_code=400, detail="Every VEVENT in the bundle must have a UID")
        groups.setdefault(uid, []).append(sub)
    resources: list[tuple[str, bytes]] = []
    for uid, components in groups.items():
        resource = Calendar()
        resource.add("VERSION", "2.0")
        resource.add("PRODID", str(bundle.get("PRODID") or "-//Pocket CalDAV//Import//EN"))
        if bundle.get("CALSCALE") is not None:
            resource.add("CALSCALE", bundle.get("CALSCALE"))
        tzids = {tzid for component in components for _prop, tzid in tzid_references(component)}
        for tzid in sorted(tzids):
            if tzid in timezones:
                resource.add_component(timezones[tzid])
        for component in components:
            resource.add_component(component)
        resources.append((uid, resource.to_ical()))
    return resources, skipped


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def build_export_calendar(owner: str, calendar_name: str) -> tuple[bytes, int, list[str]]:
    """Bundle every resource of a calendar into one VCALENDAR. Returns (ics, event count, skipped hrefs)."""
    props = read_calendar_props(collection_path(owner, calendar_name))
    bundle = Calendar()
    bundle.add("VERSION", "2.0")
    bundle.add("PRODID", "-//Pocket CalDAV//Export//EN")
    bundle.add("X-WR-CALNAME", str(props.get("D:displayname") or calendar_name))
    timezones: dict[str, Any] = {}
    components: list[Any] = []
    uids: set[str] = set()
    skipped: list[str] = []
    for path in iter_collection_files(owner, calendar_name):
        item = read_stored_item(path)
        if item is None:
            continue
        if item.calendar is None:
            skipped.append(item.href)
            continue
        for sub in item.calendar.subcomponents:
            if sub.name == "VTIMEZONE":
                timezones.setdefault(str(sub.get("TZID")), sub)
            else:
                components.append(sub)
                if sub.name == "VEVENT":
                    uids.add(str(sub.get("UID", "")))
    for tzid in sorted(timezones):
        bundle.add_component(timezones[tzid])
    for component in components:
        bundle.add_component(component)
    return bundle.to_ical(), len(uids), skipped


def build_export_zip(owner: str | None, calendar_name: str | None, include_attachments: bool) -> tuple[Path, int]:
    """Write a byte-exact backup archive to a temporary file. Returns (path, calendar count)."""
    calendars = [
        item
        for item in list_calendars()
        if (owner is None or item["owner"] == owner) and (calendar_name is None or item["calendar"] == calendar_name)
    ]
    fd, tmp_name = tempfile.mkstemp(prefix="pocket-caldav-export-", suffix=".zip")
    os.close(fd)
    manifest: dict[str, Any] = {
        "format": "pocket-caldav-export",
        "version": 1,
        "created_at": utc_now_iso(),
        "calendars": [],
        "attachments": [],
    }
    try:
        with zipfile.ZipFile(tmp_name, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for info in calendars:
                base = f"calendars/{info['owner']}/{info['calendar']}"
                directory = collection_path(info["owner"], info["calendar"])
                props_file = directory / ".Radicale.props"
                if props_file.is_file():
                    archive.write(props_file, f"{base}/props.json")
                events = []
                for path in iter_collection_files(info["owner"], info["calendar"]):
                    data = path.read_bytes()
                    item = read_stored_item(path)
                    archive.writestr(f"{base}/events/{path.name}", data)
                    events.append(
                        {
                            "href": path.name,
                            "uid": item.uid if item else None,
                            "etag": compute_etag(data),
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "size": len(data),
                        }
                    )
                bundle, _count, _skipped = build_export_calendar(info["owner"], info["calendar"])
                archive.writestr(f"{base}/calendar.ics", bundle)
                manifest["calendars"].append({**info, "events": events})
            selected = {(info["owner"], info["calendar"]) for info in calendars}
            for record in load_attachment_index():
                if (record.get("owner"), record.get("calendar")) not in selected:
                    continue
                path = attachment_file(record)
                entry = {key: record.get(key) for key in ("id", "owner", "calendar", "uid", "filename", "content_type", "uri", "created_at")}
                entry["included"] = False
                if include_attachments and path and path.is_file():
                    archive_name = f"attachments/{record.get('relative_path')}"
                    digest = hashlib.sha256()
                    with path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    archive.write(path, archive_name)
                    entry.update(included=True, path=archive_name, sha256=digest.hexdigest(), size=path.stat().st_size)
                manifest["attachments"].append(entry)
            archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
    except BaseException:
        delete_file_quietly(Path(tmp_name))
        raise
    return Path(tmp_name), len(calendars)


def delete_calendar(owner: str, calendar: str, force: bool) -> dict[str, Any]:
    with storage_lock():
        directory = collection_path(owner, calendar)
        if not directory.is_dir():
            raise HTTPException(status_code=404, detail="Calendar not found")
        files = iter_collection_files(owner, calendar)
        if files and not force:
            raise HTTPException(
                status_code=409,
                detail=f"Calendar contains {len(files)} event(s); export it first, then retry with ?force=true",
            )
        journal_sync([(owner, calendar)], "external")
        attachments_deleted = delete_managed_attachments_for_calendar(owner, calendar)
        remove_tree_under(COLLECTION_ROOT_DIR, directory)
        with _stored_item_cache_lock:
            for path in files:
                _stored_item_cache.pop(str(path), None)
        journal_sync([(owner, calendar)], "calendar-delete")
    return {
        "deleted": True,
        "owner": owner,
        "calendar": calendar,
        "events_deleted": len(files),
        "attachments_deleted": attachments_deleted,
    }


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = load_or_create_api_key()
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key")


def prompt_text(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or (default or "")


def prompt_yes_no(label: str, default: bool = False) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        answer = input(f"{label} [{default_text}]: ").strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer y or n.")


def prompt_positive_int(label: str, default: int, minimum: int = 1) -> int:
    while True:
        raw = prompt_text(label, str(default))
        try:
            value = int(raw)
        except ValueError:
            print("Please enter a whole number.")
            continue
        if value < minimum:
            print(f"Please enter a value >= {minimum}.")
            continue
        return value


def prompt_segment(label: str, default: str | None = None) -> str:
    while True:
        value = prompt_text(label, default)
        try:
            validate_segment_for_startup(value, label)
        except ValueError as exc:
            print(exc)
            continue
        return value


def prompt_password() -> str:
    generated = secrets.token_urlsafe(18)
    if prompt_yes_no("Generate a random password", True):
        return generated
    while True:
        first = getpass.getpass("Password: ")
        second = getpass.getpass("Confirm password: ")
        if first and first == second:
            return first
        print("Passwords must be non-empty and match.")


def print_cli_status() -> None:
    users = load_or_create_users()
    settings = load_settings()
    calendars = list_calendars()
    print("")
    print(f"Data directory: {DATA_DIR}")
    print(f"Users: {', '.join(sorted(users)) if users else '(none)'}")
    print("Calendars:")
    if calendars:
        for item in calendars:
            print(f"  {item['path']} ({item['display_name']})")
    else:
        print("  (none)")
    print(
        "Attachment cleanup: "
        f"every {settings['cleanup_interval_seconds']}s, TTL {settings['attachment_ttl_days']}d"
    )
    print(f"Management API key file: {API_KEY_FILE}")
    print("")


def cli_add_or_update_user() -> None:
    users = load_or_create_users()
    username = prompt_segment("Username")
    replacing = username in users
    password = prompt_password()
    users[username] = password
    write_users(users)
    print(f"{'Updated' if replacing else 'Created'} user: {username}")
    print(f"Password: {password}")


def cli_delete_user() -> None:
    users = load_or_create_users()
    if not users:
        print("No users exist.")
        return
    print(f"Existing users: {', '.join(sorted(users))}")
    username = prompt_segment("Username to delete")
    if username not in users:
        print(f"User does not exist: {username}")
        return
    env_users = parse_users_from_env(os.environ.get("CALDAV_USERS"))
    if username in env_users:
        print("This user is also present in CALDAV_USERS and will come back while that environment variable is set.")

    print("")
    print("User data must be handled before the account is removed:")
    print("1. Migrate calendars and managed attachments to another user")
    print("2. Permanently delete calendars and managed attachments")
    print("0. Cancel")
    action = input("Choose: ").strip()
    if action == "0":
        print("Canceled.")
        return
    if action == "1":
        targets = sorted(user for user in users if user != username)
        if not targets:
            print("No other users exist. Create the destination user first.")
            return
        print(f"Available target users: {', '.join(targets)}")
        target_user = prompt_segment("Target username")
        if target_user == username:
            print("Target user must be different from the deleted user.")
            return
        if target_user not in users:
            print(f"Target user does not exist: {target_user}")
            return
        if not prompt_yes_no(f"Migrate {username}'s data to {target_user} and delete {username}", False):
            print("Canceled.")
            return
        result = delete_user(username, "migrate", target_user)
    elif action == "2":
        if not prompt_yes_no(f"Permanently delete {username}'s account, calendars, and managed attachments", False):
            print("Canceled.")
            return
        result = delete_user(username, "delete")
    else:
        print("Unknown option.")
        return

    print(
        f"Deleted user={result['user_deleted']}, "
        f"mode={result['mode']}, "
        f"calendars_migrated={result['calendars_migrated']}, "
        f"attachments_migrated={result['attachments_migrated']}, "
        f"attachment_uris_rewritten={result['attachment_uris_rewritten']}, "
        f"event_attachment_uris_rewritten={result['event_attachment_uris_rewritten']}, "
        f"deleted_data={result['data_deleted']}, "
        f"deleted_attachments={result['attachments_deleted']}"
    )


def cli_create_calendar() -> None:
    users = load_or_create_users()
    if users:
        print(f"Existing users: {', '.join(sorted(users))}")
    else:
        print("No users exist yet. Create a user first, then create calendars for it.")
        return
    owner = prompt_segment("Owner username")
    if owner not in users and not prompt_yes_no("Owner is not a configured user. Create calendar anyway", False):
        print("Canceled.")
        return
    calendar_name = prompt_segment("Calendar name", "default")
    display_name = prompt_text("Display name", calendar_name)
    create_calendar(owner, calendar_name, display_name)
    print(f"Calendar URL path: /{owner}/{calendar_name}/")


def cli_configure_cleanup() -> None:
    settings = load_settings()
    ttl_days = prompt_positive_int("Attachment TTL days", settings["attachment_ttl_days"], minimum=1)
    interval_seconds = prompt_positive_int(
        "Cleanup interval seconds",
        settings["cleanup_interval_seconds"],
        minimum=60,
    )
    settings = {
        "attachment_ttl_days": ttl_days,
        "cleanup_interval_seconds": interval_seconds,
    }
    atomic_write_text(SETTINGS_FILE, json.dumps(settings, indent=2, sort_keys=True) + "\n")
    print(f"Saved cleanup settings to {SETTINGS_FILE}")
    if "ATTACHMENT_TTL_DAYS" in os.environ or "CLEANUP_INTERVAL_SECONDS" in os.environ:
        print("Environment variables still override these settings when the server starts.")


def cli_run_cleanup() -> None:
    settings = runtime_cleanup_settings()
    result = cleanup_attachments(settings["attachment_ttl_days"])
    print(f"Deleted {result['deleted']} attachment(s); {result['remaining']} remaining.")


def run_cli() -> None:
    ensure_directories()
    load_or_create_users()
    load_or_create_api_key()
    load_settings()
    write_radicale_config()
    actions = {
        "1": ("Show current status", print_cli_status),
        "2": ("Create or update user", cli_add_or_update_user),
        "3": ("Delete user", cli_delete_user),
        "4": ("Create calendar for user", cli_create_calendar),
        "5": ("Configure attachment cleanup", cli_configure_cleanup),
        "6": ("Run attachment cleanup now", cli_run_cleanup),
    }
    while True:
        print("")
        print("CalDAV server setup")
        for key, (label, _handler) in actions.items():
            print(f"{key}. {label}")
        print("0. Exit")
        choice = input("Choose: ").strip()
        if choice == "0":
            return
        action = actions.get(choice)
        if not action:
            print("Unknown option.")
            continue
        print("")
        action[1]()


def startup_banner(users: dict[str, str], settings: dict[str, int], limits: dict[str, int]) -> None:
    print("", flush=True)
    print("Pocket CalDAV is configured.", flush=True)
    print(f"Data directory: {DATA_DIR}", flush=True)
    print(f"Radicale config: {RADICALE_CONFIG_FILE}", flush=True)
    print(
        "Attachment cleanup: "
        f"every {settings['cleanup_interval_seconds']}s, TTL {settings['attachment_ttl_days']}d",
        flush=True,
    )
    print(
        "Request limits: "
        f"ICS {limits['ics']} bytes, attachment {limits['attachment']} bytes, "
        f"default {limits['request']} bytes",
        flush=True,
    )
    api_key_source = "environment" if "CALDAV_API_KEY" in os.environ else str(API_KEY_FILE)
    print(f"Management API key source: {api_key_source}", flush=True)
    if users:
        print("CalDAV users:", flush=True)
        for username in sorted(users):
            print(f"  {username}", flush=True)
    else:
        print("No CalDAV users are configured. Run `python server.py cli` to add one.", flush=True)
    print("", flush=True)


def prepare_runtime() -> tuple[dict[str, str], str, dict[str, int], dict[str, int]]:
    ensure_directories()
    validate_public_origin_policy()
    users = load_or_create_users()
    api_key = load_or_create_api_key()
    settings = runtime_cleanup_settings()
    limits = runtime_request_limits()
    write_radicale_config()
    startup_banner(users, settings, limits)
    return users, api_key, settings, limits


def create_app() -> FastAPI:
    users, api_key, settings, limits = prepare_runtime()
    cleaner = Cleaner(
        interval_seconds=settings["cleanup_interval_seconds"],
        ttl_days=settings["attachment_ttl_days"],
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        journal_sync(None, "external")
        cleanup_attachments(cleaner.ttl_days)
        cleaner.start()
        yield
        cleaner.stop()

    app = FastAPI(
        title="Pocket CalDAV",
        version="1.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(RequestBodyLimitMiddleware, limits=limits)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts())

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/api/bootstrap", dependencies=[Depends(require_api_key)])
    def bootstrap() -> dict[str, Any]:
        return {
            "users": sorted(load_or_create_users().keys()),
            "calendars": list_calendars(),
            "api_key_source": "environment" if "CALDAV_API_KEY" in os.environ else str(API_KEY_FILE),
        }

    @app.post("/api/calendars", dependencies=[Depends(require_api_key)])
    def api_create_calendar(payload: CalendarCreate) -> dict[str, str]:
        owner = validate_segment(payload.owner, "owner")
        calendar_name = validate_segment(payload.calendar, "calendar")
        display_name = payload.display_name or calendar_name
        create_calendar(owner, calendar_name, display_name)
        return {
            "owner": owner,
            "calendar": calendar_name,
            "path": f"/{owner}/{calendar_name}/",
        }

    def require_calendar(owner: str, calendar_name: str) -> tuple[str, str]:
        owner = validate_segment(owner, "owner")
        calendar_name = validate_segment(calendar_name, "calendar")
        if not collection_path(owner, calendar_name).is_dir():
            raise HTTPException(status_code=404, detail="Calendar not found")
        return owner, calendar_name

    def next_link(request: Request, param: str, value: str | None) -> dict[str, str]:
        if not value:
            return {}
        url = request.url.include_query_params(**{param: value})
        return {"Link": f'<{url.path}?{url.query}>; rel="next"'}

    # Registered before /{uid} so that GET .../events lists the calendar. An event
    # whose UID is literally "events" is still reachable by PUT/DELETE and export.
    @app.get(
        "/api/calendars/{owner}/{calendar_name}/events",
        dependencies=[Depends(require_api_key)],
        response_model=EventListResponse,
        responses=error_responses(400, 404, 422),
        summary="List or search events",
    )
    def api_list_events(
        request: Request,
        response: Response,
        owner: str,
        calendar_name: str,
        range_from: str | None = Query(None, alias="from", description="Inclusive lower bound (ISO 8601 with offset)."),
        range_to: str | None = Query(None, alias="to", description="Exclusive upper bound (ISO 8601 with offset)."),
        limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
        cursor: str | None = Query(None, description="next_cursor from the previous page."),
        expand_recurrence: bool = Query(False, description="Return one item per instance instead of one per series. Requires `to`."),
        q: str | None = Query(None, max_length=200, description="Case-insensitive substring match on SUMMARY, LOCATION or DESCRIPTION."),
        tz: str | None = Query(None, description="IANA zone for floating and all-day values. Defaults to DEFAULT_TIMEZONE or UTC."),
    ) -> dict[str, Any]:
        """Events whose time span overlaps [from, to): `start < to AND end > from`.

        Without from/to every event is returned. Recurring series match when any
        instance overlaps. Results are ordered by (start, uid, recurrence-id) and
        paged with an opaque keyset cursor, so a full sweep returns every
        matching item exactly once even if the calendar changes in between.
        """
        owner, calendar_name = require_calendar(owner, calendar_name)
        start = parse_query_datetime(range_from, "from")
        stop = parse_query_datetime(range_to, "to")
        if start and stop and start >= stop:
            raise HTTPException(status_code=400, detail="from must be before to")
        if expand_recurrence and stop is None:
            raise HTTPException(status_code=400, detail="expand_recurrence=true requires `to`")
        zone_name = tz or default_timezone_name()
        zone = resolve_timezone(zone_name)
        fingerprint = hashlib.sha256(
            json.dumps([owner, calendar_name, range_from and start.isoformat(), range_to and stop.isoformat(),
                        expand_recurrence, q, zone_name]).encode("utf-8")
        ).hexdigest()[:16]
        after_key: tuple[Any, ...] | None = None
        if cursor:
            payload = decode_token(cursor, "cursor")
            key = payload.get("k")
            if payload.get("f") != fingerprint or not isinstance(key, list) or len(key) != 4:
                raise HTTPException(status_code=400, detail="cursor does not belong to this query")
            after_key = tuple(key)
        entries, invalid = collect_event_entries(
            owner,
            calendar_name,
            range_from=start,
            range_to=stop,
            expand=expand_recurrence,
            query=q,
            tz=zone,
        )
        remaining = [entry for entry in entries if after_key is None or entry[0] > after_key]
        page = remaining[:limit]
        truncated = len(remaining) > limit
        next_cursor = encode_token({"v": 1, "f": fingerprint, "k": list(page[-1][0])}) if truncated else None
        response.headers.update(next_link(request, "cursor", next_cursor))
        return {
            "items": [summary for _key, summary in page],
            "next_cursor": next_cursor,
            "truncated": truncated,
            "total": len(entries),
            "timezone": zone_name,
            "invalid_items": invalid,
        }

    @app.patch(
        "/api/calendars/{owner}/{calendar_name}",
        dependencies=[Depends(require_api_key)],
        response_model=CalendarInfo,
        responses=error_responses(400, 404),
        summary="Rename a calendar",
    )
    def api_update_calendar(owner: str, calendar_name: str, payload: CalendarUpdate) -> dict[str, str]:
        owner, calendar_name = require_calendar(owner, calendar_name)
        create_calendar(owner, calendar_name, payload.display_name)
        return {
            "owner": owner,
            "calendar": calendar_name,
            "display_name": payload.display_name,
            "path": f"/{owner}/{calendar_name}/",
        }

    @app.delete(
        "/api/calendars/{owner}/{calendar_name}",
        dependencies=[Depends(require_api_key)],
        response_model=CalendarDeleteResult,
        responses=error_responses(400, 404, 409),
        summary="Delete a calendar",
    )
    def api_delete_calendar(
        owner: str,
        calendar_name: str,
        force: bool = Query(False, description="Required to delete a calendar that still contains events. Their managed attachments are deleted too."),
    ) -> dict[str, Any]:
        """Refuses non-empty calendars with 409 unless force=true. Take an export first."""
        owner, calendar_name = require_calendar(owner, calendar_name)
        return delete_calendar(owner, calendar_name, force)

    @app.post(
        "/api/calendars/{owner}/{calendar_name}/import",
        dependencies=[Depends(require_api_key)],
        response_model=BatchResponse,
        responses=error_responses(400, 412, 413),
        openapi_extra=ICS_REQUEST_BODY,
        summary="Import a multi-event VCALENDAR",
    )
    async def api_import(
        request: Request,
        owner: str,
        calendar_name: str,
        mode: Literal["upsert", "create", "skip_existing"] = Query(
            "upsert",
            description="upsert replaces existing UIDs; create fails the whole import (412) if any UID exists; skip_existing leaves existing UIDs untouched.",
        ),
        dry_run: bool = Query(False),
    ) -> JSONResponse:
        """Split the bundle by UID (a series keeps its RECURRENCE-ID overrides) and apply it atomically."""
        owner = validate_segment(owner, "owner")
        calendar_name = validate_segment(calendar_name, "calendar")
        data = await request.body()
        if not data:
            raise HTTPException(status_code=400, detail="Request body must contain text/calendar data")
        resources, skipped = split_calendar_bundle(data)
        if not resources:
            raise HTTPException(status_code=400, detail="The bundle contains no VEVENT")
        if len(resources) > MAX_BATCH_OPERATIONS:
            raise HTTPException(status_code=400, detail=f"At most {MAX_BATCH_OPERATIONS} events per import")
        ops = [
            BatchOp(
                op="put",
                owner=owner,
                calendar=calendar_name,
                uid=uid,
                ics=ics,
                if_none_match="*" if mode == "create" else None,
                skip_existing=mode == "skip_existing",
            )
            for uid, ics in resources
        ]
        status, body = await run_in_threadpool(
            execute_batch, ops, dry_run=dry_run, source="import", max_ics_bytes=limits["ics"]
        )
        body["skipped_components"] = skipped
        return JSONResponse(body, status_code=status)

    @app.put(
        "/api/calendars/{owner}/{calendar_name}/{uid}",
        dependencies=[Depends(require_api_key)],
        responses={
            200: {
                "description": "Stored (or, with dry_run, the normalised event that would be stored).",
                "content": {"application/json": {"schema": PutDryRunResponse.model_json_schema()}},
            },
            **error_responses(400, 409, 412, 413),
        },
        openapi_extra=ICS_REQUEST_BODY,
        summary="Create or replace an event",
    )
    async def api_put_event(
        owner: str,
        calendar_name: str,
        uid: str,
        request: Request,
        dry_run: bool = Query(False, description="Validate and return the normalised event without writing."),
        if_match: str | None = Header(None, description="Only write if the current ETag matches (412 otherwise)."),
        if_none_match: str | None = Header(None, description="Use * to only create (412 if the UID exists)."),
    ) -> JSONResponse:
        """Responds with ETag and Last-Modified headers. The body is unchanged from earlier versions."""
        owner = validate_segment(owner, "owner")
        calendar_name = validate_segment(calendar_name, "calendar")
        uid = validate_segment(uid, "uid")
        data = await request.body()
        if not data:
            raise HTTPException(status_code=400, detail="Request body must contain text/calendar data")
        if len(data) > limits["ics"]:
            raise HTTPException(status_code=413, detail=f"ICS too large. Limit is {limits['ics']} bytes.")
        result = await run_in_threadpool(
            put_event, owner, calendar_name, uid, data, if_match, if_none_match, dry_run
        )
        href = result["path"].name
        headers = {"ETag": result["etag"]}
        if dry_run:
            return JSONResponse(
                {
                    "dry_run": True,
                    "would_create": result["created"],
                    "would_change": result["changed"],
                    "etag": result["etag"],
                    "path": f"/{owner}/{calendar_name}/{href}",
                    "ics": result["data"].decode("utf-8"),
                    "validation": validate_ics(data, uid),
                },
                headers=headers,
            )
        headers["Last-Modified"] = http_date(result["last_modified"])
        return JSONResponse(
            {"owner": owner, "calendar": calendar_name, "uid": uid, "path": f"/{owner}/{calendar_name}/{href}"},
            headers=headers,
        )

    @app.get(
        "/api/calendars/{owner}/{calendar_name}/{uid}",
        dependencies=[Depends(require_api_key)],
        responses={
            200: {"description": "The stored event.", "content": {"text/calendar": {"schema": {"type": "string"}}}},
            304: {"description": "Not modified (If-None-Match matched)."},
            **error_responses(400, 404),
        },
        response_class=PlainTextResponse,
        summary="Read an event",
    )
    def api_get_event(
        owner: str,
        calendar_name: str,
        uid: str,
        if_none_match: str | None = Header(None),
    ) -> Response:
        owner = validate_segment(owner, "owner")
        calendar_name = validate_segment(calendar_name, "calendar")
        uid = validate_segment(uid, "uid")
        found = read_event(owner, calendar_name, uid)
        if found is None:
            raise HTTPException(status_code=404, detail="Event not found")
        _path, data, stat = found
        headers = {"ETag": compute_etag(data), "Last-Modified": http_date(stat.st_mtime)}
        if if_none_match_hits(if_none_match, headers["ETag"]):
            return Response(status_code=304, headers=headers)
        return PlainTextResponse(data.decode("utf-8"), media_type="text/calendar", headers=headers)

    @app.delete(
        "/api/calendars/{owner}/{calendar_name}/{uid}",
        dependencies=[Depends(require_api_key)],
        responses=error_responses(400, 412),
        summary="Delete an event and its managed attachments",
    )
    def api_delete_event(
        owner: str,
        calendar_name: str,
        uid: str,
        if_match: str | None = Header(None, description="Only delete if the current ETag matches (412 otherwise)."),
    ) -> JSONResponse:
        """Returns {"deleted": false} for an unknown UID; use .../history to tell "deleted" from "never existed"."""
        owner = validate_segment(owner, "owner")
        calendar_name = validate_segment(calendar_name, "calendar")
        uid = validate_segment(uid, "uid")
        existed = delete_event(owner, calendar_name, uid, if_match)
        return JSONResponse({"deleted": existed})

    @app.get(
        "/api/calendars/{owner}/{calendar_name}/{uid}/history",
        dependencies=[Depends(require_api_key)],
        response_model=HistoryResponse,
        responses=error_responses(400, 410),
        summary="Change history of one event",
    )
    def api_event_history(
        request: Request,
        response: Response,
        owner: str,
        calendar_name: str,
        uid: str,
        limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
        cursor: str | None = Query(None),
    ) -> dict[str, Any]:
        """Append-only record of creations, updates and deletions since the journal was introduced.

        An empty list for an unknown UID means the server never saw it.
        """
        owner = validate_segment(owner, "owner")
        calendar_name = validate_segment(calendar_name, "calendar")
        uid = validate_segment(uid, "uid")
        with storage_lock():
            journal_sync([(owner, calendar_name)], "external")
            journal_id = journal.journal_id_value()
            after = decode_change_token(cursor, journal_id, "cursor")
            rows, has_more, _current, _ = journal.changes(after, owner, calendar_name, uid, limit)
        next_cursor = encode_change_token(journal_id, rows[-1]["seq"]) if has_more else None
        response.headers.update(next_link(request, "cursor", next_cursor))
        return {
            "owner": owner,
            "calendar": calendar_name,
            "uid": uid,
            "items": [change_entry(row) for row in rows],
            "next_cursor": next_cursor,
            "truncated": has_more,
        }

    @app.post(
        "/api/calendars/{owner}/{calendar_name}/{uid}/attachments",
        dependencies=[Depends(require_api_key)],
        responses=error_responses(400, 404, 413),
        summary="Upload an attachment",
    )
    async def api_upload_attachment(
        request: Request,
        owner: str,
        calendar_name: str,
        uid: str,
        file: UploadFile = File(...),
    ) -> JSONResponse:
        """Adds an ATTACH;VALUE=URI property to the event. Responds with the event's new ETag."""
        owner = validate_segment(owner, "owner")
        calendar_name = validate_segment(calendar_name, "calendar")
        uid = validate_segment(uid, "uid")
        record, etag = await store_uploaded_attachment(request, owner, calendar_name, uid, file)
        return JSONResponse({"attachment": record}, headers={"ETag": etag})

    @app.get(
        "/api/calendars/{owner}/{calendar_name}/{uid}/attachments",
        dependencies=[Depends(require_api_key)],
        response_model=AttachmentListResponse,
        responses=error_responses(400, 404),
        summary="List an event's attachments",
    )
    def api_list_attachments(owner: str, calendar_name: str, uid: str) -> dict[str, Any]:
        """Managed uploads plus any other ATTACH references found in the event.

        `in_event` and `file_present` expose drift between the ICS and stored files.
        """
        owner = validate_segment(owner, "owner")
        calendar_name = validate_segment(calendar_name, "calendar")
        uid = validate_segment(uid, "uid")
        return list_event_attachments(owner, calendar_name, uid)

    @app.get(
        "/api/calendars/{owner}/{calendar_name}/{uid}/attachments/{attachment_id}",
        dependencies=[Depends(require_api_key)],
        responses={
            200: {"description": "The attachment bytes.", "content": {"application/octet-stream": {}}},
            **error_responses(400, 404),
        },
        response_class=FileResponse,
        summary="Download an attachment",
    )
    def api_download_attachment(owner: str, calendar_name: str, uid: str, attachment_id: str) -> FileResponse:
        owner = validate_segment(owner, "owner")
        calendar_name = validate_segment(calendar_name, "calendar")
        uid = validate_segment(uid, "uid")
        record = find_attachment_record(owner, calendar_name, uid, attachment_id)
        path = attachment_file(record)
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="Attachment file is missing")
        return FileResponse(
            path,
            media_type=safe_content_type(record.get("content_type")),
            filename=str(record.get("filename") or "attachment.bin"),
            headers={
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "Cache-Control": "private, no-store",
            },
        )

    @app.delete(
        "/api/calendars/{owner}/{calendar_name}/{uid}/attachments/{attachment_id}",
        dependencies=[Depends(require_api_key)],
        response_model=AttachmentDeleteResult,
        responses=error_responses(400, 404, 412),
        summary="Delete an attachment",
    )
    def api_delete_attachment(
        response: Response,
        owner: str,
        calendar_name: str,
        uid: str,
        attachment_id: str,
        if_match: str | None = Header(None, description="ETag the event must currently have."),
    ) -> dict[str, Any]:
        """Removes the file, its index record, and the matching ATTACH property in the event."""
        owner = validate_segment(owner, "owner")
        calendar_name = validate_segment(calendar_name, "calendar")
        uid = validate_segment(uid, "uid")
        etag = delete_event_attachment(owner, calendar_name, uid, attachment_id, if_match)
        response.headers["ETag"] = etag
        return {"deleted": True, "id": attachment_id, "etag": etag}

    @app.post(
        "/api/batch",
        dependencies=[Depends(require_api_key)],
        response_model=BatchResponse,
        responses=error_responses(400, 404, 409, 412, 413),
        summary="Apply several puts and deletes atomically",
    )
    async def api_batch(payload: BatchRequest) -> JSONResponse:
        """All operations are validated and their preconditions checked first; then
        either every operation is applied or none is. On failure the HTTP status is
        that of the first failing operation, `failed_index` points at it, and
        `applied` is false. Operations run in order, so a later operation sees the
        effect of an earlier one on the same UID.
        """
        if not payload.atomic:
            raise HTTPException(status_code=400, detail="Only atomic batches are supported; omit atomic or set it to true")
        if len(payload.operations) > MAX_BATCH_OPERATIONS:
            raise HTTPException(status_code=400, detail=f"At most {MAX_BATCH_OPERATIONS} operations per batch")
        ops = [
            BatchOp(
                op=item.op,
                owner=item.owner,
                calendar=item.calendar,
                uid=item.uid,
                ics=item.ics.encode("utf-8") if item.ics is not None else None,
                if_match=item.if_match,
                if_none_match=item.if_none_match,
            )
            for item in payload.operations
        ]
        status, body = await run_in_threadpool(
            execute_batch, ops, dry_run=payload.dry_run, source="batch", max_ics_bytes=limits["ics"]
        )
        return JSONResponse(body, status_code=status)

    @app.get(
        "/api/changes",
        dependencies=[Depends(require_api_key)],
        response_model=ChangesResponse,
        responses=error_responses(400, 410),
        summary="Change feed",
    )
    def api_changes(
        request: Request,
        response: Response,
        since: str | None = Query(None, description="next_token from an earlier call. Omit to read from the beginning."),
        owner: str | None = Query(None),
        calendar: str | None = Query(None),
        limit: int = Query(DEFAULT_LIST_LIMIT, ge=1, le=MAX_LIST_LIMIT),
    ) -> dict[str, Any]:
        """Creations, updates and deletions in commit order, including changes made
        by CalDAV clients (source `external`, detected when the feed is read).
        A token from a different journal returns 410: resync from scratch.
        """
        if owner is not None:
            owner = validate_segment(owner, "owner")
        if calendar is not None:
            calendar = validate_segment(calendar, "calendar")
            if owner is None:
                raise HTTPException(status_code=400, detail="calendar requires owner")
        with storage_lock():
            scopes = [(owner, calendar)] if owner and calendar else None
            if scopes is None:
                with journal.connect() as conn:
                    scopes = journal.all_scopes(conn, owner)
            journal_sync(scopes, "external")
            journal_id = journal.journal_id_value()
            after = decode_change_token(since, journal_id, "since")
            rows, has_more, current, _ = journal.changes(after, owner, calendar, None, limit)
        if after > current:
            raise HTTPException(status_code=410, detail="since is ahead of this journal; start a full resync")
        next_seq = rows[-1]["seq"] if has_more else max(current, after)
        next_token = encode_change_token(journal_id, next_seq)
        if has_more:
            response.headers.update(next_link(request, "since", next_token))
        return {"changes": [change_entry(row) for row in rows], "next_token": next_token, "truncated": has_more}

    @app.get(
        "/api/export",
        dependencies=[Depends(require_api_key)],
        responses={
            200: {
                "description": "text/calendar bundle (format=ics) or a zip backup (format=zip).",
                "content": {"text/calendar": {"schema": {"type": "string"}}, "application/zip": {}},
            },
            **error_responses(400, 404),
        },
        summary="Export or back up calendars",
    )
    def api_export(
        owner: str | None = Query(None),
        calendar: str | None = Query(None),
        format: Literal["ics", "zip"] = Query("ics"),
        include_attachments: bool = Query(True, description="zip only: include attachment files."),
    ) -> Response:
        """format=ics: one VCALENDAR with every component of one calendar (owner and
        calendar required); ATTACH properties keep their URIs.
        format=zip: byte-exact event files, props, a calendar.ics bundle per calendar,
        attachment files, and a manifest with SHA-256 digests. owner/calendar narrow it.
        """
        if owner is not None:
            owner = validate_segment(owner, "owner")
        if calendar is not None:
            calendar = validate_segment(calendar, "calendar")
        if format == "ics":
            if owner is None or calendar is None:
                raise HTTPException(status_code=400, detail="format=ics requires owner and calendar")
            require_calendar(owner, calendar)
            with storage_lock():
                bundle, count, skipped = build_export_calendar(owner, calendar)
            headers = {
                "Content-Disposition": f'attachment; filename="{owner}-{calendar}.ics"',
                "Content-Digest": "sha-256=:" + base64.b64encode(hashlib.sha256(bundle).digest()).decode("ascii") + ":",
                "X-Event-Count": str(count),
                "X-Skipped-Items": str(len(skipped)),
                "Cache-Control": "private, no-store",
            }
            return Response(bundle, media_type="text/calendar; charset=utf-8", headers=headers)
        if calendar is not None and owner is None:
            raise HTTPException(status_code=400, detail="calendar requires owner")
        if owner is not None and calendar is not None:
            require_calendar(owner, calendar)
        with storage_lock():
            archive, count = build_export_zip(owner, calendar, include_attachments)
        name = "-".join(part for part in ("pocket-caldav", owner, calendar) if part)
        return FileResponse(
            archive,
            media_type="application/zip",
            filename=f"{name}.zip",
            headers={"X-Calendar-Count": str(count), "Cache-Control": "private, no-store"},
            background=BackgroundTask(delete_file_quietly, archive),
        )

    @app.post(
        "/api/validate",
        dependencies=[Depends(require_api_key)],
        response_model=ValidateResponse,
        responses=error_responses(413),
        summary="Validate an event without storing it",
    )
    def api_validate(payload: ValidateRequest) -> dict[str, Any]:
        """Checks line syntax and folding, component nesting, UID/DTSTAMP/DTSTART,
        DTEND/DURATION consistency, RRULE parsing, TZIDs without a VTIMEZONE,
        EXDATEs that miss every occurrence, and whether CalDAV clients can load it.
        PUT only rejects input it cannot store faithfully; see `errors` for the rest.
        """
        data = payload.ics.encode("utf-8")
        if len(data) > limits["ics"]:
            raise HTTPException(status_code=413, detail=f"ICS too large. Limit is {limits['ics']} bytes.")
        return validate_ics(data, payload.uid)

    @app.get(
        "/api/dav-info",
        dependencies=[Depends(require_api_key)],
        summary="How CalDAV clients connect",
    )
    def api_dav_info(request: Request) -> dict[str, Any]:
        """Discovery document for automation. The management X-API-Key is not accepted
        by CalDAV endpoints; they use HTTP Basic with a per-user password.
        """
        base = public_base_url(request)
        return {
            "dav_base_url": f"{base}/",
            "well_known": f"{base}/.well-known/caldav",
            "authentication": {
                "scheme": "Basic",
                "credentials": "Per-user CalDAV password (bcrypt htpasswd), managed with `python server.py cli` or CALDAV_USERS.",
                "api_key_accepted": False,
                "rights": "owner_only: a user can only read and write calendars under /<user>/.",
            },
            "supported_reports": ["calendar-query", "calendar-multiget", "sync-collection", "principal-property-search"],
            "calendars": [
                {"owner": item["owner"], "calendar": item["calendar"], "url": f"{base}{item['path']}"}
                for item in list_calendars()
            ],
            "management_api": {
                "etag": "Management ETags hash the stored file bytes and differ from the CalDAV getetag of the same event.",
                "change_feed": "/api/changes",
            },
        }

    @app.post(
        "/api/cleanup",
        dependencies=[Depends(require_api_key)],
        response_model=CleanupResult,
        response_model_exclude_none=True,
        responses=error_responses(400),
        summary="Run attachment cleanup now (destructive)",
    )
    def api_cleanup(
        confirm: bool = Query(False, description="Required to actually delete."),
        dry_run: bool = Query(False, description="List what would be deleted without deleting."),
    ) -> dict[str, Any]:
        """Deletes managed attachment files (and the ATTACH reference in their event) when
        the event has ended (for a series: its last instance), the event no longer exists,
        or the attachment is older than ATTACHMENT_TTL_DAYS. Events themselves are never
        deleted. The same job runs automatically every CLEANUP_INTERVAL_SECONDS, so this
        only runs it early. It is idempotent. Pass dry_run=true to preview or confirm=true
        to delete; a bare call is refused with 400.
        """
        if dry_run:
            return cleanup_attachments(cleaner.ttl_days, dry_run=True)
        if not confirm:
            raise HTTPException(
                status_code=400,
                detail="Cleanup deletes attachment files. Pass dry_run=true to preview or confirm=true to run it.",
            )
        return cleanup_attachments(cleaner.ttl_days)

    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/attachments", AttachmentStaticFiles(directory=ATTACHMENTS_DIR), name="attachments")

    from radicale import application as radicale_application

    app.mount("/", WSGIMiddleware(radicale_application))
    app.state.generated_api_key = api_key
    return app


app = None if __name__ == "__main__" else create_app()


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Pocket CalDAV")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("serve", help="Run the CalDAV and management API server")
    subparsers.add_parser("cli", help="Open the guided server setup CLI")
    args = parser.parse_args()

    if args.command == "cli":
        run_cli()
        return

    global app
    if app is None:
        app = create_app()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5232"))
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
