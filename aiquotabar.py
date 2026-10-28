#!/usr/bin/env python3
#
# AiQuotaBar engine (single file, Python 3 stdlib only). The native menu bar app
# (AiQuotaBar.swift) runs `python3 aiquotabar.py --json` every minute and draws
# what it prints; `--set key=value` changes a setting.
#
# Each run: read the Claude Code OAuth token (Keychain, read-only, in memory),
# fetch usage once (reusing data under 50 s old), decide which threshold alerts
# are due with the pure `evaluate(snapshot, state)` (each fires once per
# window), then print the panel state as JSON, with any due alerts in it for the
# app to post. Any failure still prints valid JSON. The token is never logged,
# printed or saved.
#
# Unofficial. Not affiliated with or endorsed by Anthropic. "Claude" and
# "Claude Code" are trademarks of Anthropic, PBC (see NOTICE.md).

from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# Notification thresholds. The threshold engine fires at most once per meter per
# window as utilization crosses each of these. Escalating cadence: sparse early
# (25, 50), every 10% through the middle (60, 70, 80), then EVERY 1% in the
# danger zone (90–100) since each point there matters. Must stay ascending
# (alerts emit oldest-crossing-first).
THRESHOLDS: tuple[int, ...] = (25, 50, 60, 70, 80) + tuple(range(90, 101))

# Usage endpoint + headers. Undocumented and community reverse-engineered: the same
# endpoint Claude Code's `/usage` consumes. Kept here so a breakage is a one-line
# fix. The User-Agent MUST identify as claude-code: without it,
# requests hit an aggressively rate-limited bucket (persistent 429).
USAGE_ENDPOINT: str = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA_HEADER: str = "oauth-2025-04-20"
USER_AGENT: str = "claude-code/2.0.0"
FETCH_TIMEOUT: float = 5.0  # seconds
# The app re-runs the engine when the panel opens; reuse data this fresh instead of
# asking Anthropic again, so a few quick clicks can't hit its rate limit (429).
MIN_FETCH_INTERVAL: timedelta = timedelta(seconds=50)

# The endpoint's modern shape is a structured `limits` array (session / weekly_all
# / weekly_scoped per model; this is where per-model limits like Fable show up).
# We prefer it and only fall back to these legacy flat fields if it's absent.
# Fields absent or null mean that limit isn't active. `seven_day_sonnet` is
# ignored: the flat fallback tracks session / all-models weekly / Opus weekly.
FIELD_KINDS: dict[str, str] = {
    "five_hour": "session",
    "seven_day": "week",
    "seven_day_opus": "week_opus",
}

# Credential sources, re-read every invocation (tokens rotate).
KEYCHAIN_SERVICE: str = "Claude Code-credentials"
CREDENTIALS_FILE: str = os.path.expanduser("~/.claude/.credentials.json")

# Runtime state: the last snapshot (shown while offline) and the fired thresholds.
STATE_DIR: str = os.path.expanduser("~/.aiquotabar")
STATE_FILE: str = os.path.join(STATE_DIR, "state.json")

# User settings, kept in a separate file from the runtime state so flipping a
# setting can never corrupt the threshold windows. Written by the `--set` CLI
# path (the app's settings call the engine with `--set key=value`).
CONFIG_FILE: str = os.path.join(STATE_DIR, "config.json")
# "both" shows weekly then session, the same order as the panel's two rings. An old
# stored "tightest" isn't a choice any more, so it falls back to the default.
DEFAULT_CONFIG: dict = {"menubar": "both", "notifications": True, "min_threshold": 25}
MENUBAR_CHOICES: tuple[str, ...] = ("weekly", "session", "both")
# Presets for the "Alert from X%" setting: only thresholds >= this fire a
# notification (25 = every threshold, the default). Must be a subset of THRESHOLDS.
MIN_THRESHOLD_CHOICES: tuple[int, ...] = (25, 50, 70, 90)

# Launch at login: a per-user LaunchAgent that opens the AiQuotaBar app (which runs this
# engine) when you log in. A plist file needs no Automation permission, unlike a
# System Events login item. Its presence IS the setting, so there is nothing to
# keep in sync. install.sh --login writes the same file.
APP_BUNDLE_ID: str = "io.github.ankurjaiswal4.aiquotabar"
LOGIN_AGENT_LABEL: str = "local.aiquotabar.login"
LOGIN_AGENT_FILE: str = os.path.expanduser(
    f"~/Library/LaunchAgents/{LOGIN_AGENT_LABEL}.plist")

# Menu bar text: each window's short name and its percentage (`Wk 88% · 5h 13%`).
# The app colours each number by its own state.
ICON_COLD: str = "◌"     # no data yet / not signed in
MENU_BAR_LABELS: dict[str, str] = {"week": "Wk", "session": "5h"}

ORANGE_AT: int = 70
RED_AT: int = 90

# What --json prints is a contract with AiQuotaBar.swift: bump SCHEMA when a field
# changes meaning, and keep fixtures/state.sample.json in step.
VERSION: str = "1.0"
SCHEMA: int = 4

# Pace projection: how long each window is, and how much of it must have passed
# before a projection is trusted (early in a window one burst skews the rate).
SESSION_WINDOW: timedelta = timedelta(hours=5)
WEEK_WINDOW: timedelta = timedelta(days=7)
PACE_MIN_ELAPSED: float = 0.10

# Shown under the alert settings, since the fix for missing alerts lives in macOS.
NOTIF_HINT: str = "No alerts? Allow notifications for AiQuotaBar in System Settings › Notifications."

# Short labels for the known flat-field kinds (scoped models from the `limits`
# array carry their own label, such as "Fable wk").
KIND_LABELS: dict[str, str] = {
    "session": "Session",
    "week": "Week",
    "week_opus": "Opus wk",
}

# Longer labels used in notification bodies (falls back to the meter's own label).
KIND_ALERT_LABELS: dict[str, str] = {
    "session": "Session",
    "week": "Weekly",
    "week_opus": "Opus weekly",
}

# "Where your week went" (the endpoint's seven_day_breakdown): at most this many
# rows, and a display name longer than this is cut, since the response is untrusted.
BREAKDOWN_MAX_ROWS: int = 4
BREAKDOWN_NAME_MAX: int = 24

# Set True to render the hardcoded fake snapshot instead of fetching live data
# (useful for offline rendering work). False = live path.
FAKE_DATA: bool = False

# Effort watch: how long a Claude Code session has been replying at a high
# reasoning effort. High effort burns the session meter much faster, and it's easy
# to leave it on after the hard part is done. Reads only the local transcripts
# (timestamp + effort per reply): zero tokens, no network.
PROJECTS_DIR: str = os.path.expanduser("~/.claude/projects")
HIGH_EFFORTS: tuple[str, ...] = ("high", "xhigh", "max")
EFFORT_ALERT_MINUTES: int = 15
# A longer silence between replies means the person stepped away, so the streak
# starts over: 15 minutes of work, not 15 minutes of wall clock.
EFFORT_IDLE_GAP: timedelta = timedelta(minutes=10)
# Only a session that replied this recently counts as "in use now".
EFFORT_ACTIVE_WINDOW: timedelta = timedelta(minutes=5)
# Transcripts grow to tens of MB; the recent replies are all at the end.
TRANSCRIPT_TAIL_BYTES: int = 512 * 1024


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class Meter:
    kind: str            # stable identity: "session" | "week" | "week_<model>"
    pct: float           # 0–100, official
    resets_at: datetime  # timezone-aware, UTC
    label: str = ""      # display label (e.g. "Fable wk"); derived from kind if ""


def meter_label(meter: Meter) -> str:
    """Short panel label: the entry's own label, else a name derived from kind."""
    return meter.label or KIND_LABELS.get(meter.kind, meter.kind)


def meter_alert_label(meter: Meter) -> str:
    """Longer label for notification bodies (falls back to the short label)."""
    if meter.kind in KIND_ALERT_LABELS:
        return KIND_ALERT_LABELS[meter.kind]
    return meter.label or meter.kind


def meter_sort_key(meter: Meter) -> tuple[int, str]:
    """Stable ordering: session, then all-models weekly, then scoped models
    (grouped, alphabetical by label), then anything unknown."""
    if meter.kind == "session":
        return (0, "")
    if meter.kind == "week":
        return (1, "")
    if meter.kind.startswith("week_"):
        return (2, meter_label(meter))
    return (3, meter_label(meter))


@dataclass
class UsageShare:
    """One row of "Where your week went": a surface (Claude Code, Chats, …) and
    its share of this week's usage."""
    name: str
    pct: int


@dataclass
class UsageSnapshot:
    meters: list[Meter] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    official: bool = True  # False when the numbers are an estimate
    breakdown: list[UsageShare] = field(default_factory=list)  # biggest share first
    breakdown_since: datetime | None = None                     # start of the week window


@dataclass
class RenderModel:
    """What the renderer needs: the mode plus whatever data backs it."""
    mode: str                               # "live" | "degraded" | "cold" | "signin"
    snapshot: UsageSnapshot | None = None   # live: fresh; degraded: last-known
    since: datetime | None = None           # degraded: when live data last worked


@dataclass
class Alert:
    """One notification the app should post (plain data, so it is easy to test)."""
    kind: str                 # meter kind
    event: str                # "threshold" | "reset"
    threshold: int | None     # set when event == "threshold"
    body: str                 # notification body text


@dataclass
class EffortStreak:
    """One session's current run of consecutive high-effort replies."""
    session_id: str
    project: str
    effort: str               # effort of the latest reply ("high" | "xhigh" | "max")
    started_at: datetime      # first reply of the unbroken run (UTC)
    last_at: datetime         # latest reply (UTC)

    @property
    def minutes(self) -> int:
        return int((self.last_at - self.started_at).total_seconds() // 60)


# --------------------------------------------------------------------------- #
# Credentials (read-only and in memory; never logged, printed or persisted)
# --------------------------------------------------------------------------- #


def load_access_token() -> str | None:
    """Resolve the Claude Code OAuth access token: Keychain, then file."""
    return _token_from_keychain() or _token_from_file()


def _token_from_keychain() -> str | None:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=FETCH_TIMEOUT,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return _extract_token(result.stdout)


def _token_from_file() -> str | None:
    try:
        with open(CREDENTIALS_FILE, "r", encoding="utf-8") as handle:
            return _extract_token(handle.read())
    except Exception:
        return None


def _extract_token(raw: str) -> str | None:
    """Pull claudeAiOauth.accessToken out of the credentials JSON, defensively."""
    try:
        oauth = json.loads(raw).get("claudeAiOauth") or {}
    except Exception:
        return None
    token = oauth.get("accessToken")
    return token if isinstance(token, str) and token else None


# --------------------------------------------------------------------------- #
# Usage fetch + defensive parsing (the response is treated as untrusted)
# --------------------------------------------------------------------------- #


def fetch_usage(token: str) -> UsageSnapshot | None:
    """GET the usage endpoint. Any failure -> None (caller degrades)."""
    request = urllib.request.Request(USAGE_ENDPOINT, method="GET")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("anthropic-beta", OAUTH_BETA_HEADER)
    request.add_header("User-Agent", USER_AGENT)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            if response.status != 200:
                return None
            body = response.read().decode("utf-8")
    except Exception:
        # 401/403/429/timeout/DNS/non-JSON all funnel to degraded mode.
        return None
    return _parse_usage(body)


def _parse_usage(body: str) -> UsageSnapshot | None:
    try:
        data = json.loads(body)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    # Prefer the structured `limits` array (has per-model limits like Fable); fall
    # back to the legacy flat fields only if it's missing/empty.
    meters = _parse_limits(data.get("limits")) or _parse_flat_fields(data)
    if not meters:
        return None
    breakdown, since = _parse_breakdown(data.get("seven_day_breakdown"))
    return UsageSnapshot(meters=meters, fetched_at=datetime.now(timezone.utc), official=True,
                         breakdown=breakdown, breakdown_since=since)


def _parse_breakdown(block: object) -> tuple[list[UsageShare], datetime | None]:
    """Parse `seven_day_breakdown`: which surfaces used this week's limit. Rows at
    0% are dropped; anything malformed is skipped. Missing -> ([], None)."""
    if not isinstance(block, dict) or not isinstance(block.get("rows"), list):
        return [], None
    shares: list[UsageShare] = []
    for row in block["rows"]:
        if not isinstance(row, dict):
            continue
        name = row.get("display_name")
        pct = row.get("percent")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(pct, (int, float)) or isinstance(pct, bool) or not 0 < pct <= 100:
            continue
        shares.append(UsageShare(name=name.strip()[:BREAKDOWN_NAME_MAX], pct=int(round(pct))))
    shares.sort(key=lambda share: share.pct, reverse=True)
    return shares[:BREAKDOWN_MAX_ROWS], _parse_utc(block.get("window_started_at"))


def _parse_limits(limits: object) -> list[Meter]:
    """Parse the modern `limits` array. Each entry is one meter; scoped entries
    carry a per-model display name (Fable, Opus, …)."""
    if not isinstance(limits, list):
        return []
    meters: list[Meter] = []
    for entry in limits:
        meter = _parse_limit_entry(entry)
        if meter is not None:
            meters.append(meter)
    return meters


def _parse_limit_entry(entry: object) -> Meter | None:
    if not isinstance(entry, dict):
        return None
    kind, label = _classify_limit(entry)
    if kind is None:
        return None  # unknown limit kind -> skip rather than guess
    pct = entry.get("percent")
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return None
    if pct < 0 or pct > 100:
        return None
    resets_at = _parse_utc(entry.get("resets_at"))
    if resets_at is None:
        return None
    return Meter(kind=kind, pct=float(pct), resets_at=resets_at, label=label)


def _classify_limit(entry: dict) -> tuple[str | None, str]:
    """Map a `limits[]` entry to (stable kind, display label). Unknown -> (None, "")."""
    raw = entry.get("kind")
    if raw == "session":
        return "session", "Session"
    if raw == "weekly_all":
        return "week", "Week"
    if raw == "weekly_scoped":
        model = ((entry.get("scope") or {}).get("model") or {}).get("display_name")
        model = model if isinstance(model, str) and model else "Scoped"
        # Stable per-model kind so window identity + fire-once survive across runs.
        return f"week_{model.lower()}", f"{model} wk"
    return None, ""


def _parse_flat_fields(data: dict) -> list[Meter]:
    """Legacy fallback: the old flat top-level fields (utilization-based)."""
    meters: list[Meter] = []
    for field_name, kind in FIELD_KINDS.items():
        entry = data.get(field_name)
        if not isinstance(entry, dict):
            continue  # null / missing -> that limit isn't active
        meter = _parse_meter(kind, entry)
        if meter is not None:
            meters.append(meter)
    return meters


def _parse_meter(kind: str, entry: dict) -> Meter | None:
    pct = entry.get("utilization")
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return None
    if pct < 0 or pct > 100:
        return None  # out-of-range -> a surprise; skip rather than trust it
    resets_at = _parse_utc(entry.get("resets_at"))
    if resets_at is None:
        return None
    return Meter(kind=kind, pct=float(pct), resets_at=resets_at,
                 label=KIND_LABELS.get(kind, kind))


def _parse_utc(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# State store: atomic writes, and a corrupt file is simply rebuilt
# --------------------------------------------------------------------------- #


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        return {}  # normal on first run
    except Exception:
        # Unreadable/corrupt file -> delete and rebuild. Worst
        # case is one duplicate notification, which is acceptable.
        return _reset_state()
    return state if isinstance(state, dict) else _reset_state()


def _reset_state() -> dict:
    """Delete a corrupt state file and start fresh. Never raises."""
    try:
        os.remove(STATE_FILE)
    except Exception:
        pass
    return {}


def write_state(state: dict) -> None:
    """Atomically persist the full state dict. Never raises."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp, STATE_FILE)  # atomic
    except Exception:
        pass  # a failed state write must never break the menu bar


def cached_snapshot() -> UsageSnapshot | None:
    data = load_state().get("last_snapshot")
    return _snapshot_from_dict(data) if isinstance(data, dict) else None


def _snapshot_to_dict(snapshot: UsageSnapshot) -> dict:
    return {
        "meters": [
            {"kind": m.kind, "pct": m.pct, "resets_at": m.resets_at.isoformat(),
             "label": m.label}
            for m in snapshot.meters
        ],
        "fetched_at": snapshot.fetched_at.isoformat(),
        "official": snapshot.official,
        "breakdown": [{"name": share.name, "pct": share.pct} for share in snapshot.breakdown],
        "breakdown_since": snapshot.breakdown_since.isoformat() if snapshot.breakdown_since else None,
    }


def _snapshot_from_dict(data: dict) -> UsageSnapshot | None:
    try:
        meters = [
            Meter(kind=m["kind"], pct=float(m["pct"]),
                  resets_at=datetime.fromisoformat(m["resets_at"]),
                  label=m.get("label", ""))
            for m in data["meters"]
        ]
        breakdown = [UsageShare(name=str(row["name"]), pct=int(row["pct"]))
                     for row in data.get("breakdown") or []]
        since = data.get("breakdown_since")
        return UsageSnapshot(
            meters=meters,
            fetched_at=datetime.fromisoformat(data["fetched_at"]),
            official=bool(data.get("official", True)),
            breakdown=breakdown,
            breakdown_since=datetime.fromisoformat(since) if since else None,
        )
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# User settings (menu-bar meter choice + notification mute)
# --------------------------------------------------------------------------- #


def load_config() -> dict:
    """Read config.json over the defaults. Any problem -> defaults (never raises)."""
    config = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
    except FileNotFoundError:
        return config  # normal until the user changes a setting
    except Exception:
        return config  # unreadable/corrupt -> fall back to defaults
    if isinstance(stored, dict):
        if stored.get("menubar") in MENUBAR_CHOICES:
            config["menubar"] = stored["menubar"]
        if isinstance(stored.get("notifications"), bool):
            config["notifications"] = stored["notifications"]
        if stored.get("min_threshold") in MIN_THRESHOLD_CHOICES:
            config["min_threshold"] = stored["min_threshold"]
    return config


def save_config(config: dict) -> None:
    """Atomically persist the config. Never raises (a settings write must not
    break the menu bar)."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        os.replace(tmp, CONFIG_FILE)
    except Exception:
        pass


def apply_setting(token: str) -> None:
    """Apply one `key=value` setting. Whitelisted keys and values only; an
    unknown token is ignored rather than trusted."""
    key, sep, value = token.partition("=")
    if not sep:
        return
    config = load_config()
    if key == "menubar" and value in MENUBAR_CHOICES:
        config["menubar"] = value
    elif key == "notifications" and value in ("on", "off"):
        config["notifications"] = (value == "on")
    elif key == "min_threshold" and value.isdigit() and int(value) in MIN_THRESHOLD_CHOICES:
        config["min_threshold"] = int(value)
    elif key == "login" and value in ("on", "off"):
        set_launch_at_login(value == "on")
        return  # lives in LaunchAgents, not config.json
    else:
        return
    save_config(config)


def launch_at_login() -> bool:
    return os.path.exists(LOGIN_AGENT_FILE)


def set_launch_at_login(enabled: bool) -> None:
    """Write or remove the LaunchAgent. It takes effect at the next login; we
    don't `launchctl load` it, since the app is already running."""
    if not enabled:
        if os.path.exists(LOGIN_AGENT_FILE):
            os.remove(LOGIN_AGENT_FILE)
        return
    agent = {
        "Label": LOGIN_AGENT_LABEL,
        "ProgramArguments": ["/usr/bin/open", "-b", APP_BUNDLE_ID],
        "RunAtLoad": True,
    }
    os.makedirs(os.path.dirname(LOGIN_AGENT_FILE), exist_ok=True)
    with open(LOGIN_AGENT_FILE, "wb") as fh:
        plistlib.dump(agent, fh)


def run_cli(argv: list[str]) -> None:
    """Settings invocation: `aiquotabar.py --set key=value` (from the app or a shell).
    Never raises."""
    for arg in argv:
        if arg == "--set":
            continue
        if "=" in arg:
            apply_setting(arg)


# --------------------------------------------------------------------------- #
# Fake data for offline rendering work (only used when FAKE_DATA is True)
# --------------------------------------------------------------------------- #


def _next_weekday_at(hour: int, minute: int, weekday: int) -> datetime:
    now_local = datetime.now().astimezone()
    target = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    days_ahead = (weekday - now_local.weekday()) % 7
    if days_ahead == 0 and target <= now_local:
        days_ahead = 7
    return (target + timedelta(days=days_ahead)).astimezone(timezone.utc)


def build_fake_snapshot() -> UsageSnapshot:
    now = datetime.now(timezone.utc)
    monday_9am = _next_weekday_at(hour=9, minute=0, weekday=0)
    return UsageSnapshot(
        meters=[
            Meter(kind="session", pct=68.0, resets_at=now + timedelta(hours=1, minutes=40)),
            Meter(kind="week", pct=41.0, resets_at=monday_9am),
            Meter(kind="week_opus", pct=12.0, resets_at=monday_9am),
        ],
        fetched_at=now,
        official=True,
    )


# --------------------------------------------------------------------------- #
# Threshold engine (pure) and the code that persists its result
# --------------------------------------------------------------------------- #


def window_id(meter: Meter) -> str:
    """A window's identity is its kind + reset timestamp.

    When the endpoint returns a new resets_at for a kind, that is by definition
    a new window. This survives sleep, reboots, and missed refreshes with no
    timer math.

    The resets_at value JITTERS by a fraction of a second between fetches (the
    server recomputes it each request), so the raw microsecond timestamp would
    mint a "new window" on every refresh and fire a spurious session-reset alert
    every minute. We round to the nearest minute for the identity: a real reset
    moves resets_at by hours (session) or days (weekly), so minute precision
    tells real windows apart while absorbing the sub-second noise.
    """
    rounded = (meter.resets_at + timedelta(seconds=30)).replace(second=0, microsecond=0)
    return f"{meter.kind}:{rounded.isoformat()}"


def _alert_body(meter: Meter, threshold: int, now: datetime) -> str:
    """e.g. "Session at 96% — resets in 42m" / "Weekly at 90% — resets Sat 17:52"."""
    label = meter_alert_label(meter)
    if meter.kind == "session":
        relative = f"in {format_countdown(meter.resets_at - now)}"
    else:
        relative = format_reset_day(meter.resets_at)
    return f"{label} at {int(round(meter.pct))}% — resets {relative}"


def evaluate(snapshot: UsageSnapshot, state: dict,
             min_threshold: int = 25) -> tuple[list[Alert], dict]:
    """Pure: (snapshot, state) -> (alerts in emit order, new_state).

    No I/O and no notifications, so it's fully unit-testable. Estimated or
    degraded data (official=False) is a no-op: alerts come from official data only.
    Countdowns are measured against snapshot.fetched_at so the result is
    deterministic for a given input.

    `min_threshold` is the user's "Alert from X%" floor. It gates the session-reset
    alert: we only announce "full window available" if the previous window actually
    reached that floor. Otherwise a user who muted the low alerts would still get
    pinged on reset, which they'd (rightly) call noise.
    """
    new_state = dict(state)
    if not snapshot.official:
        return [], new_state

    windows = dict(state.get("windows", {}))
    alerts: list[Alert] = []
    now = snapshot.fetched_at

    for meter in sorted(snapshot.meters, key=meter_sort_key):
        wid = window_id(meter)
        prev = windows.get(meter.kind)

        if prev is None:
            # First-ever observation of this kind: baseline the already-crossed
            # thresholds silently so a mid-session install doesn't spam. Future
            # crossings still fire.
            fired = [t for t in THRESHOLDS if t <= meter.pct]
            windows[meter.kind] = {"id": wid, "fired": sorted(fired)}
            continue

        if prev.get("id") != wid:
            # Genuine new window (reset timestamp changed). Show the trail of
            # crossings we may have missed while asleep; session-reset alert only
            # if the previous window reached the user's alert floor, so raising
            # "Alert from" quiets the reset ping too.
            if meter.kind == "session" and any(
                    t >= min_threshold for t in prev.get("fired", [])):
                alerts.append(Alert(
                    kind="session", event="reset", threshold=None,
                    body="Session reset — full 5-hour window available."))
            fired = []
        else:
            fired = list(prev.get("fired", []))

        for threshold in THRESHOLDS:  # ascending -> oldest crossing first
            if threshold <= meter.pct and threshold not in fired:
                alerts.append(Alert(
                    kind=meter.kind, event="threshold", threshold=threshold,
                    body=_alert_body(meter, threshold, now)))
                fired.append(threshold)

        windows[meter.kind] = {"id": wid, "fired": sorted(fired)}

    new_state["windows"] = windows
    new_state["version"] = 1
    return alerts, new_state


def process_live(snapshot: UsageSnapshot, config: dict) -> list[str]:
    """Run the engine on official data, persist windows + cache, and return the
    alert texts the app should post.

    When notifications are muted we still run the engine and persist the fired
    thresholds, so re-enabling later doesn't dump a backlog of alerts. Muting
    means "don't announce", not "replay everything when I come back".
    """
    min_threshold = config.get("min_threshold", 25)
    alerts, new_state = evaluate(snapshot, load_state(), min_threshold)
    new_state["last_snapshot"] = _snapshot_to_dict(snapshot)
    new_state.setdefault("version", 1)
    write_state(new_state)
    # The threshold windows are already persisted above, so a raised "Alert from"
    # floor suppresses the notification without ever replaying it later.
    if not config.get("notifications", True):
        return []
    due: list[str] = []
    for alert in alerts:
        if (alert.event == "threshold" and alert.threshold is not None
                and alert.threshold < min_threshold):
            continue
        due.append(alert.body)
    return due


# --------------------------------------------------------------------------- #
# Effort watch: pure streak and alert logic, then the transcript reader
# --------------------------------------------------------------------------- #


def effort_streak(replies: list[tuple[datetime, str]], now: datetime,
                  session_id: str = "", project: str = "") -> EffortStreak | None:
    """Pure: the unbroken high-effort run ending at the latest reply, or None.

    `replies` is (timestamp, effort) in time order. No streak when the session
    is idle (latest reply older than EFFORT_ACTIVE_WINDOW) or the latest reply
    isn't high effort. Walking back, the run ends at a lower-effort reply or at a
    gap longer than EFFORT_IDLE_GAP.
    """
    if not replies:
        return None
    last_at, last_effort = replies[-1]
    if now - last_at > EFFORT_ACTIVE_WINDOW or last_effort not in HIGH_EFFORTS:
        return None

    started_at = last_at
    for at, effort in reversed(replies[:-1]):
        if effort not in HIGH_EFFORTS or started_at - at > EFFORT_IDLE_GAP:
            break
        started_at = at
    return EffortStreak(session_id=session_id, project=project, effort=last_effort,
                        started_at=started_at, last_at=last_at)


def session_effort(replies: list[tuple[datetime, str]], now: datetime,
                   session_id: str = "", project: str = "") -> EffortStreak | None:
    """Pure: what an active session is running at right now, or None if idle.

    High effort returns its streak; any other effort returns a zero-length reading
    so the panel can still show "Effort medium", since a meter that only
    appears when it fires looks like it's missing.
    """
    if not replies or now - replies[-1][0] > EFFORT_ACTIVE_WINDOW:
        return None
    streak = effort_streak(replies, now, session_id, project)
    if streak is not None:
        return streak
    last_at, effort = replies[-1]
    return EffortStreak(session_id=session_id, project=project, effort=effort,
                        started_at=last_at, last_at=last_at)


def evaluate_effort(streaks: list[EffortStreak], state: dict) -> tuple[list[Alert], dict]:
    """Pure: one alert per streak the first time it reaches EFFORT_ALERT_MINUTES.

    State remembers the start of the streak we already announced per session, so
    the same streak never re-fires, while a new streak (after a break or an
    effort drop) can. Sessions without a live streak are forgotten.
    """
    announced = state.get("effort_alerted", {})
    kept: dict[str, str] = {}
    alerts: list[Alert] = []
    for streak in streaks:
        start = streak.started_at.isoformat()
        if announced.get(streak.session_id) == start:
            kept[streak.session_id] = start
            continue
        if streak.minutes >= EFFORT_ALERT_MINUTES:
            alerts.append(Alert(
                kind="effort", event="effort", threshold=None,
                body=(f"{streak.project}: effort {streak.effort} for {streak.minutes} min. "
                      f"If the hard part is done, /effort medium saves the session meter.")))
            kept[streak.session_id] = start
    new_state = dict(state)
    new_state["effort_alerted"] = kept
    return alerts, new_state


def _project_name(slug: str) -> str:
    """Transcript folders are the project path with '/' turned into '-'.
    `...-projects-deck-app` -> `deck-app`; otherwise the last segment."""
    if "-projects-" in slug:
        return slug.rsplit("-projects-", 1)[1]
    return slug.rstrip("-").rsplit("-", 1)[-1] or slug


def _recent_replies(path: str) -> list[tuple[datetime, str]]:
    """(timestamp, effort) for each assistant reply in the transcript's tail.
    Only those two fields are read, never message content."""
    replies: list[tuple[datetime, str]] = []
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        handle.seek(max(0, handle.tell() - TRANSCRIPT_TAIL_BYTES))
        tail = handle.read().decode("utf-8", errors="ignore")
    for line in tail.splitlines():
        if '"effort"' not in line:
            continue  # cheap filter before parsing a potentially large line
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # the first line of the tail is usually cut mid-way
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        at = _parse_utc(entry.get("timestamp"))
        effort = entry.get("effort")
        if at is not None and isinstance(effort, str):
            replies.append((at, effort))
    replies.sort(key=lambda reply: reply[0])
    return replies


def find_active_sessions(now: datetime) -> list[EffortStreak]:
    """Every main session that replied in the last EFFORT_ACTIVE_WINDOW, with its
    current effort. High-effort streaks first, longest first. Never raises."""
    streaks: list[EffortStreak] = []
    try:
        project_dirs = os.listdir(PROJECTS_DIR)
    except OSError:
        return streaks
    cutoff = (now - EFFORT_ACTIVE_WINDOW).timestamp()
    for slug in project_dirs:
        folder = os.path.join(PROJECTS_DIR, slug)
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            # Top-level *.jsonl are main sessions; subagents live in subfolders
            # and inherit their parent's effort, so they'd only double-count.
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(folder, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    continue
                streak = session_effort(_recent_replies(path), now,
                                        session_id=name[:-len(".jsonl")],
                                        project=_project_name(slug))
            except Exception:
                continue  # one unreadable transcript must not hide the others
            if streak is not None:
                streaks.append(streak)
    streaks.sort(key=lambda s: (s.effort in HIGH_EFFORTS, s.minutes), reverse=True)
    return streaks


def process_effort(streaks: list[EffortStreak], config: dict) -> list[str]:
    """Persist which streaks were announced and return the due alert texts. Like
    the threshold engine, muting still records them so unmuting replays nothing."""
    alerts, new_state = evaluate_effort(streaks, load_state())
    write_state(new_state)
    if not config.get("notifications", True):
        return []
    return [alert.body for alert in alerts]


# --------------------------------------------------------------------------- #
# Gather: resolve what to render (live / degraded / cold / signin)
# --------------------------------------------------------------------------- #


def gather() -> RenderModel:
    if FAKE_DATA:
        return RenderModel(mode="live", snapshot=build_fake_snapshot())

    token = load_access_token()
    if token is None:
        return RenderModel(mode="signin")

    cached = cached_snapshot()
    if is_fresh(cached, datetime.now(timezone.utc)):
        return RenderModel(mode="live", snapshot=cached)

    snapshot = fetch_usage(token)
    if snapshot is not None:
        return RenderModel(mode="live", snapshot=snapshot)

    if cached is not None:
        return RenderModel(mode="degraded", snapshot=cached, since=cached.fetched_at)
    return RenderModel(mode="cold")


def is_fresh(snapshot: UsageSnapshot | None, now: datetime) -> bool:
    """True if `snapshot` is official data fetched less than MIN_FETCH_INTERVAL
    ago (and not from the future, in case the clock moved)."""
    if snapshot is None or not snapshot.official:
        return False
    age = now - snapshot.fetched_at
    return timedelta(0) <= age < MIN_FETCH_INTERVAL


# --------------------------------------------------------------------------- #
# Pure formatting helpers
# --------------------------------------------------------------------------- #


def format_countdown(delta: timedelta) -> str:
    """Human countdown like "2d 4h", "1h 40m" or "42m". Never negative."""
    total = max(0, int(delta.total_seconds()))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def format_local_time(when_utc: datetime) -> str:
    return when_utc.astimezone().strftime("%H:%M")


def format_reset_day(when_utc: datetime) -> str:
    return when_utc.astimezone().strftime("%a %H:%M")


def meter_state(pct: float) -> str:
    if pct >= RED_AT:
        return "hot"
    if pct >= ORANGE_AT:
        return "warn"
    return "ok"


def projected_full_at(meter: Meter, now: datetime) -> datetime | None:
    """When the meter hits 100% if usage keeps its average pace so far, or None
    if that's after the reset (or too early in the window to tell)."""
    window = SESSION_WINDOW if meter.kind == "session" else WEEK_WINDOW
    remaining = meter.resets_at - now
    if remaining <= timedelta(0) or remaining >= window:
        return None
    elapsed = window - remaining
    if elapsed < window * PACE_MIN_ELAPSED or meter.pct <= 0 or meter.pct >= 100:
        return None
    full_at = now + elapsed * ((100 - meter.pct) / meter.pct)
    return full_at if full_at < meter.resets_at else None


def format_reset(meter: Meter) -> str:
    """Clock time of the reset: `18:30` for the session, `Sun 21:29` for weeks."""
    if meter.kind == "session":
        return format_local_time(meter.resets_at)
    return format_reset_day(meter.resets_at)


def tightest_meter(meters: list[Meter]) -> Meter:
    """The meter closest to its limit. Session wins ties (it resets soonest)."""
    return max(meters, key=lambda m: (m.pct, -meter_sort_key(m)[0]))


def find_meter(meters: list[Meter], kind: str) -> Meter | None:
    for meter in meters:
        if meter.kind == kind:
            return meter
    return None


def ring_meters(meters: list[Meter]) -> list[Meter]:
    """The two big rings: the all-models weekly limit, then the session.
    Either one can be missing; the other limits are drawn as bars."""
    rings = [find_meter(meters, "week"), find_meter(meters, "session")]
    return [meter for meter in rings if meter is not None]


def menu_bar_meters(meters: list[Meter], config: dict) -> list[Meter]:
    """The meters shown on the menu bar, in display order. "both" shows the two
    rings; "weekly" / "session" show one. A missing meter falls back to what's
    there, and to the tightest meter when neither ring meter exists."""
    rings = ring_meters(meters)
    choice = config.get("menubar")
    if choice in ("weekly", "session"):
        wanted = find_meter(meters, "week" if choice == "weekly" else "session")
        if wanted is not None:
            return [wanted]
    if rings:
        return rings
    return [tightest_meter(meters)]


# --------------------------------------------------------------------------- #
# Panel state (what --json prints for AiQuotaBar.swift)
# --------------------------------------------------------------------------- #


def menu_bar_label(meter: Meter) -> str:
    """`Wk`, `5h`, or a scoped model's name (`Fable`)."""
    if meter.kind in MENU_BAR_LABELS:
        return MENU_BAR_LABELS[meter.kind]
    return meter_label(meter).removesuffix(" wk")


def menu_bar_title(model: RenderModel, config: dict,
                   streaks: list[EffortStreak], now: datetime) -> dict:
    """`Wk 77% · 5h 42%` for weekly · session, as plain `text` and as `parts`
    the app colours one by one. Reset times live in the panel, not here, to keep
    the menu bar short. A trailing ⚠ when offline, and ⚡ once a high-effort
    streak passes EFFORT_ALERT_MINUTES (streaks[0] is the longest). `state` is
    the worst part."""
    snapshot = model.snapshot
    parts: list[dict] = []
    suffix = ""
    if model.mode in ("live", "degraded") and snapshot and snapshot.meters:
        shown = menu_bar_meters(snapshot.meters, config)
        worst = max(shown, key=lambda m: m.pct)
        live = model.mode == "live"
        for meter in shown:
            parts.append({"label": menu_bar_label(meter), "value": f"{_used(meter)}%",
                          "kind": meter.kind,
                          "state": meter_state(meter.pct) if live else "stale"})
        if live:
            state = meter_state(worst.pct)
        else:
            suffix, state = " ⚠", "stale"
        text = " · ".join(f"{part['label']} {part['value']}" for part in parts)
    else:
        text, state = f"{ICON_COLD} —", "stale"
    if streaks and streaks[0].effort in HIGH_EFFORTS and streaks[0].minutes >= EFFORT_ALERT_MINUTES:
        suffix += " ⚡"
    return {"text": text + suffix, "state": state, "parts": parts, "suffix": suffix}


def limit_name(meter: Meter) -> str:
    if meter.kind == "session":
        return "Session"
    if meter.kind == "week":
        return "Weekly · all models"
    if meter.kind.startswith("week_"):
        return f"Weekly · {meter_label(meter).removesuffix(' wk')}"
    return meter_label(meter)


def _used(meter: Meter) -> int:
    return int(round(min(100.0, max(0.0, meter.pct))))


def reset_text(meter: Meter, now: datetime) -> str:
    """`Resets in 4h 30m · 12:49` (session) or `Resets Sun 21:29 · in 4d 13h`."""
    delta = meter.resets_at - now
    if delta.total_seconds() <= 0:
        return "Resetting now"
    countdown = format_countdown(delta)
    if meter.kind == "session":
        return f"Resets in {countdown} · {format_local_time(meter.resets_at)}"
    return f"Resets {format_reset_day(meter.resets_at)} · in {countdown}"


def format_day_time(when_utc: datetime, now: datetime) -> str:
    """`today 23:54`, `tomorrow 09:10`, or `Thu 13:33` (local time)."""
    when_local = when_utc.astimezone()
    days_away = (when_local.date() - now.astimezone().date()).days
    if days_away == 0:
        return f"today {when_local.strftime('%H:%M')}"
    if days_away == 1:
        return f"tomorrow {when_local.strftime('%H:%M')}"
    return when_local.strftime("%a %H:%M")


def format_lead(delta: timedelta) -> str:
    """How long before the reset the limit runs out: `about 4 days`, `about 1 day`,
    or a countdown (`about 2h 10m`) when it's under a day."""
    days = delta.total_seconds() / 86400
    if days >= 1:
        count = int(round(days))
        return f"about {count} day" + ("" if count == 1 else "s")
    return f"about {format_countdown(delta)}"


def pace_subject(meter: Meter) -> str:
    if meter.kind == "session":
        return "session limit"
    if meter.kind == "week":
        return "weekly limit"
    return f"weekly {meter_label(meter).removesuffix(' wk')} limit"


def pace_state(meter: Meter, now: datetime) -> dict | None:
    """When the meter would hit 100% at its pace so far, if before the reset:
    {"subject": "weekly limit", "when": "today 23:54", "lead": "about 4 days"}."""
    full_at = projected_full_at(meter, now)
    if full_at is None:
        return None
    return {"subject": pace_subject(meter), "when": format_day_time(full_at, now),
            "lead": format_lead(meter.resets_at - full_at)}


def ring_title(meter: Meter) -> str:
    return "Session · 5h" if meter.kind == "session" else limit_name(meter)


def short_reset_text(meter: Meter, now: datetime) -> str:
    """The reset as a clock time under a ring: `Resets 18:00` (session) or
    `Resets Sun 21:29` (weekly)."""
    if (meter.resets_at - now).total_seconds() <= 0:
        return "Resetting now"
    return f"Resets {format_reset(meter)}"


def countdown_text(meter: Meter, now: datetime) -> str:
    """`in 3h 1m` under the reset time; empty once the reset has passed."""
    delta = meter.resets_at - now
    if delta.total_seconds() <= 0:
        return ""
    return f"in {format_countdown(delta)}"


def ring_state(meter: Meter, live: bool, now: datetime) -> dict:
    used = _used(meter)
    return {
        "title": ring_title(meter),
        "kind": "session" if meter.kind == "session" else "week",
        "used": used,
        "left": 100 - used,
        "state": meter_state(meter.pct) if live else "stale",
        "reset": short_reset_text(meter, now),
        "countdown": countdown_text(meter, now),
        "pace": pace_state(meter, now) if live else None,
    }


def limit_state(meter: Meter, live: bool, now: datetime) -> dict:
    used = _used(meter)
    return {
        "name": limit_name(meter),
        "used": used,
        "left": 100 - used,
        "state": meter_state(meter.pct) if live else "stale",
        "reset": reset_text(meter, now).replace("Resets", "resets", 1),
        "pace": pace_state(meter, now) if live else None,
    }


def breakdown_state(snapshot: UsageSnapshot) -> dict | None:
    """"Where your week went": {"since": "Sun 21:30", "rows": [{"name", "percent"}]}."""
    if not snapshot.breakdown:
        return None
    since = format_reset_day(snapshot.breakdown_since) if snapshot.breakdown_since else None
    return {"since": since,
            "rows": [{"name": share.name, "percent": share.pct} for share in snapshot.breakdown]}


def activity_state(streaks: list[EffortStreak]) -> list[dict]:
    """One row per project. `streaks` is sorted high effort first, longest first,
    so the first streak seen for a project is the one worth showing; the others
    only add to its session count."""
    rows: list[dict] = []
    by_project: dict[str, dict] = {}
    for streak in streaks:
        row = by_project.get(streak.project)
        if row is not None:
            row["sessions"] += 1
            continue
        row = {"project": streak.project, "effort": streak.effort, "minutes": streak.minutes,
               "due": streak.effort in HIGH_EFFORTS and streak.minutes >= EFFORT_ALERT_MINUTES,
               "sessions": 1}
        by_project[streak.project] = row
        rows.append(row)
    return rows


def settings_state(config: dict) -> dict:
    return {
        "launch_at_login": launch_at_login(),
        "notifications": bool(config.get("notifications", True)),
        "menubar": config.get("menubar", DEFAULT_CONFIG["menubar"]),
        "alert_from": config.get("min_threshold", 25),
        "alert_choices": list(MIN_THRESHOLD_CHOICES),
    }


def build_state(model: RenderModel, config: dict, streaks: list[EffortStreak],
                now: datetime, alerts: list[str] | None = None) -> dict:
    """Everything the panel shows, plus the alerts to post, as plain data. Pure
    apart from reading the login-item file; the shape is the contract in
    fixtures/state.sample.json."""
    snapshot = model.snapshot
    has_data = model.mode in ("live", "degraded") and snapshot is not None and bool(snapshot.meters)
    live = model.mode == "live"
    rings: list[dict] = []
    limits: list[dict] = []
    breakdown = None
    message = None
    if has_data:
        breakdown = breakdown_state(snapshot)
        ringed = ring_meters(snapshot.meters)
        rings = [ring_state(meter, live, now) for meter in ringed]
        limits = [limit_state(meter, live, now)
                  for meter in sorted(snapshot.meters, key=meter_sort_key)
                  if meter not in ringed]
    elif model.mode == "signin":
        message = {"title": "Sign in via Claude Code",
                   "body": "Run /login in Claude Code, allow the Keychain prompt, then refresh."}
    else:
        message = {"title": "Live usage data unavailable",
                   "body": "Couldn't reach Anthropic and there's no saved data yet. "
                           "Check your connection or firewall, then refresh."}
    return {
        "schema": SCHEMA,
        "version": VERSION,
        "mode": model.mode if (has_data or model.mode == "signin") else "cold",
        "updated": format_local_time(snapshot.fetched_at) if (live and snapshot) else None,
        "offline_since": (format_local_time(model.since) if model.since else None)
                         if model.mode == "degraded" else None,
        "message": message,
        "menubar": menu_bar_title(model, config, streaks, now),
        "rings": rings,
        "limits": limits,
        "breakdown": breakdown,
        "activity": activity_state(streaks),
        "settings": settings_state(config),
        "notification_hint": NOTIF_HINT,
        "alerts": list(alerts or []),
    }


def _redact(text: str) -> str:
    """Strip anything token-shaped so a stray error line can never leak a secret."""
    return re.sub(r"sk-ant-[A-Za-z0-9_\-]+", "<redacted>", text)


def error_state(exc: BaseException) -> dict:
    """On any unhandled error the app still gets valid JSON. The message is
    redacted and truncated: never internal state or a credential."""
    raw = str(exc).splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return {
        "schema": SCHEMA, "version": VERSION, "mode": "error",
        "updated": None, "offline_since": None,
        "message": {"title": "AiQuotaBar hit an error", "body": _redact(raw)[:200]},
        "menubar": {"text": f"{ICON_COLD} —", "state": "stale", "parts": [], "suffix": ""},
        "rings": [], "limits": [], "breakdown": None, "activity": [],
        "settings": settings_state(DEFAULT_CONFIG),
        "notification_hint": NOTIF_HINT,
        "alerts": [],
    }


def main() -> None:
    config = load_config()
    model = gather()
    # The threshold engine runs only on official live data, never on fake,
    # estimated or degraded data. The panel state is printed regardless.
    alerts: list[str] = []
    if (not FAKE_DATA and model.mode == "live"
            and model.snapshot is not None and model.snapshot.official):
        alerts += process_live(model.snapshot, config)
    # The effort watch is local and independent of the usage fetch, so it runs in
    # every mode, offline included, and never blocks the meters.
    now = datetime.now(timezone.utc)
    streaks = find_active_sessions(now)
    alerts += process_effort(streaks, config)
    print_json(build_state(model, config, streaks, now, alerts))


def print_json(state: dict) -> None:
    sys.stdout.write(json.dumps(state, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    # `--set key=value` changes a setting and exits. Anything else (`--json`, or
    # no arguments) prints the panel state.
    if "--set" in sys.argv[1:]:
        try:
            run_cli(sys.argv[1:])
        except Exception:
            pass
        sys.exit(0)
    # Top-level crash guard: always valid JSON, always exit 0.
    try:
        main()
    except Exception as exc:  # broad by design: the app must always get a state
        print_json(error_state(exc))
    sys.exit(0)
