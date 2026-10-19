"""Regression tests for findings from the independent audit."""

import base64
import io
import json
import os
import time
import zipfile

from test_api import base, ics, put, simple


def test_zero_interval_rrule_is_rejected_and_never_hangs_listings(client, calendar, server):
    body = ics("zero", "DTSTART:20261020T090000Z", "DTEND:20261020T100000Z", "RRULE:FREQ=DAILY;INTERVAL=0")
    response = put(client, calendar, "zero", body)
    assert response.status_code == 400 and "INTERVAL" in response.json()["detail"]
    report = client.post("/api/validate", json={"ics": body}).json()
    assert report["valid"] is False and "invalid_rrule" in {issue["code"] for issue in report["errors"]}

    # A CalDAV client can still write such a file; listings must report it instead of spinning.
    (server.collection_path(*calendar) / "zero.ics").write_bytes(body.encode())
    started = time.monotonic()
    listing = client.get(f"{base(calendar)}/events", params={"from": "2026-10-21T00:00:00Z", "to": "2026-10-22T00:00:00Z"})
    assert time.monotonic() - started < 5
    assert listing.status_code == 200
    assert [item["href"] for item in listing.json()["invalid_items"]] == ["zero.ics"]
    expanded = client.get(f"{base(calendar)}/events", params={"from": "2026-10-21T00:00:00Z", "to": "2026-10-22T00:00:00Z", "expand_recurrence": "true"})
    assert expanded.status_code == 200 and expanded.json()["items"] == []


def test_validation_cost_is_bounded(client):
    minutes = ",".join(str(minute) for minute in range(60))
    exdates = [f"EXDATE:2030{month:02d}01T000000Z" for month in range(1, 13)]
    body = ics("dense", "DTSTART:20000101T000000Z", "DTEND:20000101T000100Z", f"RRULE:FREQ=HOURLY;BYMINUTE={minutes}", *exdates)
    started = time.monotonic()
    report = client.post("/api/validate", json={"ics": body}).json()
    assert time.monotonic() - started < 10
    assert "exdate_not_checked" in {issue["code"] for issue in report["warnings"]}


def test_tampered_cursor_is_a_client_error(client, calendar):
    for index in range(3):
        put(client, calendar, f"cur-{index}", simple(f"cur-{index}", f"2026110{index + 1}T100000Z", f"2026110{index + 1}T110000Z"))
    page = client.get(f"{base(calendar)}/events", params={"limit": 1}).json()
    payload = json.loads(base64.urlsafe_b64decode(page["next_cursor"] + "=="))
    payload["k"][0] = "not-a-number"
    forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    response = client.get(f"{base(calendar)}/events", params={"limit": 1, "cursor": forged})
    assert response.status_code == 400 and response.json()["error"]["code"] == "cursor_mismatch"


def test_malformed_if_none_match_on_get_is_ignored(client, calendar):
    put(client, calendar, "inm", simple("inm", "20261101T100000Z", "20261101T110000Z"))
    response = client.get(f"{base(calendar)}/inm", headers={"If-None-Match": "unquoted-etag"})
    assert response.status_code == 200 and "BEGIN:VCALENDAR" in response.text


def test_weak_if_match_explains_itself(client, calendar):
    etag = put(client, calendar, "weak", simple("weak", "20261101T100000Z", "20261101T110000Z")).headers["etag"]
    response = put(client, calendar, "weak", simple("weak", "20261101T100000Z", "20261101T120000Z"), **{"If-Match": f"W/{etag}"})
    assert response.status_code == 412 and "weak" in response.json()["detail"].lower()


def test_writes_into_a_new_calendar_are_labelled_api(client):
    owner, name = "tester", "auto-" + os.urandom(3).hex()
    put(client, (owner, name), "first", simple("first", "20261101T100000Z", "20261101T110000Z"))
    changes = client.get("/api/changes", params={"owner": owner, "calendar": name}).json()["changes"]
    assert [(c["uid"], c["type"], c["source"]) for c in changes] == [("first", "created", "api")]


def test_in_place_edit_with_same_size_and_mtime_is_detected(client, calendar, server):
    owner, name = calendar
    put(client, calendar, "inplace", simple("inplace", "20261101T100000Z", "20261101T110000Z", "AAAA"))
    token = client.get("/api/changes", params={"owner": owner, "calendar": name}).json()["next_token"]
    path = server.collection_path(*calendar) / "inplace.ics"
    stat = path.stat()
    with path.open("r+b") as handle:
        data = handle.read().replace(b"SUMMARY:AAAA", b"SUMMARY:BBBB")
        handle.seek(0)
        handle.write(data)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    listing = client.get(f"{base(calendar)}/events", params={"q": "BBBB"}).json()
    assert [item["uid"] for item in listing["items"]] == ["inplace"]
    feed = client.get("/api/changes", params={"since": token, "owner": owner, "calendar": name}).json()
    assert [(c["uid"], c["type"]) for c in feed["changes"]] == [("inplace", "updated")]


def test_import_reports_duplicate_uids(client, calendar):
    bundle = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:x",
        "BEGIN:VEVENT", "UID:twice", "DTSTAMP:20260101T000000Z", "DTSTART:20261101T100000Z", "DTEND:20261101T110000Z", "END:VEVENT",
        "BEGIN:VEVENT", "UID:twice", "DTSTAMP:20260101T000000Z", "DTSTART:20261102T100000Z", "DTEND:20261102T110000Z", "END:VEVENT",
        "END:VCALENDAR", "",
    ])
    response = client.post(f"{base(calendar)}/import", content=bundle)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "duplicate_uids" and response.json()["error"]["details"]["uids"] == ["twice"]


def test_zip_restore_round_trip(client, calendar, server):
    owner, name = calendar
    put(client, calendar, "keep-1", simple("keep-1", "20991101T100000Z", "20991101T110000Z"))
    # Two files sharing a UID cannot go through an ICS import, but the zip restores them byte-for-byte.
    (server.collection_path(*calendar) / "dup-copy.ics").write_bytes(simple("keep-1", "20991102T100000Z", "20991102T110000Z").encode())
    upload = client.post(f"{base(calendar)}/keep-1/attachments", files={"file": ("t.pdf", b"%PDF ticket", "application/pdf")}).json()
    client.patch(base(calendar), json={"display_name": "Backed up"})
    before = {p.name: p.read_bytes() for p in server.collection_path(*calendar).iterdir() if not p.name.startswith(".")}

    archive = client.get("/api/export", params={"owner": owner, "calendar": name, "format": "zip"}).content
    assert client.delete(base(calendar), params={"force": "true"}).status_code == 200
    assert client.get(f"{base(calendar)}/keep-1/attachments").status_code == 404

    preview = client.post("/api/restore", params={"dry_run": "true"}, content=archive, headers={"Content-Type": "application/zip"})
    assert preview.status_code == 200, preview.text
    assert preview.json()["calendars"][0]["events_written"] == 2
    assert not server.collection_path(*calendar).exists()

    restored = client.post("/api/restore", params={"owner": owner, "calendar": name}, content=archive)
    assert restored.status_code == 200, restored.text
    after = {p.name: p.read_bytes() for p in server.collection_path(*calendar).iterdir() if not p.name.startswith(".")}
    assert after == before
    attachments = client.get(f"{base(calendar)}/keep-1/attachments").json()["attachments"]
    assert [a["id"] for a in attachments] == [upload["attachment"]["id"]] and attachments[0]["file_present"] is True
    names = {c["calendar"]: c["display_name"] for c in client.get("/api/calendars").json()["calendars"]}
    assert names[name] == "Backed up"

    again = client.post("/api/restore", content=archive, params={"owner": owner, "calendar": name})
    assert again.status_code == 409 and again.json()["error"]["code"] == "calendar_not_empty"
    put(client, calendar, "extra", simple("extra", "20991103T100000Z", "20991103T110000Z"))
    replaced = client.post("/api/restore", content=archive, params={"owner": owner, "calendar": name, "replace": "true"})
    assert replaced.status_code == 200 and replaced.json()["calendars"][0]["events_deleted"] == 1
    assert client.get(f"{base(calendar)}/extra").status_code == 404

    tampered = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive)) as source, zipfile.ZipFile(tampered, "w") as target:
        for item in source.infolist():
            data = source.read(item)
            if item.filename.endswith("keep-1.ics"):
                data = data.replace(b"20991101", b"20991109")
            target.writestr(item, data)
    response = client.post("/api/restore", content=tampered.getvalue(), params={"replace": "true", "owner": owner, "calendar": name})
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_backup"
    assert client.post("/api/restore", content=b"not a zip").json()["error"]["code"] == "invalid_backup"


def test_long_series_is_visible_to_caldav_clients(client, calendar, dav_auth):
    owner, name = calendar
    body = ics("long-series", "DTSTART:20200101T090000Z", "DTEND:20200101T100000Z", "RRULE:FREQ=DAILY;UNTIL=20991231T000000Z", "SUMMARY:Long")
    assert put(client, calendar, "long-series", body).status_code == 200
    listing = client.request("PROPFIND", f"/{owner}/{name}/", auth=dav_auth, headers={"Depth": "1", "X-API-Key": ""})
    assert listing.status_code == 207 and "long-series.ics" in listing.text
