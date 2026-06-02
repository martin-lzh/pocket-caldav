#!/usr/bin/env python3
"""Single-file CalDAV bridge server backed by Radicale.

The FastAPI management API writes standard iCalendar resources into
Radicale's documented filesystem storage. Radicale serves those same files
over CalDAV for Apple Calendar, macOS Calendar, and native Android CalDAV
providers where the device/OEM includes one.
"""

from __future__ import annotations

import asyncio
import argparse
import getpass
import json
import os
import re
import secrets
import shutil
import sys
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
COLLECTION_ROOT_DIR = COLLECTIONS_DIR / "collection-root"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
CONFIG_DIR = DATA_DIR / "config"
RADICALE_CONFIG_FILE = CONFIG_DIR / "radicale.conf"
HTPASSWD_FILE = CONFIG_DIR / "users"
USERS_JSON_FILE = DATA_DIR / "users.json"
API_KEY_FILE = DATA_DIR / "api_key.txt"
ATTACHMENT_INDEX_FILE = DATA_DIR / "attachments.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,199}$")
DEFAULT_SETTINGS = {
    "attachment_ttl_days": 14,
    "cleanup_interval_seconds": 3600,
}

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
    for user, password in users.items():
        validate_segment_for_startup(user, "username")
        if not isinstance(password, str) or not password:
            raise ValueError(f"password for {user!r} must be a non-empty string")

    atomic_write_text(USERS_JSON_FILE, json.dumps(users, indent=2, sort_keys=True))
    atomic_write_text(
        HTPASSWD_FILE,
        "".join(f"{username}:{password}\n" for username, password in sorted(users.items())),
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
    return COLLECTION_ROOT_DIR / owner / calendar


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
        calendar = parse_calendar(event_file.read_bytes())
        event = list(calendar.walk("VEVENT"))[0]
        attachments = event.get("ATTACH")
        if attachments is None:
            continue
        if not isinstance(attachments, list):
            attachments = [attachments]
        changed = False
        for item in attachments:
            uri = str(item)
            new_uri = rewrite_attachment_uri_owner(uri, source_owner, target_owner)
            if new_uri != uri:
                changed = True
                rewritten += 1
        if changed:
            event.pop("ATTACH", None)
            for item in attachments:
                uri = str(item)
                new_uri = rewrite_attachment_uri_owner(uri, source_owner, target_owner)
                if new_uri != uri:
                    params = dict(getattr(item, "params", {}))
                    event.add("ATTACH", new_uri, parameters=params)
                else:
                    event.add("ATTACH", item, encode=0)
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


def startup_banner(users: dict[str, str], settings: dict[str, int]) -> None:
    print("", flush=True)
    print("CalDAV bridge is configured.", flush=True)
    print(f"Data directory: {DATA_DIR}", flush=True)
    print(f"Radicale config: {RADICALE_CONFIG_FILE}", flush=True)
    print(
        "Attachment cleanup: "
        f"every {settings['cleanup_interval_seconds']}s, TTL {settings['attachment_ttl_days']}d",
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


def prepare_runtime() -> tuple[dict[str, str], str, dict[str, int]]:
    ensure_directories()
    users = load_or_create_users()
    api_key = load_or_create_api_key()
    settings = runtime_cleanup_settings()
    write_radicale_config()
    startup_banner(users, settings)
    return users, api_key, settings


def create_app() -> FastAPI:
    users, api_key, settings = prepare_runtime()
    cleaner = Cleaner(
        interval_seconds=settings["cleanup_interval_seconds"],
        ttl_days=settings["attachment_ttl_days"],
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
            "users": sorted(load_or_create_users().keys()),
            "calendars": list_calendars(),
            "ttl_days": cleaner.ttl_days,
            "cleanup_interval_seconds": cleaner.interval_seconds,
        }

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


app = None if __name__ == "__main__" else create_app()


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="CalDAV Subscription Server")
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
