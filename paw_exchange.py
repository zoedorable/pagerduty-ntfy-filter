"""
PAW Exchange in the dashboard: show pending uploads from the PAW and have them analysed
by Claude (Enterprise account) with one click.

All crypto and Git work is done by analyze.sh from the analyze skill; it is only called
from here. Without analyze.sh the page is off.

  PAW_ANALYZE         optional, path to analyze.sh.
                      Default: ~/.claude-work/skills/analyze/analyze.sh
  PAW_CLAUDE_CONFIG   optional, Claude config of the Enterprise account. Default: ~/.claude-work
  PAW_CCUSAGE         optional, command for the cost per analysis. Default: npx --yes ccusage@20.0.45
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

ANALYZE_SH = Path(os.path.expanduser(os.environ.get("PAW_ANALYZE", "~/.claude-work/skills/analyze/analyze.sh")))
CLAUDE_CONFIG = Path(os.path.expanduser(os.environ.get("PAW_CLAUDE_CONFIG", "~/.claude-work")))
WORK_DIR = Path.home() / ".paw-exchange"
UPLOAD_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")
CACHE_SECONDS = 20
COST_SECONDS = 120
CCUSAGE = shlex.split(os.environ.get("PAW_CCUSAGE", "npx --yes ccusage@20.0.45"))
# Claude stores sessions under projects/<path with - instead of / and .>
SESSIONS_DIR = CLAUDE_CONFIG / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(WORK_DIR))
SESSION_UPLOAD_RE = re.compile(r"Upload: +(\d{4}-\d{2}-\d{2}_\d{6})|<command-args>\s*(\d{4}-\d{2}-\d{2}_\d{6})")

_lock = threading.Lock()
_cache = {"at": 0.0, "data": None}
_started = {}                   # upload ID -> time the analysis was started
_cost_lock = threading.Lock()
_cost_cache = {"at": 0.0, "data": []}
_session_uploads = {}           # session file -> ((mtime, size), upload ID or None)

# Auto-start runs in the service, not the browser: new uploads start after AUTO_SEC seconds,
# even if the PAW page is not open. Only uploads that appear after the service started
# and are younger than AUTO_MAX_AGE; each upload is auto-started at most once.
AUTO_SEC = 10
AUTO_MAX_AGE = 15 * 60
AUTO_TICK = 2
_auto = {}                      # upload ID -> time (epoch) at which the analysis auto-starts
_auto_handled = set()           # stopped, already auto-started or started manually
_auto_seen = None               # upload IDs when the service started
_auto_thread = None


def enabled():
    return ANALYZE_SH.is_file()


def _run(*args, timeout=120):
    # The dashboard is not Claude: don't pass on variables of a Claude session that may have started it,
    # otherwise analyze.sh blocks (it only releases data to the Enterprise account)
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "CLAUDE_CONFIG_DIR")}
    try:
        r = subprocess.run([str(ANALYZE_SH), *args], capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return "analyze.sh did not respond in time", ""
    if r.returncode != 0:
        return (r.stderr.strip() or r.stdout.strip() or "analyze.sh failed").lstrip("✗ "), ""
    return None, r.stdout


def state(force=False):
    """Uploads and results, fetched fresh from the repo at most every CACHE_SECONDS."""
    with _lock:
        if not force and _cache["data"] and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return _cache["data"] | {"auto": dict(_auto), "costs": costs()}
        err, out = _run("list", "--json")
        if err:
            data = dict(_cache["data"] or {"uploads": [], "results": []}, error=err)
        else:
            data = json.loads(out) | {"error": None}
            ids = {u["id"] for u in data["uploads"]}
            for k in [k for k in _started if k not in ids]:
                del _started[k]
        data["started"] = {k: v for k, v in _started.items()}
        data["auto"] = dict(_auto)
        data["updated"] = time.time()
        _cache.update(at=time.monotonic(), data=data)
        return data | {"costs": costs()}


def _session_upload(path):
    """Upload ID a Claude session analysed last (from the get output or the /analyze arguments).
    The last, not the first: a resumed session (follow-up question) contains the history of the first analysis."""
    st = path.stat()
    key = (st.st_mtime, st.st_size)
    hit = _session_uploads.get(path)
    if hit and hit[0] == key:
        return hit[1]
    found = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            for m in SESSION_UPLOAD_RE.finditer(line):
                found = m.group(1) or m.group(2)
    _session_uploads[path] = (key, found)
    return found


def _ccusage_sessions():
    path = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"
    exe = shutil.which(CCUSAGE[0], path=path)
    if not exe:
        return []
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"} | {"CLAUDE_CONFIG_DIR": str(CLAUDE_CONFIG), "PATH": path}
    try:
        r = subprocess.run([exe, *CCUSAGE[1:], "session", "--json", "--offline"], capture_output=True, text=True,
                           timeout=90, env=env, cwd=str(Path.home()))
        return json.loads(r.stdout).get("session", []) if r.returncode == 0 else []
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return []


def costs():
    """Cost per analysis run according to ccusage (one Claude session = one run, follow-ups in the chat count too). Newest first."""
    with _cost_lock:
        if time.monotonic() - _cost_cache["at"] < COST_SECONDS:
            return _cost_cache["data"]
        uploads = {}
        if SESSIONS_DIR.is_dir():
            uploads = {p.stem: u for p in SESSIONS_DIR.glob("*.jsonl") if (u := _session_upload(p))}
        runs = []
        for s in _ccusage_sessions() if uploads else []:
            upload_id = uploads.get(s.get("period"))
            if upload_id:
                runs.append({"id": upload_id, "session": s.get("period"), "cost": s.get("totalCost") or 0,
                             "model": ", ".join(m.replace("claude-", "") for m in s.get("modelsUsed") or []),
                             "tokens": s.get("totalTokens") or 0, "cacheRead": s.get("cacheReadTokens") or 0,
                             "last": (s.get("metadata") or {}).get("lastActivity") or ""})
        data = sorted(runs, key=lambda c: c["last"], reverse=True)
        _cost_cache.update(at=time.monotonic(), data=data)
        return data


def _upload(upload_id):
    if not UPLOAD_ID_RE.match(upload_id):
        return None
    return next((u for u in state()["uploads"] if u["id"] == upload_id), None)


def _known(upload_id):
    return _upload(upload_id) is not None


def _session_for(upload_id):
    """Newest Claude session that analysed this upload (for follow-up questions), otherwise None."""
    if not UPLOAD_ID_RE.match(upload_id or "") or not SESSIONS_DIR.is_dir():
        return None
    hits = [p for p in SESSIONS_DIR.glob("*.jsonl") if _session_upload(p) == upload_id]
    return max(hits, key=lambda p: p.stat().st_mtime).stem if hits else None


def analyze(upload_id, note="", boost=False):
    """Opens iTerm with Claude (Enterprise account) running /analyze for this upload.
    boost: /analyze-boost instead of /analyze – same flow, but on Opus instead of Sonnet."""
    u = _upload(upload_id)
    if not u:
        return "Upload not found"
    # Follow-up question from the PAW: resume the session of the original analysis so Claude has the context.
    # --fork-session: copy with a new ID, the old session (maybe still open in a window) stays untouched.
    session = _session_for(((u.get("followup") or {}).get("inbox")) or "")
    resume = f"--resume {shlex.quote(session)} --fork-session " if session else ""
    run_dir = WORK_DIR / "launch"
    run_dir.mkdir(parents=True, exist_ok=True)
    prompt = f"/{'analyze-boost' if boost else 'analyze'} {upload_id}"
    if note.strip():
        prompt += "\n\nAdditional notes from the analyst (from the Mac):\n" + note.strip()
    # Separate files per start so two runs started shortly after each other don't overwrite each other
    stem = f"{upload_id}_{time.strftime('%H%M%S')}"
    prompt_file = run_dir / f"{stem}.prompt"
    prompt_file.write_text(prompt, encoding="utf-8")
    cmd = run_dir / f"{stem}.command"
    # If Claude exits right away, the window stays open so the error message can be read
    cmd.write_text(
        "#!/bin/zsh -l\n"
        f"cd {shlex.quote(str(WORK_DIR))}\n"
        f"CLAUDE_CONFIG_DIR={shlex.quote(str(CLAUDE_CONFIG))} claude {resume}\"$(<{shlex.quote(str(prompt_file))})\"\n"
        "rc=$?\n"
        "if (( SECONDS < 30 )); then print \"\\nClaude exited after $SECONDS s (exit $rc). Press Enter to close the window.\"; read -r; fi\n",
        encoding="utf-8")
    cmd.chmod(0o700)
    r = subprocess.run(["open", "-a", "iTerm", str(cmd)], capture_output=True, text=True)
    if r.returncode != 0:
        r = subprocess.run(["open", "-a", "Terminal", str(cmd)], capture_output=True, text=True)
        if r.returncode != 0:
            return "Could not open a terminal: " + r.stderr.strip()
    with _lock:
        _started[upload_id] = time.time()
        _auto.pop(upload_id, None)
        _auto_handled.add(upload_id)
        if _cache["data"]:
            _cache["data"]["started"] = dict(_started)
    return None


def done(upload_id):
    """Delete the upload from the repo (analyze.sh done)."""
    if not _known(upload_id):
        return "Upload not found"
    err, _ = _run("done", upload_id)
    if not err:
        with _lock:
            _started.pop(upload_id, None)
        state(force=True)
    return err


def stop_auto(upload_id):
    """Cancel the auto-start for this upload (Stop or + note on the page)."""
    if not UPLOAD_ID_RE.match(upload_id):
        return "Upload not found"
    with _lock:
        _auto.pop(upload_id, None)
        _auto_handled.add(upload_id)
    return None


def _upload_age(u):
    try:
        return time.time() - time.mktime(time.strptime(u["id"], "%Y-%m-%d_%H%M%S"))
    except (KeyError, ValueError):
        return None


def _auto_step():
    global _auto_seen
    try:
        data = state()
    except Exception:
        return
    if data.get("error") and not data.get("uploads"):
        return
    ids = {u["id"] for u in data["uploads"]}
    if _auto_seen is None:
        _auto_seen = ids
        return
    with_result = {r.get("inbox") for r in data["results"]}
    due = []
    with _lock:
        for k in [k for k in _auto if k not in ids]:
            del _auto[k]
        for u in data["uploads"]:
            uid = u["id"]
            age = _upload_age(u)
            eligible = (uid not in _auto_seen and uid not in _auto_handled and not u.get("error")
                        and uid not in with_result and uid not in _started and age is not None and age < AUTO_MAX_AGE)
            if not eligible:
                _auto.pop(uid, None)
            elif uid not in _auto:
                _auto[uid] = time.time() + AUTO_SEC
            elif _auto[uid] <= time.time():
                del _auto[uid]
                _auto_handled.add(uid)      # never auto-start twice, even if the start fails
                due.append(uid)
    for uid in due:
        err = analyze(uid)
        _auto_log(f"PAW: auto-started ({uid})" if not err else f"PAW: auto-start failed ({uid}): {err}")


def _auto_log(text):
    try:
        with open(WORK_DIR / "auto.log", "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + text + "\n")
    except OSError:
        pass


def start_auto():
    """Background thread for the auto-start (once per process)."""
    global _auto_thread
    if _auto_thread or not enabled():
        return

    def loop():
        while True:
            _auto_step()
            time.sleep(AUTO_TICK)

    _auto_thread = threading.Thread(target=loop, name="paw-auto", daemon=True)
    _auto_thread.start()
