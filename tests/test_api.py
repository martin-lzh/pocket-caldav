import base64
import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

from icalendar import Calendar

BERLIN_TZ = """BEGIN:VTIMEZONE
TZID:Europe/Berlin
BEGIN:DAYLIGHT
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
TZNAME:CEST
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
TZNAME:CET
DTSTART:19701025T030000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE"""


def ics(uid, *lines, timezone_block=False):
    body = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//tests//EN",
        *(BERLIN_TZ.splitlines() if timezone_block else []),
        "BEGIN:VEVENT",
        f"UID:{uid}",
        "DTSTAMP:20260101T000000Z",
        *lines,
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ]
    return "\r\n".join(body)


def simple(uid, start, end, summary="Event"):
    return ics(uid, f"DTSTART:{start}", f"DTEND:{end}", f"SUMMARY:{summary}")


def base(calendar):
    owner, name = calendar
    return f"/api/calendars/{owner}/{name}"


def put(client, calendar, uid, body, **headers):
    return client.put(f"{base(calendar)}/{uid}", content=body, headers={"Content-Type": "text/calendar", **headers})


def list_all(client, calendar, **params):
    items, cursor, pages = [], None, 0
    while True:
        query = dict(params)
        if cursor:
            query["cursor"] = cursor
        response = client.get(f"{base(calendar)}/events", params=query)
        assert response.status_code == 200, response.text
        payload = response.json()
        items.extend(payload["items"])
        pages += 1
        assert payload["truncated"] == (payload["next_cursor"] is not None)
        if not payload["truncated"]:
            return items, pages, payload
        cursor = payload["next_cursor"]


# --- backwards compatibility -------------------------------------------------


def test_point_crud_success_bodies_are_unchanged(client, calendar):
    owner, name = calendar
    body = simple("compat-1", "20261101T100000Z", "20261101T110000Z")
    response = put(client, calendar, "compat-1", body)
    assert response.status_code == 200
    assert response.content == json.dumps(
        {"owner": owner, "calendar": name, "uid": "compat-1", "path": f"/{owner}/{name}/compat-1.ics"},
        separators=(",", ":"),
    ).encode()
    assert response.headers["etag"].startswith('"')
    assert "last-modified" in response.headers

    response = client.get(f"{base(calendar)}/compat-1")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/calendar; charset=utf-8"
    assert "BEGIN:VEVENT" in response.text

    assert client.delete(f"{base(calendar)}/compat-1").json() == {"deleted": True}
    assert client.delete(f"{base(calendar)}/compat-1").json() == {"deleted": False}
    response = client.get(f"{base(calendar)}/compat-1")
    assert response.status_code == 404


def test_put_preserves_calendar_props(client, calendar, server):
    props_file = server.collection_path(*calendar) / ".Radicale.props"
    props = json.loads(props_file.read_text())
    props["ICAL:calendar-color"] = "#FF0000"
    props_file.write_text(json.dumps(props))
    put(client, calendar, "props-1", simple("props-1", "20261101T100000Z", "20261101T110000Z"))
    stored = json.loads(props_file.read_text())
    assert stored["ICAL:calendar-color"] == "#FF0000"
    assert stored["D:displayname"] == props["D:displayname"]


def test_put_rejects_properties_the_parser_would_drop(client, calendar):
    body = ics("drop-1", "DTSTART:20261101T100000Z", "DTEND:garbage", "SUMMARY:x")
    response = put(client, calendar, "drop-1", body)
    assert response.status_code == 400
    assert "DTEND" in response.json()["detail"]


def test_series_with_overrides_can_be_stored(client, calendar):
    body = "\r\n".join(
        [
            "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:x",
            "BEGIN:VEVENT", "UID:series-ov", "DTSTAMP:20260101T000000Z",
            "DTSTART:20261102T090000Z", "DTEND:20261102T100000Z", "RRULE:FREQ=DAILY;COUNT=3", "SUMMARY:Daily",
            "END:VEVENT",
            "BEGIN:VEVENT", "UID:series-ov", "DTSTAMP:20260101T000000Z", "RECURRENCE-ID:20261103T090000Z",
            "DTSTART:20261103T150000Z", "DTEND:20261103T160000Z", "SUMMARY:Moved",
            "END:VEVENT",
            "END:VCALENDAR", "",
        ]
    )
    assert put(client, calendar, "series-ov", body).status_code == 200
    items, _pages, _ = list_all(
        client, calendar, expand_recurrence="true", **{"from": "2026-11-01T00:00:00Z", "to": "2026-11-10T00:00:00Z"}
    )
    assert [(item["summary"], item["is_override"]) for item in items] == [
        ("Daily", False), ("Moved", True), ("Daily", False),
    ]


# --- listing -------------------------------------------------------------------


def test_window_overlap_and_full_pagination(client, calendar):
    seeded = {}
    day = datetime(2026, 11, 1, 8, tzinfo=timezone.utc)
    for index in range(23):
        start = day + timedelta(hours=6 * index)
        uid = f"pg-{index:02d}"
        seeded[uid] = start
        body = simple(uid, start.strftime("%Y%m%dT%H%M%SZ"), (start + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ"))
        assert put(client, calendar, uid, body).status_code == 200
    # Long-running and all-day events that start before the window must still match.
    put(client, calendar, "long", simple("long", "20261020T000000Z", "20261210T000000Z", "Long"))
    put(client, calendar, "allday", ics("allday", "DTSTART;VALUE=DATE:20261102", "SUMMARY:All day"))
    put(client, calendar, "outside", simple("outside", "20261201T000000Z", "20261201T010000Z"))

    items, pages, last = list_all(client, calendar, limit=7)
    assert pages > 1
    uids = [item["uid"] for item in items]
    assert len(uids) == len(set(uids)) == 26 == last["total"]

    window_from = datetime(2026, 11, 2, 0, tzinfo=timezone.utc)
    window_to = datetime(2026, 11, 3, 0, tzinfo=timezone.utc)
    items, _pages, _ = list_all(
        client, calendar, limit=2, **{"from": window_from.isoformat(), "to": window_to.isoformat()}
    )
    expected = {uid for uid, start in seeded.items() if start < window_to and start + timedelta(hours=1) > window_from}
    expected |= {"long", "allday"}
    got = [item["uid"] for item in items]
    assert sorted(got) == sorted(expected)
    allday = next(item for item in items if item["uid"] == "allday")
    assert allday["all_day"] is True and allday["dtstart"] == "2026-11-02" and allday["dtend"] == "2026-11-03"


def test_query_parameters_are_validated(client, calendar):
    url = f"{base(calendar)}/events"
    assert client.get(url, params={"from": "2026-11-01T00:00:00"}).status_code == 400
    assert client.get(url, params={"expand_recurrence": "true"}).status_code == 400
    assert client.get(url, params={"tz": "Mars/Base"}).status_code == 400
    first = client.get(url, params={"limit": 1}).json()
    assert client.get(url, params={"cursor": "garbage"}).status_code == 400
    # An unencoded "+" offset arrives as a space and is still understood.
    response = client.get(url + "?from=2026-11-01T00:00:00 01:00")
    assert response.status_code == 200
    assert client.get("/api/calendars/tester/missing-cal/events").status_code == 404
    assert first["truncated"] is False


def test_text_search_finds_duplicates(client, calendar):
    put(client, calendar, "dup-a", simple("dup-a", "20261101T100000Z", "20261101T110000Z", "Dentist appointment"))
    put(client, calendar, "dup-b", simple("dup-b", "20261101T100000Z", "20261101T110000Z", "dentist APPOINTMENT"))
    put(client, calendar, "other", simple("other", "20261101T100000Z", "20261101T110000Z", "Lunch"))
    items, _, _ = list_all(client, calendar, q="dentist")
    assert sorted(item["uid"] for item in items) == ["dup-a", "dup-b"]


def test_recurrence_across_dst_with_exdate(client, calendar):
    body = ics(
        "weekly",
        "DTSTART;TZID=Europe/Berlin:20261020T100000",
        "DTEND;TZID=Europe/Berlin:20261020T110000",
        "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261201T000000Z",
        "EXDATE;TZID=Europe/Berlin:20261103T100000",
        "SUMMARY:Standup",
        timezone_block=True,
    )
    assert put(client, calendar, "weekly", body).status_code == 200
    window = {"from": "2026-10-01T00:00:00Z", "to": "2026-12-31T00:00:00Z"}
    items, _, _ = list_all(client, calendar, expand_recurrence="true", **window)
    starts = [item["dtstart"] for item in items]
    assert starts == [
        "2026-10-20T10:00:00+02:00",
        "2026-10-27T10:00:00+01:00",
        "2026-11-10T10:00:00+01:00",
        "2026-11-17T10:00:00+01:00",
        "2026-11-24T10:00:00+01:00",
    ]
    assert all(item["recurrence_id"] for item in items)

    series, _, _ = list_all(client, calendar, **{"from": "2026-11-03T00:00:00Z", "to": "2026-11-04T00:00:00Z"})
    assert series == []  # the only instance that day is excluded
    series, _, _ = list_all(client, calendar, **{"from": "2026-11-10T00:00:00Z", "to": "2026-11-11T00:00:00Z"})
    assert [item["uid"] for item in series] == ["weekly"]
    assert series[0]["rrule"] == "FREQ=WEEKLY;BYDAY=TU;UNTIL=20261201T000000Z"
    assert series[0]["is_recurring"] is True


def test_event_stored_under_other_filename_is_addressable(client, calendar, server):
    # CalDAV clients pick their own resource names.
    path = server.collection_path(*calendar) / "CLIENT-NAMED.ics"
    path.write_bytes(simple("client-uid", "20261101T100000Z", "20261101T110000Z").encode())
    response = client.get(f"{base(calendar)}/client-uid")
    assert response.status_code == 200
    put(client, calendar, "client-uid", simple("client-uid", "20261101T100000Z", "20261101T120000Z", "Edited"))
    files = sorted(p.name for p in server.collection_path(*calendar).iterdir() if not p.name.startswith("."))
    assert files == ["CLIENT-NAMED.ics"]
    items, _, _ = list_all(client, calendar)
    assert [(item["uid"], item["href"], item["summary"]) for item in items] == [("client-uid", "CLIENT-NAMED.ics", "Edited")]


# --- ETags and preconditions -----------------------------------------------------


def test_conditional_requests(client, calendar):
    first = put(client, calendar, "cond", simple("cond", "20261101T100000Z", "20261101T110000Z", "v1"))
    etag_v1 = first.headers["etag"]
    assert client.get(f"{base(calendar)}/cond").headers["etag"] == etag_v1
    assert client.get(f"{base(calendar)}/cond", headers={"If-None-Match": etag_v1}).status_code == 304

    second = put(client, calendar, "cond", simple("cond", "20261101T100000Z", "20261101T110000Z", "v2"), **{"If-Match": etag_v1})
    assert second.status_code == 200
    stored_v2 = client.get(f"{base(calendar)}/cond").text

    stale = put(client, calendar, "cond", simple("cond", "20261101T100000Z", "20261101T110000Z", "v3"), **{"If-Match": etag_v1})
    assert stale.status_code == 412
    assert stale.headers["etag"] == second.headers["etag"]
    assert client.get(f"{base(calendar)}/cond").text == stored_v2

    create_only = put(client, calendar, "cond", simple("cond", "20261101T100000Z", "20261101T110000Z", "v4"), **{"If-None-Match": "*"})
    assert create_only.status_code == 412
    assert put(client, calendar, "fresh", simple("fresh", "20261101T100000Z", "20261101T110000Z"), **{"If-None-Match": "*"}).status_code == 200

    assert client.delete(f"{base(calendar)}/cond", headers={"If-Match": etag_v1}).status_code == 412
    assert client.get(f"{base(calendar)}/cond").status_code == 200
    assert client.delete(f"{base(calendar)}/cond", headers={"If-Match": second.headers["etag"]}).json() == {"deleted": True}
    assert client.delete(f"{base(calendar)}/cond", headers={"If-Match": "*"}).status_code == 412


def test_put_dry_run_writes_nothing(client, calendar, server):
    response = client.put(f"{base(calendar)}/dry?dry_run=1", content=simple("dry", "20261101T100000Z", "20261101T110000Z"))
    assert response.status_code == 200
    payload = response.json()
    assert payload["dry_run"] is True and payload["would_create"] is True
    assert "UID:dry" in payload["ics"]
    assert payload["validation"]["valid"] is True
    assert client.get(f"{base(calendar)}/dry").status_code == 404
    assert not (server.collection_path(*calendar) / "dry.ics").exists()


# --- batch and change feed --------------------------------------------------------


def test_integration_flow_with_batch_and_change_feed(client, calendar):
    owner, name = calendar
    token = client.get("/api/changes", params={"owner": owner, "calendar": name}).json()["next_token"]
    for uid in ("flow-a", "flow-b", "flow-c"):
        put(client, calendar, uid, simple(uid, "20261105T100000Z", "20261105T110000Z", uid))
    items, _, _ = list_all(client, calendar, **{"from": "2026-11-05T00:00:00Z", "to": "2026-11-06T00:00:00Z"})
    assert sorted(item["uid"] for item in items) == ["flow-a", "flow-b", "flow-c"]
    etags = {item["uid"]: item["etag"] for item in items}

    stale_etag = etags["flow-a"]
    put(client, calendar, "flow-a", simple("flow-a", "20261105T100000Z", "20261105T113000Z", "flow-a"), **{"If-Match": stale_etag})
    assert put(client, calendar, "flow-a", simple("flow-a", "20261105T100000Z", "20261105T120000Z"), **{"If-Match": stale_etag}).status_code == 412

    # Atomic: the stale precondition on the last operation rejects the whole batch.
    ops = [{"op": "delete", "owner": owner, "calendar": name, "uid": uid} for uid in ("flow-b", "flow-c")]
    ops.append({"op": "delete", "owner": owner, "calendar": name, "uid": "flow-a", "if_match": stale_etag})
    rejected = client.post("/api/batch", json={"operations": ops})
    assert rejected.status_code == 412
    assert rejected.json()["applied"] is False and rejected.json()["failed_index"] == 2
    assert [r["outcome"] for r in rejected.json()["results"]] == ["not_applied", "not_applied", "failed"]
    assert client.get(f"{base(calendar)}/flow-b").status_code == 200

    ops[-1]["if_match"] = client.get(f"{base(calendar)}/flow-a").headers["etag"]
    preview = client.post("/api/batch", json={"operations": ops, "dry_run": True})
    assert preview.status_code == 200 and preview.json()["applied"] is False
    assert client.get(f"{base(calendar)}/flow-b").status_code == 200
    applied = client.post("/api/batch", json={"operations": ops})
    assert applied.status_code == 200 and applied.json()["applied"] is True
    assert all(r["deleted"] for r in applied.json()["results"])
    assert list_all(client, calendar)[0] == []

    feed = client.get("/api/changes", params={"since": token, "owner": owner, "calendar": name}).json()
    deletions = {change["uid"] for change in feed["changes"] if change["type"] == "deleted"}
    assert deletions == {"flow-a", "flow-b", "flow-c"}
    assert {c["source"] for c in feed["changes"] if c["type"] == "deleted"} == {"batch"}
    assert client.get("/api/changes", params={"since": feed["next_token"], "owner": owner, "calendar": name}).json()["changes"] == []

    history = client.get(f"{base(calendar)}/flow-a/history").json()["items"]
    assert [entry["type"] for entry in history] == ["created", "updated", "deleted"]
    assert client.get(f"{base(calendar)}/never-existed/history").json()["items"] == []


def test_batch_put_then_delete_same_uid_and_validation_failure(client, calendar):
    owner, name = calendar
    body = simple("bt-1", "20261101T100000Z", "20261101T110000Z")
    result = client.post(
        "/api/batch",
        json={"operations": [
            {"op": "put", "owner": owner, "calendar": name, "uid": "bt-1", "ics": body},
            {"op": "put", "owner": owner, "calendar": name, "uid": "bt-2", "ics": "not ics"},
        ]},
    )
    assert result.status_code == 400 and result.json()["failed_index"] == 1
    assert client.get(f"{base(calendar)}/bt-1").status_code == 404

    result = client.post(
        "/api/batch",
        json={"operations": [
            {"op": "put", "owner": owner, "calendar": name, "uid": "bt-1", "ics": body, "if_none_match": "*"},
            {"op": "delete", "owner": owner, "calendar": name, "uid": "bt-1"},
        ]},
    )
    assert result.status_code == 200
    assert [r["status"] for r in result.json()["results"]] == [201, 200]
    assert client.get(f"{base(calendar)}/bt-1").status_code == 404
    assert client.post("/api/batch", json={"operations": [], "atomic": True}).status_code == 422
    assert client.post("/api/batch", json={"operations": [{"op": "delete", "owner": owner, "calendar": name, "uid": "x"}], "atomic": False}).status_code == 400


def test_change_feed_detects_external_edits(client, calendar, server):
    owner, name = calendar
    put(client, calendar, "ext", simple("ext", "20261101T100000Z", "20261101T110000Z"))
    token = client.get("/api/changes", params={"owner": owner, "calendar": name}).json()["next_token"]
    path = server.collection_path(*calendar) / "ext.ics"
    path.write_bytes(simple("ext", "20261101T100000Z", "20261101T113000Z", "edited on phone").encode())
    feed = client.get("/api/changes", params={"since": token, "owner": owner, "calendar": name}).json()
    assert [(c["uid"], c["type"], c["source"]) for c in feed["changes"]] == [("ext", "updated", "external")]
    path.unlink()
    feed = client.get("/api/changes", params={"since": feed["next_token"], "owner": owner, "calendar": name}).json()
    assert [(c["uid"], c["type"]) for c in feed["changes"]] == [("ext", "deleted")]
    bad = base64.urlsafe_b64encode(json.dumps({"v": 1, "j": "other", "s": 1}).encode()).decode().rstrip("=")
    assert client.get("/api/changes", params={"since": bad}).status_code == 410


# --- attachments --------------------------------------------------------------------


def attach_uris(text):
    calendar = Calendar.from_ical(text)
    event = next(iter(calendar.walk("VEVENT")))
    values = event.get("ATTACH") or []
    return [str(value) for value in (values if isinstance(values, list) else [values])]


def test_attachment_lifecycle(client, calendar):
    put(client, calendar, "att", simple("att", "20991101T100000Z", "20991101T110000Z"))
    upload = client.post(f"{base(calendar)}/att/attachments", files={"file": ("ticket.pdf", b"%PDF-1.4 test", "application/pdf")})
    assert upload.status_code == 200
    record = upload.json()["attachment"]
    assert list(record) == ["id", "owner", "calendar", "uid", "filename", "content_type", "relative_path", "uri", "created_at", "event_end"]
    assert upload.headers["etag"] == client.get(f"{base(calendar)}/att").headers["etag"]
    assert attach_uris(client.get(f"{base(calendar)}/att").text) == [record["uri"]]

    listing = client.get(f"{base(calendar)}/att/attachments").json()["attachments"]
    assert len(listing) == 1
    assert listing[0]["id"] == record["id"] and listing[0]["size"] == 13
    assert listing[0]["in_event"] is True and listing[0]["file_present"] is True

    download = client.get(f"{base(calendar)}/att/attachments/{record['id']}")
    assert download.status_code == 200 and download.content == b"%PDF-1.4 test"
    assert download.headers["content-type"] == "application/pdf"
    assert "attachment" in download.headers["content-disposition"]

    assert client.delete(f"{base(calendar)}/att/attachments/{record['id']}", headers={"If-Match": '"stale"'}).status_code == 412
    deleted = client.delete(f"{base(calendar)}/att/attachments/{record['id']}")
    assert deleted.status_code == 200
    assert attach_uris(client.get(f"{base(calendar)}/att").text) == []
    assert client.get(f"{base(calendar)}/att/attachments").json()["attachments"] == []
    assert client.get(f"{base(calendar)}/att/attachments/{record['id']}").status_code == 404


def test_cleanup_is_gated_and_keeps_attachments_of_running_series(client, calendar):
    # A series that started in the past but runs until 2099 must keep its attachment.
    too_long = ics("series-att", "DTSTART:20200101T090000Z", "DTEND:20200101T100000Z", "RRULE:FREQ=DAILY;COUNT=150000")
    rejected = put(client, calendar, "series-att", too_long)
    assert rejected.status_code == 400 and "CalDAV clients could not load" in rejected.json()["detail"]
    # About 29,000 instances: above Radicale's default limit, accepted with the raised one.
    body = ics("series-att", "DTSTART:20200101T090000Z", "DTEND:20200101T100000Z", "RRULE:FREQ=DAILY;UNTIL=20991231T000000Z", "SUMMARY:s")
    assert put(client, calendar, "series-att", body).status_code == 200
    client.post(f"{base(calendar)}/series-att/attachments", files={"file": ("a.txt", b"a", "text/plain")})
    put(client, calendar, "past-att", simple("past-att", "20200101T090000Z", "20200101T100000Z"))
    client.post(f"{base(calendar)}/past-att/attachments", files={"file": ("b.txt", b"b", "text/plain")})

    assert client.post("/api/cleanup").status_code == 400
    preview = client.post("/api/cleanup", params={"dry_run": "true"}).json()
    targets = {(a["uid"], a["reason"]) for a in preview["attachments"]}
    assert ("past-att", "event_ended") in targets
    assert not any(uid == "series-att" for uid, _ in targets)
    assert len(client.get(f"{base(calendar)}/past-att/attachments").json()["attachments"]) == 1

    result = client.post("/api/cleanup", params={"confirm": "true"}).json()
    assert result["deleted"] >= 1
    assert client.get(f"{base(calendar)}/past-att/attachments").json()["attachments"] == []
    assert attach_uris(client.get(f"{base(calendar)}/past-att").text) == []
    assert len(client.get(f"{base(calendar)}/series-att/attachments").json()["attachments"]) == 1


# --- calendars, export, import ---------------------------------------------------------


def test_calendar_rename_and_delete(client, calendar, server):
    owner, name = calendar
    response = client.patch(base(calendar), json={"display_name": "Household"})
    assert response.status_code == 200
    assert next(c for c in client.get("/api/bootstrap").json()["calendars"] if c["calendar"] == name)["display_name"] == "Household"
    put(client, calendar, "keep", simple("keep", "20261101T100000Z", "20261101T110000Z"))
    assert client.delete(base(calendar)).status_code == 409
    response = client.delete(base(calendar), params={"force": "true"})
    assert response.status_code == 200 and response.json()["events_deleted"] == 1
    assert not server.collection_path(owner, name).exists()
    assert client.delete(base(calendar)).status_code == 404
    assert client.patch(base(calendar), json={"display_name": "x"}).status_code == 404


def test_export_import_round_trip(client, calendar, server):
    owner, name = calendar
    series = ics(
        "rt-series",
        "DTSTART;TZID=Europe/Berlin:20261020T100000",
        "DTEND;TZID=Europe/Berlin:20261020T110000",
        "RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261201T000000Z",
        "EXDATE;TZID=Europe/Berlin:20261103T100000",
        "LOCATION:Room 1",
        "DESCRIPTION:Line one\\nLine two with a long text that will certainly need folding when serialised again",
        "ATTACH;FMTTYPE=application/pdf:https://example.invalid/ticket.pdf",
        "SUMMARY:Series",
        timezone_block=True,
    )
    put(client, calendar, "rt-series", series)
    put(client, calendar, "rt-allday", ics("rt-allday", "DTSTART;VALUE=DATE:20261224", "DTEND;VALUE=DATE:20261227", "SUMMARY:Holiday"))

    export = client.get("/api/export", params={"owner": owner, "calendar": name})
    assert export.status_code == 200
    assert export.headers["x-event-count"] == "2"
    digest = base64.b64encode(hashlib.sha256(export.content).digest()).decode()
    assert export.headers["content-digest"] == f"sha-256=:{digest}:"

    def snapshot():
        result = {}
        for uid in ("rt-series", "rt-allday"):
            event = next(iter(Calendar.from_ical(client.get(f"{base(calendar)}/{uid}").text).walk("VEVENT")))
            result[uid] = {
                key: event.get(key).to_ical() if event.get(key) is not None else None
                for key in ("UID", "DTSTART", "DTEND", "RRULE", "EXDATE", "LOCATION", "DESCRIPTION", "ATTACH", "SUMMARY")
            }
            result[uid]["DTSTART.params"] = dict(event["DTSTART"].params)
        return result

    before = snapshot()
    assert client.delete(base(calendar), params={"force": "true"}).status_code == 200
    restored = client.post(f"{base(calendar)}/import", content=export.content, headers={"Content-Type": "text/calendar"})
    assert restored.status_code == 200, restored.text
    assert {r["uid"] for r in restored.json()["results"]} == {"rt-series", "rt-allday"}
    assert snapshot() == before

    again = client.post(f"{base(calendar)}/import", params={"mode": "create"}, content=export.content)
    assert again.status_code == 412
    skipped = client.post(f"{base(calendar)}/import", params={"mode": "skip_existing"}, content=export.content)
    assert {r["outcome"] for r in skipped.json()["results"]} == {"skipped"}


def test_zip_backup_is_byte_exact(client, calendar, server):
    owner, name = calendar
    put(client, calendar, "zip-1", simple("zip-1", "20991101T100000Z", "20991101T110000Z"))
    client.post(f"{base(calendar)}/zip-1/attachments", files={"file": ("f.bin", b"\x00\x01binary", "application/octet-stream")})
    response = client.get("/api/export", params={"owner": owner, "calendar": name, "format": "zip"})
    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    manifest = json.loads(archive.read("manifest.json"))
    stored = (server.collection_path(owner, name) / "zip-1.ics").read_bytes()
    assert archive.read(f"calendars/{owner}/{name}/events/zip-1.ics") == stored
    event_entry = manifest["calendars"][0]["events"][0]
    assert event_entry["sha256"] == hashlib.sha256(stored).hexdigest()
    attachment = manifest["attachments"][0]
    assert attachment["included"] is True
    assert archive.read(attachment["path"]) == b"\x00\x01binary"
    assert "BEGIN:VCALENDAR" in archive.read(f"calendars/{owner}/{name}/calendar.ics").decode()


# --- validation ----------------------------------------------------------------------


def codes(report):
    return {issue["code"] for issue in report["errors"]}


def test_validate_reports_common_mistakes(client):
    lines = ("DTSTART;TZID=Europe/Berlin:20261020T100000", "DTEND;TZID=Europe/Berlin:20261020T110000",
             "RRULE:FREQ=WEEKLY;UNTIL=20261201T000000Z", "EXDATE;TZID=Europe/Berlin:20261027T100000")
    good = ics("v-good", *lines, timezone_block=True)
    report = client.post("/api/validate", json={"ics": good}).json()
    assert report["valid"] is True, report
    assert report["uid"] == "v-good"

    def check(text, **extra):
        return codes(client.post("/api/validate", json={"ics": text, **extra}).json())

    assert "malformed_line" in check(good.replace("SUMMARY", "").replace("RRULE:", "RRULE "))
    assert "missing_dtstamp" in check(good.replace("DTSTAMP:20260101T000000Z\r\n", ""))
    assert "missing_uid" in check(good.replace("UID:v-good\r\n", ""))
    assert "invalid_property" in check(good.replace("FREQ=WEEKLY", "FREQ=SOMETIMES"))
    assert "missing_vtimezone" in check(ics("v-good", *lines))
    assert "exdate_not_in_recurrence" in check(good.replace("EXDATE;TZID=Europe/Berlin:20261027T100000", "EXDATE;TZID=Europe/Berlin:20261027T090000"))
    assert "exdate_type_mismatch" in check(good.replace("EXDATE;TZID=Europe/Berlin:20261027T100000", "EXDATE;VALUE=DATE:20261027"))
    assert "unbalanced_component" in check(good.replace("END:VEVENT\r\n", ""))
    assert "uid_mismatch" in check(good, uid="other")


# --- CalDAV interoperability ------------------------------------------------------------


def test_dav_clients_see_management_writes(client, calendar, dav_auth):
    owner, name = calendar
    put(client, calendar, "dav-1", simple("dav-1", "20261101T100000Z", "20261101T110000Z", "From API"))
    response = client.get(f"/{owner}/{name}/dav-1.ics", auth=dav_auth, headers={"X-API-Key": ""})
    assert response.status_code == 200
    assert "From API" in response.text
    report = client.request(
        "PROPFIND",
        f"/{owner}/{name}/",
        auth=dav_auth,
        headers={"Depth": "1", "X-API-Key": ""},
    )
    assert report.status_code == 207
    assert "dav-1.ics" in report.text
    info = client.get("/api/dav-info").json()
    assert info["authentication"]["api_key_accepted"] is False


def test_openapi_documents_new_routes(client):
    paths = client.get("/openapi.json").json()["paths"]
    for path in (
        "/api/calendars/{owner}/{calendar_name}/events",
        "/api/batch",
        "/api/changes",
        "/api/export",
        "/api/validate",
        "/api/calendars/{owner}/{calendar_name}/{uid}/history",
        "/api/calendars/{owner}/{calendar_name}/{uid}/attachments/{attachment_id}",
    ):
        assert path in paths
    put_doc = paths["/api/calendars/{owner}/{calendar_name}/{uid}"]["put"]
    assert "412" in put_doc["responses"] and "text/calendar" in put_doc["requestBody"]["content"]
    assert "description" in paths["/api/cleanup"]["post"]
