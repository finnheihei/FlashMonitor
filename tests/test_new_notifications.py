"""Acknowledgement races and home preferences, using temporary application state."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from flashmonitor.app import MonitorApi
from flashmonitor.settings import AppSettings
from flashmonitor.store import TrafficStore
from flashmonitor.views import RankCache, build_view, snapshot_alerts
from test_server import ServerTestBase


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.store=TrafficStore(self.root)
        self.addCleanup(self.store.close)
        self.store.update([])
        self.first={"process":"first","ts":10}
        self.second={"process":"second","ts":11}
        self.store.new_apps=[self.first.copy(),self.second.copy()]
        self.state={"ts":1,"status":{},"alerts":[{"kind":"new",**self.first},
                    {"kind":"new",**self.second},{"kind":"leak","count":2}]}
        self.api=MonitorApi(SimpleNamespace(store=self.store))

    def test_ack_is_idempotent_and_preserves_later_events(self):
        # The user saw first; second arrived while the request was in flight.
        self.api.ack_new([self.first])
        self.api.ack_new([self.first])
        self.assertEqual(self.store.recent_new(),[self.second])

    def test_same_process_new_event_is_not_acknowledged_by_old_identity(self):
        later={"process":"first","ts":12}
        self.store.new_apps.append(later)
        self.api.ack_new([self.first,self.second])
        self.assertEqual(self.store.recent_new(),[later])

    def test_stale_collector_cache_cannot_resurrect_acknowledged_alert(self):
        self.api.ack_new([self.first])
        # A collector result constructed before ACK is published after ACK.
        data=build_view("settings","today",state=self.state,store=self.store,
                        ranks=RankCache(self.store.ranking),prefs={},sub=None)
        self.assertEqual([a["process"] for a in data["alerts"] if a["kind"]=="new"],["second"])
        self.assertEqual(self.state["alerts"][0]["process"],"first","shared state is not mutated")
        self.assertEqual([a for a in data["alerts"] if a["kind"]=="leak"],[{"kind":"leak","count":2}])

    def test_banner_settings_ignore_list_and_limits_remain_effective(self):
        self.assertFalse(any(a["kind"]=="new" for a in snapshot_alerts(self.state,self.store,{"show_new_banner":False})))
        news=snapshot_alerts(self.state,self.store,{"ignored":["FIRST"]})
        self.assertEqual([a["process"] for a in news if a["kind"]=="new"],["second"])
        self.store.new_apps=[{"process":str(i),"ts":i} for i in range(20)]
        self.assertEqual(len([a for a in snapshot_alerts(self.state,self.store,{}) if a["kind"]=="new"]),6)

    def test_confirmed_program_is_not_new_after_store_restart(self):
        flow={"id":"one","process":"app","upload":1,"download":2,"outbound":"proxy","remote":"example:443"}
        self.store.update([flow])
        events=[e for e in self.store.recent_new() if e["process"]=="app"]
        self.assertTrue(events)
        self.api.ack_new(events)
        self.store.close()
        reopened=TrafficStore(self.root)
        try:
            reopened.update([])
            reopened.update([flow])
            self.assertFalse(any(e["process"]=="app" for e in reopened.recent_new()))
        finally: reopened.close()

    def test_home_scope_persists_independently_from_cumulative_period(self):
        path=self.root/"preferences.json"
        settings=AppSettings(path)
        settings.apply({"home_scope":"proxy","last_period":"7d"})
        reopened=AppSettings(path)
        self.assertEqual(reopened.public()["home_scope"],"proxy")
        self.assertEqual(reopened.ui_prefs()["home_scope"],"proxy")
        reopened.apply({"last_period":"today"})
        self.assertEqual(reopened.public()["home_scope"],"proxy")
        reopened.apply({"home_scope":"all"})
        self.assertEqual(reopened.public()["last_period"],"today")


class AckRoutes(ServerTestBase):
    def test_invalid_or_unbounded_batch_never_clears_all(self):
        for body in [{},{"process":""},{"events":[]},{"events":[{"process":"app","ts":True}]},
                     {"events":[{"process":"app","ts":1}]*81},{"events":[{"process":"app"}]}]:
            code,_,_=self.request("POST","/api/ack-new",body)
            self.assertEqual(code,400,body)
        self.assertFalse(any(c[0]=="ack_new" for c in self.app.calls))

    def test_batch_requires_token_and_forwards_exact_identities(self):
        body={"events":[{"process":"app","ts":123}]}
        code,_,_=self.request("POST","/api/ack-new",body,headers={"X-FM-Token":None})
        self.assertEqual(code,403)
        code,_,_=self.request("POST","/api/ack-new",body)
        self.assertEqual(code,200)
        self.assertEqual(self.app.calls[-1],("ack_new",body["events"]))
