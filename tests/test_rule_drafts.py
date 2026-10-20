"""Local drafts, safe fragments, current identity and controller read-only boundary."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from flashmonitor.rule_drafts import RuleDrafts, DraftError, identity, snippet, token
from flashmonitor.app import MonitorApi
from flashmonitor.classify import clash_to_flow
from flashmonitor.clash import ClashClient

class DraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/"rule-drafts.json"
        self.store=RuleDrafts(self.path)
        self.flow={"process":"edge","rule_exe":"msedge.exe"}
        self.live=Mock();self.live.get.return_value={"ts":time.time(),"flows":[self.flow]}
        self.clash=Mock();self.clash.rule_inventory.return_value={"groups":["Proxy '组'"],"rules":[],"groups_error":"","rules_error":""}
        self.api=MonitorApi(SimpleNamespace(rule_drafts=self.store,live=self.live,clash=self.clash))

    def body(self,**extra):
        return {"process":"edge","exe":"msedge.exe","operation":"save","action":"direct","revision":0,**extra}

    def test_persistence_reopen_and_delete(self):
        self.api.rule_draft(self.body())
        second=RuleDrafts(self.path)
        self.assertEqual(second.view("edge")["draft"]["target"],"DIRECT")
        text=second.export("edge",1)
        self.assertIn('PROCESS-NAME,msedge.exe,DIRECT',text)
        self.assertIn("未应用",text)
        second.delete("edge",1)
        self.assertIsNone(self.store.view("edge")["draft"])
        self.clash.close_connection.assert_not_called()

    def test_preview_never_writes(self):
        value=self.api.rule_draft(self.body(operation="preview"))
        self.assertFalse(value["applied"]);self.assertFalse(self.path.exists())

    def test_display_name_does_not_create_identity(self):
        for flow in [{"process":"edge"},{"process":"edge","rule_exe":"edge"}]:
            self.assertFalse(identity([flow],"edge")[0])
        self.assertEqual(identity([{"process":"edge","proc_path":r"C:\Apps\msedge.exe"}],"edge")[0],"msedge.exe")

    def test_metadata_preserves_real_exe_not_display_name(self):
        f=clash_to_flow({"metadata":{"process":"msedge.exe"}})
        self.assertEqual(f["process"],"msedge");self.assertEqual(f["rule_exe"],"msedge.exe")

    def test_missing_ambiguous_stale_and_changed_identity_rejected(self):
        for flows in [[],[{"process":"edge"}], [self.flow,{"process":"edge","rule_exe":"other.exe"}]]:
            self.live.get.return_value={"ts":time.time(),"flows":flows}
            with self.assertRaises(DraftError): self.api.rule_draft(self.body())
        self.live.get.return_value={"ts":1,"flows":[self.flow]}
        with self.assertRaises(DraftError): self.api.rule_draft(self.body())
        self.live.get.return_value={"ts":time.time(),"flows":[self.flow]}
        with self.assertRaises(DraftError): self.api.rule_draft(self.body(exe="invented.exe"))

    def test_control_characters_and_rule_delimiters_rejected(self):
        for name in ["x,y","x\ny"," x","x\x00y","x\u0085y","x\u2028y",None,"",123]:
            with self.assertRaises(DraftError): token(name)
        for exe in ['nice.exe,REJECT','a.exe\nMATCH,REJECT','a/b.exe','friendly']:
            with self.assertRaises(DraftError): snippet(exe,"DIRECT")

    def test_yaml_quoting_unicode_hash_quotes_backslash(self):
        target='Proxy "组" # : \\ test'
        text=snippet("app.exe",target)
        # JSON double-quoted scalars are valid YAML: special content remains one scalar.
        self.assertEqual(json.loads(text.split("  - ")[1]),"PROCESS-NAME,app.exe,"+target)

    def test_unavailable_or_forged_group_rejected(self):
        with self.assertRaises(DraftError): self.api.rule_draft(self.body(action="proxy",target="fake"))
        self.clash.rule_inventory.return_value["groups_error"]="offline"
        with self.assertRaises(DraftError): self.api.rule_draft(self.body(action="proxy",target="Proxy '组'"))

    def test_duplicates_conflicts_and_revision(self):
        self.store.save("edge","msedge.exe","direct","DIRECT",0)
        for target in ["DIRECT","Proxy"]:
            with self.assertRaises(DraftError): self.store.save("alias","MSEDGE.EXE","proxy",target,1)
        with self.assertRaises(DraftError): self.store.delete("edge",0)
        with self.assertRaises(DraftError): self.store.export("edge",0)
        self.store.save("edge","msedge.exe","direct","DIRECT",1)
        self.assertEqual(len(self.store.read()["drafts"]),1)

    def test_known_conflict_and_unchecked_warning(self):
        self.clash.rule_inventory.return_value["rules"]=[{"exe":"MSEDGE.EXE","target":"Other"}]
        result=self.api.rule_draft(self.body(operation="preview"))
        self.assertTrue(any("已知冲突" in x for x in result["warnings"]))
        self.clash.rule_inventory.return_value["rules_error"]="未检查冲突"
        self.assertIn("未检查冲突",self.api.rule_draft(self.body(operation="preview"))["warnings"])

    def test_failed_write_preserves_saved_draft_and_corrupt_file(self):
        self.store.save("edge","msedge.exe","direct","DIRECT",0)
        old=self.path.read_bytes()
        with patch.object(Path,"replace",side_effect=OSError("denied")):
            with self.assertRaises(DraftError): self.store.delete("edge",1)
        self.assertEqual(self.path.read_bytes(),old)
        self.path.write_text("broken")
        with self.assertRaises(DraftError): self.store.view("edge")
        self.assertEqual(self.path.read_text(),"broken")

    def test_no_apply_action(self):
        with self.assertRaises(DraftError): self.api.rule_draft(self.body(operation="apply"))
        self.assertFalse(self.path.exists())

    def test_inventory_uses_only_get_and_filters_groups(self):
        responses=[{"proxies":{"Group":{"type":"Selector","all":["DIRECT"]},"node":{"type":"Shadowsocks"}}},
                   {"rules":[{"type":"ProcessName","payload":"app.exe","proxy":"Group"}]}]
        class Response:
            def __init__(self,data): self.data=data
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return json.dumps(self.data).encode()
        opener=Mock();opener.open.side_effect=[Response(v) for v in responses]
        client=ClashClient(lambda:"http://127.0.0.1:9090",lambda:"",opener=opener)
        result=client.rule_inventory()
        self.assertEqual(result["groups"],["Group"])
        self.assertEqual([c.args[0].get_method() for c in opener.open.call_args_list],["GET","GET"])
        self.assertEqual([c.args[0].full_url for c in opener.open.call_args_list],["http://127.0.0.1:9090/proxies","http://127.0.0.1:9090/rules"])

    def test_catalog_survives_exit_and_restart_without_live_or_controller(self):
        self.api.rule_draft(self.body())
        self.live.get.side_effect=AssertionError("saved access must not query live identity")
        self.clash.rule_inventory.side_effect=AssertionError("saved access must not query controller")
        reopened=MonitorApi(SimpleNamespace(rule_drafts=RuleDrafts(self.path)))
        catalog=reopened.rule_catalog()
        self.assertEqual(catalog["drafts"][0]["draft"]["exe"],"msedge.exe")
        self.assertEqual(catalog["drafts"][0]["process"],"edge")
        self.assertFalse(catalog["applied"])
        self.assertIn("msedge.exe",reopened.rule_draft(self.body(operation="export",revision=1))["text"])
        reopened.rule_draft(self.body(operation="delete",revision=1))
        self.assertEqual(reopened.rule_catalog()["drafts"],[])

    def test_saved_identity_is_not_replaced_by_current_process(self):
        self.api.rule_draft(self.body())
        self.live.get.return_value={"ts":time.time(),"flows":[{"process":"edge","rule_exe":"different.exe"}]}
        self.assertEqual(self.api.rule_catalog()["drafts"][0]["draft"]["exe"],"msedge.exe")
        self.assertIn("msedge.exe",self.api.rule_draft(self.body(operation="export",revision=1))["text"])

    def test_catalog_corruption_does_not_overwrite_file(self):
        self.path.write_text("broken")
        with self.assertRaises(DraftError): self.api.rule_catalog()
        self.assertEqual(self.path.read_text(),"broken")
