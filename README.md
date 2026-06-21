# claude-limits

Tiny macOS menu bar app that shows your Claude Code usage limits at a glance and
notifies you before you hit them.

```
🟢 2h44 34% · wk 5%
```

- **`2h44`** — time left in the current 5-hour session window
- **`34%`** — session usage
- **`wk 5%`** — weekly usage
- Icon: 🟢 < 50% · 🟡 < 80% · 🔴 ≥ 80% (of whichever limit is closest)

Click the icon for both limits + exact reset times.

## Notifications

Native macOS alerts when you cross **50%**, **80%**, and **90%** — on *both* the
5-hour session and the weekly window. Each fires once and re-arms when that
window resets.

## Read-only — by design

It only ever **reads**:

- Calls `GET https://api.anthropic.com/api/oauth/usage` — the same endpoint that
  powers Claude Code's `/usage`. A plain GET, nothing is ever sent.
- Reads Claude Code's own OAuth token from the macOS keychain
  (`Claude Code-credentials`) at runtime. The token is held in memory only —
  never logged, never written to disk, never committed.
- Does **not** refresh the token. If it expires, the bar shows `⚠ re-auth`;
  run `claude` once and it recovers.

No API keys or secrets live in this repo.

## Install

Requires Python 3 and the [Claude Code](https://claude.com/claude-code) CLI
(for the keychain token).

```bash
git clone https://github.com/figueiredouc/claude-limits.git
cd claude-limits
python3 -m venv .venv
.venv/bin/pip install rumps
```

Verify the endpoint works (prints raw JSON):

```bash
.venv/bin/python claude_limits.py --probe
```

Run it:

```bash
.venv/bin/python claude_limits.py
```

## Auto-start at login

Create `~/Library/LaunchAgents/com.<you>.claude-limits.plist` pointing at the
venv Python and `claude_limits.py`, with `RunAtLoad` + `KeepAlive`, then:

```bash
launchctl load ~/Library/LaunchAgents/com.<you>.claude-limits.plist
```

Reload after editing the code:

```bash
launchctl kickstart -k gui/$(id -u)/com.<you>.claude-limits
```

## Notes

- Polls every 180s. The endpoint rate-limits aggressively; on a `429` the app
  backs off (5 → 30 min) and recovers on its own.
- Don't run `--probe` while the app is running — two callers share the same rate
  limit bucket.

## Tests

```bash
python3 claude_limits.py --selftest   # threshold + window-reset logic, no deps
```

## License

MIT
