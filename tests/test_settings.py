"""settings.py。

SettingsBehaviourTest 检查 apply() 后的 data 和读回来的文件。
SettingsFixesTest 覆盖无效地址整批拒绝、密钥不泄露、保存失败和保存后的回调。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

from flashmonitor.settings import DEFAULT_IGNORE, AppSettings, contains_secret, url_hint

SECRET = "s3cr3t-Very-Long-Token-9f2c"
SUB = "https://sub.example.com/api/v1/client/subscribe?token=abcdef1234567890"


class Hooks:
    def __init__(self) -> None:
        self.enabled = False
        self.calls: list[bool] = []
        self.saved: list[set[str]] = []
        self.fail: str = ""

    def autostart_enabled(self) -> bool:
        return self.enabled

    def set_autostart(self, on: bool) -> None:
        self.calls.append(on)
        if self.fail:
            raise RuntimeError(self.fail)
        self.enabled = on


def new_factory(path: Path, hooks: Hooks) -> Any:
    return AppSettings(
        path,
        autostart_enabled=hooks.autostart_enabled,
        set_autostart=hooks.set_autostart,
        on_saved=hooks.saved.append,
    )


class SettingsTestBase(unittest.TestCase):
    factory = staticmethod(new_factory)

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "settings.json"
        self.hooks = Hooks()

    def make(self) -> Any:
        return self.factory(self.path, self.hooks)

    def on_disk(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))


class SettingsBehaviourTest(SettingsTestBase):
    def test_defaults_without_file(self) -> None:
        s = self.make()
        self.assertEqual(s.data["clash_api"], "http://127.0.0.1:9090")
        self.assertEqual(s.data["retention_days"], 40)
        self.assertEqual(s.data["ignored"], DEFAULT_IGNORE)
        self.assertEqual(s.data["pinned"], [])
        self.assertIs(s.data["autostart"], False)

    def test_existing_file_without_ignored_gets_default_ignore(self) -> None:
        self.path.write_text(json.dumps({"retention_days": 90, "unknown": 1}), encoding="utf-8")
        s = self.make()
        self.assertEqual((s.data["retention_days"], s.data["ignored"]), (90, DEFAULT_IGNORE))
        self.assertNotIn("unknown", s.data)

    def test_explicit_empty_ignore_list_is_kept(self) -> None:
        self.path.write_text(json.dumps({"ignored": [], "pinned": ["a", "A", " ", "b"]}), encoding="utf-8")
        s = self.make()
        self.assertEqual((s.data["ignored"], s.data["pinned"]), ([], ["a", "b"]))

    def test_corrupt_file_means_defaults(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.make().data["ignored"], DEFAULT_IGNORE)

    def test_numbers_are_clamped(self) -> None:
        s = self.make()
        s.apply({"retention_days": 3, "quota_total_mb": -5, "quota_program_mb": "12"})
        self.assertEqual((s.data["retention_days"], s.data["quota_total_mb"], s.data["quota_program_mb"]), (7, 0, 12))
        s.apply({"retention_days": 9999, "quota_total_mb": 10**12})
        self.assertEqual((s.data["retention_days"], s.data["quota_total_mb"]), (365, 10_000_000))
        s.apply({"retention_days": "abc", "quota_program_mb": None})
        self.assertEqual((s.data["retention_days"], s.data["quota_program_mb"]), (40, 0))

    def test_urls(self) -> None:
        s = self.make()
        s.apply({"clash_api": " http://127.0.0.1:9097/ "})
        self.assertEqual(s.data["clash_api"], "http://127.0.0.1:9097")
        self.assertIs(s.apply({"clash_api": "ftp://x"}), False)
        self.assertEqual(s.data["clash_api"], "http://127.0.0.1:9097", "不认识的协议不改")
        s.apply({"sub_url": SUB})
        self.assertEqual(s.data["sub_url"], SUB)
        self.assertIs(s.apply({"sub_url": "sub.example.com/no-scheme"}), False)
        self.assertEqual(s.data["sub_url"], SUB, "无效订阅地址保留原设置")
        self.assertIs(s.apply({"sub_url": "https://x/" + "a" * 2000}), False)
        self.assertEqual(s.data["sub_url"], SUB)
        s.apply({"clash_secret": f"  {SECRET}  "})
        self.assertEqual(s.data["clash_secret"], SECRET)
        s.apply({"clash_secret": ""})
        self.assertEqual(s.data["clash_secret"], "")

    def test_choices_and_bools(self) -> None:
        s = self.make()
        s.apply({"last_filter": "rank", "last_period": "7d", "notify_leak": 0, "show_airport": "", "close_to_tray": 1})
        self.assertEqual(
            (s.data["last_filter"], s.data["last_period"], s.data["notify_leak"], s.data["show_airport"], s.data["close_to_tray"]),
            ("rank", "7d", False, False, True),
        )
        s.apply({"last_filter": "bogus", "last_period": "5y"})
        self.assertEqual((s.data["last_filter"], s.data["last_period"]), ("rank", "7d"))

    def test_pin_and_ignore_are_exclusive(self) -> None:
        s = self.make()
        s.apply({"ignored": ["steam"], "pinned": []})
        s.apply({"process": "chrome", "pinned": True})
        s.apply({"process": "Steam", "pinned": True})
        self.assertEqual((s.data["pinned"], s.data["ignored"]), (["chrome", "Steam"], []))
        s.apply({"process": "chrome", "ignored": True})
        self.assertEqual((s.data["pinned"], s.data["ignored"]), (["Steam"], ["chrome"]))
        s.apply({"process": "CHROME", "ignored": False})
        self.assertEqual(s.data["ignored"], [])
        s.apply({"process": "Steam", "pinned": False})
        self.assertEqual(s.data["pinned"], [])

    def test_lists_ignored_when_process_given(self) -> None:
        s = self.make()
        s.apply({"ignored": ["a"]})
        s.apply({"process": "", "ignored": ["x", "y"]})
        self.assertEqual(s.data["ignored"], ["a"])
        s.apply({"ignored": ["b", "B", "c"] + [f"n{i}" for i in range(100)]})
        self.assertEqual(s.data["ignored"][:3], ["b", "c", "n0"])
        self.assertEqual(len(s.data["ignored"]), 80)

    def test_saved_file_roundtrip(self) -> None:
        s = self.make()
        s.apply({"clash_secret": SECRET, "sub_url": SUB, "quota_total_mb": 500, "process": "chrome", "pinned": True})
        disk = self.on_disk()
        self.assertEqual((disk["clash_secret"], disk["sub_url"], disk["quota_total_mb"]), (SECRET, SUB, 500))
        self.assertEqual(disk["pinned"], ["chrome"])
        again = self.make()
        self.assertEqual((again.data["clash_secret"], again.data["sub_url"], again.data["pinned"]), (SECRET, SUB, ["chrome"]))

    def test_autostart(self) -> None:
        s = self.make()
        s.apply({"autostart": True})
        self.assertEqual((self.hooks.calls, s.data["autostart"]), ([True], True))
        s.apply({"start_minimized": True})
        self.assertEqual(self.hooks.calls, [True, True], "开着自启时改启动方式会重写启动项")
        s.apply({"autostart": False})
        s.apply({"open_browser": True})
        self.assertEqual(self.hooks.calls, [True, True, False])
        self.hooks.fail = "无法写入开机启动项：拒绝访问"
        s.apply({"autostart": True})
        self.assertEqual((s.error, s.data["autostart"]), ("无法写入开机启动项：拒绝访问", False))
        self.hooks.fail = ""
        s.apply({"notify_hide": True})
        self.assertEqual(s.error, "", "下次保存清掉旧错误")


class SettingsFixesTest(SettingsTestBase):
    def test_invalid_urls_reject_entire_patch_without_writes_or_hooks(self) -> None:
        s = self.make()
        s.apply({"clash_api": "http://127.0.0.1:9097", "sub_url": SUB})
        before = deepcopy(s.data)
        disk_before = self.path.read_bytes()
        self.hooks.saved.clear()
        invalid = (
            "127.0.0.1:9090", "http://", "http:///missing", "ftp://example.com",
            "http://example.com:abc", "http://example.com:65536", "http://example.com:0",
            "http://example.com:", "http://[broken", "http://999.0.0.1",
            "http://bad host", "http://bad\nhost", "http://-bad.example", "http://bad..example",
            "http://example.com\\path", "http://user:password@example.com", "http://example.com/#fragment",
            "http://example.com/#",
            None, False,
        )
        for key in ("clash_api", "sub_url"):
            for value in invalid:
                with self.subTest(key=key, value=value):
                    patch = {"autostart": True, "close_to_tray": False, "clash_secret": "new-secret",
                             "clash_api": "https://controller.example.com", key: value}
                    self.assertIs(s.apply(patch), False)
                    self.assertEqual(s.data, before)
                    self.assertEqual(self.path.read_bytes(), disk_before)
                    self.assertEqual(self.hooks.calls, [])
                    self.assertEqual(self.hooks.saved, [])
                    self.assertIn("地址无效", s.public()["error"])
                    self.assertNotIn("password", s.public()["error"])

    def test_rejected_url_does_not_create_settings_file(self) -> None:
        s = self.make()
        self.assertIs(s.apply({"clash_api": ""}), False)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.hooks.saved, [])

    def test_url_recovery_valid_hosts_and_explicit_subscription_clear(self) -> None:
        s = self.make()
        self.assertIs(s.apply({"clash_api": "http://example.com/?bad=1"}), False)
        self.assertIs(s.apply({"clash_api": "http://example.com/?"}), False)
        for address in ("http://localhost:9090", "http://[::1]:9090", "https://proxy.example.com/clash",
                        "https://例子.测试:443", "HTTP://127.0.0.1:9090"):
            with self.subTest(address=address):
                self.assertIsNot(s.apply({"clash_api": f" {address}/ "}), False)
                self.assertEqual(s.data["clash_api"], address)
                self.assertEqual(s.error, "")
        s.apply({"sub_url": SUB})
        self.assertEqual(s.data["sub_url"], SUB)
        self.assertIsNot(s.apply({"sub_url": ""}), False)
        self.assertEqual(s.data["sub_url"], "")
        self.assertFalse(s.public()["sub_url_set"])
        self.assertEqual(self.on_disk()["sub_url"], "")
        self.assertEqual(self.hooks.saved[-1], {"sub_url"})

    def test_public_view_has_no_secrets(self) -> None:
        s = self.make()
        s.apply({"clash_secret": SECRET, "sub_url": SUB})
        pub = s.public()
        self.assertFalse(contains_secret(pub, [SECRET, SUB, "abcdef1234567890", "/api/v1/client"]))
        self.assertNotIn("clash_secret", pub)
        self.assertNotIn("sub_url", pub)
        self.assertEqual((pub["clash_secret_set"], pub["sub_url_set"], pub["sub_url_hint"]), (True, True, "https://sub.example.com"))
        s.apply({"clash_secret": "", "sub_url": ""})
        pub = s.public()
        self.assertEqual((pub["clash_secret_set"], pub["sub_url_set"], pub["sub_url_hint"]), (False, False, ""))
        for key in ("clash_api", "retention_days", "ignored", "pinned", "last_period", "error", "autostart"):
            self.assertIn(key, pub)

    def test_url_hint(self) -> None:
        self.assertEqual(url_hint("https://user:pw@Sub.Example.com:8443/x?token=1"), "https://sub.example.com")
        self.assertEqual(url_hint("http://[2001:db8::1]:80/a"), "http://[2001:db8::1]")
        self.assertEqual(url_hint("ftp://x/y"), "")
        self.assertEqual(url_hint("not a url"), "")
        self.assertEqual(url_hint("http://[bad"), "")
        self.assertEqual(url_hint(""), "")

    def test_save_failure_is_reported_not_raised(self) -> None:
        s = self.make()
        self.path.mkdir()  # settings.json 变成目录：写不进去
        self.assertIs(s.apply({"retention_days": 30}), False)
        self.assertIn("设置没存上", s.error)
        self.assertEqual(s.data["retention_days"], 40, "写入失败时保留已生效的设置")
        self.assertIn("设置没存上", s.public()["error"])
        self.assertEqual(self.hooks.saved, [])

    def test_failed_replace_preserves_all_runtime_settings_and_has_no_side_effects(self) -> None:
        s = self.make()
        self.assertTrue(s.apply({"clash_api": "http://127.0.0.1:9090", "sub_url": SUB,
                                 "ignored": ["background"], "pinned": ["browser"]}))
        before = deepcopy(s.data)
        data_reference = s.data
        disk_before = self.path.read_bytes()
        self.hooks.saved.clear()
        with patch.object(Path, "replace", side_effect=PermissionError("synthetic disk denied")):
            self.assertFalse(s.apply({"clash_api": "http://127.0.0.1:9097",
                                      "clash_secret": "unsaved-secret", "sub_url": "",
                                      "process": "background", "pinned": True,
                                      "autostart": True, "start_minimized": True}))
        self.assertIs(s.data, data_reference)
        self.assertEqual(s.data, before)
        self.assertEqual(self.path.read_bytes(), disk_before)
        self.assertEqual(self.hooks.calls, [])
        self.assertEqual(self.hooks.saved, [])
        self.assertFalse(s.public()["clash_secret_set"])
        self.assertTrue(s.public()["sub_url_set"])

        # An unrelated successful save must never persist the rejected values.
        self.assertTrue(s.apply({"notify_hide": True}))
        reopened = self.make()
        for key in ("clash_api", "clash_secret", "sub_url", "ignored", "pinned", "start_minimized"):
            self.assertEqual(reopened.data[key], before[key])

    def test_hooks_see_durable_settings_and_existing_data_reference_is_preserved(self) -> None:
        s = self.make()
        data_reference = s.data
        observed = []

        def observe_autostart(on: bool) -> None:
            observed.append((s.data["start_minimized"], self.on_disk()["start_minimized"]))
            self.hooks.set_autostart(on)

        s._set_autostart = observe_autostart
        self.assertTrue(s.apply({"autostart": True, "start_minimized": True}))
        self.assertEqual(observed, [(True, True)])
        self.assertIs(s.data, data_reference)
        self.assertEqual(self.hooks.saved, [{"autostart", "start_minimized"}])
        self.assertNotIn("autostart", self.on_disk(), "系统自启状态不在 JSON 中重复持久化")
        self.assertTrue(self.make().data["autostart"])

    def test_autostart_failure_reports_actual_state_without_losing_saved_preferences(self) -> None:
        s = self.make()
        self.hooks.fail = "无法写入开机启动项：拒绝访问"
        self.assertFalse(s.apply({"autostart": True, "start_minimized": True}))
        self.assertEqual(s.error, "其他设置已保存，开机启动项未更新：" + self.hooks.fail)
        self.assertFalse(s.data["autostart"])
        self.assertFalse(s.public()["autostart"])
        self.assertTrue(s.data["start_minimized"])
        self.assertTrue(self.on_disk()["start_minimized"])
        self.assertEqual(self.hooks.saved, [{"autostart", "start_minimized"}])

    def test_old_autostart_snapshot_never_overrides_system_state(self) -> None:
        self.path.write_text(json.dumps({"autostart": True}), encoding="utf-8")
        s = self.make()
        self.assertFalse(s.data["autostart"])
        self.assertTrue(s.apply({"notify_hide": True}))
        self.assertNotIn("autostart", self.on_disk())

    def test_on_saved_gets_changed_keys_and_errors_are_contained(self) -> None:
        s = self.make()
        s.apply({"sub_url": SUB, "notify_leak": False})
        self.assertEqual(self.hooks.saved, [{"sub_url", "notify_leak"}])

        def boom(keys: set[str]) -> None:
            raise ValueError("x")

        s2 = AppSettings(self.path, on_saved=boom)
        s2.apply({"notify_leak": True})
        self.assertIs(s2.data["notify_leak"], True)

    def test_helpers(self) -> None:
        s = self.make()
        s.apply({"ignored": ["Steam", "chrome"], "pinned": ["Code", "code"]})
        self.assertEqual(s.ignored_set(), {"steam", "chrome"})
        self.assertEqual(s.pinned(), ["Code"])
        prefs = s.ui_prefs()
        self.assertEqual(prefs["ignored"], ["Steam", "chrome"])
        self.assertEqual(
            set(prefs),
            {"ignored", "pinned", "show_airport", "notify_new_app", "notify_leak", "notify_quota", "show_new_banner", "home_scope"},
        )


if __name__ == "__main__":
    unittest.main()
