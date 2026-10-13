"""Read-only calendar subscriptions by iCal URL.

Some providers (notably Google) no longer accept password logins over CalDAV,
but every major provider publishes a private iCal feed URL. A subscription
stores that URL in the owner's prefs (encrypted, since the URL itself is the
credential) and mirrors the feed into one local `CalendarCal` row with
`source="ics"`.

Design notes:
- Each refresh downloads the whole feed and reconciles it: rows are upserted
  by a deterministic uid derived from (calendar, VEVENT UID, RECURRENCE-ID),
  and rows that vanished from the feed are deleted. A feed that fails to
  download or parse leaves the local copy untouched.
- Detached occurrences (RECURRENCE-ID) become standalone rows and their
  original start is added to the series' `recurrence_exdates`, the same
  convention the email-invitation importer uses. Cancelled occurrences only
  add the exdate.
- The calendar is read-only: event writes against it are refused by the
  routes and the agent tool, because the next refresh would revert them.
- Refreshes run on calendar open (via /api/calendar/sync) and from a
  background loop every ODYSSEUS_ICS_REFRESH_MINUTES (default 15).
"""

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)

PREFS_KEY = "ics_subscriptions"
SOURCE = "ics"
READ_ONLY_ERROR = "This calendar is a read-only URL subscription; change the event at its source"
_FETCH_TIMEOUT = 30.0
_MAX_REDIRECTS = 3
# Non-recurring events that ended before this are not mirrored. Google feeds
# carry the account's whole history, which the calendar UI never shows.
_HISTORY_DAYS = 400


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _refresh_interval_seconds() -> int:
    try:
        minutes = int(os.environ.get("ODYSSEUS_ICS_REFRESH_MINUTES", "15"))
    except ValueError:
        minutes = 15
    return max(minutes, 1) * 60


def normalize_feed_url(raw_url: str) -> str:
    """Validate a feed URL with the CalDAV SSRF rules; webcal:// means https://."""
    from src.caldav_sync import validate_caldav_url

    url = (raw_url if isinstance(raw_url, str) else "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    try:
        return validate_caldav_url(url)
    except ValueError as e:
        raise ValueError(str(e).replace("CalDAV URL", "Calendar URL")) from None


def calendar_id_for(owner: str, sub_id: str) -> str:
    h = hashlib.sha256(f"{owner}\n{sub_id}".encode("utf-8")).hexdigest()[:24]
    return f"ics-{h}"


def _event_uid(cal_id: str, source_uid: str, recurrence_key: str) -> str:
    h = hashlib.sha256(json.dumps([cal_id, source_uid, recurrence_key]).encode("utf-8")).hexdigest()[:32]
    return f"ics-{h}"


# ── Prefs storage ──

def load_subscriptions(owner: str) -> list:
    from routes.prefs_routes import _load_for_user

    subs = (_load_for_user(owner) or {}).get(PREFS_KEY) or []
    return [s for s in subs if isinstance(s, dict) and s.get("id")]


def save_subscriptions(owner: str, subs: list) -> None:
    from routes.prefs_routes import _load_for_user, _save_for_user

    prefs = _load_for_user(owner) or {}
    prefs[PREFS_KEY] = subs
    _save_for_user(owner, prefs)


def subscription_url(sub: dict) -> str:
    from src.secret_storage import decrypt

    return decrypt(sub.get("url") or "")


def public_view(sub: dict) -> dict:
    """Subscription without its secret URL — only the host is shown."""
    host = urlparse(subscription_url(sub)).hostname or ""
    return {
        "id": sub.get("id"),
        "label": sub.get("label") or "",
        "host": host,
        "calendar_id": sub.get("calendar_id"),
        "last_refresh": sub.get("last_refresh"),
        "last_error": sub.get("last_error"),
        "events": sub.get("events", 0),
    }


def new_subscription(owner: str, label: str, url: str) -> dict:
    from src.secret_storage import encrypt

    sub_id = str(uuid.uuid4())
    return {
        "id": sub_id,
        "label": label,
        "url": encrypt(url),
        "calendar_id": calendar_id_for(owner, sub_id),
    }


def remove_subscription_for_calendar(owner: str, cal_id: str) -> None:
    subs = load_subscriptions(owner)
    kept = [s for s in subs if s.get("calendar_id") != cal_id]
    if len(kept) != len(subs):
        save_subscriptions(owner, kept)


# ── Fetch ──

async def fetch_feed(url: str, max_bytes: int | None = None) -> bytes:
    """GET a feed with a byte cap, re-validating every redirect hop."""
    import httpx
    from src.upload_limits import ICS_MAX_BYTES

    limit = max_bytes or ICS_MAX_BYTES
    current = url
    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=False) as client:
        for _ in range(_MAX_REDIRECTS + 1):
            async with client.stream("GET", current, headers={"Accept": "text/calendar"}) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location") or ""
                    current = await asyncio.to_thread(normalize_feed_url, urljoin(current, location))
                    continue
                if resp.status_code != 200:
                    raise ValueError(f"Feed returned HTTP {resp.status_code}")
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise ValueError(f"Feed is larger than {limit // (1024 * 1024)} MB")
                    chunks.append(chunk)
                return b"".join(chunks)
    raise ValueError("Feed redirected too many times")


# ── Parse ──

def _occurrence_key(value, all_day: bool) -> str:
    """Exdate key in the format `_expand_rrule` compares against."""
    from routes.calendar_routes import _ics_naive_dtstart

    if all_day or (isinstance(value, date) and not isinstance(value, datetime)):
        return value.strftime("%Y-%m-%d")
    return _ics_naive_dtstart(value).strftime("%Y-%m-%dT%H:%M")


def _event_times(comp):
    """(start, end, all_day, is_utc) stored the same way import_ics stores them."""
    from routes.calendar_routes import _ensure_positive_duration
    from src.caldav_sync import _to_utc_naive

    dtstart_p = comp.get("dtstart")
    start_dt, all_day = _to_utc_naive(dtstart_p.dt)
    dtend_p = comp.get("dtend")
    if dtend_p:
        end_dt, _ = _to_utc_naive(dtend_p.dt)
    elif comp.get("duration"):
        end_dt = start_dt + comp.get("duration").dt
    elif all_day:
        end_dt = start_dt + timedelta(days=1)
    else:
        end_dt = start_dt + timedelta(hours=1)
    end_dt = _ensure_positive_duration(start_dt, end_dt, all_day)
    is_utc = not all_day and isinstance(dtstart_p.dt, datetime) and dtstart_p.dt.tzinfo is not None
    return start_dt, end_dt, all_day, is_utc


def _exdates(comp, all_day: bool) -> set:
    raw = comp.get("exdate")
    if raw is None:
        return set()
    out = set()
    for entry in raw if isinstance(raw, list) else [raw]:
        for item in getattr(entry, "dts", []):
            out.add(_occurrence_key(item.dt, all_day))
    return out


def parse_feed(content: bytes, cal_id: str, now: datetime | None = None) -> tuple[str, list[dict]]:
    """Return (feed name, event rows) for a raw iCal feed. Raises ValueError
    when the feed cannot be parsed at all."""
    from icalendar import Calendar as iCal

    try:
        cal = iCal.from_ical(content)
    except Exception as e:
        raise ValueError(f"Invalid iCal feed: {e}") from None
    feed_name = str(cal.get("x-wr-calname", "") or "").strip()
    cutoff = (now or _utcnow()) - timedelta(days=_HISTORY_DAYS)

    masters: dict[str, dict] = {}
    overrides: list[tuple[str, str, dict | None]] = []
    for comp in cal.walk("VEVENT"):
        source_uid = str(comp.get("uid", "") or "").strip()
        if not comp.get("dtstart"):
            continue
        if not source_uid:
            source_uid = hashlib.sha256(comp.to_ical()).hexdigest()
        try:
            start_dt, end_dt, all_day, is_utc = _event_times(comp)
        except Exception as e:
            logger.debug("Skipping unreadable VEVENT %s: %s", source_uid, e)
            continue
        cancelled = str(comp.get("status", "")).upper() == "CANCELLED"
        recurrence = comp.get("recurrence-id")
        rrule = comp.get("rrule").to_ical().decode() if comp.get("rrule") and recurrence is None else ""
        row = None
        if not cancelled:
            row = {
                "summary": str(comp.get("summary", "") or ""),
                "description": str(comp.get("description", "") or ""),
                "location": str(comp.get("location", "") or ""),
                "dtstart": start_dt,
                "dtend": end_dt,
                "all_day": all_day,
                "is_utc": is_utc,
                "rrule": rrule,
                "exdates": _exdates(comp, all_day) if rrule else set(),
            }
        if recurrence is not None:
            key = _occurrence_key(recurrence.dt, all_day)
            if row:
                row["uid"] = _event_uid(cal_id, source_uid, key)
            overrides.append((source_uid, key, row))
        elif row:
            row["uid"] = _event_uid(cal_id, source_uid, "")
            masters[source_uid] = row

    for source_uid, key, _ in overrides:
        master = masters.get(source_uid)
        if master and master["rrule"]:
            master["exdates"].add(key)

    rows = list(masters.values()) + [row for _, _, row in overrides if row]
    kept = [r for r in rows if r["rrule"] or r["dtend"] >= cutoff]
    for r in kept:
        r["recurrence_exdates"] = json.dumps(sorted(r.pop("exdates"))) if r["exdates"] else ""
    return feed_name, kept


# ── Reconcile ──

def _apply_rows(owner: str, sub: dict, feed_name: str, rows: list[dict]) -> dict:
    from core.database import CalendarCal, CalendarEvent, SessionLocal

    cal_id = sub["calendar_id"]
    db = SessionLocal()
    try:
        cal = db.query(CalendarCal).filter(CalendarCal.id == cal_id, CalendarCal.owner == owner).first()
        if not cal:
            cal = CalendarCal(
                id=cal_id,
                owner=owner,
                name=(sub.get("label") or feed_name or "Subscribed")[:120],
                color="#4caf50",
                source=SOURCE,
            )
            db.add(cal)
            db.flush()
        existing = {
            ev.uid: ev
            for ev in db.query(CalendarEvent).filter(CalendarEvent.calendar_id == cal_id).all()
        }
        fields = ("summary", "description", "location", "dtstart", "dtend",
                  "all_day", "is_utc", "rrule", "recurrence_exdates")
        added = updated = 0
        seen = set()
        for row in rows:
            uid = row["uid"]
            if uid in seen:
                continue
            seen.add(uid)
            ev = existing.get(uid)
            if ev is None:
                db.add(CalendarEvent(uid=uid, calendar_id=cal_id, origin=SOURCE,
                                     **{f: row[f] for f in fields}))
                added += 1
                continue
            changed = False
            for f in fields:
                if getattr(ev, f) != row[f]:
                    setattr(ev, f, row[f])
                    changed = True
            updated += changed
        stale = [ev for uid, ev in existing.items() if uid not in seen]
        for ev in stale:
            db.delete(ev)
        db.commit()
        return {"events": len(seen), "added": added, "updated": updated, "deleted": len(stale)}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def refresh_subscription(owner: str, sub: dict) -> dict:
    url = await asyncio.to_thread(normalize_feed_url, subscription_url(sub))
    content = await fetch_feed(url)
    feed_name, rows = await asyncio.to_thread(parse_feed, content, sub["calendar_id"])
    return await asyncio.to_thread(_apply_rows, owner, sub, feed_name, rows)


async def refresh_owner(owner: str) -> dict:
    """Refresh every subscription of `owner`. Returns counts in the same
    shape as the CalDAV pull so /api/calendar/sync can merge them."""
    totals = {"calendars": 0, "events": 0, "deleted": 0, "errors": []}
    subs = load_subscriptions(owner)
    if not subs:
        return totals
    stamp = _utcnow().isoformat(timespec="seconds") + "Z"
    for sub in subs:
        label = sub.get("label") or sub.get("id")
        try:
            result = await refresh_subscription(owner, sub)
            totals["calendars"] += 1
            totals["events"] += result["added"] + result["updated"]
            totals["deleted"] += result["deleted"]
            sub.update(last_refresh=stamp, last_error=None, events=result["events"])
        except Exception as e:
            logger.warning("iCal subscription %s refresh failed: %s", label, e)
            message = str(e)[:200] or type(e).__name__
            totals["errors"].append(f"{label}: {message}")
            sub.update(last_refresh=stamp, last_error=message)
    # Re-read before writing so a subscription added or removed meanwhile
    # is not clobbered by this refresh's copy.
    status = {s["id"]: s for s in subs}
    current = load_subscriptions(owner)
    for s in current:
        if s["id"] in status:
            for k in ("last_refresh", "last_error", "events"):
                s[k] = status[s["id"]].get(k)
    save_subscriptions(owner, current)
    return totals


def _owners_with_subscriptions() -> list[str]:
    from routes.prefs_routes import _load

    users = (_load() or {}).get("_users")
    if not isinstance(users, dict):
        return []
    return [name for name, prefs in users.items() if isinstance(prefs, dict) and prefs.get(PREFS_KEY)]


async def refresh_loop() -> None:
    while True:
        await asyncio.sleep(_refresh_interval_seconds())
        try:
            for owner in await asyncio.to_thread(_owners_with_subscriptions):
                await refresh_owner(owner)
        except Exception as e:
            logger.warning("iCal subscription loop error: %s", e)
