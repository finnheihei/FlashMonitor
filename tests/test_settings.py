"""settings.py。

SettingsBehaviourTest 只看 apply() 之后的 data 和读回来的文件，描述 0.8.0 原有的校验规则
（用同一套用例跑过 0.8.0 的 AppSettings）。SettingsFixesTest 是新增的：给页面的版本不含密钥原文、
保存失败不抛异常、保存后的回调。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

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
        s.apply({"clash_api": "ftp://x"})
        self.assertEqual(s.data["clash_api"], "http://127.0.0.1:9097", "不认识的协议不改")
        s.apply({"sub_url": SUB})
        self.assertEqual(s.data["sub_url"], SUB)
        s.apply({"sub_url": "sub.example.com/no-scheme"})
        self.assertEqual(s.data["sub_url"], "", "不是 http(s) 的订阅地址清空")
        s.apply({"sub_url": "https://x/" + "a" * 2000})
        self.assertEqual(s.data["sub_url"], "")
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
        s.apply({"retention_days": 30})
        self.assertIn("设置没存上", s.error)
        self.assertEqual(s.data["retention_days"], 30, "内存里的设置照样生效")
        self.assertIn("设置没存上", s.public()["error"])

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
