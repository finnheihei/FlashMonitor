"""tray.py：托盘提示文字和气泡提醒的规则（用假托盘，不建真窗口）。"""

from __future__ import annotations

import unittest

from flashmonitor.tray import TrayPulse, process_label, tray_tip_text


class FakeTray:
    def __init__(self) -> None:
        self.tips: list[str] = []
        self.balloons: list[tuple[str, str]] = []

    def set_tip(self, text: str) -> None:
        self.tips.append(text)

    def balloon(self, title: str, text: str) -> None:
        self.balloons.append((title, text))


class TrayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tray = FakeTray()
        self.live = {"down": 2 * 1024 * 1024, "up": 512, "eaters": []}
        self.alerts: list[dict] = []
        self.settings = {"notify_new_app": True, "notify_leak": True, "notify_quota": True}
        self.pulse = TrayPulse(self.tray, live_view=lambda: self.live, alerts=lambda: self.alerts, settings=lambda: self.settings)

    def test_tip_text(self) -> None:
        self.assertEqual(tray_tip_text(self.live), "↓ 2.0 MB/s  ↑ 512 B/s")
        live = {**self.live, "eaters": [{"process": "Weixin", "proxy_down": 1, "direct_down": 9}]}
        self.assertEqual(tray_tip_text(live), "↓ 2.0 MB/s  ↑ 512 B/s · 微信 走直连")
        live["eaters"][0]["title"] = "WeChat"
        self.assertTrue(tray_tip_text(live).endswith("WeChat 走直连"))
        self.assertEqual(process_label("unknown-app"), "unknown-app")

    def test_tip_only_updates_on_change(self) -> None:
        self.pulse.once()
        self.pulse.once()
        self.assertEqual(len(self.tray.tips), 1)
        self.live["down"] = 0
        self.pulse.once()
        self.assertEqual(self.tray.tips[-1], "↓ 0  ↑ 512 B/s")

    def test_one_balloon_per_pulse_and_each_event_once(self) -> None:
        self.alerts = [
            {"kind": "new", "process": "steam"},
            {"kind": "leak", "count": 3},
            {"kind": "total", "used": 1, "limit": 1},
            {"kind": "program", "process": "chrome", "used": 1, "limit": 1},
        ]
        for _ in range(6):
            self.pulse.once()
        self.assertEqual(
            [b[0] for b in self.tray.balloons],
            ["新程序走网", "漏网", "日流量", "日流量"],
        )
        self.assertIn("Steam 开始走网了", self.tray.balloons[0][1])
        self.assertIn("3 条连接没进 TUN", self.tray.balloons[1][1])
        self.assertIn("Chrome", self.tray.balloons[3][1])

    def test_leak_balloon_rearms_after_leak_clears(self) -> None:
        self.alerts = [{"kind": "leak", "count": 1}]
        self.pulse.once()
        self.pulse.once()
        self.alerts = []
        self.pulse.once()
        self.alerts = [{"kind": "leak", "count": 2}]
        self.pulse.once()
        self.assertEqual([b[0] for b in self.tray.balloons], ["漏网", "漏网"])

    def test_settings_switch_balloons_off(self) -> None:
        self.settings = {"notify_new_app": False, "notify_leak": False, "notify_quota": False}
        self.alerts = [{"kind": "new", "process": "x"}, {"kind": "leak", "count": 1}, {"kind": "total"}]
        self.pulse.once()
        self.assertEqual(self.tray.balloons, [])
        self.settings = {"notify_new_app": True, "notify_leak": False, "notify_quota": False}
        self.pulse.once()
        self.assertEqual(self.tray.balloons, [], "新程序只提醒一次，关着的时候就算提醒过了")

    def test_tray_errors_are_contained(self) -> None:
        class Broken(FakeTray):
            def set_tip(self, text: str) -> None:
                raise OSError("shell 没响应")

            def balloon(self, title: str, text: str) -> None:
                raise OSError("x")

        pulse = TrayPulse(Broken(), live_view=lambda: self.live, alerts=lambda: [{"kind": "leak"}], settings=lambda: self.settings)
        pulse.once()
        self.assertTrue(pulse.leak_shown)


if __name__ == "__main__":
    unittest.main()
