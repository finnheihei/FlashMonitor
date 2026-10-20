"""Read-only Windows identity/icon checks; does not start/stop Monitor or services."""
import argparse
import ctypes
import json
import os
import sqlite3
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from flashmonitor.process_identity import query_process, query_services
from flashmonitor.icons import extract_icon_png
from flashmonitor.db import SCHEMA


def check(output):
    output.mkdir(parents=True,exist_ok=True)
    kernel=ctypes.WinDLL('kernel32')
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    kernel.GetProcessHandleCount.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
    def handles():
        n=wintypes.DWORD();assert kernel.GetProcessHandleCount(kernel.GetCurrentProcess(),ctypes.byref(n));return n.value
    own=query_process(os.getpid());assert own['status']=='verified'
    before=handles();start=time.perf_counter()
    for _ in range(100):assert query_process(os.getpid())['created']==own['created']
    elapsed=(time.perf_counter()-start)*1000;after=handles()
    assert after<=before+2,(before,after)
    services=query_services();hosts=[]
    for pid,names in list(services['services'].items())[:64]:
        identity=query_process(pid)
        if identity.get('status')=='verified' and Path(identity['path']).name.lower()=='svchost.exe':
            icon=extract_icon_png(identity['path'],output/f'host-{pid}.png')
            hosts.append({'identity':identity,'services':names,'icon_extracted':icon})
            if len(hosts)>=3:break
    python_icon=extract_icon_png(sys.executable,output/'python.png')
    generic=extract_icon_png('@generic',output/'generic.png')
    assert python_icon and generic
    extract_icon_png('@generic',output/'generic-loop.png')
    icons_before=handles()
    for _ in range(30):assert extract_icon_png('@generic',output/'generic-loop.png')
    icons_after=handles();assert icons_after<=icons_before+2,(icons_before,icons_after)
    # Synthetic sizing only, no migration or mutation of user DB.
    path=output/'pair-slot-prototype.sqlite'
    conn=sqlite3.connect(path);conn.executescript(SCHEMA);conn.commit();empty=path.stat().st_size
    conn.executemany('INSERT INTO slot_traffic VALUES (?,?,?,?,?,?,?)',[
        ('procsite',i//500*900,json.dumps(['program'+str(i%8),'site'+str(i%500)+'.example.com']),0,100,0,50) for i in range(10000)])
    conn.commit();size=path.stat().st_size;conn.close()
    report={'self':own,'handle_count_before':before,'handle_count_after_100_queries':after,
            'queries_100_ms':round(elapsed,2),'service_error':services['error'],'verified_hosts':hosts,
            'python_icon':python_icon,'stock_generic_icon':generic,
            'handles_before_icon_loop':icons_before,'handles_after_30_icons':icons_after,
            'sizing_only':{'synthetic_rows':10000,'empty_bytes':empty,'populated_bytes':size,
                          'incremental_bytes_per_row':(size-empty)/10000,
                          'example_100_pairs_12_slots_per_day_40_days_bytes':round((size-empty)/10000*100*12*40)},
            'limits':'Read-only sampled processes only; not whole-app CPU/memory or packaged-size benchmark.'}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'handle_counts':[before,after],'queries_100_ms':round(elapsed,2),'verified_hosts':len(hosts),
                      'python_icon':python_icon,'stock_generic_icon':generic,'bytes_per_synthetic_row':(size-empty)/10000}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();check(args.output)
