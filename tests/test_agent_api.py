"""Tests for the agent-oriented JSON layer: JSON events, structured errors, search, guide."""

from icalendar import Calendar


def base(calendar):
    owner, name = calendar
    return f"/api/calendars/{owner}/{name}"


def create(client, calendar, **fields):
    return client.post(f"{base(calendar)}/events", json=fields)


def error_code(response):
    return response.json()["error"]["code"]


def test_create_json_event_generates_valid_ics(client, calendar, dav_auth):
    response = create(
        client,
        calendar,
        summary="Dentist",
        start="2026-11-27T15:00",
        end="2026-11-27T16:00",
        timezone="Europe/Berlin",
        location="Main St 1",
        alarms=[{"minutes_before": 30}],
    )
    assert response.status_code == 201, response.text
    body = response.json()
    event = body["event"]
    assert event["start"] == "2026-11-27T15:00:00+01:00"
    assert event["end"] == "2026-11-27T16:00:00+01:00"
    assert event["timezone"] == "Europe/Berlin"
    assert event["duration"] == "PT1H"
    assert event["alarms"] == [{"minutes_before": 30, "description": "Dentist"}]
    assert body["etag"] == event["etag"]

    raw = client.get(f"{base(calendar)}/{event['uid']}")
    assert raw.headers["etag"] == body["etag"]
    parsed = Calendar.from_ical(raw.text)
    assert [tz["TZID"] for tz in parsed.walk("VTIMEZONE")] == ["Europe/Berlin"]
    assert client.post("/api/validate", json={"ics": raw.text}).json()["valid"] is True

    owner, name = calendar
    dav = client.get(f"/{owner}/{name}/{event['href']}", auth=dav_auth, headers={"X-API-Key": ""})
    assert dav.status_code == 200 and "SUMMARY:Dentist" in dav.text


def test_create_detects_duplicates(client, calendar):
    first = create(client, calendar, title="Team lunch", start="2026-11-27T12:00", duration="PT1H", timezone="Europe/Berlin")
    assert first.status_code == 201
    uid = first.json()["event"]["uid"]

    again = create(client, calendar, summary="team  LUNCH", start="2026-11-27T11:00:00Z", duration="PT1H", timezone="Europe/Berlin")
    assert again.status_code == 409
    error = again.json()["error"]
    assert error["code"] == "duplicate_event" and error["details"]["duplicates"][0]["uid"] == uid
    assert "detail" in again.json()

    existing = create(client, calendar, summary="Team lunch", start="2026-11-27T12:00", duration="PT1H",
                      timezone="Europe/Berlin", on_duplicate="return_existing")
    assert existing.status_code == 200
    assert existing.json()["created"] is False and existing.json()["duplicate_of"] == uid

    allowed = create(client, calendar, summary="Team lunch", start="2026-11-27T12:00", duration="PT1H",
                     timezone="Europe/Berlin", on_duplicate="allow")
    assert allowed.status_code == 201

    taken = create(client, calendar, uid=uid, summary="Other", start="2026-12-01")
    assert taken.status_code == 409 and error_code(taken) == "uid_exists"


def test_create_validation_errors_are_actionable(client, calendar):
    missing_end = create(client, calendar, summary="x", start="2026-11-27T10:00")
    assert missing_end.status_code == 400
    assert error_code(missing_end) == "invalid_field" and missing_end.json()["error"]["details"]["field"] == "end"
    assert "duration" in missing_end.json()["error"]["hint"]

    same_day = create(client, calendar, summary="x", start="2026-11-27", end="2026-11-27")
    assert same_day.status_code == 400 and "exclusive" in same_day.json()["error"]["hint"]

    typo = client.post(f"{base(calendar)}/events", json={"summary": "x", "start": "2026-11-27", "begin": "oops"})
    assert typo.status_code == 422
    assert error_code(typo) == "invalid_request" and "begin" in typo.json()["error"]["message"]

    bad_zone = create(client, calendar, summary="x", start="2026-11-27T10:00", duration="PT1H", timezone="Berlin")
    assert bad_zone.status_code == 400 and bad_zone.json()["error"]["details"]["field"] == "timezone"

    bad_exdate = create(client, calendar, summary="x", start="2026-11-02T09:00", duration="PT1H", timezone="Europe/Berlin",
                        rrule="FREQ=WEEKLY", exdates=["2026-11-09T10:00"])
    assert bad_exdate.status_code == 400 and error_code(bad_exdate) == "invalid_event"
    assert bad_exdate.json()["error"]["details"]["errors"][0]["code"] == "exdate_not_in_recurrence"

    assert client.post("/api/calendars/tester/does-not-exist/events", json={"summary": "x", "start": "2026-11-27"}).status_code == 404


def test_all_day_and_recurring_series_with_until_date(client, calendar):
    holiday = create(client, calendar, summary="Holiday", start="2026-12-24", end="2026-12-27")
    assert holiday.status_code == 201
    assert holiday.json()["event"]["all_day"] is True

    series = create(client, calendar, summary="Standup", start="2026-11-02T09:00", duration="PT15M",
                    timezone="Europe/Berlin", rrule="FREQ=WEEKLY;BYDAY=MO;UNTIL=2026-11-30")
    assert series.status_code == 201, series.text
    event = series.json()["event"]
    assert event["rrule"] == "FREQ=WEEKLY;BYDAY=MO;UNTIL=20261130T225959Z"
    listing = client.get(f"{base(calendar)}/events", params={
        "from": "2026-11-01T00:00:00Z", "to": "2026-12-31T00:00:00Z", "expand_recurrence": "true", "q": "standup"}).json()
    assert [item["dtstart"][:10] for item in listing["items"]] == ["2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23", "2026-11-30"]


def test_patch_keeps_duration_and_unrelated_properties(client, calendar):
    created = create(client, calendar, summary="Review", start="2026-11-02T09:00", duration="PT45M", timezone="Europe/Berlin",
                     rrule="FREQ=WEEKLY;COUNT=4", exdates=["2026-11-09T09:00"])
    event = created.json()["event"]
    url = f"{base(calendar)}/{event['uid']}"
    # A property the JSON layer does not model must survive a PATCH.
    raw = client.get(url).text.replace("SUMMARY:Review", "SUMMARY:Review\r\nX-CUSTOM:keep-me")
    etag = client.put(url, content=raw).headers["etag"]

    stale = client.patch(url, json={"location": "Room 2"}, headers={"If-Match": '"stale"'})
    assert stale.status_code == 412
    assert stale.json()["error"]["retryable"] is True and stale.json()["error"]["details"]["current_etag"] == etag

    moved = client.patch(url, json={"start": "2026-11-02T10:30", "location": "Room 2"}, headers={"If-Match": etag})
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["event"]["end"] == "2026-11-02T11:15:00+01:00"
    assert body["event"]["exdates"] == ["2026-11-09T10:30:00+01:00"]  # moved with the series
    assert body["event"]["location"] == "Room 2"
    assert body["event"]["sequence"] == 1
    assert "X-CUSTOM" in body["event"]["other_properties"]
    assert "X-CUSTOM:keep-me" in client.get(url).text

    skipped = client.patch(url, json={"add_exdates": ["2026-11-16T10:30"]}, headers={"If-Match": body["etag"]})
    assert skipped.status_code == 200
    assert len(skipped.json()["event"]["exdates"]) == 2

    cleared = client.patch(url, json={"location": None, "rrule": None, "exdates": []})
    assert cleared.status_code == 200
    assert cleared.json()["event"]["location"] is None and cleared.json()["event"]["rrule"] is None

    preview = client.patch(url, json={"summary": "Renamed"}, params={"dry_run": "true"})
    assert preview.json()["event"]["summary"] == "Renamed"
    assert client.get(url, params={"format": "json"}).json()["summary"] == "Review"
    assert client.patch(f"{base(calendar)}/missing", json={"summary": "x"}).json()["error"]["code"] == "event_not_found"
    assert client.patch(url, json={}).json()["error"]["code"] == "empty_patch"


def test_get_event_as_json(client, calendar):
    created = create(client, calendar, summary="Read me", start="2026-11-27T15:00", duration="PT1H", timezone="Europe/Berlin")
    uid = created.json()["event"]["uid"]
    response = client.get(f"{base(calendar)}/{uid}", params={"format": "json"})
    assert response.status_code == 200
    assert response.headers["etag"] == response.json()["etag"]
    assert response.json()["summary"] == "Read me"
    assert client.get(f"{base(calendar)}/{uid}").headers["content-type"].startswith("text/calendar")


def test_search_across_calendars(client, calendar, server):
    owner, name = calendar
    other = client.post("/api/calendars", json={"owner": owner, "calendar": name + "-b"}).json()
    created = create(client, calendar, summary="Concert", start="2026-11-27T20:00", duration="PT2H", timezone="Europe/Berlin")
    uid = created.json()["event"]["uid"]
    copy = client.get(f"{base(calendar)}/{uid}").text
    client.put(f"/api/calendars/{owner}/{other['calendar']}/{uid}", content=copy)

    found = client.get("/api/events", params={"uid": uid}).json()
    assert sorted(item["calendar"] for item in found["items"]) == sorted([name, other["calendar"]])
    assert all(item["owner"] == owner for item in found["items"])
    found = client.get("/api/events", params={"q": "concert", "owner": owner, "from": "2026-11-27T00:00:00Z", "to": "2026-11-28T00:00:00Z"}).json()
    assert len(found["items"]) == 2


def test_errors_are_structured_everywhere(client, calendar):
    unauthorized = client.get("/api/bootstrap", headers={"X-API-Key": "wrong"})
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "unauthorized" and "X-API-Key" in unauthorized.json()["error"]["hint"]
    assert unauthorized.json()["detail"] == "Missing or invalid X-API-Key"

    missing = client.get(f"{base(calendar)}/nope")
    assert missing.status_code == 404 and missing.json() == {"detail": "Event not found", "error": missing.json()["error"]}

    unknown = client.get("/api/no-such-endpoint")
    assert unknown.status_code == 404 and error_code(unknown) == "unknown_endpoint"

    calendars = client.get("/api/calendars").json()["calendars"]
    entry = next(item for item in calendars if item["calendar"] == calendar[1])
    assert entry["event_count"] == 0 and entry["display_name"]


def test_guide_and_operation_ids(client):
    guide = client.get("/api/guide", headers={"X-API-Key": ""})
    assert guide.status_code == 200 and guide.headers["content-type"].startswith("text/markdown")
    assert "If-Match" in guide.text
    spec = client.get("/openapi.json").json()
    operation_ids = {op["operationId"] for path in spec["paths"].values() for op in path.values()}
    assert {"list_events", "create_event", "patch_event", "search_events", "batch"} <= operation_ids
