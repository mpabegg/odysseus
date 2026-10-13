"""Read-only calendar subscriptions by iCal URL.

A subscription mirrors a remote feed into one local calendar: each refresh
upserts the feed's events, deletes the ones that vanished, turns detached
occurrences (RECURRENCE-ID) into standalone rows excluded from their series,
and refuses local edits because the next refresh would revert them.
"""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("icalendar")

from fastapi import HTTPException

from tests.helpers.import_state import clear_fake_database_modules
from tests.helpers.sqlite_db import make_temp_sqlite

clear_fake_database_modules()

import core.database as cdb  # noqa: E402
import routes.calendar_routes as cr  # noqa: E402
import src.caldav_sync as caldav_sync  # noqa: E402
import src.ics_subscriptions as ics  # noqa: E402
from core.database import CalendarCal, CalendarEvent  # noqa: E402

_TS, _ENGINE, _TMPDB = make_temp_sqlite(cdb.Base.metadata)

OWNER = "tester"


def _feed(*events, name="Personal"):
    body = "".join(f"BEGIN:VEVENT\r\n{e.strip()}\r\nEND:VEVENT\r\n" for e in events)
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        f"X-WR-CALNAME:{name}\r\n{body}END:VCALENDAR\r\n"
    ).encode()


WEEKLY = """
UID:standup@x
SUMMARY:Standup
DTSTART:20261005T120000Z
DTEND:20261005T123000Z
RRULE:FREQ=WEEKLY;COUNT=4
EXDATE:20261012T120000Z
""".strip().replace("\n", "\r\n")

MOVED = """
UID:standup@x
RECURRENCE-ID:20261019T120000Z
SUMMARY:Standup (moved)
DTSTART:20261019T150000Z
DTEND:20261019T153000Z
""".strip().replace("\n", "\r\n")

CANCELLED = """
UID:standup@x
RECURRENCE-ID:20261026T120000Z
STATUS:CANCELLED
SUMMARY:Standup
DTSTART:20261026T120000Z
DTEND:20261026T123000Z
""".strip().replace("\n", "\r\n")

DENTIST = """
UID:dentist@x
SUMMARY:Dentist
DTSTART:20261020T170000Z
DTEND:20261020T180000Z
""".strip().replace("\n", "\r\n")

ANCIENT = """
UID:ancient@x
SUMMARY:Old trip
DTSTART:20200101T100000Z
DTEND:20200101T110000Z
""".strip().replace("\n", "\r\n")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    with _TS() as db:
        db.query(CalendarEvent).delete()
        db.query(CalendarCal).delete()
        db.commit()
    store = {"subs": []}
    feed = {"content": _feed(WEEKLY, MOVED, CANCELLED, DENTIST, ANCIENT)}

    async def fake_fetch(url, max_bytes=None):
        if isinstance(feed["content"], Exception):
            raise feed["content"]
        return feed["content"]

    monkeypatch.setattr(cdb, "SessionLocal", _TS)
    monkeypatch.setattr(cr, "SessionLocal", _TS)
    monkeypatch.setattr(cr, "require_user", lambda request: OWNER)
    monkeypatch.setattr(ics, "load_subscriptions", lambda owner: [dict(s) for s in store["subs"]])
    monkeypatch.setattr(ics, "save_subscriptions", lambda owner, subs: store.update(subs=[dict(s) for s in subs]))
    monkeypatch.setattr(ics, "normalize_feed_url", lambda url: url)
    monkeypatch.setattr(ics, "fetch_feed", fake_fetch)
    monkeypatch.setattr(ics, "subscription_url", lambda sub: "https://example.com/basic.ics")
    yield SimpleNamespace(store=store, feed=feed)


def _endpoints():
    eps = {}
    for route in cr.setup_calendar_routes().routes:
        for method in route.methods:
            eps[(method, route.path)] = route.endpoint
    return eps


class _JsonRequest(SimpleNamespace):
    def __init__(self, body=None):
        super().__init__(state=SimpleNamespace(current_user=OWNER))
        self._body = body or {}

    async def json(self):
        return self._body


def _subscribe(label="Google"):
    eps = _endpoints()
    return asyncio.run(eps[("POST", "/api/calendar/subscriptions")](
        _JsonRequest({"label": label, "url": "https://example.com/basic.ics"})
    ))


def _list(start="2026-10-01T00:00:00", end="2026-11-01T00:00:00"):
    eps = _endpoints()
    out = asyncio.run(eps[("GET", "/api/calendar/events")](_JsonRequest(), start=start, end=end))
    return sorted((e["dtstart"], e["summary"]) for e in out["events"])


def test_subscription_mirrors_recurrence_exceptions_and_skips_old_history(_env):
    res = _subscribe()
    assert res["ok"] and res["errors"] == []
    assert "url" not in res["subscription"]

    assert _list() == [
        ("2026-10-05T12:00:00Z", "Standup"),           # 10-12 is an EXDATE
        ("2026-10-19T15:00:00Z", "Standup (moved)"),   # detached occurrence replaces 12:00
        ("2026-10-20T17:00:00Z", "Dentist"),           # 10-26 was cancelled
    ]
    with _TS() as db:
        cal = db.query(CalendarCal).one()
        assert (cal.name, cal.source, cal.owner) == ("Google", "ics", OWNER)
        assert "Old trip" not in {e.summary for e in db.query(CalendarEvent)}
    assert _env.store["subs"][0]["events"] == 3


def test_refresh_reconciles_and_keeps_local_copy_when_feed_fails(_env):
    _subscribe()
    sub = _env.store["subs"][0]
    _env.feed["content"] = _feed(DENTIST.replace("Dentist", "Dentist (new room)"))
    out = asyncio.run(ics.refresh_owner(OWNER))
    assert out["errors"] == [] and out["deleted"] == 2
    assert _list() == [("2026-10-20T17:00:00Z", "Dentist (new room)")]

    _env.feed["content"] = ValueError("Feed returned HTTP 404")
    out = asyncio.run(ics.refresh_owner(OWNER))
    assert out["errors"] == ["Google: Feed returned HTTP 404"]
    assert _list() == [("2026-10-20T17:00:00Z", "Dentist (new room)")]
    assert _env.store["subs"][0]["last_error"] == "Feed returned HTTP 404"
    assert _env.store["subs"][0]["calendar_id"] == sub["calendar_id"]


def test_refresh_is_idempotent(_env):
    _subscribe()
    out = asyncio.run(ics.refresh_owner(OWNER))
    assert (out["events"], out["deleted"]) == (0, 0)


def test_event_writes_on_subscribed_calendar_are_refused(_env):
    _subscribe()
    eps = _endpoints()
    with _TS() as db:
        ev = db.query(CalendarEvent).filter(CalendarEvent.summary == "Dentist").one()
        uid, cal_id = ev.uid, ev.calendar_id

    with pytest.raises(HTTPException) as exc:
        asyncio.run(eps[("PUT", "/api/calendar/events/{uid}")](
            _JsonRequest(), uid, cr.EventUpdate(summary="x")))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        asyncio.run(eps[("DELETE", "/api/calendar/events/{uid}")](_JsonRequest(), uid))
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        asyncio.run(eps[("POST", "/api/calendar/events")](_JsonRequest(), cr.EventCreate(
            summary="x", dtstart="2026-10-21T10:00:00", calendar_href=cal_id)))
    assert exc.value.status_code == 403


def test_agent_tool_refuses_writes_on_subscribed_calendar(_env):
    from src.tools import calendar as cal_tool

    _subscribe()
    with _TS() as db:
        ev = db.query(CalendarEvent).filter(CalendarEvent.summary == "Dentist").one()
        uid, cal_id = ev.uid, ev.calendar_id
    run = cal_tool.do_manage_calendar
    for args in (
        {"action": "update_event", "uid": uid, "summary": "x"},
        {"action": "delete_event", "uid": uid},
        {"action": "create_event", "summary": "x", "dtstart": "2026-10-21T10:00:00", "calendar_href": cal_id},
    ):
        out = asyncio.run(run(json.dumps(args), owner=OWNER))
        assert out == {"error": ics.READ_ONLY_ERROR, "exit_code": 1}, args


def test_sync_endpoint_refreshes_subscriptions_without_caldav(_env, monkeypatch):
    _subscribe()
    _env.feed["content"] = _feed(DENTIST)

    async def no_caldav(owner, direction="pull"):
        return {"calendars": 0, "events": 0, "deleted": 0, "errors": ["CalDAV is not configured"]}

    monkeypatch.setattr(caldav_sync, "sync_caldav_direction", no_caldav)
    out = asyncio.run(_endpoints()[("POST", "/api/calendar/sync")](_JsonRequest()))
    assert out == {"calendars": 1, "events": 0, "deleted": 2, "errors": []}


def test_deleting_the_calendar_removes_the_subscription(_env):
    _subscribe()
    cal_id = _env.store["subs"][0]["calendar_id"]
    asyncio.run(_endpoints()[("DELETE", "/api/calendar/calendars/{cal_id}")](_JsonRequest(), cal_id))
    assert _env.store["subs"] == []


def test_delete_subscription_drops_calendar(_env):
    res = _subscribe()
    asyncio.run(_endpoints()[("DELETE", "/api/calendar/subscriptions/{sub_id}")](
        res["subscription"]["id"], _JsonRequest()))
    assert _env.store["subs"] == []
    with _TS() as db:
        assert db.query(CalendarCal).count() == 0
        assert db.query(CalendarEvent).count() == 0


def test_webcal_scheme_is_https_and_private_hosts_are_rejected(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(caldav_sync, "_validate_caldav_hostname", lambda host: None)
    assert ics.normalize_feed_url("webcal://calendar.example.com/x.ics") == "https://calendar.example.com/x.ics"
    with pytest.raises(ValueError, match="Calendar URL"):
        ics.normalize_feed_url("http://127.0.0.1/x.ics")
    with pytest.raises(ValueError):
        ics.normalize_feed_url("file:///etc/passwd")
