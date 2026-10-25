"""Reminder regressions through the real collector/cache/live-state/tray pipeline."""

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from flashmonitor.collector import Collector
from flashmonitor.store import TrafficStore
from flashmonitor.tray import TrayPulse
from flashmonitor.views import LiveState, RankCache, snapshot_alerts

MB = 1024 * 1024


class NotificationPipelineTest(unittest.TestCase):
    def setUp(self):
        self.day = date(2026, 10, 23)
        self.clock = 0.0
        self.pack = {"upload": 0, "download": 0, "rows": []}
        self.news = []
        self.prefs = {"show_new_banner": True, "notify_new_app": True,
                      "notify_quota": True, "quota_total_mb": 1, "quota_program_mb": 1}
        self.ignored = set()
        self.store = SimpleNamespace(recent_new=lambda: list(self.news))
        self.ranks = RankCache(lambda period: self.pack, clock=lambda: self.clock, local_day=lambda: self.day)
        self.live = LiveState()
        self.collector = Collector(
            store=self.store, clash=object(), ranks=self.ranks, live=self.live,
            settings=lambda: self.prefs, ignored=lambda: self.ignored,
            proxy_ports=set(), listen_ports=object(), routes=object(),
            cmdlines=object(), identities=object(), services=object(),
        )
        self.balloons = []
        self.tray = SimpleNamespace(set_tip=lambda text: None,
                                    balloon=lambda title, text: self.balloons.append((title, text)))
        self.pulse = TrayPulse(self.tray, live_view=lambda: {}, alerts=self.live.alerts,
                               settings=lambda: self.prefs, local_day=lambda: self.day)

    def publish(self, *, leaks=None):
        self.live.set(self.collector.with_alerts({"status": {"tun_up": True},
                                                "leaks": leaks or {"rows": []}}))

    def page_alerts(self):
        return snapshot_alerts(self.live.get(), self.store, self.prefs, local_day=lambda: self.day)

    def pump(self, count=20):
        for _ in range(count):
            self.pulse.once()

    def test_midnight_stale_live_state_and_today_cache_preserve_new_day_quota(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        wall_clock = [datetime(2026, 10, 23, 23, 59, 58).timestamp()]
        self.store = TrafficStore(Path(tmp.name), clock=lambda: wall_clock[0])
        self.addCleanup(self.store.close)
        self.collector.store = self.store
        self.ranks = RankCache(self.store.ranking, clock=lambda: self.clock, local_day=lambda: self.day)
        self.collector.ranks = self.ranks
        self.prefs.update(show_new_banner=False, notify_new_app=False)
        self.store.update([])
        flow = {"id": "one", "process": "app", "upload": 2 * MB, "download": 0,
                "outbound": "proxy", "remote": "example:443"}
        self.store.update([flow])
        self.publish()
        self.pump()
        self.assertEqual(len(self.balloons), 2)
        self.day = date(2026, 10, 24)
        wall_clock[0] = datetime(2026, 10, 24, 0, 0, 1).timestamp()
        self.pump()  # The collector has not published since midnight.
        self.assertEqual(len(self.balloons), 2)
        self.assertEqual(self.page_alerts(), [])
        self.publish()  # Monotonic TTL has not elapsed, but calendar day changed.
        self.assertEqual(self.live.alerts(), [])
        self.assertEqual(self.ranks.computes, 2)
        self.store.update([{**flow, "upload": 4 * MB}])
        self.clock = 4.0
        self.publish()
        self.assertTrue(all(a["stats_day"] == "2026-10-24" for a in self.live.alerts()))
        self.pump()
        self.assertEqual(len(self.balloons), 4)

    def test_page_read_crossing_midnight_drops_yesterdays_quota(self):
        self.pack = {"upload": 2 * MB, "rows": []}
        self.publish()

        def read_news():
            self.day = date(2026, 10, 24)
            return []

        self.store.recent_new = read_news
        self.assertEqual(self.page_alerts(), [])
        self.assertEqual(self.live.alerts()[0]["stats_day"], "2026-10-23")

    def test_banner_and_tray_switches_are_independent(self):
        for banner, tray in ((False, True), (True, False), (False, False), (True, True)):
            with self.subTest(banner=banner, tray=tray):
                self.prefs.update(show_new_banner=banner, notify_new_app=tray)
                self.news = [{"process": f"app-{banner}-{tray}", "ts": 1}]
                self.balloons.clear()
                self.publish()
                self.assertEqual(any(a["kind"] == "new" for a in self.live.alerts()), banner or tray)
                self.assertEqual(any(a["kind"] == "new" for a in self.page_alerts()), banner)
                self.pump()
                self.assertEqual(bool(self.balloons), tray)

    def test_ignored_high_usage_apps_do_not_consume_quota_candidate_slots(self):
        self.ignored = {f"ignored{i}" for i in range(8)}
        rows = [{"process": f"Ignored{i}", "upload": 10 * MB} for i in range(8)]
        rows.append({"process": "visible", "upload": 2 * MB})
        self.pack = {"upload": 82 * MB, "rows": rows}
        self.publish()
        self.assertEqual([(a["kind"], a["process"]) for a in self.live.alerts()],
                         [("total", ""), ("program", "visible")])
        self.pump()
        self.assertEqual(len(self.balloons), 2)
        self.assertIn("visible", self.balloons[-1][1])

    def test_new_apps_and_leaks_cannot_truncate_later_quota_events(self):
        self.news = [{"process": f"new{i}", "ts": i} for i in range(6)]
        self.pack = {"upload": 16 * MB, "rows": [
            {"process": f"quota{i}", "upload": 2 * MB} for i in range(8)]}
        self.publish(leaks={"rows": [{"process": "leaking", "n": 1}]})
        self.assertEqual(len(self.live.alerts()), 15)
        self.assertEqual(len(self.page_alerts()), 15)
        self.assertEqual(sum(a["kind"] in {"total", "program"} for a in self.live.alerts()), 8)
        self.pump()
        self.assertEqual(len(self.balloons), 15)
        self.assertEqual(sum(title == "日流量" for title, _ in self.balloons), 8)
        self.assertIn("quota6", self.balloons[-1][1])


if __name__ == "__main__":
    unittest.main()
