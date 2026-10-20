import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flashmonitor.app import MonitorApi
from flashmonitor.ipv4_drafts import IPv4Drafts, normalize_subject, subject_id
from flashmonitor.rule_drafts import DraftError
from test_server import ServerTestBase


def subject(kind="domain", value="Example.COM.", sub=False):
    return {"kind": kind, "value": value, "include_subdomains": sub}


class IPv4DraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ipv4-drafts.json"
        self.store = IPv4Drafts(self.path)
        self.live = Mock()
        self.live.get.return_value = {"ts": time.time(), "flows": [
            {"process": "worker", "proc_path": r"C:\A\worker.exe", "host": "a.example.com"},
            {"process": "worker", "proc_path": r"D:\B\worker.exe", "host": "b.example.com"},
            {"process": "fallback", "rule_exe": "fallback.exe"}]}
        self.clash = Mock()
        self.api = MonitorApi(SimpleNamespace(ipv4_drafts=self.store, live=self.live, clash=self.clash))

    def body(self, **extra):
        return {"operation": "save", "revision": 0, "subject": subject(), **extra}

    def test_normalization_identity_and_exact_suffix(self):
        self.assertEqual(normalize_subject(subject())["value"], "example.com")
        self.assertEqual(normalize_subject(subject(value="https://例子.测试:443/path?q=x"))["value"], "xn--fsqu00a.xn--0zwm56d")
        a = normalize_subject(subject("process_path", "C:/A/Worker.exe"))
        b = normalize_subject(subject("process_path", r"c:\a\worker.exe"))
        self.assertEqual(subject_id(a), subject_id(b))
        self.assertNotEqual(subject_id(a), subject_id(normalize_subject(subject("process_path", r"D:\B\worker.exe"))))
        self.assertNotEqual(subject_id(normalize_subject(subject())), subject_id(normalize_subject(subject(sub=True))))

    def test_strict_input_validation(self):
        for value in ["::1", "[2001:db8::1]", "127.0.0.1", "*.example.com", "example.com,REJECT",
                      "example.com\n", " example.com", "localhost", "example.com:443", "a..com",
                      "https://user:pw@example.com", "https://example.com:wrong"]:
            with self.subTest(value=value), self.assertRaises(DraftError):
                normalize_subject(subject(value=value))
        for value in ["worker.exe", r"C:\A\..\worker.exe", r"C:\A\worker.exe:stream", r"C:\A\*.exe"]:
            with self.assertRaises(DraftError): normalize_subject(subject("process_path", value))
        for value in ["co.uk", "com.cn"]:
            with self.assertRaises(DraftError): normalize_subject(subject(value=value, sub=True))
        with self.assertRaises(DraftError): normalize_subject(subject(sub="false"))

    def test_preview_save_reopen_export_and_delete_stay_unapplied(self):
        preview = self.api.ipv4_draft(self.body(operation="preview"))
        self.assertFalse(preview["applied"]); self.assertFalse(self.path.exists())
        saved = self.api.ipv4_draft(self.body())
        self.assertEqual(saved["execution"], "unverified"); self.assertEqual(saved["draft"]["route"], "preserve")
        reopened = IPv4Drafts(self.path)
        self.assertEqual(len(reopened.catalog()["drafts"]), 1)
        exported = reopened.operate({"operation": "export", "revision": 1, "id": saved["id"]})
        data = json.loads(exported["text"])
        self.assertFalse(data["applied"]); self.assertEqual(data["route"], "preserve")
        self.assertIn("不是可导入", data["notice"])
        self.assertNotIn("proxies", data); self.assertNotIn("rules", data)
        reopened.operate({"operation": "delete", "revision": 1, "id": saved["id"]})
        self.assertEqual(reopened.catalog()["drafts"], [])
        self.assertEqual(self.clash.mock_calls, [])

    def test_live_application_identity_and_offline_saved_identity(self):
        context = self.api.ipv4_context()
        self.assertEqual(len(context["applications"]), 3)
        self.assertEqual(context["domains"], ["a.example.com", "b.example.com"])
        path = subject("process_path", r"C:\A\worker.exe")
        saved = self.api.ipv4_draft(self.body(subject=path))
        self.live.get.return_value = {"ts": 1, "flows": []}
        self.api.ipv4_draft(self.body(subject=path, operation="preview", revision=1))
        with self.assertRaises(DraftError):
            self.api.ipv4_draft(self.body(subject=subject("process_path", r"D:\B\worker.exe"), revision=1))
        self.assertEqual(self.store.catalog()["drafts"][0]["id"], saved["id"])
        # Website drafts remain available without live data/controller access.
        self.api.ipv4_draft(self.body(revision=1))
        self.assertEqual(self.clash.mock_calls, [])

    def test_revision_duplicates_overlaps_and_preserve_route(self):
        saved = self.api.ipv4_draft(self.body(subject=subject(sub=True)))
        with self.assertRaises(DraftError): self.api.ipv4_draft(self.body())
        self.api.ipv4_draft(self.body(subject=subject(value="example.com", sub=True), revision=1))
        self.assertEqual(len(self.store.catalog()["drafts"]), 1)
        preview = self.api.ipv4_draft(self.body(subject=subject(value="a.example.com"), revision=2, operation="preview"))
        self.assertTrue(any("重叠" in w for w in preview["warnings"]))
        preview = self.api.ipv4_draft(self.body(subject=subject(value="notexample.com"), revision=2, operation="preview"))
        self.assertFalse(any("重叠" in w for w in preview["warnings"]))
        for extra in [{"route": "direct"}, {"desired_family": "dual"}, {"operation": "apply"}]:
            with self.assertRaises(DraftError): self.api.ipv4_draft(self.body(revision=2, **extra))
        with self.assertRaises(DraftError): self.store.operate({"operation": "delete", "revision": 2, "id": "missing"})
        self.assertEqual(saved["draft"]["route"], "preserve")

    def test_corruption_and_atomic_write_failure_keep_original(self):
        self.api.ipv4_draft(self.body())
        original = self.path.read_bytes()
        with patch.object(Path, "replace", side_effect=OSError("denied")):
            with self.assertRaises(DraftError): self.api.ipv4_draft(self.body(revision=1))
        self.assertEqual(self.path.read_bytes(), original)
        injected = json.loads(original)
        next(iter(injected["drafts"].values()))["applied"] = True
        self.path.write_text(json.dumps(injected), encoding="utf-8")
        with self.assertRaises(DraftError): self.store.catalog()
        self.path.write_text('{"schema_version": 1, "revision": true, "drafts": {}}')
        original = self.path.read_bytes()
        with self.assertRaises(DraftError): self.api.ipv4_draft(self.body())
        self.assertEqual(self.path.read_bytes(), original)


class IPv4Routes(ServerTestBase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.store = IPv4Drafts(Path(self.tmp.name) / "ipv4-drafts.json")
        self.srv.app = MonitorApi(SimpleNamespace(ipv4_drafts=self.store,
            live=SimpleNamespace(get=lambda: {"ts": time.time(), "flows": []})))

    def test_auth_and_lifecycle(self):
        for path in ["/api/ipv4-drafts", "/api/ipv4-draft"]:
            code, _, _ = self.request("POST" if path.endswith("draft") else "GET", path,
                                      headers={"X-FM-Token": None})
            self.assertEqual(code, 403)
        code, _, raw = self.request("GET", "/api/ipv4-drafts")
        self.assertEqual(code, 200); self.assertFalse(json.loads(raw)["applied"])
        body = {"operation": "save", "revision": 0, "subject": subject()}
        code, _, raw = self.request("POST", "/api/ipv4-draft", body)
        self.assertEqual(code, 200); key = json.loads(raw)["id"]
        for op in ["export", "delete"]:
            code, _, _ = self.request("POST", "/api/ipv4-draft", {"operation": op, "revision": 1, "id": key})
            self.assertEqual(code, 200)
        code, _, _ = self.request("POST", "/api/ipv4-draft", {**body, "operation": "apply"})
        self.assertEqual(code, 400)
