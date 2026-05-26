#!/usr/bin/env python3
"""Single-file CalDAV bridge server backed by Radicale.

The FastAPI management API writes standard iCalendar resources into
Radicale's documented filesystem storage. Radicale serves those same files
over CalDAV for Apple Calendar, macOS Calendar, and native Android CalDAV
providers where the device/OEM includes one.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.wsgi import WSGIMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from icalendar import Calendar
from pydantic import BaseModel, Field


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", APP_DIR / "data")).resolve()
COLLECTIONS_DIR = DATA_DIR / "collections"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
CONFIG_DIR = DATA_DIR / "config"
RADICALE_CONFIG_FILE = CONFIG_DIR / "radicale.conf"
HTPASSWD_FILE = CONFIG_DIR / "users"
USERS_JSON_FILE = DATA_DIR / "users.json"
API_KEY_FILE = DATA_DIR / "api_key.txt"
ATTACHMENT_INDEX_FILE = DATA_DIR / "attachments.json"
SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,199}$")
DEFAULT_USERS = ("work", "personal", "shared", "main")
DEFAULT_CALENDARS = (
    ("work", "default", "Work"),
    ("personal", "default", "Personal"),
    ("shared", "default", "Shared"),
    ("main", "work", "Work"),
    ("main", "personal", "Personal"),
    ("main", "shared", "Shared"),
)

storage_mutex = threading.RLock()


class CalendarCreate(BaseModel):
    owner: str = Field(..., examples=["work", "main"])
    calendar: str = Field("default", examples=["default", "personal"])
    display_name: str | None = Field(None, examples=["Work"])


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
    for path in (DATA_DIR, COLLECTIONS_DIR, ATTACHMENTS_DIR, CONFIG_DIR):
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
    elif not users:
        users = {user: secrets.token_urlsafe(18) for user in DEFAULT_USERS}

    for user in users:
        validate_segment_for_startup(user, "username")

    atomic_write_text(USERS_JSON_FILE, json.dumps(users, indent=2, sort_keys=True))
    atomic_write_text(
        HTPASSWD_FILE,
        "".join(f"{username}:{password}\n" for username, password in sorted(users.items())),
    )
    return users


def validate_segment_for_startup(value: str, name: str) -> None:
    if not SEGMENT_RE.fullmatch(value):
        raise ValueError(f"{name} {value!r} must match {SEGMENT_RE.pattern}")


def load_or_create_api_key() -> str:
    env_key = os.environ.get("CALDAV_API_KEY", "").strip()
    if env_key:
        atomic_write_text(API_KEY_FILE, env_key + "\n")
        return env_key
    if API_KEY_FILE.exists():
        return API_KEY_FILE.read_text(encoding="utf-8").strip()
    key = secrets.token_urlsafe(32)
    atomic_write_text(API_KEY_FILE, key + "\n")
    return key


def write_radicale_config() -> None:
    config = f"""
[auth]
type = htpasswd
htpasswd_filename = {HTPASSWD_FILE}
htpasswd_encryption = plain
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


def bootstrap_default_calendars() -> None:
    for owner, calendar, display_name in DEFAULT_CALENDARS:
        create_calendar(owner, calendar, display_name)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as tmp:
            tmp.write(content)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(content)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


@contextmanager
def storage_lock() -> Iterable[None]:
    COLLECTIONS_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = COLLECTIONS_DIR / ".Radicale.lock"
    with storage_mutex:
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


def collection_path(owner: str, calendar: str) -> Path:
    return COLLECTIONS_DIR / owner / calendar


def event_path(owner: str, calendar: str, uid: str) -> Path:
    return collection_path(owner, calendar) / f"{uid}.ics"


def create_calendar(owner: str, calendar: str, display_name: str | None = None) -> Path:
    validate_segment_for_startup(owner, "owner")
    validate_segment_for_startup(calendar, "calendar")
    path = collection_path(owner, calendar)
    props: dict[str, str] = {"tag": "VCALENDAR"}
    if display_name:
        props["D:displayname"] = display_name
    with storage_lock():
        path.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path / ".Radicale.props", json.dumps(props, sort_keys=True))
    return path


def parse_calendar(data: bytes) -> Calendar:
    try:
        calendar = Calendar.from_ical(data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid iCalendar data: {exc}") from exc
    events = list(calendar.walk("VEVENT"))
    if len(events) != 1:
        raise HTTPException(status_code=400, detail="ICS must contain exactly one VEVENT")
    uid = str(events[0].get("UID", "")).strip()
    if not uid:
        raise HTTPException(status_code=400, detail="VEVENT must contain UID")
    return calendar


def event_uid(calendar: Calendar) -> str:
    return str(list(calendar.walk("VEVENT"))[0].get("UID"))


def normalize_calendar(data: bytes, expected_uid: str) -> bytes:
    calendar = parse_calendar(data)
    uid = event_uid(calendar)
    if uid != expected_uid:
        raise HTTPException(
            status_code=409,
            detail=f"VEVENT UID {uid!r} does not match URL uid {expected_uid!r}",
        )
    return calendar.to_ical()


def load_event(owner: str, calendar: str, uid: str) -> bytes:
    path = event_path(owner, calendar, uid)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Event not found")
    return path.read_bytes()


def upsert_event(owner: str, calendar: str, uid: str, data: bytes) -> Path:
    validate_segment_for_startup(owner, "owner")
    validate_segment_for_startup(calendar, "calendar")
    validate_segment_for_startup(uid, "uid")
    normalized = normalize_calendar(data, uid)
    create_calendar(owner, calendar, calendar)
    with storage_lock():
        path = event_path(owner, calendar, uid)
        atomic_write_bytes(path, normalized)
        reconcile_managed_attachments(owner, calendar, uid, attachment_uris_from_ics(normalized))
    return path


def delete_event_and_attachments(owner: str, calendar: str, uid: str) -> bool:
    validate_segment_for_startup(owner, "owner")
    validate_segment_for_startup(calendar, "calendar")
    validate_segment_for_startup(uid, "uid")
    with storage_lock():
        path = event_path(owner, calendar, uid)
        existed = path.exists()
        if existed:
            path.unlink()
        delete_managed_attachments(owner, calendar, uid)
    return existed


def attachment_uris_from_ics(data: bytes) -> set[str]:
    calendar = parse_calendar(data)
    event = list(calendar.walk("VEVENT"))[0]
    attachments = event.get("ATTACH")
    if attachments is None:
        return set()
    if not isinstance(attachments, list):
        attachments = [attachments]
    return {str(item) for item in attachments if str(item).startswith(("http://", "https://", "/"))}


def add_attachment_to_event(
    owner: str,
    calendar_name: str,
    uid: str,
    public_uri: str,
    content_type: str,
    filename: str,
) -> None:
    current = load_event(owner, calendar_name, uid)
    calendar = parse_calendar(current)
    event = list(calendar.walk("VEVENT"))[0]
    event.add(
        "ATTACH",
        public_uri,
        parameters={
            "VALUE": "URI",
            "FMTTYPE": content_type or "application/octet-stream",
            "FILENAME": filename,
        },
    )
    atomic_write_bytes(event_path(owner, calendar_name, uid), calendar.to_ical())


def event_end_from_ics(data: bytes) -> datetime | None:
    calendar = parse_calendar(data)
    event = list(calendar.walk("VEVENT"))[0]
    end_value = event.decoded("DTEND", None)
    if end_value is None:
        duration = event.decoded("DURATION", None)
        start_value = event.decoded("DTSTART", None)
        if start_value is not None and duration is not None:
            end_value = start_value + duration
        else:
            end_value = start_value
    if end_value is None:
        return None
    if isinstance(end_value, date) and not isinstance(end_value, datetime):
        return datetime.combine(end_value, datetime_time.min, tzinfo=timezone.utc)
    if isinstance(end_value, datetime):
        if end_value.tzinfo is None:
            return end_value.replace(tzinfo=timezone.utc)
        return end_value.astimezone(timezone.utc)
    return None


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
    configured = os.environ.get("PUBLIC_BASE_URL", "").strip()
    if configured:
        return configured.rstrip("/")
    return str(request.base_url).rstrip("/")


def attachment_static_path(owner: str, calendar: str, uid: str, stored_name: str) -> str:
    parts = [quote(part, safe="") for part in (owner, calendar, uid, stored_name)]
    return "/attachments/" + "/".join(parts)


async def store_uploaded_attachment(
    request: Request,
    owner: str,
    calendar_name: str,
    uid: str,
    upload: UploadFile,
) -> dict[str, Any]:
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

    with target.open("wb") as handle:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            handle.write(chunk)

    static_path = attachment_static_path(owner, calendar_name, uid, stored_name)
    public_uri = f"{public_base_url(request)}{static_path}"
    now = datetime.now(timezone.utc).isoformat()
    content_type = upload.content_type or "application/octet-stream"
    with storage_lock():
        add_attachment_to_event(owner, calendar_name, uid, public_uri, content_type, original_name)
        event_end = event_end_from_ics(load_event(owner, calendar_name, uid))
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
    return record


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


def remove_attachment_uris_from_event(owner: str, calendar_name: str, uid: str, stale_uris: set[str]) -> None:
    path = event_path(owner, calendar_name, uid)
    if not path.exists():
        return
    calendar = parse_calendar(path.read_bytes())
    event = list(calendar.walk("VEVENT"))[0]
    attachments = event.get("ATTACH")
    if attachments is None:
        return
    if not isinstance(attachments, list):
        attachments = [attachments]
    kept = [item for item in attachments if str(item) not in stale_uris]
    event.pop("ATTACH", None)
    for item in kept:
        event.add("ATTACH", item, encode=0)
    atomic_write_bytes(path, calendar.to_ical())


def cleanup_attachments(ttl_days: int) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=ttl_days)
    deleted = 0
    with storage_lock():
        records = load_attachment_index()
        kept: list[dict[str, Any]] = []
        stale_by_event: dict[tuple[str, str, str], set[str]] = {}
        for record in records:
            owner = str(record.get("owner", ""))
            calendar_name = str(record.get("calendar", ""))
            uid = str(record.get("uid", ""))
            created_at = datetime.fromisoformat(str(record["created_at"]))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            event_file = event_path(owner, calendar_name, uid)
            event_end = event_end_from_ics(event_file.read_bytes()) if event_file.exists() else now - timedelta(seconds=1)
            expired_by_age = created_at <= cutoff
            expired_by_event = bool(event_end and event_end <= now)
            if expired_by_age or expired_by_event:
                path = (ATTACHMENTS_DIR / str(record.get("relative_path", ""))).resolve()
                if path.is_relative_to(ATTACHMENTS_DIR):
                    delete_file_quietly(path)
                    delete_empty_parents(path)
                stale_by_event.setdefault((owner, calendar_name, uid), set()).add(str(record.get("uri", "")))
                deleted += 1
            else:
                if event_end:
                    record["event_end"] = event_end.isoformat()
                kept.append(record)
        save_attachment_index(kept)
        for (owner, calendar_name, uid), stale_uris in stale_by_event.items():
            remove_attachment_uris_from_event(owner, calendar_name, uid, stale_uris)
    return {"deleted": deleted, "remaining": len(load_attachment_index())}


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = load_or_create_api_key()
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key")


def startup_banner(users: dict[str, str], api_key: str) -> None:
    print("", flush=True)
    print("CalDAV bridge is configured.", flush=True)
    print(f"Data directory: {DATA_DIR}", flush=True)
    print(f"Radicale config: {RADICALE_CONFIG_FILE}", flush=True)
    if "CALDAV_API_KEY" not in os.environ:
        print(f"Generated API key: {api_key}", flush=True)
    if "CALDAV_USERS" not in os.environ:
        print("Generated CalDAV users:", flush=True)
        for username, password in sorted(users.items()):
            print(f"  {username}: {password}", flush=True)
    print("", flush=True)


def prepare_runtime() -> tuple[dict[str, str], str]:
    ensure_directories()
    users = load_or_create_users()
    api_key = load_or_create_api_key()
    write_radicale_config()
    bootstrap_default_calendars()
    startup_banner(users, api_key)
    return users, api_key


def create_app() -> FastAPI:
    users, api_key = prepare_runtime()
    cleaner = Cleaner(
        interval_seconds=int(os.environ.get("CLEANUP_INTERVAL_SECONDS", "3600")),
        ttl_days=int(os.environ.get("ATTACHMENT_TTL_DAYS", "14")),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        cleanup_attachments(cleaner.ttl_days)
        cleaner.start()
        yield
        cleaner.stop()

    app = FastAPI(
        title="CalDAV Subscription Server",
        version="1.0.0",
        lifespan=lifespan,
    )

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "ok": True,
            "collections": str(COLLECTIONS_DIR),
            "users": sorted(users.keys()),
            "ttl_days": cleaner.ttl_days,
        }

    @app.get("/api/bootstrap", dependencies=[Depends(require_api_key)])
    def bootstrap() -> dict[str, Any]:
        return {
            "users": sorted(users.keys()),
            "collections": [
                {"owner": owner, "calendar": calendar}
                for owner, calendar, _display_name in DEFAULT_CALENDARS
            ],
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

    @app.put("/api/calendars/{owner}/{calendar_name}/{uid}", dependencies=[Depends(require_api_key)])
    async def api_put_event(owner: str, calendar_name: str, uid: str, request: Request) -> dict[str, str]:
        owner = validate_segment(owner, "owner")
        calendar_name = validate_segment(calendar_name, "calendar")
        uid = validate_segment(uid, "uid")
        data = await request.body()
        if not data:
            raise HTTPException(status_code=400, detail="Request body must contain text/calendar data")
        upsert_event(owner, calendar_name, uid, data)
        return {"owner": owner, "calendar": calendar_name, "uid": uid, "path": f"/{owner}/{calendar_name}/{uid}.ics"}

    @app.get("/api/calendars/{owner}/{calendar_name}/{uid}", dependencies=[Depends(require_api_key)])
    def api_get_event(owner: str, calendar_name: str, uid: str) -> PlainTextResponse:
        owner = validate_segment(owner, "owner")
        calendar_name = validate_segment(calendar_name, "calendar")
        uid = validate_segment(uid, "uid")
        return PlainTextResponse(load_event(owner, calendar_name, uid).decode("utf-8"), media_type="text/calendar")

    @app.delete("/api/calendars/{owner}/{calendar_name}/{uid}", dependencies=[Depends(require_api_key)])
    def api_delete_event(owner: str, calendar_name: str, uid: str) -> JSONResponse:
        owner = validate_segment(owner, "owner")
        calendar_name = validate_segment(calendar_name, "calendar")
        uid = validate_segment(uid, "uid")
        existed = delete_event_and_attachments(owner, calendar_name, uid)
        return JSONResponse({"deleted": existed})

    @app.post("/api/calendars/{owner}/{calendar_name}/{uid}/attachments", dependencies=[Depends(require_api_key)])
    async def api_upload_attachment(
        request: Request,
        owner: str,
        calendar_name: str,
        uid: str,
        file: UploadFile = File(...),
    ) -> dict[str, Any]:
        owner = validate_segment(owner, "owner")
        calendar_name = validate_segment(calendar_name, "calendar")
        uid = validate_segment(uid, "uid")
        record = await store_uploaded_attachment(request, owner, calendar_name, uid, file)
        return {"attachment": record}

    @app.post("/api/cleanup", dependencies=[Depends(require_api_key)])
    def api_cleanup() -> dict[str, int]:
        return cleanup_attachments(cleaner.ttl_days)

    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/attachments", StaticFiles(directory=ATTACHMENTS_DIR), name="attachments")

    from radicale import application as radicale_application

    app.mount("/", WSGIMiddleware(radicale_application))
    app.state.generated_api_key = api_key
    return app


app = create_app()


def main() -> None:
    import uvicorn

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5232"))
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
