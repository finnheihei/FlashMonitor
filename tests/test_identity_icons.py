import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch

from flashmonitor.process_identity import IdentityReader, ServiceCache, enrich, query_process, query_services
from flashmonitor.icons import IconCache, ICON_PENDING, ICON_STATES, local_executable
from flashmonitor.netinfo import CmdlineCache
from flashmonitor.app import MonitorApi
from flashmonitor.store import TrafficStore

PNG=b'\x89PNG\r\n\x1a\n'+b'x'*64


class IdentityTests(unittest.TestCase):
    def test_reader_bounded_and_requeries_reused_pid(self):
        query=Mock(side_effect=lambda pid:dict(pid=pid,status='verified',created=pid,path=rf'C:\Apps\p{pid}.exe'))
        reader=IdentityReader(query,batch=2)
        first=reader.get([1,2,3,4]);second=reader.get([1,2,3,4])
        self.assertEqual(query.call_count,4)
        self.assertEqual(first[3]['status'],'pending');self.assertEqual(second[3]['status'],'verified')
        query.side_effect=lambda pid:dict(status='verified',created=99,path=r'C:\New\new.exe')
        self.assertEqual(reader.get([1])[1]['created'],99)

    def test_conflicting_and_reused_identity_never_become_verified(self):
        stamp=int((time.time()-100+11644473600)*10_000_000)
        flow=dict(pid=1,process='worker',outbound='proxy',proc_path=r'C:\A\worker.exe')
        enrich([flow],{1:dict(status='verified',path=r'D:\B\worker.exe',created=stamp)},time.time())
        self.assertFalse(flow['identity_verified']);self.assertTrue(flow['identity_conflict'])
        self.assertEqual(flow['proc_path'],r'C:\A\worker.exe');self.assertEqual(flow['outbound'],'proxy')
        flow=dict(pid=1,process='worker')
        enrich([flow],{1:dict(status='verified',path=r'C:\A\worker.exe',created=stamp+2_000_000_000)},time.time())
        self.assertEqual(flow['identity_reason'],'PID 已复用');self.assertNotIn('proc_path',flow)

    def test_failure_reasons_and_core_source_preserved(self):
        for status,text in [('denied','访问受限'),('exited','已退出'),('failed','查询失败'),('pending','下一轮')]:
            flow=dict(pid=1,process='x')
            enrich([flow],{1:dict(status=status)},time.time())
            self.assertIn(text,flow['identity_reason']);self.assertFalse(flow['identity_verified'])
        core=dict(pid=0,process='x',proc_path=r'C:\x.exe')
        enrich([core],{},time.time())
        self.assertEqual(core['identity_source'],'核心元数据');self.assertFalse(core['identity_verified'])

    def test_command_cache_generation_and_lookup_race(self):
        generation={1:10};calls=[]
        def lookup(pids):calls.append(list(pids));return {1:'command-'+str(generation[1])}
        c=CmdlineCache(lookup,identity_lookup=lambda pid:dict(status='verified',created=generation[pid]))
        self.assertEqual(c.get([1],generations=generation),{1:'command-10'})
        c.get([1],generations=generation);self.assertEqual(len(calls),1)
        generation[1]=11
        self.assertEqual(c.get([1],generations=generation),{1:'command-11'});self.assertEqual(len(calls),2)
        race=CmdlineCache(lambda pids:{1:'stale'},identity_lookup=lambda pid:dict(status='verified',created=12))
        self.assertEqual(race.get([1],generations={1:11}),{})

    def test_shared_service_mapping_no_connection_attribution_and_reuse_invalidation(self):
        generation={1:5};query=Mock(return_value={'services':{1:['Dnscache','Other']},'error':''})
        verify=lambda pid:dict(status='verified',created=generation[pid])
        cache=ServiceCache(query=query,verify=verify)
        flow=dict(pid=1,proc_path=r'C:\Windows\System32\svchost.exe',identity_verified=True,identity_created=5)
        cache.annotate([flow]);cache.annotate([flow]);self.assertEqual(query.call_count,1)
        self.assertEqual(flow['services'],['Dnscache','Other']);self.assertIn('不代表',flow['service_note'])
        generation[1]=6;flow['identity_created']=6;cache.annotate([flow]);self.assertEqual(query.call_count,2)
        generation[1]=7;cache.annotate([flow]);self.assertEqual(flow['services'],[])
        bounded_verify=Mock(side_effect=verify)
        cache=ServiceCache(query=query,verify=bounded_verify)
        cache.annotate([dict(flow) for _ in range(1000)])
        self.assertEqual(bounded_verify.call_count,1)

    def test_full_long_path_persists_without_changing_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=TrafficStore(Path(tmp),describe=lambda p:'')
            try:
                store.update([dict(id='a',process='worker',download=10,upload=0,path='direct',outbound='direct',remote='example:443')])
                store.update([dict(id='a',process='worker',download=20,upload=0,path='direct',outbound='direct',remote='example:443')])
                path='C:\\'+('long-directory\\'*40)+'worker.exe'
                before=store.program_rows()[0]['download']
                store.remember_origin('worker',path=path);store.flush()
                self.assertEqual(store.program_path('worker'),path)
                self.assertEqual(store.program_rows()[0]['download'],before)
            finally:store.close()
            second=TrafficStore(Path(tmp),describe=lambda p:'')
            try:self.assertEqual(second.program_path('worker'),path)
            finally:second.close()


class CacheTests(unittest.TestCase):
    def test_process_folder_requires_fresh_verified_live_identity_and_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=str(Path(tmp)/'worker.exe');Path(path).write_bytes(b'x')
            live=Mock();live.get.return_value={'ts':time.time(),'flows':[dict(process='worker',pid=1,proc_path=path,identity_verified=True,identity_created=5)]}
            api=MonitorApi(SimpleNamespace(live=live))
            with patch('flashmonitor.app.query_process',return_value=dict(status='verified',created=5,path=path)),patch('flashmonitor.app.os.startfile') as opening:
                self.assertTrue(api.process_folder({'process':'worker','path':path})['opened'])
                opening.assert_called_once_with(str(Path(tmp)))
            with patch('flashmonitor.app.query_process',return_value=dict(status='verified',created=6,path=path)),patch('flashmonitor.app.os.startfile') as opening:
                with self.assertRaises(ValueError):api.process_folder({'process':'worker','path':path})
                opening.assert_not_called()
            for bad in [r'\\host\share\worker.exe',r'Folder\worker.exe',str(Path(tmp)/'other.exe')]:
                with self.assertRaises(ValueError):api.process_folder({'process':'worker','path':bad})

    def test_distinct_paths_updates_and_nonsynchronous_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs=[];calls=[]
            def extract(src,dest):calls.append(src);dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(PNG);return True
            cache=IconCache(Path(tmp)/'icons',extract=extract,spawn=jobs.append)
            cache.schedule('worker',r'C:\A\worker.exe');cache.schedule('worker',r'D:\B\worker.exe')
            self.assertEqual(len(jobs),2)
            self.assertNotEqual(cache.path_for('worker',r'C:\A\worker.exe'),cache.path_for('worker',r'D:\B\worker.exe'))
            self.assertIsNone(cache.fetch('worker',r'C:\A\worker.exe'));self.assertEqual(calls,[])
            for fn in jobs:fn()
            self.assertIsNotNone(cache.fetch('worker',r'C:\A\worker.exe'))
            source=Path(tmp)/'real.exe';source.write_bytes(b'1')
            a=cache.resource_key(str(source));source.write_bytes(b'22')
            self.assertNotEqual(a,cache.resource_key(str(source)))
            self.assertFalse(local_executable(r'\\remote\share\worker.exe'))
            self.assertFalse(local_executable('worker.exe'))
            self.assertFalse(local_executable('C:\\worker.exe\x00'))
            self.assertFalse(local_executable(r'\\?\unc\remote\share\worker.exe'))

    def test_pending_and_state_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            jobs=[];cache=IconCache(Path(tmp),extract=lambda s,p:False,spawn=jobs.append)
            for i in range(2000):cache.schedule('worker',rf'C:\A\p{i}.exe')
            self.assertEqual(len(jobs),ICON_PENDING);self.assertEqual(cache.stats()['icon_pending'],ICON_PENDING)
            for job in jobs:job()
            for i in range(600):cache.schedule('worker',rf'C:\B\p{i}.exe');jobs[-1]()
            self.assertLessEqual(cache.stats()['icon_states'],ICON_STATES)

    def test_fixed_worker_pool_under_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            gate=threading.Event()
            def extract(src,dest):gate.wait(3);return False
            cache=IconCache(Path(tmp),extract=extract)
            try:
                for i in range(1000):cache.schedule('worker',rf'C:\A\p{i}.exe')
                self.assertEqual(cache.stats()['icon_workers'],2)
                self.assertLessEqual(cache.stats()['icon_pending'],64)
            finally:gate.set();cache.close()
            self.assertFalse(any(t.is_alive() for t in cache._workers))

    def test_api_ambiguous_and_unverified_history_use_generic(self):
        icons=Mock();icons.fetch.return_value=None
        live=Mock();live.get.return_value={'flows':[dict(process='worker',proc_path=r'C:\A\worker.exe'),dict(process='worker',proc_path=r'D:\B\worker.exe')]}
        store=Mock();store.program_path.return_value=r'Folder\worker.exe'
        api=MonitorApi(SimpleNamespace(icons=icons,live=live,store=store))
        self.assertIsNone(api.icon_png('worker','generic'));icons.fetch.assert_called_with('worker','')
        live.get.return_value={'flows':[]};api.icon_png('worker');icons.fetch.assert_called_with('worker','')

    def test_api_keeps_stock_fallback_distinct_from_native_icon(self):
        icons=Mock();generic=Path('stock.png');icons.path_for.return_value=generic;icons.fetch.return_value=generic
        live=Mock();live.get.return_value={'flows':[]}
        store=Mock();store.program_path.return_value=''
        api=MonitorApi(SimpleNamespace(icons=icons,live=live,store=store))
        self.assertIsNone(api.icon_png('worker'))


@unittest.skipUnless(sys.platform=='win32','Windows native query')
class WindowsIdentityTests(unittest.TestCase):
    def test_current_process_verified_without_elevation_and_exit_unknown(self):
        info=query_process(os.getpid())
        self.assertEqual(info['status'],'verified');self.assertGreater(info['created'],0)
        self.assertEqual(Path(info['path']).resolve(),Path(sys.executable).resolve())
        self.assertNotEqual(query_process(0xFFFFFFFE)['status'],'verified')
        services=query_services()
        self.assertIsInstance(services['services'],dict)
