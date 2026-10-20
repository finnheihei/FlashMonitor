"""Durable diagnostic selections: validation, rollback, isolation and concurrent writes."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flashmonitor.app import MonitorApi
from flashmonitor.connection_draft import normalize_draft
from flashmonitor.rule_drafts import DraftError
from flashmonitor.settings import AppSettings

DRAFT = {'version': 1, 'kind': 'browser', 'applications': [r'C:\Apps\browser.exe'],
         'domains': [{'value': 'chatgpt.com', 'include_subdomains': False}],
         'target': 'GPT', 'block_ipv6': True, 'block_quic': False}


class ConnectionDraftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'settings.json'
        self.callbacks = []
        self.settings = AppSettings(self.path, on_saved=self.callbacks.append)

    def test_real_api_receipt_file_and_reopen_without_callback_or_credentials(self):
        ctx = SimpleNamespace(settings=self.settings, settings_public=self.settings.public)
        result = MonitorApi(ctx).apply_settings({'connection_draft': DRAFT})
        self.assertIs(result['connection_draft_saved'], True);self.assertIs(result['network_applied'], False)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['connection_draft'], DRAFT)
        self.assertEqual(AppSettings(self.path).public()['connection_draft'], DRAFT)
        self.assertEqual(self.callbacks, [])
        self.assertNotIn('clash_secret', result['connection_draft'])
        result['connection_draft']['domains'][0]['include_subdomains'] = True
        self.assertFalse(self.settings.data['connection_draft']['domains'][0]['include_subdomains'])

    def test_failed_replace_never_publishes_or_later_saves_failed_choice(self):
        self.assertTrue(self.settings.apply({'connection_draft': DRAFT}));before = self.path.read_bytes()
        changed = {**DRAFT, 'target': 'Other'}
        with patch.object(Path, 'replace', side_effect=PermissionError('synthetic disk denied')):
            self.assertFalse(self.settings.apply({'connection_draft': changed}))
        self.assertTrue(self.settings.error);self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.settings.data['connection_draft'], DRAFT)
        self.settings.apply({'home_scope': 'proxy'})
        self.assertEqual(AppSettings(self.path).data['connection_draft'], DRAFT)

    def test_mixed_patch_rejects_before_any_setting_or_secret_mutation(self):
        self.settings.apply({'clash_secret': 'existing-token', 'home_scope': 'proxy'})
        before = self.path.read_bytes()
        self.assertFalse(self.settings.apply({'connection_draft': DRAFT, 'clash_secret': 'new-token'}))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.settings.data['clash_secret'], 'existing-token')
        self.assertEqual(self.settings.data['home_scope'], 'proxy')

    def test_strict_whitelist_types_scope_and_urls(self):
        invalid = [None, {}, {**DRAFT, 'version': True}, {**DRAFT, 'kind': []},
                   {**DRAFT, 'secret': 'never-save'}, {**DRAFT, 'block_quic': 1},
                   {**DRAFT, 'applications': ['relative.exe']}, {**DRAFT, 'target': ''},
                   {**DRAFT, 'domains': [{'value': 'https://x.test/?token=abc', 'include_subdomains': False}]},
                   {**DRAFT, 'domains': [{'value': 'x.test', 'include_subdomains': 'yes'}]},
                   {**DRAFT, 'kind': 'application'}, {**DRAFT, 'kind': 'domain'},
                   {**DRAFT, 'applications': [r'C:\x.exe']*33},
                   {**DRAFT, 'applications': ['C:\\bad\ud800.exe']}]
        for raw in invalid:
            with self.subTest(raw=repr(raw)):
                with self.assertRaises(DraftError):normalize_draft(raw)
        self.assertFalse(self.path.exists())

    def test_normalization_preserves_explicit_scope_and_conflicting_suffix_refused(self):
        raw = copy.deepcopy(DRAFT);raw['applications'] = [r'C:\Apps\browser.exe', 'c:/apps/browser.exe']
        raw['domains'] = [{'value': 'CHATGPT.COM.', 'include_subdomains': True}]
        result = normalize_draft(raw)
        self.assertEqual(len(result['applications']), 1)
        self.assertEqual(result['domains'], [{'value': 'chatgpt.com', 'include_subdomains': True}])
        raw['domains'].append({'value': 'chatgpt.com', 'include_subdomains': False})
        with self.assertRaises(DraftError):normalize_draft(raw)

    def test_invalid_saved_draft_is_not_rewritten_on_load(self):
        self.path.write_text(json.dumps({'connection_draft': {'secret': 'invalid'}, 'ignored': []}), encoding='utf-8')
        before = self.path.read_bytes();loaded = AppSettings(self.path)
        self.assertIsNone(loaded.public()['connection_draft']);self.assertTrue(loaded.public()['connection_draft_error'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_draft_and_preferences_keep_both_fields(self):
        gate = threading.Barrier(3);errors = []
        def save(patch_value):
            try:gate.wait();self.settings.apply(patch_value)
            except Exception as exc:errors.append(str(exc))
        threads = [threading.Thread(target=save, args=(p,)) for p in ({'connection_draft': DRAFT}, {'home_scope': 'proxy'})]
        for thread in threads:thread.start()
        gate.wait()
        for thread in threads:thread.join(5)
        self.assertFalse(errors);self.assertTrue(all(not t.is_alive() for t in threads))
        disk = AppSettings(self.path).data
        self.assertEqual(disk['connection_draft'], DRAFT);self.assertEqual(disk['home_scope'], 'proxy')


if __name__ == '__main__':unittest.main()
