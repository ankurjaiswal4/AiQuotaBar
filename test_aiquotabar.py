"""Tests for aiquotabar.py (the engine behind the AiQuotaBar app).

Run: python3 -m unittest   (stdlib only, no pytest)

Covers the required cases: crossing one threshold, crossing three in
one refresh (all fire, oldest first), the same threshold twice (fires once),
session reset re-arms session but not weekly, weekly reset re-arms weekly, stale
data fires nothing, and a 95%+ alert whose message includes the countdown.
"""

import importlib.util
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

# Load the engine by path (it's a script, not an installed package). Register in sys.modules before exec so dataclasses can
# resolve the module (a Python 3.9 requirement).
_HERE = os.path.dirname(os.path.abspath(__file__))
_SPEC = importlib.util.spec_from_file_location(
    "aiquotabar", os.path.join(_HERE, "aiquotabar.py"))
bar = importlib.util.module_from_spec(_SPEC)
sys.modules["aiquotabar"] = bar
_SPEC.loader.exec_module(bar)

NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
SESSION_RESET = NOW + timedelta(hours=2)
WEEK_RESET = NOW + timedelta(days=3)


def meter(kind, pct, resets):
    return bar.Meter(kind=kind, pct=float(pct), resets_at=resets)


def snapshot(meters, official=True, now=NOW):
    return bar.UsageSnapshot(meters=meters, fetched_at=now, official=official)


def window(meter_obj, fired):
    """Build a stored-window entry matching a meter's identity."""
    return {"id": bar.window_id(meter_obj), "fired": list(fired)}


def thresholds_fired(alerts):
    return [a.threshold for a in alerts if a.event == "threshold"]


def reset_alerts(alerts):
    return [a for a in alerts if a.event == "reset"]


class ThresholdEngineTests(unittest.TestCase):

    def test_crossing_one_threshold(self):
        # A window we're already tracking (was below 25) climbs past 25.
        sess = meter("session", 30, SESSION_RESET)
        state = {"windows": {"session": window(sess, [])}}
        alerts, new_state = bar.evaluate(snapshot([sess]), state)
        self.assertEqual(thresholds_fired(alerts), [25])
        self.assertEqual(reset_alerts(alerts), [])
        self.assertEqual(new_state["windows"]["session"]["fired"], [25])

    def test_three_thresholds_one_refresh_oldest_first(self):
        # Tracked window jumps (e.g. the Mac was asleep): the whole trail fires.
        sess = meter("session", 71, SESSION_RESET)
        state = {"windows": {"session": window(sess, [])}}
        alerts, new_state = bar.evaluate(snapshot([sess]), state)
        self.assertEqual(thresholds_fired(alerts), [25, 50, 60, 70])  # ascending
        self.assertEqual(new_state["windows"]["session"]["fired"], [25, 50, 60, 70])

    def test_same_threshold_twice_fires_once(self):
        sess = meter("session", 30, SESSION_RESET)
        state = {"windows": {"session": window(sess, [25])}}
        alerts, new_state = bar.evaluate(snapshot([sess]), state)
        self.assertEqual(alerts, [])  # 25 already fired this window
        self.assertEqual(new_state["windows"]["session"]["fired"], [25])

    def test_session_reset_rearms_session_not_weekly(self):
        old_sess = meter("session", 55, SESSION_RESET)
        wk = meter("week", 30, WEEK_RESET)
        state = {"windows": {
            "session": window(old_sess, [25, 50]),
            "week": window(wk, [25]),
        }}
        # New session window (different reset time); weekly window unchanged.
        new_sess = meter("session", 30, SESSION_RESET + timedelta(hours=5))
        alerts, new_state = bar.evaluate(snapshot([new_sess, wk]), state)

        self.assertEqual(len(reset_alerts(alerts)), 1)             # session reset fired
        self.assertIn("full 5-hour window", reset_alerts(alerts)[0].body)
        # session re-armed -> 25 fires again; weekly 25 already fired -> silent.
        self.assertEqual([a.threshold for a in alerts if a.kind == "session"
                          and a.event == "threshold"], [25])
        self.assertEqual([a for a in alerts if a.kind == "week"], [])
        self.assertEqual(new_state["windows"]["session"]["fired"], [25])
        self.assertEqual(new_state["windows"]["week"]["fired"], [25])

    def test_weekly_reset_rearms_weekly(self):
        old_wk = meter("week", 55, WEEK_RESET)
        state = {"windows": {"week": window(old_wk, [25, 50])}}
        new_wk = meter("week", 60, WEEK_RESET + timedelta(days=3))
        alerts, new_state = bar.evaluate(snapshot([new_wk]), state)
        self.assertEqual(thresholds_fired(alerts), [25, 50, 60])  # re-armed
        self.assertEqual(reset_alerts(alerts), [])            # no reset alert for weekly
        self.assertEqual(new_state["windows"]["week"]["fired"], [25, 50, 60])

    def test_stale_data_fires_nothing(self):
        sess = meter("session", 99, SESSION_RESET)
        alerts, new_state = bar.evaluate(snapshot([sess], official=False), {"windows": {}})
        self.assertEqual(alerts, [])
        self.assertEqual(new_state.get("windows"), {})  # unchanged, nothing recorded

    def test_ninety_five_includes_countdown_in_message(self):
        sess = meter("session", 96, NOW + timedelta(minutes=42))
        state = {"windows": {"session": window(sess, [25, 50, 60, 70, 80, 90, 91, 92, 93, 94, 95])}}
        alerts, _ = bar.evaluate(snapshot([sess]), state)
        self.assertEqual(thresholds_fired(alerts), [96])
        body = alerts[0].body
        self.assertIn("96%", body)
        self.assertIn("resets in 42m", body)

    # --- extra guards for the "avoid noise" clause -----------------------------

    def test_first_run_baselines_silently(self):
        # First-ever observation at 80%: record the crossed thresholds, fire none.
        sess = meter("session", 80, SESSION_RESET)
        alerts, new_state = bar.evaluate(snapshot([sess]), {})
        self.assertEqual(alerts, [])
        self.assertEqual(new_state["windows"]["session"]["fired"], [25, 50, 60, 70, 80])

    def test_window_change_fires_trail_and_reset(self):
        # Asleep across a session reset: new window already at 71% -> reset + trail.
        old = meter("session", 90, SESSION_RESET)
        state = {"windows": {"session": window(old, [25, 50, 70, 80, 90])}}
        new = meter("session", 71, SESSION_RESET + timedelta(hours=5))
        alerts, _ = bar.evaluate(snapshot([new]), state)
        self.assertEqual(len(reset_alerts(alerts)), 1)
        self.assertEqual(thresholds_fired(alerts), [25, 50, 60, 70])

    def test_new_session_window_with_no_prior_activity_no_reset(self):
        old_sess = meter("session", 5, SESSION_RESET)
        state = {"windows": {"session": window(old_sess, [])}}  # prior window, no activity
        new_sess = meter("session", 5, SESSION_RESET + timedelta(hours=5))
        alerts, _ = bar.evaluate(snapshot([new_sess]), state)
        self.assertEqual(alerts, [])  # no reset (prior fired empty), no thresholds (5%)


def replies(*spec):
    """(minutes before NOW, effort) pairs -> time-ordered (timestamp, effort)."""
    return sorted((NOW - timedelta(minutes=ago), effort) for ago, effort in spec)


def streak_of(minutes, session="s1", effort="high"):
    return bar.EffortStreak(session_id=session, project="marketdesk", effort=effort,
                            started_at=NOW - timedelta(minutes=minutes), last_at=NOW)


class EffortWatchTests(unittest.TestCase):

    def test_streak_spans_consecutive_high_replies(self):
        streak = bar.effort_streak(replies((20, "high"), (12, "xhigh"), (4, "high"), (0, "high")), NOW)
        self.assertEqual(streak.minutes, 20)
        self.assertEqual(streak.effort, "high")

    def test_lower_effort_reply_ends_the_run(self):
        streak = bar.effort_streak(replies((30, "high"), (18, "medium"), (8, "high"), (0, "high")), NOW)
        self.assertEqual(streak.minutes, 8)

    def test_idle_gap_restarts_the_run(self):
        # 25 min of silence is a break, not 25 min of high-effort work.
        streak = bar.effort_streak(replies((40, "high"), (15, "high"), (8, "high"), (0, "high")), NOW)
        self.assertEqual(streak.minutes, 15)

    def test_no_streak_when_latest_reply_is_not_high(self):
        self.assertIsNone(bar.effort_streak(replies((10, "high"), (0, "medium")), NOW))

    def test_no_streak_when_session_is_idle(self):
        self.assertIsNone(bar.effort_streak(replies((30, "high"), (20, "high")), NOW))

    def test_alert_fires_once_per_streak(self):
        alerts, state = bar.evaluate_effort([streak_of(16)], {})
        self.assertEqual(len(alerts), 1)
        self.assertIn("16 min", alerts[0].body)
        alerts, _ = bar.evaluate_effort([streak_of(16)], state)
        self.assertEqual(alerts, [])  # same streak start -> already announced

    def test_no_alert_below_the_line(self):
        alerts, state = bar.evaluate_effort([streak_of(14)], {})
        self.assertEqual(alerts, [])
        self.assertEqual(state["effort_alerted"], {})

    def test_badge_and_rows_render_only_when_streaks_exist(self):
        model = bar.RenderModel(mode="cold")
        config = dict(bar.DEFAULT_CONFIG)
        self.assertNotIn("⚡", bar.build_state(model, config, [], NOW)["menubar"]["text"])
        state = bar.build_state(model, config, [streak_of(22)], NOW)
        self.assertIn("⚡", state["menubar"]["text"])
        self.assertEqual(state["activity"], [{"project": "marketdesk", "effort": "high",
                                              "minutes": 22, "due": True, "sessions": 1}])
        quiet = bar.build_state(model, config, [streak_of(5)], NOW)
        self.assertNotIn("⚡", quiet["menubar"]["text"])  # the row shows; the badge waits
        self.assertFalse(quiet["activity"][0]["due"])

    def test_active_medium_session_is_still_reported(self):
        reading = bar.session_effort(replies((3, "high"), (0, "medium")), NOW)
        self.assertEqual(reading.effort, "medium")
        self.assertEqual(reading.minutes, 0)
        self.assertIsNone(bar.session_effort(replies((20, "medium")), NOW))  # idle

    def test_activity_lists_active_sessions_only(self):
        model = bar.RenderModel(mode="cold")
        medium = bar.session_effort(replies((0, "medium")), NOW, project="marketdesk")
        state = bar.build_state(model, dict(bar.DEFAULT_CONFIG), [medium], NOW)
        self.assertEqual(state["activity"][0]["effort"], "medium")
        self.assertEqual(bar.build_state(model, dict(bar.DEFAULT_CONFIG), [], NOW)["activity"], [])

    def test_sessions_in_one_project_share_one_row(self):
        model = bar.RenderModel(mode="cold")
        high = streak_of(22)
        medium = bar.session_effort(replies((0, "medium")), NOW, project="marketdesk")
        other = bar.session_effort(replies((0, "medium")), NOW, project="notes")
        rows = bar.build_state(model, dict(bar.DEFAULT_CONFIG), [high, medium, other], NOW)["activity"]
        self.assertEqual([(r["project"], r["effort"], r["sessions"]) for r in rows],
                         [("marketdesk", "high", 2), ("notes", "medium", 1)])

    def test_project_name_from_transcript_folder(self):
        self.assertEqual(bar._project_name("-Users-me-Coding-projects-deck-app"), "deck-app")
        self.assertEqual(bar._project_name("-Users-me-notes"), "notes")


class PanelStateTests(unittest.TestCase):
    """build_state() is the contract with AiQuotaBar.swift (fixtures/state.sample.json)."""

    def setUp(self):
        self.config = dict(bar.DEFAULT_CONFIG)
        self.live = bar.UsageSnapshot(meters=[
            meter("session", 17, NOW + timedelta(hours=4, minutes=30)),
            meter("week", 77, NOW + timedelta(days=4, hours=13)),
            bar.Meter(kind="week_fable", pct=12.0, resets_at=NOW + timedelta(days=4, hours=13),
                      label="Fable wk"),
        ], fetched_at=NOW)

    def test_shape_matches_the_contract_fixture(self):
        with open(os.path.join(_HERE, "fixtures", "state.sample.json"), encoding="utf-8") as fh:
            sample = json.load(fh)
        state = bar.build_state(bar.RenderModel(mode="live", snapshot=self.live), self.config, [], NOW)
        self.assertEqual(set(state), set(sample))
        self.assertEqual(set(state["rings"][0]), set(sample["rings"][0]))
        self.assertEqual(set(state["menubar"]), set(sample["menubar"]))
        self.assertEqual(set(state["menubar"]["parts"][0]), set(sample["menubar"]["parts"][0]))
        self.assertEqual(set(state["limits"][0]), set(sample["limits"][0]))
        self.assertEqual(set(state["settings"]), set(sample["settings"]))
        self.assertEqual(state["schema"], sample["schema"])
        json.dumps(state)                                      # serialisable

    def test_countdown_uses_days_past_24_hours(self):
        self.assertEqual(bar.format_countdown(timedelta(days=2, hours=4, minutes=9)), "2d 4h")
        self.assertEqual(bar.format_countdown(timedelta(hours=1, minutes=40)), "1h 40m")
        self.assertEqual(bar.format_countdown(timedelta(minutes=-3)), "0m")

    def test_menu_bar_never_shows_a_countdown(self):
        calm = bar.UsageSnapshot(meters=[meter("session", 68, NOW + timedelta(hours=2))])
        tight = bar.UsageSnapshot(meters=[meter("session", 92, NOW + timedelta(minutes=38))])
        self.assertEqual(bar.build_state(bar.RenderModel(mode="live", snapshot=calm),
                                         self.config, [], NOW)["menubar"],
                         {"text": "5h 68%", "state": "ok", "suffix": "",
                          "parts": [{"label": "5h", "value": "68%", "kind": "session", "state": "ok"}]})
        red = bar.build_state(bar.RenderModel(mode="live", snapshot=tight), self.config, [], NOW)
        self.assertEqual((red["menubar"]["text"], red["menubar"]["state"]), ("5h 92%", "hot"))
        self.assertEqual(red["menubar"]["suffix"], "")

    def menubar_text(self, choice, snap=None):
        config = dict(self.config, menubar=choice)
        model = bar.RenderModel(mode="live", snapshot=snap or self.live)
        return bar.build_state(model, config, [], NOW)["menubar"]

    def test_menu_bar_choices_weekly_session_both(self):
        both = self.menubar_text("both")
        self.assertEqual((both["text"], both["state"]), ("Wk 77% · 5h 17%", "warn"))
        self.assertEqual([(p["label"], p["state"]) for p in both["parts"]], [("Wk", "warn"), ("5h", "ok")])
        self.assertEqual(self.menubar_text("weekly")["text"], "Wk 77%")
        self.assertEqual(self.menubar_text("session")["text"], "5h 17%")
        # A missing meter falls back to what's there rather than showing nothing.
        # A missing meter falls back to what's there rather than showing nothing.
        only_week = bar.UsageSnapshot(meters=[meter("week", 30, NOW + timedelta(days=2))])
        self.assertEqual(self.menubar_text("session", only_week)["text"], "Wk 30%")

    def test_both_colours_each_meter_by_its_own_state(self):
        snap = bar.UsageSnapshot(meters=[meter("session", 93, NOW + timedelta(minutes=38)),
                                         meter("week", 60, NOW + timedelta(days=2))])
        bar_state = self.menubar_text("both", snap)
        self.assertEqual((bar_state["text"], bar_state["state"]), ("Wk 60% · 5h 93%", "hot"))
        self.assertEqual([p["state"] for p in bar_state["parts"]], ["ok", "hot"])

    def test_old_tightest_setting_falls_back_to_both(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            saved = bar.CONFIG_FILE
            bar.CONFIG_FILE = os.path.join(tmp, "config.json")
            try:
                with open(bar.CONFIG_FILE, "w", encoding="utf-8") as fh:
                    json.dump({"menubar": "tightest"}, fh)
                self.assertEqual(bar.load_config()["menubar"], "both")
            finally:
                bar.CONFIG_FILE = saved

    def test_rings_are_weekly_then_session_and_the_rest_are_bars(self):
        state = bar.build_state(bar.RenderModel(mode="live", snapshot=self.live), self.config, [], NOW)
        self.assertEqual([ring["title"] for ring in state["rings"]], ["Weekly · all models", "Session · 5h"])
        self.assertEqual((state["rings"][0]["used"], state["rings"][0]["left"]), (77, 23))
        self.assertEqual(state["rings"][1]["reset"],
                         "Resets " + bar.format_local_time(NOW + timedelta(hours=4, minutes=30)))
        self.assertEqual(state["rings"][1]["countdown"], "in 4h 30m")
        self.assertEqual([row["name"] for row in state["limits"]], ["Weekly · Fable"])
        self.assertEqual(state["updated"], bar.format_local_time(NOW))

    def test_limit_reached_ring_has_nothing_left(self):
        full = bar.UsageSnapshot(meters=[meter("week", 100, NOW + timedelta(days=1))], fetched_at=NOW)
        state = bar.build_state(bar.RenderModel(mode="live", snapshot=full), self.config, [], NOW)
        self.assertEqual((state["rings"][0]["used"], state["rings"][0]["left"]), (100, 0))
        self.assertEqual(state["rings"][0]["state"], "hot")

    def test_stale_data_is_grey_and_never_projects(self):
        state = bar.build_state(bar.RenderModel(mode="degraded", snapshot=self.live, since=NOW),
                                self.config, [], NOW)
        self.assertEqual(state["mode"], "degraded")
        self.assertEqual(state["offline_since"], bar.format_local_time(NOW))
        self.assertTrue(state["menubar"]["text"].endswith("⚠"))
        self.assertEqual({p["state"] for p in state["menubar"]["parts"]}, {"stale"})
        self.assertEqual({ring["state"] for ring in state["rings"]}, {"stale"})
        self.assertEqual([ring["pace"] for ring in state["rings"]], [None, None])

    def test_signin_and_cold_messages(self):
        signin = bar.build_state(bar.RenderModel(mode="signin"), self.config, [], NOW)
        self.assertEqual(signin["message"]["title"], "Sign in via Claude Code")
        self.assertEqual(signin["rings"], [])
        cold = bar.build_state(bar.RenderModel(mode="cold"), self.config, [], NOW)
        self.assertEqual(cold["mode"], "cold")
        self.assertEqual(cold["message"]["title"], "Live usage data unavailable")

    def test_errors_become_valid_json_without_secrets(self):
        state = bar.error_state(RuntimeError("boom sk-ant-oat01-SECRETSECRET"))
        self.assertEqual(state["mode"], "error")
        self.assertNotIn("SECRET", json.dumps(state))
        self.assertIn("<redacted>", state["message"]["body"])


class AlertHandoffTests(unittest.TestCase):
    """The engine no longer posts notifications; it hands the texts to the app."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.original = bar.STATE_DIR, bar.STATE_FILE
        bar.STATE_DIR = self.tmp.name
        bar.STATE_FILE = os.path.join(self.tmp.name, "state.json")
        # Seed a window so the next crossing is a real one, not the silent baseline.
        session = meter("session", 10, NOW + timedelta(hours=3))
        bar.write_state({"windows": {"session": window(session, [])}})
        self.snap = snapshot([meter("session", 55, NOW + timedelta(hours=3))])

    def tearDown(self):
        bar.STATE_DIR, bar.STATE_FILE = self.original
        self.tmp.cleanup()

    def test_due_alerts_are_returned_and_recorded_once(self):
        config = dict(bar.DEFAULT_CONFIG)
        bodies = bar.process_live(self.snap, config)
        self.assertEqual(len(bodies), 2)                  # 25 and 50
        self.assertTrue(bodies[0].startswith("Session at 55%"))
        self.assertEqual(bar.process_live(self.snap, config), [])

    def test_muted_and_below_floor_alerts_are_not_returned(self):
        self.assertEqual(bar.process_live(self.snap, dict(bar.DEFAULT_CONFIG, notifications=False)), [])
        bar.write_state({"windows": {"session": window(meter("session", 10, NOW + timedelta(hours=3)), [])}})
        self.assertEqual(len(bar.process_live(self.snap, dict(bar.DEFAULT_CONFIG, min_threshold=50))), 1)

    def test_alerts_travel_in_the_panel_state(self):
        model = bar.RenderModel(mode="live", snapshot=self.snap)
        state = bar.build_state(model, dict(bar.DEFAULT_CONFIG), [], NOW, ["hello"])
        self.assertEqual(state["alerts"], ["hello"])
        self.assertEqual(bar.error_state(RuntimeError("x"))["alerts"], [])


class LaunchAtLoginTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.original = bar.LOGIN_AGENT_FILE
        bar.LOGIN_AGENT_FILE = os.path.join(self.tmp.name, "LaunchAgents", "x.plist")

    def tearDown(self):
        bar.LOGIN_AGENT_FILE = self.original
        self.tmp.cleanup()

    def test_toggle_writes_and_removes_the_agent(self):
        import plistlib
        bar.apply_setting("login=on")
        with open(bar.LOGIN_AGENT_FILE, "rb") as fh:
            agent = plistlib.load(fh)
        self.assertEqual(agent["ProgramArguments"][:2], ["/usr/bin/open", "-b"])
        self.assertTrue(agent["RunAtLoad"])
        self.assertTrue(bar.settings_state(dict(bar.DEFAULT_CONFIG))["launch_at_login"])
        self.assertEqual(agent["ProgramArguments"][2], bar.APP_BUNDLE_ID)    # opens the app
        bar.apply_setting("login=off")
        self.assertFalse(os.path.exists(bar.LOGIN_AGENT_FILE))
        bar.apply_setting("login=maybe")  # unknown value is ignored
        self.assertFalse(os.path.exists(bar.LOGIN_AGENT_FILE))


class PaceAndHeroTests(unittest.TestCase):
    def test_projection_warns_only_when_running_out_before_reset(self):
        # 3h into a 5h session (2h left) at 75%: 1h more at this pace -> full first.
        fast = meter("session", 75, NOW + timedelta(hours=2))
        self.assertEqual(bar.projected_full_at(fast, NOW), NOW + timedelta(hours=1))
        # Same time, 30%: would need 7h more, after the reset -> no warning.
        self.assertIsNone(bar.projected_full_at(meter("session", 30, NOW + timedelta(hours=2)), NOW))
        # 10 minutes into the window is too early to judge.
        early = meter("session", 20, NOW + timedelta(hours=4, minutes=50))
        self.assertIsNone(bar.projected_full_at(early, NOW))
        self.assertIsNone(bar.projected_full_at(meter("session", 100, NOW + timedelta(hours=1)), NOW))


    def test_pace_says_when_and_how_early(self):
        # 2 days into the week at 50%: full in 2 more days, 3 days before the reset.
        fast = meter("week", 50, NOW + timedelta(days=5))
        pace = bar.pace_state(fast, NOW)
        self.assertEqual(pace["subject"], "weekly limit")
        self.assertEqual(pace["when"], bar.format_day_time(NOW + timedelta(days=2), NOW))
        self.assertEqual(pace["lead"], "about 3 days")
        self.assertEqual(bar.format_lead(timedelta(hours=2, minutes=10)), "about 2h 10m")
        self.assertEqual(bar.format_lead(timedelta(hours=26)), "about 1 day")
        self.assertIsNone(bar.pace_state(meter("week", 10, NOW + timedelta(days=5)), NOW))


class BreakdownTests(unittest.TestCase):
    def body(self, block):
        return json.dumps({"limits": [{"kind": "session", "percent": 10,
                                       "resets_at": "2026-07-16T14:00:00+00:00"}],
                           "seven_day_breakdown": block})

    def test_rows_sorted_and_zero_rows_dropped(self):
        snap = bar._parse_usage(self.body({
            "window_started_at": "2026-07-12T16:00:00+00:00",
            "rows": [{"display_name": "Chats", "percent": 3}, {"display_name": "Claude Code", "percent": 96},
                     {"display_name": "Other", "percent": 0}]}))
        self.assertEqual([(s.name, s.pct) for s in snap.breakdown], [("Claude Code", 96), ("Chats", 3)])
        self.assertEqual(snap.breakdown_since, datetime(2026, 7, 12, 16, tzinfo=timezone.utc))
        state = bar.breakdown_state(snap)
        self.assertEqual(state["rows"][0], {"name": "Claude Code", "percent": 96})

    def test_malformed_breakdown_is_ignored_not_fatal(self):
        for block in (None, "x", {"rows": "x"}, {"rows": [None, {"display_name": 5, "percent": 9},
                                                           {"display_name": "A", "percent": 140},
                                                           {"display_name": "B", "percent": True}]}):
            snap = bar._parse_usage(self.body(block))
            self.assertIsNotNone(snap)                 # the meters still parse
            self.assertEqual(snap.breakdown, [])
            self.assertIsNone(bar.breakdown_state(snap))

    def test_breakdown_survives_the_cache(self):
        snap = bar._parse_usage(self.body({"rows": [{"display_name": "Claude Code", "percent": 99}]}))
        again = bar._snapshot_from_dict(json.loads(json.dumps(bar._snapshot_to_dict(snap))))
        self.assertEqual(again.breakdown, snap.breakdown)
        old = bar._snapshot_to_dict(snap)
        del old["breakdown"], old["breakdown_since"]       # a cache written by an older version
        self.assertEqual(bar._snapshot_from_dict(old).breakdown, [])


class FetchThrottleTests(unittest.TestCase):
    def test_recent_official_data_is_reused(self):
        snap = bar.UsageSnapshot(meters=[meter("session", 10, NOW + timedelta(hours=1))],
                                 fetched_at=NOW - timedelta(seconds=20))
        self.assertTrue(bar.is_fresh(snap, NOW))
        self.assertFalse(bar.is_fresh(snap, NOW + timedelta(seconds=40)))   # 60 s old
        self.assertFalse(bar.is_fresh(None, NOW))
        future = bar.UsageSnapshot(meters=[], fetched_at=NOW + timedelta(minutes=5))
        self.assertFalse(bar.is_fresh(future, NOW))                          # clock moved

