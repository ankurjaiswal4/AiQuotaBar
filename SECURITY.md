# Security

## What AiQuotaBar reads, sends and stores

| Data | Where it goes |
|---|---|
| Claude Code OAuth token | Read from the macOS Keychain item `Claude Code-credentials` (`security find-generic-password`), or from `~/.claude/.credentials.json`. Held in memory for one run. Never logged, printed or saved. Re-read on every run. |
| The token, sent | Only to `https://api.anthropic.com/api/oauth/usage` as a Bearer header, over HTTPS, 5 second timeout. The URL is a fixed constant. |
| Request headers | `anthropic-beta: oauth-2025-04-20` and `User-Agent: claude-code/…` (without it the endpoint rate-limits the request). |
| Usage data (percentages, reset times) | Saved to `~/.aiquotabar/state.json` (also which thresholds fired, and the last known values for offline mode). |
| Your settings | `~/.aiquotabar/config.json`. |
| Claude Code transcripts | The tail of `~/.claude/projects/*/*.jsonl` is read for the effort watch. Only each reply's timestamp and effort are used. Project folder names appear in menu rows and alerts. Nothing is sent anywhere. |
| Telemetry | None. |

## Protections

- No `eval`, no `shell=True`. The engine's only subprocess is `security`, called with an argument list.
- Notifications are posted by the app itself (Apple's UserNotifications framework) as plain text. Project names in alerts are data and are never run as code.
- Crash output is redacted for anything shaped like `sk-ant-…` and truncated. On any error the engine still prints valid JSON and exits 0.
- The API response is untrusted. Types and ranges are checked, and any surprise falls back to the offline view.
- Network goes to one hardcoded HTTPS host. No user-controlled URL.
- `state.json` and `config.json` are written atomically. A corrupt state file is deleted and rebuilt.
- The app runs the engine as `/usr/bin/python3 <app>/Contents/Resources/aiquotabar.py` with an argument list (no shell). Settings go through a fixed set of `--set` tokens built from constants, and the engine accepts only known keys and values.
- The app never shows raw engine output; error lines are short and redacted.
- Zero third-party dependencies. From a clone, `install.sh` downloads nothing: it compiles the app locally with `swiftc` and signs it ad hoc. The one-line install also downloads `aiquotabar.py`, `AiQuotaBar.swift` and the logo from this repo over HTTPS and refuses to build files that aren't AiQuotaBar.

## Known limits

- The usage endpoint is undocumented. It can change, and Anthropic's terms apply to your use of it (see [NOTICE.md](NOTICE.md)).
- Anyone who can run code as you can already read your Keychain item. AiQuotaBar does not make that worse, but it does not defend against it.
- If notifications are turned off for AiQuotaBar in System Settings, alerts are dropped silently; the panel always shows a hint pointing there.
- If you fork it, run `gitleaks detect --source . -v` before publishing.

## Reporting a vulnerability

Use GitHub private vulnerability reporting: open the repository's **Security** tab, then **Report a vulnerability**. Please do not open a public issue for a security problem. Say what you found, how to reproduce it, and which version.
