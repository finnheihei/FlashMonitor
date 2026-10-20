"""paths.py：FLASHMONITOR_DATA_DIR 开发实例隔离。"""

from __future__ import annotations

import os
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from flashmonitor import paths


class DevInstanceTest(unittest.TestCase):
    def test_env_var_redirects_all_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {paths.DATA_DIR_ENV: tmp}):
                want = Path(tmp).resolve()
                self.assertTrue(paths.is_dev_instance())
                self.assertEqual(paths.data_home(), want)
                self.assertEqual(paths.state_home(), want)
                tag = paths.instance_tag()
                self.assertTrue(tag.startswith(".dev-"))
                self.assertEqual(tag, paths.instance_tag())

    def test_different_dirs_get_different_tags(self) -> None:
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            with mock.patch.dict(os.environ, {paths.DATA_DIR_ENV: a}):
                ta = paths.instance_tag()
            with mock.patch.dict(os.environ, {paths.DATA_DIR_ENV: b}):
                tb = paths.instance_tag()
        self.assertNotEqual(ta, tb)

    def test_default_instance(self) -> None:
        with mock.patch.dict(os.environ, {paths.DATA_DIR_ENV: ""}):
            self.assertFalse(paths.is_dev_instance())
            self.assertEqual(paths.instance_tag(), "")
            if os.environ.get("LOCALAPPDATA") and os.name == "nt":
                self.assertEqual(paths.data_home(), Path(os.environ["LOCALAPPDATA"]) / "FlashMonitor" / "data")

    def test_legacy_migration_skipped_for_dev_instance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "data"
            with mock.patch.dict(os.environ, {paths.DATA_DIR_ENV: str(dest)}):
                paths.migrate_legacy_data(dest)
            self.assertFalse(dest.exists())

    def test_entry_script_is_repo_monitor_py(self) -> None:
        self.assertEqual(paths.entry_script().name, "monitor.py")
        self.assertTrue(paths.entry_script().is_file())
        self.assertTrue((paths.resource_dir() / "ui.html").is_file())


class ExplicitLegacyMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.binary = self.root / "binary"
        self.local = self.root / "local"
        self.dest = self.local / "FlashMonitor" / "data"
        for patcher in (
            mock.patch.dict(os.environ, {paths.DATA_DIR_ENV: "", "LOCALAPPDATA": str(self.local)}),
            mock.patch.object(paths, "project_root", return_value=self.project),
            mock.patch.object(paths, "app_dir", return_value=self.binary),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def old_data(self, directory: Path, size: int = 300_000) -> None:
        directory.mkdir(parents=True)
        (directory / "connections.jsonl").write_bytes(b"x" * size)
        (directory / "settings.json").write_text('{"source":"old"}', encoding="utf-8")
        (directory / "programs.json").write_text('{"old":{}}', encoding="utf-8")

    def snapshot(self) -> dict[str, str]:
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob("*") if p.is_file()}

    def test_first_start_does_not_import_or_create_destination(self) -> None:
        self.old_data(self.project / "data")
        before = self.snapshot()
        paths.migrate_legacy_data(self.dest)
        self.assertFalse(self.dest.exists())
        self.assertEqual(self.snapshot(), before)

    def test_empty_destination_remains_empty_until_explicit_migration(self) -> None:
        self.old_data(self.local / "FenGuang" / "data")
        self.dest.mkdir(parents=True)
        before = self.snapshot()
        paths.migrate_legacy_data(self.dest)
        self.assertEqual(list(self.dest.iterdir()), [])
        self.assertEqual(self.snapshot(), before)

    def test_larger_sources_never_replace_existing_settings_or_json(self) -> None:
        self.old_data(self.project / "data", 400_000)
        self.old_data(self.binary / "data", 500_000)
        self.old_data(self.local / "FenGuang" / "data", 600_000)
        self.old_data(self.dest, 100)
        (self.dest / "settings.json").write_text('{"source":"current","autostart":false}', encoding="utf-8")
        before = self.snapshot()
        for _ in range(2):
            paths.migrate_legacy_data(self.dest)
        self.assertEqual(self.snapshot(), before)

    def test_settings_only_target_is_not_treated_as_empty_history(self) -> None:
        self.old_data(self.project / "data")
        self.dest.mkdir(parents=True)
        (self.dest / "settings.json").write_text('{"source":"current"}', encoding="utf-8")
        before = self.snapshot()
        paths.migrate_legacy_data(self.dest)
        self.assertEqual(self.snapshot(), before)

    def test_existing_sqlite_and_wal_are_not_mixed_with_old_json(self) -> None:
        self.old_data(self.project / "data")
        self.dest.mkdir(parents=True)
        for name in ("flashmonitor.db", "flashmonitor.db-wal", "flashmonitor.db-shm"):
            (self.dest / name).write_bytes(b"existing fixture " + name.encode())
        before = self.snapshot()
        paths.migrate_legacy_data(self.dest)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.dest / "settings.json").exists())

    def test_hook_does_not_scan_unavailable_or_running_source(self) -> None:
        with mock.patch.object(Path, "is_dir", side_effect=PermissionError("busy source")), \
             mock.patch.object(Path, "mkdir", side_effect=AssertionError("unexpected write")):
            self.assertIsNone(paths.migrate_legacy_data(self.dest))

    def test_normal_windows_paths_unchanged(self) -> None:
        with mock.patch.object(paths.sys, "platform", "win32"), \
             mock.patch.object(paths, "frozen", return_value=False):
            self.assertEqual(paths.data_home(), self.dest)
            self.assertEqual(paths.state_home(), self.dest.parent)
            self.assertEqual(paths.install_exe_path(), self.dest.parent / "FlashMonitor.exe")
            self.assertFalse(self.local.exists())

    def test_first_store_start_still_creates_persistent_database(self) -> None:
        from flashmonitor.store import TrafficStore
        paths.migrate_legacy_data(self.dest)
        store = TrafficStore(self.dest)
        try:
            self.assertTrue(store.db.persistent)
            self.assertTrue((self.dest / "flashmonitor.db").is_file())
        finally:
            store.close()

    def test_in_place_legacy_conversion_remains_available(self) -> None:
        from flashmonitor.store import TrafficStore
        self.dest.mkdir(parents=True)
        legacy = {"current.exe": {"upload": 17, "download": 29, "proxy_up": 17, "proxy_down": 29}}
        (self.dest / "programs.json").write_text(json.dumps({"programs": legacy}), encoding="utf-8")
        (self.dest / "settings.json").write_text('{"source":"current"}', encoding="utf-8")
        settings = (self.dest / "settings.json").read_bytes()
        self.old_data(self.project / "data")
        paths.migrate_legacy_data(self.dest)
        store = TrafficStore(self.dest)
        try:
            rows = store.db.load_programs()
            self.assertEqual(rows["current.exe"]["upload"], 17)
            self.assertEqual(rows["current.exe"]["download"], 29)
            self.assertNotIn("old", rows)
            self.assertEqual((self.dest / "settings.json").read_bytes(), settings)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
