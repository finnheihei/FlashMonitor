import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from test_server import ServerTestBase
from flashmonitor.app import MonitorApi
from flashmonitor.rule_drafts import RuleDrafts

class RuleDraftRoutes(ServerTestBase):
    def setUp(self):
        super().setUp()
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=RuleDrafts(Path(self.tmp.name)/"rule-drafts.json")
        self.srv.app=MonitorApi(SimpleNamespace(rule_drafts=self.store,
            live=SimpleNamespace(get=lambda:{"ts":time.time(),"flows":[{"process":"app","rule_exe":"app.exe"}]}),
            clash=SimpleNamespace(rule_inventory=lambda:{"groups":["Proxy"],"rules":[],"groups_error":"","rules_error":""})))

    def test_context_preview_save_export_delete_over_http(self):
        code,_,raw=self.request("GET","/api/rule-context?process=app")
        self.assertEqual(code,200);self.assertEqual(json.loads(raw)["exe"],"app.exe")
        body={"process":"app","exe":"app.exe","action":"direct","operation":"preview","revision":0}
        code,_,raw=self.request("POST","/api/rule-draft",body)
        self.assertEqual(code,200);self.assertFalse(self.store.path.exists())
        body["operation"]="save"
        code,_,raw=self.request("POST","/api/rule-draft",body)
        self.assertEqual(code,200);self.assertFalse(json.loads(raw)["applied"])
        for operation in ["export","delete"]:
            code,_,raw=self.request("POST","/api/rule-draft",{**body,"operation":operation,"revision":1})
            self.assertEqual(code,200)
        self.assertIsNone(self.store.view("app")["draft"])

    def test_new_routes_require_token_and_reject_apply_or_forged_identity(self):
        code,_,_=self.request("GET","/api/rule-context?process=app",headers={"X-FM-Token":None})
        self.assertEqual(code,403)
        for body in [{"process":"app","operation":"apply"},
                     {"process":"app","exe":"wrong.exe","action":"direct","operation":"save","revision":0}]:
            code,_,_=self.request("POST","/api/rule-draft",body)
            self.assertEqual(code,400)
        self.assertFalse(self.store.path.exists())

    def test_catalog_auth_and_persistent_identity(self):
        code,_,_=self.request("GET","/api/rule-drafts",headers={"X-FM-Token":None})
        self.assertEqual(code,403)
        self.store.save("app","original.exe","direct","DIRECT",0)
        code,_,raw=self.request("GET","/api/rule-drafts")
        self.assertEqual(code,200)
        data=json.loads(raw)
        self.assertEqual(data["drafts"][0]["draft"]["exe"],"original.exe")
        self.assertEqual(data["revision"],1)
