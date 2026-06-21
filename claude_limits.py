#!/usr/bin/env python3
"""Menu bar monitor for Claude Code 5h-session + weekly usage limits.

Read-only: GETs the same official endpoint /usage uses, reads Claude Code's own
OAuth token from the macOS keychain. Never writes anything to Claude / Anthropic.

Run:  python3 claude_limits.py
Test: python3 claude_limits.py --selftest
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
STATE_FILE = os.path.expanduser("~/.claude-limits-state.json")
POLL_SECONDS = 180  # ponytail: endpoint 429s aggressively; drop lower only if it holds up
THRESHOLDS = [50, 80, 90]
# windows we alert on: (json key, short label)
WINDOWS = [("five_hour", "5h"), ("seven_day", "wk")]


# ---- pure logic (covered by --selftest) ----
def newly_crossed(util, already_fired):
    """Thresholds util has reached that haven't fired yet."""
    return [t for t in THRESHOLDS if util >= t and t not in already_fired]


def update_window(state, key, util, resets_at):
    """Mutate state for one window; return list of thresholds to notify now.
    Re-arms (clears fired) when resets_at changes -> new window."""
    w = state.get(key)
    if not w or w.get("resets_at") != resets_at:
        w = {"resets_at": resets_at, "fired": []}
        state[key] = w
    fires = newly_crossed(util, w["fired"])
    w["fired"].extend(fires)
    return fires


# ---- io ----
def now_ts():
    return datetime.now(timezone.utc).timestamp()


def get_token():
    """Read Claude Code's OAuth access token from the keychain. None if missing/expired."""
    r = subprocess.run(
        ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    try:
        o = json.loads(r.stdout).get("claudeAiOauth", {})
    except json.JSONDecodeError:
        return None
    exp = o.get("expiresAt")  # ms epoch
    if exp and exp / 1000 < now_ts():
        return None
    return o.get("accessToken")


def claude_version():
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=5)
        return out.stdout.split()[0] or "2.1.153"
    except Exception:
        return "2.1.153"  # ponytail: UA must look like claude-code/<ver> or endpoint 429s


def fetch_usage(token, ver):
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": f"claude-code/{ver}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def notify(title, msg):
    subprocess.run([
        "osascript", "-e",
        f'display notification {json.dumps(msg)} with title {json.dumps(title)} sound name "Glass"',
    ])


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(s):
    json.dump(s, open(STATE_FILE, "w"))


def fmt_reset(iso):
    try:
        dt = datetime.fromisoformat(iso).astimezone()
        return dt.strftime("%a %d %b %H:%M")
    except (ValueError, TypeError):
        return "?"


def fmt_left(iso):
    """Time until iso timestamp, e.g. '2h45' or '38m'."""
    try:
        secs = datetime.fromisoformat(iso).timestamp() - now_ts()
    except (ValueError, TypeError):
        return "?"
    if secs <= 0:
        return "0m"
    h, m = divmod(int(secs // 60), 60)
    return f"{h}h{m:02d}" if h else f"{m}m"


# ---- app ----
def run_app():
    import rumps  # deferred so --selftest needs no deps

    class ClaudeLimits(rumps.App):
        def __init__(self):
            super().__init__("claude-limits", title="claude … ")
            self.ver = claude_version()
            self.next_ok = 0.0   # don't call endpoint before this ts (429 backoff)
            self.backoff = 0.0
            self.line_5h = rumps.MenuItem("5h: …")
            self.line_wk = rumps.MenuItem("week: …")
            self.line_reset = rumps.MenuItem("")
            self.menu = [self.line_5h, self.line_wk, self.line_reset, None,
                         rumps.MenuItem("Refresh now", callback=lambda _: self.tick(None))]
            self.timer = rumps.Timer(self.tick, POLL_SECONDS)
            self.timer.start()
            self.tick(None)

        def tick(self, _):
            if now_ts() < self.next_ok:
                return  # in 429 backoff window; don't touch the bucket
            token = get_token()
            if not token:
                self.title = "⚠ re-auth"
                self.line_5h.title = "Token missing/expired"
                self.line_wk.title = "Run `claude` once to refresh, then Refresh now"
                self.line_reset.title = ""
                return
            try:
                data = fetch_usage(token, self.ver)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    # exponential backoff 5min -> 10 -> 20, cap 30min
                    self.backoff = min(max(self.backoff * 2, 300), 1800)
                    self.next_ok = now_ts() + self.backoff
                    mins = round(self.backoff / 60)
                    self.line_reset.title = f"⚠ 429 rate-limited, retry in {mins}min"
                else:
                    self.line_reset.title = f"⚠ HTTP {e.code} (last value kept)"
                return  # keep last good title
            except Exception as e:
                self.line_reset.title = f"⚠ {type(e).__name__} (last value kept)"
                return
            self.backoff = 0.0  # success -> reset backoff

            state = load_state()
            u5 = float((data.get("five_hour") or {}).get("utilization") or 0)
            uw = float((data.get("seven_day") or {}).get("utilization") or 0)
            r5 = (data.get("five_hour") or {}).get("resets_at")
            rw = (data.get("seven_day") or {}).get("resets_at")

            for key, label in WINDOWS:
                w = data.get(key) or {}
                util = float(w.get("utilization") or 0)
                for t in update_window(state, key, util, w.get("resets_at")):
                    notify(f"Claude {label} limit", f"{util:.0f}% used (crossed {t}%)")
            save_state(state)

            icon = "🟢" if max(u5, uw) < 50 else "🟡" if max(u5, uw) < 80 else "🔴"
            self.title = f"{icon} {fmt_left(r5)} {u5:.0f}% · wk {uw:.0f}%"
            self.line_5h.title = f"session: {u5:.0f}%  ({fmt_left(r5)} left, resets {fmt_reset(r5)})"
            self.line_wk.title = f"week:       {uw:.0f}%  (resets {fmt_reset(rw)})"
            self.line_reset.title = f"updated {datetime.now().strftime('%H:%M:%S')}"

    ClaudeLimits().run()


def selftest():
    s = {}
    assert update_window(s, "five_hour", 33, "T1") == []          # below 50
    assert update_window(s, "five_hour", 55, "T1") == [50]        # cross 50
    assert update_window(s, "five_hour", 60, "T1") == []          # no re-fire
    assert update_window(s, "five_hour", 95, "T1") == [80, 90]    # jump fires both
    assert update_window(s, "five_hour", 99, "T1") == []          # all fired
    assert update_window(s, "five_hour", 55, "T2") == [50]        # window reset re-arms
    assert newly_crossed(100, []) == [50, 80, 90]
    print("selftest ok")


def probe():
    token = get_token()
    if not token:
        print("no valid token (missing/expired) -> run `claude` once, retry")
        return
    try:
        print(json.dumps(fetch_usage(token, claude_version()), indent=2))
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}" + (" — rate-limited, wait a few min" if e.code == 429 else ""))


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--probe" in sys.argv:
        probe()
    else:
        run_app()
