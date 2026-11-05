#!/usr/bin/env python3
"""
PagerDuty filter: only rings for incidents whose title contains one of your customers.

- Regularly fetches open (triggered) incidents via the PagerDuty REST API
- Checks the title against the customer list (case-insensitive, substring)
- On a match, sends a push message with the highest priority via ntfy
- Remembers already reported incidents in seen.json (no double alarm after a restart)
- Runs until you stop it. Optionally only within a time window (ONLY_IN_WINDOW=true).

Customer list: file customers.txt (re-read on every poll)
or variable CUSTOMERS (comma- or newline-separated).

Configuration: file config.env in the same folder (KEY=value per line)
or environment variables (these take precedence):
  PD_API_TOKEN        personal PagerDuty REST API key (required)
  NTFY_TOPIC          your secret ntfy topic name (required)
  CUSTOMERS           optional, e.g. "CustomerA,CustomerB"
  NTFY_SERVER         optional, default: https://ntfy.sh
  POLL_SECONDS        optional, default: 30
  PD_SERVICE_IDS      optional, comma-separated: only query these services
  SERVICE_FILTER      optional, e.g. "CSOC (High)": only ring if the service name contains
                      this text (case-insensitive, several separated by ";").
                      Empty = all services.
  SILENT_FOR          optional, what gets a silent notification (no sound):
                        customers = your customers when the service doesn't match (default)
                        all       = additionally all other incidents
                        none      = only the loud alarms
  NTFY_TOPIC_SILENT   optional, separate ntfy topic for the silent notifications
  TZ_NAME             optional, default: Europe/Berlin
  ONLY_ON_CALL        optional, default: false. true = only active while PagerDuty says you are
                      currently on call in a schedule (otherwise paused, no notifications)
  OVERRIDE_USER       optional, default: CSOC Dummy. PagerDuty user (name or email) that the
                      "Hand off on-call" button hands the shift to via an override
  ONLY_IN_WINDOW      optional, default: false. true = only active from START_HOUR to END_HOUR
  START_HOUR          optional, default: 8
  END_HOUR            optional, default: 18
  WEEKDAYS_ONLY       optional, default: true (only applies with ONLY_IN_WINDOW=true)
  LAS_END_HOUR        optional, default: 18. PD notifications turned on via "LAS"
                      (Last Analyst Standing) run until then and are removed automatically afterwards
  LAS_CONTACTS        optional, comma-separated IDs of the PagerDuty contacts (devices, SMS, call, email)
                      that LAS / "On (no end)" turns on. Empty = all devices with the PagerDuty app (push)
  LAS_URGENCY         optional, default: high. Which urgency: high | low | both
Privacy is always on (QUIET): no incident titles in the log, no customer data sent to ntfy.
  WEB_PORT            optional, default: 8790 (dashboard, 127.0.0.1 only). 0 = off
  PD_FROM_EMAIL       optional, only for account keys: email for the From header on ack
"""

import json
import os
import re
import signal
import sys
import threading
import time
import unicodedata
from collections import deque
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import paw_exchange

BASE_DIR = Path(__file__).resolve().parent
CUSTOMERS_FILE = BASE_DIR / "customers.txt"
SEEN_FILE = BASE_DIR / "seen.json"
OVERRIDES_FILE = BASE_DIR / "overrides.json"
LAS_FILE = BASE_DIR / "las.json"
CONFIG_FILE = BASE_DIR / "config.env"


def load_config_file():
    """Read config.env; environment variables that are already set are not overwritten."""
    if not CONFIG_FILE.exists():
        return
    for line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_config_file()


def env_bool(name, default):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "ja")


# Old German config values keep working
LEGACY_VALUES = {
    "SILENT_FOR": {"kunden": "customers", "alle": "all", "keine": "none"},
    "LAS_URGENCY": {"hoch": "high", "niedrig": "low", "beide": "both"},
}


def legacy_value(key, value):
    return LEGACY_VALUES.get(key, {}).get(value, value)


PD_API_TOKEN = os.environ.get("PD_API_TOKEN")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "30"))
PD_SERVICE_IDS = [s.strip() for s in os.environ.get("PD_SERVICE_IDS", "").split(",") if s.strip()]
SERVICE_FILTER = [s.strip().lower() for s in os.environ.get("SERVICE_FILTER", "").split(";") if s.strip()]
SERVICE_FILTER_TEXT = os.environ.get("SERVICE_FILTER", "").strip()     # original spelling for display
SILENT_FOR = legacy_value("SILENT_FOR", os.environ.get("SILENT_FOR", "customers").strip().lower())
if SILENT_FOR not in ("customers", "all", "none"):
    SILENT_FOR = "customers"
NTFY_TOPIC_SILENT = os.environ.get("NTFY_TOPIC_SILENT", "").strip()      # optional separate topic for silent
TZ = ZoneInfo(os.environ.get("TZ_NAME", "Europe/Berlin"))
START_HOUR = int(os.environ.get("START_HOUR", "8"))
END_HOUR = int(os.environ.get("END_HOUR", "18"))
WEEKDAYS_ONLY = env_bool("WEEKDAYS_ONLY", True)
ONLY_IN_WINDOW = env_bool("ONLY_IN_WINDOW", False)
ONLY_ON_CALL = env_bool("ONLY_ON_CALL", False)
OVERRIDE_USER = os.environ.get("OVERRIDE_USER", "CSOC Dummy").strip()
LAS_END_HOUR = int(os.environ.get("LAS_END_HOUR", "18"))
LAS_CONTACTS = ",".join(s.strip() for s in os.environ.get("LAS_CONTACTS", "").split(",") if s.strip())
LAS_URGENCY = legacy_value("LAS_URGENCY", os.environ.get("LAS_URGENCY", "high").strip().lower())
if LAS_URGENCY not in ("high", "low", "both"):
    LAS_URGENCY = "high"
QUIET = True   # always on, cannot be turned off via config.env or the dashboard
WEB_PORT = int(os.environ.get("WEB_PORT", "8790") or 0)   # 0 = dashboard off
PD_FROM_EMAIL = os.environ.get("PD_FROM_EMAIL", "").strip()
PD_API = "https://api.pagerduty.com"
CONSOLE_FILE = BASE_DIR / "console.html"      # main view (also works standalone via file://)
DASHBOARD_FILE = BASE_DIR / "dashboard.html"
MINI_FILE = BASE_DIR / "mini.html"          # compact view for the extension popup
PAW_FILE = BASE_DIR / "paw.html"            # PAW Exchange: analyze uploads from the PAW with one click

# State for the dashboard
STATE_LOCK = threading.Lock()
STATE = {
    "started": None,
    "last_poll": None,
    "last_error": None,
    "paused": None,             # None = active, otherwise reason for the pause
    "poll_seconds": POLL_SECONDS,
    "customers": [],
    "service_filter": SERVICE_FILTER,
    "incidents": [],            # currently open incidents with decision
    "acked": [],                # acknowledged: my customers (by anyone) + by me (or all if own user unknown)
    "acked_mine": False,        # True = "acked" is filtered to my user
    "shift": None,              # current shift: {"schedules": [...], "end": ISO time}
    "override_user": OVERRIDE_USER,
    "overrides": [],            # overrides I set that are still running: [{"schedule", "end", ...}]
    "las": None,                # push rules active: {"mode": "las"|"shift", "until": ISO time or None}
    "customers_editable": not os.environ.get("CUSTOMERS", "").strip(),
    "paw": paw_exchange.enabled(),   # show link to the PAW page
}
EVENTS = deque(maxlen=300)      # history (newest first)

PD_HEADERS = {
    "Authorization": f"Token token={PD_API_TOKEN}",
    "Accept": "application/vnd.pagerduty+json;version=2",
}


def now_str():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(now_str(), msg, flush=True)


def add_event(kind, text, inc=None, customer=None):
    """kind: alarm | service | customer | error | info"""
    ev = {"time": now_str(), "kind": kind, "text": text}
    if inc:
        ev.update(title=inc.get("title", ""), service=service_name(inc),
                  url=inc.get("html_url", ""), customer=customer)
    with STATE_LOCK:
        EVENTS.appendleft(ev)


def show(title):
    return "(title hidden)" if QUIET else title


def in_time_window(now=None):
    if not ONLY_IN_WINDOW:
        return True
    now = now or datetime.now(TZ)
    if WEEKDAYS_ONLY and now.weekday() >= 5:
        return False
    return START_HOUR <= now.hour < END_HOUR


def load_customers():
    """From CUSTOMERS (env) or customers.txt. Empty lines and # comments are ignored."""
    raw = os.environ.get("CUSTOMERS")
    if raw and raw.strip():
        raw = raw.replace(",", "\n")   # in the variable: comma- or newline-separated
    else:
        if not CUSTOMERS_FILE.exists():
            return []
        raw = CUSTOMERS_FILE.read_text(encoding="utf-8")
    global _RTF_WARNED
    if raw.lstrip().startswith("{\\rtf") and not _RTF_WARNED:
        _RTF_WARNED = True
        add_event("error", "customers.txt is rich text instead of plain text")
        log("WARNING: customers.txt is rich text (TextEdit). Please save it as plain text: "
            "Format -> Make Plain Text.")
    customers = []
    for line in raw.splitlines():
        line = norm(line)
        if line and not line.startswith("#"):
            customers.append(line)
    return customers


# ---------- Settings from the dashboard ----------

# Editable in the dashboard (no secrets): key -> type
EDITABLE = {
    "SILENT_FOR": ("customers", "all", "none"),
    "SERVICE_FILTER": str,
    "ONLY_ON_CALL": bool,
    "OVERRIDE_USER": str,
    "LAS_END_HOUR": int,
    "LAS_CONTACTS": str,
    "LAS_URGENCY": ("high", "low", "both"),
    "ONLY_IN_WINDOW": bool,
    "START_HOUR": int,
    "END_HOUR": int,
    "WEEKDAYS_ONLY": bool,
    "POLL_SECONDS": int,
}
SETTINGS_LOCK = threading.Lock()


def current_settings():
    g = globals()
    return {k: SERVICE_FILTER_TEXT if k == "SERVICE_FILTER" else g[k] for k in EDITABLE}


def parse_setting(key, value):
    """Validate a value from the dashboard. Returns (Python value, text for config.env)."""
    kind = EDITABLE[key]
    if kind is bool:
        if not isinstance(value, bool):
            raise ValueError("expected true/false")
        return value, "true" if value else "false"
    text = " ".join(str(value).split())
    if len(text) > 200:
        raise ValueError("too long")
    if isinstance(kind, tuple):
        text = legacy_value(key, text)
        if text not in kind:
            raise ValueError("allowed: " + ", ".join(kind))
        return text, text
    if kind is int:
        number = int(text)
        low, high = (5, 3600) if key == "POLL_SECONDS" else (0, 24)
        if not low <= number <= high:
            raise ValueError(f"allowed: {low} to {high}")
        return number, str(number)
    if key == "SERVICE_FILTER":
        parts = [s.strip() for s in text.split(";") if s.strip()]
        return [s.lower() for s in parts], ";".join(parts)
    if key == "LAS_CONTACTS":
        ids = [s.strip() for s in text.split(",") if s.strip()]
        if not all(ID_RE.match(i) for i in ids):
            raise ValueError("invalid contact ID")
        return ",".join(ids), ",".join(ids)
    return text, text


def write_config(values):
    """Set values in config.env: replace the existing line, otherwise append. Comments stay."""
    lines = CONFIG_FILE.read_text(encoding="utf-8").splitlines() if CONFIG_FILE.exists() else []
    todo = dict(values)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if not line.lstrip().startswith("#") and "=" in line and key in todo:
            lines[i] = f"{key}={todo.pop(key)}"
    lines += [f"{k}={v}" for k, v in todo.items()]
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(CONFIG_FILE)


def update_settings(changes):
    """Validate settings, apply them immediately and save them in config.env.
    Returns (error text or None, list of changed keys)."""
    parsed, lines = {}, {}
    for key, value in changes.items():
        if key not in EDITABLE:
            return f"{key} cannot be changed", []
        try:
            parsed[key], lines[key] = parse_setting(key, value)
        except (ValueError, TypeError) as e:
            return f"{key}: {e}", []
    with SETTINGS_LOCK:
        changed = [k for k in parsed if globals()[k] != parsed[k]]
        try:
            write_config({k: lines[k] for k in changed})
        except OSError as e:
            return f"config.env not saved: {e}", []
        globals().update({k: parsed[k] for k in changed})
        if "SERVICE_FILTER" in changed:
            globals()["SERVICE_FILTER_TEXT"] = lines["SERVICE_FILTER"]
    _ON_CALL_CACHE["at"] = 0.0
    with STATE_LOCK:
        STATE.update(settings=current_settings(), service_filter=SERVICE_FILTER,
                     poll_seconds=POLL_SECONDS, override_user=OVERRIDE_USER)
    return None, sorted(changed)


CUSTOMERS_FROM_ENV = bool(os.environ.get("CUSTOMERS", "").strip())
CUSTOMERS_LOCK = threading.Lock()


def edit_customers(add=None, remove=None):
    """Add or remove customers in customers.txt (from the dashboard).
    Comments and other lines stay. Returns error text or None."""
    if CUSTOMERS_FROM_ENV:
        return "Customers come from the CUSTOMERS variable, not from customers.txt"
    with CUSTOMERS_LOCK:
        raw = CUSTOMERS_FILE.read_text(encoding="utf-8") if CUSTOMERS_FILE.exists() else ""
        if raw.lstrip().startswith("{\\rtf"):
            return "customers.txt is rich text, please save it as plain text first"
        lines = raw.splitlines()
        if add is not None:
            name = " ".join(add.split())
            if not name or name.startswith("#") or len(name) > 100:
                return "Invalid customer name"
            if norm(name) in {norm(l) for l in lines}:
                return f"{name} is already in the list"
            lines.append(name)
        else:
            kept = [l for l in lines if norm(l) != norm(remove)]
            if len(kept) == len(lines):
                return f"{remove} is not in the list"
            lines = kept
        tmp = CUSTOMERS_FILE.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(CUSTOMERS_FILE)
    with STATE_LOCK:
        STATE["customers"] = load_customers()
    return None


def load_seen():
    try:
        return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(sorted(seen)), encoding="utf-8")


def fetch_incidents(status):
    incidents = []
    offset = 0
    while True:
        params = {"statuses[]": status, "limit": 100, "offset": offset}
        if PD_SERVICE_IDS:
            params["service_ids[]"] = PD_SERVICE_IDS
        r = requests.get(f"{PD_API}/incidents",
                         headers=PD_HEADERS, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        page = data.get("incidents", [])
        incidents.extend(page)
        # An empty page despite more=true would otherwise loop forever
        if not data.get("more") or not page:
            return incidents
        offset += len(page)


MY_USER_ID = None   # None = not determined yet, "" = cannot be determined
MY_USER_NAME = ""   # own name in PagerDuty, for "Acked by" right after the click


def find_my_user_id():
    """Own PagerDuty user ID: via /users/me with a personal key, otherwise via PD_FROM_EMAIL.
    On network errors it stays None -> the next poll tries again."""
    global MY_USER_ID, MY_USER_NAME
    try:
        r = requests.get(f"{PD_API}/users/me", headers=PD_HEADERS, timeout=20)
        if r.ok:
            MY_USER_ID = r.json()["user"]["id"]
            MY_USER_NAME = r.json()["user"].get("name", "")
        elif PD_FROM_EMAIL:
            r = requests.get(f"{PD_API}/users", headers=PD_HEADERS,
                             params={"query": PD_FROM_EMAIL}, timeout=20)
            r.raise_for_status()
            me = next((u for u in r.json().get("users", [])
                       if u.get("email", "").lower() == PD_FROM_EMAIL.lower()), {})
            MY_USER_ID, MY_USER_NAME = me.get("id", ""), me.get("name", "")
        else:
            MY_USER_ID = ""
    except (requests.RequestException, KeyError, ValueError) as e:
        log(f"Own PagerDuty user ID not determined, retrying later: {e}")
        return
    if not MY_USER_ID:
        log("Own PagerDuty user ID unknown: showing all acknowledged incidents "
            "(set PD_FROM_EMAIL for an account key).")


_ON_CALL_CACHE = {"at": 0.0, "value": True, "known": False}


def fetch_my_shifts():
    """My current shifts: {schedule_id: {"schedule": name, "end": ISO time or None}}."""
    r = requests.get(f"{PD_API}/oncalls", headers=PD_HEADERS, timeout=20,
                     params={"user_ids[]": MY_USER_ID, "limit": 100})
    r.raise_for_status()
    return {o["schedule"]["id"]: {"schedule": o["schedule"].get("summary", ""), "end": o.get("end")}
            for o in r.json().get("oncalls", []) if o.get("schedule")}


def on_call_now():
    """True if I am currently on call in a schedule (asked at most once per minute).
    Also updates the shift display. If PagerDuty is unreachable (e.g. right after the
    machine wakes up), the last known status applies; only without one, keep running rather
    than miss alarms. Wall clock instead of monotonic: that one stands still on macOS while asleep."""
    if abs(time.time() - _ON_CALL_CACHE["at"]) < 60:
        return _ON_CALL_CACHE["value"]
    if MY_USER_ID is None:
        find_my_user_id()
    shifts = {}
    if not MY_USER_ID:
        log("On-call status cannot be checked (own PagerDuty user unknown), keeps running.")
        value = True
    else:
        try:
            shifts = fetch_my_shifts()
            value = bool(shifts)
            _ON_CALL_CACHE["known"] = True
        except (requests.RequestException, KeyError, ValueError) as e:
            if _ON_CALL_CACHE["known"]:
                log(f"On-call status not available, keeping last state: {e}")
                _ON_CALL_CACHE["at"] = time.time()
                return _ON_CALL_CACHE["value"]
            log(f"On-call status not available, keeps running: {e}")
            value = True
    _ON_CALL_CACHE.update(at=time.time(), value=value)
    ends = [s["end"] for s in shifts.values() if s["end"]]
    if MY_USER_ID:
        try:
            handoffs = fetch_handoffs(shifts)
        except (requests.RequestException, KeyError, ValueError) as e:
            log(f"Handoffs not loaded from PagerDuty, keeping last state: {e}")
            with STATE_LOCK:
                handoffs = STATE["overrides"]
    else:
        handoffs = load_overrides()
    with STATE_LOCK:
        STATE["overrides"] = handoffs
        STATE["shift"] = {"schedules": [s["schedule"] for s in shifts.values()],
                          "end": max(ends) if ends else None} if shifts else None
    return value


def find_user_id(name_or_email):
    """PagerDuty user ID for a name or email (exact, case-insensitive)."""
    r = requests.get(f"{PD_API}/users", headers=PD_HEADERS, timeout=20, params={"query": name_or_email})
    r.raise_for_status()
    wanted = name_or_email.casefold()
    return next((u["id"] for u in r.json().get("users", [])
                 if wanted in (u.get("name", "").casefold(), u.get("email", "").casefold())), None)


_OVERRIDE_USER_ID = {}   # OVERRIDE_USER -> PagerDuty user ID


def override_user_id():
    if OVERRIDE_USER not in _OVERRIDE_USER_ID:
        _OVERRIDE_USER_ID[OVERRIDE_USER] = find_user_id(OVERRIDE_USER) if OVERRIDE_USER else None
    return _OVERRIDE_USER_ID[OVERRIDE_USER]


def fetch_handoffs(shifts):
    """Running or upcoming overrides from my schedules to OVERRIDE_USER, read live from PagerDuty,
    so a handoff made elsewhere (e.g. the console on the PAW) shows up and can be taken back here.
    My schedules: current shifts, my shifts in the next 14 days and the ones I handed off from here."""
    target = override_user_id()
    if not target:
        return []
    now = datetime.now(TZ)
    r = requests.get(f"{PD_API}/oncalls", headers=PD_HEADERS, timeout=20, params={
        "user_ids[]": MY_USER_ID, "since": now.isoformat(), "until": (now + timedelta(days=14)).isoformat(),
        "limit": 100})
    r.raise_for_status()
    schedules = {o["schedule"]["id"]: o["schedule"].get("summary", "") for o in r.json().get("oncalls", [])
                 if o.get("schedule")}
    schedules.update({sid: s["schedule"] for sid, s in shifts.items()})
    schedules.update({o["schedule_id"]: o["schedule"] for o in load_overrides()})
    handoffs = []
    for schedule_id, name in schedules.items():
        r = requests.get(f"{PD_API}/schedules/{schedule_id}/overrides", headers=PD_HEADERS, timeout=20,
                         params={"since": now.isoformat(), "until": (now + timedelta(days=1)).isoformat()})
        r.raise_for_status()
        for o in r.json().get("overrides", []):
            if (o.get("user") or {}).get("id") == target and parse_time(o["end"]) > now:
                handoffs.append({"schedule_id": schedule_id, "schedule": name, "id": o["id"], "end": o["end"]})
    return handoffs


def parse_time(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)


def load_overrides():
    """Overrides I set that have not expired yet (survive a restart)."""
    try:
        overrides = json.loads(OVERRIDES_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        overrides = []
    now = datetime.now(TZ)
    return [o for o in overrides if parse_time(o["end"]) > now]


def save_overrides(overrides):
    OVERRIDES_FILE.write_text(json.dumps(overrides), encoding="utf-8")
    with STATE_LOCK:
        STATE["overrides"] = overrides


def pd_write_headers():
    headers = dict(PD_HEADERS, **{"Content-Type": "application/json"})
    if PD_FROM_EMAIL:
        headers["From"] = PD_FROM_EMAIL
    return headers


def hand_over_shift():
    """Hand the rest of my current shift(s) to OVERRIDE_USER via an override.
    Returns (error text or None, description)."""
    if not OVERRIDE_USER:
        return "OVERRIDE_USER is not set", ""
    if MY_USER_ID is None:
        find_my_user_id()
    if not MY_USER_ID:
        return "Own PagerDuty user unknown (set PD_FROM_EMAIL for an account key)", ""
    headers = pd_write_headers()
    overrides = load_overrides()
    try:
        shifts = fetch_my_shifts()
        if not shifts:
            return "You are not on call right now", ""
        user_id = find_user_id(OVERRIDE_USER)
        if not user_id:
            return f"PagerDuty user {OVERRIDE_USER!r} not found", ""
        start = datetime.now(TZ).isoformat(timespec="seconds")
        done = []
        for schedule_id, s in shifts.items():
            if not s["end"]:
                return f"On-call in {s['schedule']} has no end, not handed off", ""
            r = requests.post(f"{PD_API}/schedules/{schedule_id}/overrides", headers=headers, timeout=20,
                              json={"overrides": [{"start": start, "end": s["end"],
                                                   "user": {"id": user_id, "type": "user_reference"}}]})
            if not r.ok:
                return f"{s['schedule']}: {pd_error_text(r)}", ""
            # Bulk endpoint: the status is in the response per override
            body = r.json()
            for item in body if isinstance(body, list) else [body]:
                if item.get("status", 201) >= 300:
                    return f"{s['schedule']}: {'; '.join(item.get('errors') or ['rejected'])}", ""
                # Remember the ID so "Take back on-call" can delete the override again
                if item.get("override", {}).get("id"):
                    overrides.append({"schedule_id": schedule_id, "schedule": s["schedule"],
                                      "id": item["override"]["id"], "end": s["end"]})
                    save_overrides(overrides)
            done.append(f"{s['schedule']} until {parse_time(s['end']):%H:%M}")
    except (requests.RequestException, KeyError, ValueError) as e:
        return f"PagerDuty error: {e}", ""
    _ON_CALL_CACHE["at"] = 0.0   # check again on the next poll -> then pauses
    return None, f"On-call handed off to {OVERRIDE_USER}: " + ", ".join(done)


def take_back_shift():
    """Delete my overrides, the shift is mine again. If an override is already running,
    PagerDuty shortens it to now. Also takes back handoffs made on another machine.
    Returns (error text or None, description)."""
    try:
        overrides = fetch_handoffs(fetch_my_shifts()) if MY_USER_ID else load_overrides()
    except (requests.RequestException, KeyError, ValueError):
        overrides = load_overrides()
    if not overrides:
        return "No handoff found", ""
    headers = pd_write_headers()
    done = []
    try:
        for o in list(overrides):
            r = requests.delete(f"{PD_API}/schedules/{o['schedule_id']}/overrides/{o['id']}",
                                headers=headers, timeout=20)
            if not r.ok and r.status_code != 404:   # 404: already deleted in PagerDuty
                save_overrides(overrides)
                return f"{o['schedule']}: {pd_error_text(r)}", ""
            overrides.remove(o)
            done.append(o["schedule"])
    except requests.RequestException as e:
        save_overrides(overrides)
        return f"PagerDuty error: {e}", ""
    save_overrides(overrides)
    _ON_CALL_CACHE["at"] = 0.0   # check again on the next poll -> active again
    return None, "On-call taken back: " + ", ".join(done)


# ---------- LAS (Last Analyst Standing): PD notifications temporarily on ----------

def load_las():
    try:
        las = json.loads(LAS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    if las and las.get("mode") == "schicht":   # stored by the German version
        las["mode"] = "shift"
    return las


def save_las(las):
    if las:
        LAS_FILE.write_text(json.dumps(las), encoding="utf-8")
    else:
        LAS_FILE.unlink(missing_ok=True)
    with STATE_LOCK:
        STATE["las"] = las_view(las)


def las_view(las):
    return {"mode": las.get("mode", "las"), "until": las["until"]} if las else None


CONTACT_TYPES = {
    "push_notification_contact_method": "Push",
    "sms_contact_method": "SMS",
    "phone_contact_method": "Call",
    "email_contact_method": "Email",
}
URGENCY_TEXT = {"high": "high urgency", "low": "low urgency", "both": "high and low urgency"}


def fetch_contacts():
    """My PagerDuty contacts (devices, SMS, call, email) for the selection in the dashboard.
    Returns (error text or None, list)."""
    if MY_USER_ID is None:
        find_my_user_id()
    if not MY_USER_ID:
        return "Own PagerDuty user unknown (set PD_FROM_EMAIL for an account key)", []
    try:
        r = requests.get(f"{PD_API}/users/{MY_USER_ID}/contact_methods", headers=PD_HEADERS, timeout=20)
        r.raise_for_status()
        contacts = r.json().get("contact_methods", [])
    except (requests.RequestException, ValueError) as e:
        return f"PagerDuty error: {e}", []
    return None, [{"id": c["id"], "kind": CONTACT_TYPES[c["type"]],
                   "label": c.get("label") or c.get("summary") or "", "address": c.get("address", "")}
                  for c in contacts if c.get("type") in CONTACT_TYPES]


def start_las(mode="las"):
    """Ensure a PD notification rule (immediate, urgency per LAS_URGENCY) for the selected
    contacts (LAS_CONTACTS, empty = all push devices): with "las" until LAS_END_HOUR, with
    "shift" until turned off by hand. Existing push rules (including delayed ones) are deleted
    and replaced by an immediate one, which is removed again at the end. Existing rules for
    SMS, call or email stay, even after turning off.
    Returns (error text or None, description)."""
    if load_las():
        return "PagerDuty notification is already on", ""
    until = None
    if mode == "las":
        now = datetime.now(TZ)
        until = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=LAS_END_HOUR)
        if until <= now:
            return f"LAS end {LAS_END_HOUR}:00 has already passed today", ""
    until_text = until.isoformat() if until else None
    if MY_USER_ID is None:
        find_my_user_id()
    if not MY_USER_ID:
        return "Own PagerDuty user unknown (set PD_FROM_EMAIL for an account key)", ""
    base = f"{PD_API}/users/{MY_USER_ID}"
    headers = pd_write_headers()
    created = []
    try:
        r = requests.get(f"{base}/contact_methods", headers=PD_HEADERS, timeout=20)
        r.raise_for_status()
        contacts = [c for c in r.json().get("contact_methods", []) if c.get("type") in CONTACT_TYPES]
        wanted = set(LAS_CONTACTS.split(",")) if LAS_CONTACTS else None
        devices = [c for c in contacts if c["id"] in wanted] if wanted else \
            [c for c in contacts if c["type"] == "push_notification_contact_method"]
        if not devices:
            return ("None of the selected contacts exist in PagerDuty anymore (check settings)"
                    if wanted else "No device registered for PagerDuty push (sign in to the PagerDuty app)"), ""
        urgencies = {"high": ["high"], "low": ["low"], "both": ["high", "low"]}[LAS_URGENCY]
        r = requests.get(f"{base}/notification_rules", headers=PD_HEADERS, timeout=20)
        r.raise_for_status()
        # There can be several rules per contact and urgency (e.g. immediately and after 5 min)
        existing = {}
        for n in r.json().get("notification_rules", []):
            existing.setdefault((n["contact_method"].get("id"), n.get("urgency")), []).append(n["id"])
        for c in devices:
            for urgency in urgencies:
                if (c["id"], urgency) in existing:
                    # Never delete call/SMS/email rules
                    if c["type"] != "push_notification_contact_method":
                        continue
                    # Delete push rules and recreate as an immediate rule below, otherwise a
                    # delayed rule would stay and LAS would only push after minutes
                    for rule_id in existing[(c["id"], urgency)]:
                        r = requests.delete(f"{base}/notification_rules/{rule_id}", headers=headers,
                                            timeout=20)
                        if not r.ok and r.status_code != 404:   # 404: already deleted in PagerDuty
                            raise RuntimeError(f"Rule {rule_id}: {pd_error_text(r)}")
                r = requests.post(f"{base}/notification_rules", headers=headers, timeout=20, json={
                    "notification_rule": {
                        "type": "assignment_notification_rule", "start_delay_in_minutes": 0, "urgency": urgency,
                        "contact_method": {"id": c["id"], "type": c["type"]},
                    }})
                if not r.ok:
                    raise RuntimeError(f"{c.get('summary', c['id'])}: {pd_error_text(r)}")
                created.append(r.json()["notification_rule"]["id"])
    except (requests.RequestException, KeyError, ValueError, RuntimeError) as e:
        # Don't leave half-created rules behind (call/SMS/email rules stay)
        save_las({"mode": mode, "until": until_text, "rules": created})
        stop_las()
        return f"PagerDuty error: {e}", ""
    save_las({"mode": mode, "until": until_text, "rules": created})
    names = ", ".join(f"{CONTACT_TYPES[c['type']]} {c.get('summary', '')}".strip() for c in devices)
    what = f"LAS until {until:%H:%M}" if until else "no end"
    return None, f"PagerDuty notification on ({what}, {URGENCY_TEXT[LAS_URGENCY]}): {names}"


def stop_las():
    """Delete the notification rules created for LAS again.
    Returns (error text or None, description)."""
    las = load_las()
    if not las:
        return "PagerDuty notification is not on", ""
    headers = pd_write_headers()
    try:
        for rule_id in list(las["rules"]):
            r = requests.delete(f"{PD_API}/users/{MY_USER_ID}/notification_rules/{rule_id}",
                                headers=headers, timeout=20)
            if not r.ok and r.status_code != 404:   # 404: already deleted in PagerDuty
                save_las(las)
                return f"Rule {rule_id}: {pd_error_text(r)}", ""
            las["rules"].remove(rule_id)
    except requests.RequestException as e:
        save_las(las)
        return f"PagerDuty error: {e}", ""
    save_las(None)
    return None, "PagerDuty notification off: rules removed"


def end_las_if_due():
    """Turn off automatically at LAS_END_HOUR (or on the next start after that)."""
    las = load_las()
    if not las or not las["until"] or parse_time(las["until"]) > datetime.now(TZ):
        return
    if MY_USER_ID is None:
        find_my_user_id()
    err, text = stop_las()
    log(f"Ending LAS automatically failed, retrying later: {err}" if err else text)
    add_event("error" if err else "info", err or text)


def fetch_acked(customers, all_mine=False):
    """Acknowledged incidents of my customers (by anyone) plus all that I acknowledged.
    With LAS/"On (no end)" (all_mine) and without a known own user ID: all acknowledged incidents."""
    acked = []
    for inc in fetch_incidents("acknowledged"):
        customer = match_customer(inc.get("title", ""), customers)
        acks = inc.get("acknowledgements") or []
        mine = bool(MY_USER_ID) and any((a.get("acknowledger") or {}).get("id") == MY_USER_ID for a in acks)
        if MY_USER_ID and not mine and not customer and not all_mine:
            continue
        by, by_ids = [], set()
        for a in acks:
            who = a.get("acknowledger") or {}
            if who.get("id") in by_ids:
                continue
            by_ids.add(who.get("id"))
            by.append({"name": who.get("summary") or "?", "me": who.get("id") == MY_USER_ID})
        assigned = [(a.get("assignee") or {}).get("summary") for a in inc.get("assignments") or []]
        last = (acks or [{}])[-1]
        acked.append({
            "id": inc["id"],
            "title": inc.get("title", ""),
            "service": service_name(inc),
            "url": inc.get("html_url", ""),
            "customer": customer,
            "mine": mine,
            "acked_by": by,
            "assigned": [a for a in assigned if a],
            "acked_at": last.get("at") or inc.get("updated_at") or inc.get("created_at", ""),
        })
    return sorted(acked, key=lambda a: a["acked_at"], reverse=True)


def pd_error_text(r):
    """Make an error message from a PagerDuty response readable."""
    try:
        err = r.json().get("error", {})
        parts = [err.get("message", "")] + list(err.get("errors", []))
        text = "; ".join(p for p in parts if p)
    except ValueError:
        text = r.text[:200]
    return f"HTTP {r.status_code}: {text or r.reason}"


def set_incident_status(incident_id, status):
    """Set an incident in PagerDuty to acknowledged/resolved. Returns None or error text."""
    headers = dict(PD_HEADERS, **{"Content-Type": "application/json"})
    if PD_FROM_EMAIL:
        headers["From"] = PD_FROM_EMAIL
    try:
        r = requests.put(f"{PD_API}/incidents/{incident_id}", headers=headers, timeout=20,
                         json={"incident": {"type": "incident_reference", "status": status}})
    except requests.RequestException as e:
        return f"PagerDuty unreachable: {e}"
    return None if r.ok else pd_error_text(r)


# Sentinel / Defender XDR links from the alert payload, fetched once per incident
LINKS = {}   # incident ID -> {"sentinel": URL or None, "defender": URL or None}
LINK_RE = re.compile(r"^https://([a-z0-9-]+\.)*(portal\.azure\.com|security\.microsoft\.com)/", re.I)
PROVIDER_RE = re.compile(r"Provider ID:\s*(\d+)")
TENANT_RE = re.compile(r"[?&]tid=([0-9a-f-]{36})", re.I)


def extract_links(alert):
    """Sentinel link from the alert contexts, Defender link from the context or from
    "Provider ID" plus the customer tenant in the details. Only Azure/Defender URLs count."""
    body = alert.get("body") or {}
    contexts = body.get("contexts") or (body.get("cef_details") or {}).get("contexts") or []
    hrefs = [(c.get("text") or "", c.get("href") or "") for c in contexts if LINK_RE.match(c.get("href") or "")]
    sentinel = next((h for t, h in hrefs if "sentinel" in t.lower() or "Microsoft_Azure_Security_Insights" in h), None)
    defender = next((h for _, h in hrefs if "security.microsoft.com/incident" in h.lower()), None)
    if not defender:
        details = json.dumps(body.get("details") or "")
        provider, tenant = PROVIDER_RE.search(details), TENANT_RE.search(details)
        if provider and tenant:
            defender = f"https://security.microsoft.com/incidents/{provider.group(1)}?tid={tenant.group(1)}"
    return {"sentinel": sentinel, "defender": defender}


def fill_links(ids, budget=20):
    """Fetch links for incidents not seen yet (at most `budget` per poll), forget old ones."""
    for incident_id in [i for i in ids if i not in LINKS][:budget]:
        try:
            r = requests.get(f"{PD_API}/incidents/{incident_id}/alerts", headers=PD_HEADERS,
                             params={"limit": 1}, timeout=20)
            r.raise_for_status()
            alerts = r.json().get("alerts") or []
        except (requests.RequestException, ValueError) as e:
            log(f"Incident links not loaded, retrying later: {e}")
            break
        LINKS[incident_id] = extract_links(alerts[0]) if alerts else {"sentinel": None, "defender": None}
    for incident_id in [i for i in LINKS if i not in ids]:
        del LINKS[incident_id]


def service_name(incident):
    return (incident.get("service") or {}).get("summary", "")


def service_matches(incident):
    if not SERVICE_FILTER:
        return True
    name = service_name(incident).lower()
    return any(f in name for f in SERVICE_FILTER)


_RTF_WARNED = False
_INVISIBLE = dict.fromkeys(map(ord, "﻿​‌‍⁠­"), None)


def norm(s):
    """For comparison: normalize Unicode, drop invisible characters,
    turn all kinds of whitespace into a normal space, lowercase."""
    s = unicodedata.normalize("NFKC", s or "").translate(_INVISIBLE)
    return " ".join(s.split()).casefold()


def match_customer(title, customers):
    """Contains check: the customer appears anywhere in the title (case-insensitive)."""
    t = norm(title)
    for c in customers:
        if norm(c) and norm(c) in t:
            return c
    return None


def notify(incident, customer, loud=True):
    """loud=True: priority 'urgent' (loud alarm).
    loud=False: priority 'low' (notification only, no sound), optionally on a separate topic."""
    title = incident.get("title", "(no title)")
    url = incident.get("html_url", "")
    service = service_name(incident)
    topic = NTFY_TOPIC if loud else (NTFY_TOPIC_SILENT or NTFY_TOPIC)
    label = customer or service or "Alarm"
    # ntfy topics are publicly readable: with QUIET only "Incident", details in the dashboard
    body = "Incident" if QUIET else f"{title}\nService: {service}"
    r = requests.post(
        f"{NTFY_SERVER}/{topic}",
        data=body.encode("utf-8"),
        headers={
            "Title": "Incident" if QUIET else f"PagerDuty: {label}".encode("utf-8"),
            "Priority": "urgent" if loud else "low",
            "Tags": "rotating_light" if loud else "information_source",
            "Click": url,                  # tapping opens the incident
        },
        timeout=20,
    )
    r.raise_for_status()


def classify(inc, customers):
    """Returns (decision, customer).
    alarm    = your customer and the service matches (e.g. CSOC (High))  -> loud
    service  = service doesn't match (e.g. CSOC (Medium))
    customer = service matches, but not one of your customers"""
    customer = match_customer(inc.get("title", ""), customers)
    if not service_matches(inc):
        return "service", customer
    if customer:
        return "alarm", customer
    return "customer", None


def wants_silent(decision, customer):
    """Should this (not loud) incident get a silent notification?"""
    if SILENT_FOR == "all":
        return decision in ("service", "customer")
    if SILENT_FOR == "customers":
        return decision == "service" and customer is not None
    return False


# ---------- Dashboard (only reachable locally on this machine) ----------

ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")
# Path -> (PagerDuty status, event kind, text for log/history)
ACTIONS = {
    "/api/ack": ("acknowledged", "ack", "Acknowledged"),
    "/api/resolve": ("resolved", "resolve", "Resolved"),
}


class DashboardHandler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, data):
        self._send(code, json.dumps(data).encode("utf-8"), "application/json")

    def _host_ok(self):
        """Protection against DNS rebinding: only localhost/127.0.0.1 with our port."""
        return self.headers.get("Host", "") in (f"localhost:{WEB_PORT}", f"127.0.0.1:{WEB_PORT}")

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        return origin is None or origin in (f"http://localhost:{WEB_PORT}", f"http://127.0.0.1:{WEB_PORT}")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden", "text/plain; charset=utf-8")
        pages = {"/": CONSOLE_FILE, "/index.html": CONSOLE_FILE, "/classic": DASHBOARD_FILE, "/mini": MINI_FILE}
        if paw_exchange.enabled():
            pages["/paw"] = PAW_FILE
        if self.path in pages:
            try:
                body = pages[self.path].read_bytes()
            except FileNotFoundError:
                return self._send(404, f"{pages[self.path].name} missing".encode(), "text/plain; charset=utf-8")
            return self._send(200, body, "text/html; charset=utf-8")
        if self.path == "/api/contacts":
            err, contacts = fetch_contacts()
            return self._json(502, {"error": err}) if err else self._json(200, {"contacts": contacts})
        if self.path in ("/api/paw", "/api/paw?refresh=1") and paw_exchange.enabled():
            return self._json(200, paw_exchange.state(force=self.path.endswith("refresh=1")))
        if self.path == "/api/state":
            with STATE_LOCK:
                data = dict(STATE, events=list(EVENTS))
            # With LAS / "On (no end)" I take over all customers -> all incidents count as mine
            data["all_mine"] = bool(data.get("las"))
            return self._send(200, json.dumps(data).encode("utf-8"), "application/json")
        self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        # Only from our own dashboard: correct host, matching origin, custom header
        if not (self._host_ok() and self._origin_ok() and self.headers.get("X-PD-Filter") == "1"):
            return self._json(403, {"error": "forbidden"})
        if self.path == "/api/override":
            err, text = hand_over_shift()
            if err:
                log(f"Override failed: {err}")
                add_event("error", f"Override failed: {err}")
                return self._json(502, {"error": err})
            log(text)
            add_event("info", text)
            with STATE_LOCK:
                STATE["shift"] = None
            return self._json(200, {"ok": True, "text": text})
        if self.path in ("/api/las/start", "/api/shift/start", "/api/las/stop"):
            if self.path.endswith("start"):
                err, text = start_las("shift" if "shift" in self.path else "las")
            else:
                err, text = stop_las()
            if err:
                log(f"PagerDuty notification: {err}")
                add_event("error", f"PagerDuty notification: {err}")
                return self._json(502, {"error": err})
            log(text)
            add_event("info", text)
            return self._json(200, {"ok": True, "text": text})
        if self.path == "/api/override/remove":
            err, text = take_back_shift()
            if err:
                log(f"Taking back failed: {err}")
                add_event("error", f"Taking back failed: {err}")
                return self._json(502, {"error": err})
            log(text)
            add_event("info", text)
            return self._json(200, {"ok": True, "text": text})
        if self.path not in ACTIONS and self.path not in (
                "/api/customers/add", "/api/customers/remove", "/api/settings", "/api/paw/analyze", "/api/paw/done", "/api/paw/stop"):
            return self._json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, AttributeError):
            return self._json(400, {"error": "invalid request"})

        if self.path.startswith("/api/paw/"):
            if not paw_exchange.enabled():
                return self._json(404, {"error": "PAW Exchange is not set up"})
            upload_id = str(body.get("id", ""))
            if self.path.endswith("/stop"):
                err = paw_exchange.stop_auto(upload_id)
                return self._json(502 if err else 200, {"error": err} if err else {"ok": True})
            if self.path.endswith("/analyze"):
                boost = body.get("boost") is True
                err = paw_exchange.analyze(upload_id, str(body.get("note", ""))[:2000], boost)
                text = f"PAW: analysis started ({upload_id}{', boost/Opus' if boost else ''})"
            else:
                err = paw_exchange.done(upload_id)
                text = f"PAW: upload deleted ({upload_id})"
            if err:
                log(f"PAW: {err}")
                return self._json(502, {"error": err})
            log(text)
            add_event("info", text)
            return self._json(200, {"ok": True})

        if self.path == "/api/settings":
            err, changed = update_settings(body)
            if err:
                return self._json(400, {"error": err})
            if changed:
                text = "Settings changed: " + ", ".join(changed)
                log(text)
                add_event("info", text)
            return self._json(200, {"ok": True})

        if self.path.startswith("/api/customers/"):
            name = str(body.get("name", ""))
            adding = self.path.endswith("/add")
            err = edit_customers(add=name) if adding else edit_customers(remove=name)
            if err:
                return self._json(400, {"error": err})
            text = f"Customer {'added' if adding else 'removed'}: {name.strip()}"
            log(text)
            add_event("info", text)
            return self._json(200, {"ok": True})

        status, kind, label = ACTIONS[self.path]
        incident_id = str(body.get("id", ""))
        if not ID_RE.match(incident_id):
            return self._json(400, {"error": "invalid incident ID"})

        with STATE_LOCK:
            inc = next((i for i in STATE["incidents"] + STATE["acked"] if i["id"] == incident_id), None)
        err = set_incident_status(incident_id, status)
        if err:
            log(f"{label} failed ({incident_id}): {err}")
            add_event("error", f"{label} failed: {err}")
            return self._json(502, {"error": err})

        log(f"{label}: {show(inc['title']) if inc else incident_id}")
        with STATE_LOCK:
            STATE["incidents"] = [i for i in STATE["incidents"] if i["id"] != incident_id]
            STATE["acked"] = [i for i in STATE["acked"] if i["id"] != incident_id]
            # Show at the top right away, not only after the next poll. If others had already
            # acknowledged it, their names stay and I'm added.
            if status == "acknowledged" and inc:
                by = [a for a in inc.get("acked_by") or [] if not a.get("me")]
                STATE["acked"].insert(0, {k: inc.get(k) for k in ("id", "title", "service", "url", "customer", "links")}
                                      | {"acked_at": datetime.now(TZ).isoformat(), "mine": True,
                                         "acked_by": by + [{"name": MY_USER_NAME or "me", "me": True}],
                                         "assigned": inc.get("assigned") or []})
        if inc:
            add_event(kind, f"✓ {label}",
                      {"title": inc["title"], "service": {"summary": inc["service"]}, "html_url": inc["url"]},
                      inc.get("customer"))
        else:
            add_event(kind, f"✓ {label} ({incident_id})")
        self._json(200, {"ok": True})

    def log_message(self, *args):
        pass  # don't write requests to the log


def start_dashboard():
    if not WEB_PORT:
        return
    try:
        server = ThreadingHTTPServer(("127.0.0.1", WEB_PORT), DashboardHandler)
    except OSError as e:
        log(f"Dashboard not started: port {WEB_PORT} is in use ({e.strerror}). "
            f"The filter keeps running; set a different WEB_PORT in config.env.")
        add_event("error", f"Dashboard: port {WEB_PORT} in use")
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"Dashboard: http://localhost:{WEB_PORT}")
    paw_exchange.start_auto()   # start new PAW uploads automatically even without the page open


# ---------- Main loop ----------

def poll_once(seen):
    customers = load_customers()
    incidents = fetch_incidents("triggered")
    current_ids = {i["id"] for i in incidents}
    view = []

    for inc in incidents:
        decision, customer = classify(inc, customers)
        silent = wants_silent(decision, customer)
        alarmed = decision == "alarm" and inc["id"] in seen

        if inc["id"] not in seen:
            title = show(inc.get("title"))
            who = "***" if QUIET else customer
            send_loud = decision == "alarm"
            failed = False
            if send_loud or silent:
                try:
                    notify(inc, customer, loud=send_loud)
                except Exception as e:
                    # Don't mark as seen -> the next poll tries again
                    log(f"ntfy error, retrying later: {e}")
                    add_event("error", f"ntfy error: {e}", inc, customer)
                    failed = True

            if failed:
                decision = "pending"
            elif send_loud:
                alarmed = True
                log(f"ALARM loud [{who}] {title}")
                add_event("alarm", "Loud", inc, customer)
            elif silent:
                log(f"silent notification [{who or service_name(inc)}] {title}")
                add_event("silent", "Silent notification", inc, customer)
            elif decision == "service":
                log(f"ignored (service {service_name(inc)}): {title}")
                add_event("service", "Service filtered", inc, customer)
            else:
                log(f"ignored: {title}")
                add_event("customer", "Not your customer", inc)
            if not failed:
                seen.add(inc["id"])

        view.append({
            "id": inc["id"],
            "title": inc.get("title", ""),
            "service": service_name(inc),
            "created_at": inc.get("created_at", ""),
            "url": inc.get("html_url", ""),
            "decision": "silent" if silent and decision in ("service", "customer") else decision,
            "customer": customer,
            "alarmed": alarmed,
            "assigned_me": bool(MY_USER_ID) and any(
                (a.get("assignee") or {}).get("id") == MY_USER_ID for a in inc.get("assignments") or []),
        })

    # Only keep IDs that are still open -> the file doesn't grow forever
    seen &= current_ids
    save_seen(seen)

    with STATE_LOCK:
        STATE.update(last_poll=now_str(), last_error=None, customers=customers,
                     incidents=sorted(view, key=lambda v: v["created_at"], reverse=True))

    # Only after the alarms: an error here must not prevent a notification
    if MY_USER_ID is None:
        find_my_user_id()
    with STATE_LOCK:
        all_mine = bool(STATE["las"])
    acked = fetch_acked(customers, all_mine)
    with STATE_LOCK:
        STATE.update(acked=acked, acked_mine=bool(MY_USER_ID))

    fill_links({i["id"] for i in view} | {a["id"] for a in acked})
    with STATE_LOCK:
        for item in STATE["incidents"] + STATE["acked"]:
            item["links"] = LINKS.get(item["id"])


def main():
    if not PD_API_TOKEN or not NTFY_TOPIC:
        sys.exit("Please set PD_API_TOKEN and NTFY_TOPIC.")

    seen = load_seen()
    window = f", only {START_HOUR:02d}-{END_HOUR:02d}h" if ONLY_IN_WINDOW else ""
    log(f"ON. Interval {POLL_SECONDS}s, {len(load_customers())} customers{window}. Stop with Ctrl+C.")
    with STATE_LOCK:
        STATE["started"] = now_str()
        STATE["settings"] = current_settings()
        STATE["overrides"] = load_overrides()
        STATE["las"] = las_view(load_las())
    add_event("info", "Filter turned on")
    start_dashboard()

    paused = None
    while True:
        end_las_if_due()
        # Pause instead of exiting outside the time window / without a schedule, so it stays "on"
        if not in_time_window():
            reason = "outside time window"
        elif not on_call_now() and ONLY_ON_CALL:
            reason = "no schedule active"
        else:
            reason = None
        if reason != paused:
            text = f"Paused: {reason}" if reason else "Active again"
            log(text)
            add_event("info", text)
            paused = reason
            with STATE_LOCK:
                STATE["paused"] = reason
        if reason:
            # Keep the customer list current during the pause too, otherwise the dashboard stays empty
            with STATE_LOCK:
                STATE["customers"] = load_customers()
            time.sleep(60)
            continue

        try:
            poll_once(seen)
        except Exception as e:
            log(f"Error: {e}")
            add_event("error", f"Error while polling: {e}")
            with STATE_LOCK:
                STATE["last_error"] = str(e)

        time.sleep(POLL_SECONDS)


def test_title(title, service):
    """Dry run without PagerDuty: python pd_filter.py --test "Title" "CSOC (High)" """
    customers = load_customers()
    inc = {"title": title, "service": {"summary": service}}
    decision, customer = classify(inc, customers)
    print(f"Customer list: {customers}")
    print(f"Service filter: {SERVICE_FILTER or 'all'}")
    print(f"Title: {title!r}  |  Service: {service!r}")
    print(f"Silent notifications for: {SILENT_FOR}")
    if decision == "alarm":
        print(f"-> LOUD (customer: {customer})")
    elif wants_silent(decision, customer):
        print(f"-> SILENT notification without sound ({'customer: ' + customer if customer else 'no customer'}, "
              f"{'service does not match' if decision == 'service' else 'service matches'})")
    elif decision == "service":
        print("-> ignored: service does not match the service filter")
    else:
        print("-> ignored: no customer from the list in the title")


def on_sigterm(*_):
    """Exit on kill (e.g. ./pd-filter.sh stop) as cleanly as on Ctrl+C."""
    raise KeyboardInterrupt


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--test":
        test_title(sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else "CSOC (High)")
        sys.exit(0)
    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        main()
    except KeyboardInterrupt:
        log("OFF.")
