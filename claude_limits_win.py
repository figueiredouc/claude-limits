#!/usr/bin/env python3
"""System-tray monitor for Claude Code 5h-session + weekly usage limits (Windows).

Read-only: GETs the same official endpoint /usage uses, reads Claude Code's own
OAuth token from %USERPROFILE%\\.claude\\.credentials.json. Never writes anything
to Claude / Anthropic.

Run:  python claude_limits_win.py
Test: python claude_limits_win.py --selftest
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
CRED_FILE = os.path.expanduser("~/.claude/.credentials.json")
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
    """Read Claude Code's OAuth access token from the creds file. None if missing/expired.
    Windows/Linux Claude Code stores creds as plaintext JSON, not in a keychain."""
    try:
        o = json.load(open(CRED_FILE)).get("claudeAiOauth", {})
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    exp = o.get("expiresAt")  # ms epoch
    if exp and exp / 1000 < now_ts():
        return None
    return o.get("accessToken")


def claude_version():
    try:
        # ponytail: claude is a .cmd shim on Windows -> needs shell=True to resolve
        out = subprocess.run("claude --version", capture_output=True, text=True,
                             timeout=5, shell=True)
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
def _icon_img(color):
    """Solid colored dot for the tray. ponytail: PIL only, no asset files."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((8, 8, 56, 56), fill=color)
    return img


COLORS = {"🟢": (40, 200, 80), "🟡": (240, 200, 40), "🔴": (230, 60, 60), "⚠": (150, 150, 150)}


def run_app():
    import pystray  # deferred so --selftest needs no deps

    state_view = {
        "icon": "⚠", "tip": "claude … ",
        "5h": "5h: …", "wk": "week: …", "reset": "",
        "next_ok": 0.0, "backoff": 0.0, "ver": claude_version(),
    }

    def menu_text(key):
        return lambda _: state_view[key]

    def tick(icon, _item=None):
        if now_ts() < state_view["next_ok"]:
            return  # in 429 backoff window; don't touch the bucket
        token = get_token()
        if not token:
            state_view.update(icon="⚠", tip="⚠ re-auth",
                              **{"5h": "Token missing/expired",
                                 "wk": "Run `claude` once to refresh, then Refresh now",
                                 "reset": ""})
            apply(icon)
            return
        try:
            data = fetch_usage(token, state_view["ver"])
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # exponential backoff 5min -> 10 -> 20, cap 30min
                state_view["backoff"] = min(max(state_view["backoff"] * 2, 300), 1800)
                state_view["next_ok"] = now_ts() + state_view["backoff"]
                mins = round(state_view["backoff"] / 60)
                state_view["reset"] = f"⚠ 429 rate-limited, retry in {mins}min"
            else:
                state_view["reset"] = f"⚠ HTTP {e.code} (last value kept)"
            apply(icon)
            return  # keep last good title
        except Exception as e:
            state_view["reset"] = f"⚠ {type(e).__name__} (last value kept)"
            apply(icon)
            return
        state_view["backoff"] = 0.0  # success -> reset backoff

        state = load_state()
        u5 = float((data.get("five_hour") or {}).get("utilization") or 0)
        uw = float((data.get("seven_day") or {}).get("utilization") or 0)
        r5 = (data.get("five_hour") or {}).get("resets_at")
        rw = (data.get("seven_day") or {}).get("resets_at")

        for key, label in WINDOWS:
            w = data.get(key) or {}
            util = float(w.get("utilization") or 0)
            for t in update_window(state, key, util, w.get("resets_at")):
                icon.notify(f"{util:.0f}% used (crossed {t}%)", f"Claude {label} limit")
        save_state(state)

        ico = "🟢" if max(u5, uw) < 50 else "🟡" if max(u5, uw) < 80 else "🔴"
        state_view.update(
            icon=ico,
            tip=f"{ico} {fmt_left(r5)} {u5:.0f}% · wk {uw:.0f}%",
            **{"5h": f"session: {u5:.0f}%  ({fmt_left(r5)} left, resets {fmt_reset(r5)})",
               "wk": f"week:       {uw:.0f}%  (resets {fmt_reset(rw)})",
               "reset": f"updated {datetime.now().strftime('%H:%M:%S')}"})
        apply(icon)

    def apply(icon):
        icon.icon = _icon_img(COLORS[state_view["icon"]])
        icon.title = state_view["tip"]
        icon.update_menu()

    menu = pystray.Menu(
        pystray.MenuItem(menu_text("5h"), None, enabled=False),
        pystray.MenuItem(menu_text("wk"), None, enabled=False),
        pystray.MenuItem(menu_text("reset"), None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Refresh now", lambda icon, _: tick(icon)),
        pystray.MenuItem("Quit", lambda icon, _: icon.stop()),
    )
    icon = pystray.Icon("claude-limits", _icon_img(COLORS["⚠"]), "claude … ", menu)

    def setup(icon):
        icon.visible = True
        tick(icon)
        # ponytail: pystray has no built-in timer; one daemon thread polling is enough
        import threading
        def loop():
            import time
            while True:
                time.sleep(POLL_SECONDS)
                tick(icon)
        threading.Thread(target=loop, daemon=True).start()

    icon.run(setup=setup)


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
