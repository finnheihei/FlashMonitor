"""Build an offline candidate from an explicit source allowlist; never install dependencies."""
from __future__ import annotations
import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--iscc', type=Path, help='Existing official Inno Setup compiler; never downloaded automatically')
    parser.add_argument('--acceptance-report', type=Path, help='Independent real-backend UI report containing source hashes')
    args=parser.parse_args()
    if os.name!='nt' or struct.calcsize('P')!=8:
        parser.error('Build with 64-bit Windows Python.')
    try:
        import PyInstaller
    except ImportError:
        parser.error('PyInstaller 6.20.0 is required. No dependencies were installed.')
    if PyInstaller.__version__!='6.20.0':
        parser.error('Use the reviewed PyInstaller 6.20.0 toolchain; no automatic dependency change.')
    root=args.source.resolve(); output=args.output.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error('Output must be absent or empty; previous artifacts are never erased.')
    output.mkdir(parents=True,exist_ok=True)
    installer=Path(__file__).resolve().parent
    files=[Path(n) for n in ('monitor.py','webview_win.py','ui.html','icon.ico','LICENSE','tools/gen_versioninfo.py','native/WebView2Loader.dll')]
    files+=sorted(p.relative_to(root) for p in (root/'flashmonitor').glob('*.py') if p.name!='ipv6_bridge.py')
    for relative in files:
        if not (root/relative).is_file() or (root/relative).is_symlink():
            raise RuntimeError(f'Missing or linked build input: {relative}')
    hashes={str(p).replace('\\','/'):sha(root/p) for p in files}
    acceptance=None
    if args.acceptance_report:
        evidence=json.loads(args.acceptance_report.read_text(encoding='utf-8-sig'))
        expected=evidence.get('hashes',{})
        required={'ui.html','flashmonitor/classify.py','flashmonitor/store.py','flashmonitor/db.py','flashmonitor/views.py'}
        if not required <= expected.keys() or any(hashes.get(k)!=v for k,v in expected.items()) or evidence.get('page_errors') or not evidence.get('passed'):
            raise RuntimeError('Independent acceptance report does not match this source snapshot.')
        acceptance={'report_sha256':sha(args.acceptance_report),'source_hashes_verified':True,
                    'passed':evidence['passed'],'page_errors':evidence['page_errors']}
    version=None
    for node in ast.parse((root/'flashmonitor/__init__.py').read_text(encoding='utf-8')).body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='APP_VERSION' for t in node.targets):
            version=ast.literal_eval(node.value)
    if not isinstance(version,str) or len(version.split('.'))!=3 or not all(x.isdigit() for x in version.split('.')):
        raise RuntimeError('Expected a three-part APP_VERSION.')
    # Verify the already-present loader; no bootstrap download or runtime installation.
    loader=str(root/'native/WebView2Loader.dll').replace("'","''")
    command="$s=Get-AuthenticodeSignature -LiteralPath '"+loader+"'; if($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notlike '*Microsoft Corporation*'){exit 1}"
    signature_shell=shutil.which('pwsh.exe') or shutil.which('powershell.exe')
    if not signature_shell:
        raise RuntimeError('A local PowerShell installation is required to verify the loader signature.')
    subprocess.run([signature_shell,'-NoProfile','-Command',command],check=True)
    package=output/f'FlashMonitor-{version}-candidate-offline'
    (package/'payload').mkdir(parents=True)
    build_env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'}
    # Source snapshot and all PyInstaller temporary files stay outside the real project.
    with tempfile.TemporaryDirectory(prefix='fm-installer-build-') as scratch:
        scratch=Path(scratch); snapshot=scratch/'source';snapshot.mkdir()
        for relative in files:
            target=snapshot/relative;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(root/relative,target)
            if sha(target)!=hashes[str(relative).replace('\\','/')]:
                raise RuntimeError('Source changed while snapshotting. Retry after the editing task finishes.')
        subprocess.run([sys.executable,'-B','tools/gen_versioninfo.py'],cwd=snapshot,env=build_env,check=True)
        cmd=[sys.executable,'-B','-m','PyInstaller','--noconfirm','--clean','--noupx','--onefile','--windowed',
             '--name','FlashMonitor','--icon','icon.ico','--version-file','versioninfo.txt',
             '--add-data','ui.html;.','--add-data','icon.ico;.','--add-binary','native/WebView2Loader.dll;.',
             '--exclude-module','flashmonitor.ipv6_bridge','--distpath',str(package/'payload'),
             '--workpath',str(scratch/'work'),'--specpath',str(snapshot),'monitor.py']
        with (output/'pyinstaller.log').open('w',encoding='utf-8') as log:
            subprocess.run(cmd,cwd=snapshot,env=build_env,stdout=log,stderr=subprocess.STDOUT,check=True)
    # Prevent silently mixing the latest UI with earlier backend sources during concurrent edits.
    changed=[str(p) for p in files if sha(root/p)!=hashes[str(p).replace('\\','/')]]
    if changed:
        raise RuntimeError('Source changed during build; candidate is not accepted: '+', '.join(changed))
    payload=package/'payload/FlashMonitor.exe'
    from PyInstaller.archive.readers import CArchiveReader
    archive=CArchiveReader(str(payload))
    pyz=archive.open_embedded_archive('PYZ.pyz')
    if 'flashmonitor.ipv6_bridge' in pyz.toc:
        raise RuntimeError('Undeployable direct bridge unexpectedly included.')
    for name in ('Install.ps1','Migrate-Data.ps1'):
        shutil.copyfile(installer/name,package/name)
    (package/'Install.cmd').write_bytes(b'@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install.ps1"\r\nif errorlevel 1 pause\r\n')
    (package/'INSTALLATION.txt').write_text(
        'Flash Monitor '+version+' CANDIDATE - not release acceptance.\n\n'
        'Extract this entire folder, then run Install.cmd. No administrator rights or downloads are required.\n'
        'Exit an existing Monitor from its tray before installation. Setup never stops or starts it.\n'
        'Files: %LOCALAPPDATA%\\FlashMonitor. Data remains in the existing data subfolder.\n'
        'Uninstall through Windows Installed Apps; data and upgrade backups are retained.\n'
        'Autostart is unchanged by default. Explicit opt-in: Install.ps1 -EnableAutostart.\n'
        'The installer never migrates data. Migrate-Data.ps1 defaults to a preview; read the packaging guide before applying.\n'
        'Application startup does not import another directory automatically. In-place JSON-to-SQLite conversion remains supported.\n'
        'Candidate status: native setup lifecycle and final release acceptance remain pending.\n'
        'Build and installer scripts are unsigned. SHA256 detects changes but does not authenticate the publisher.\n'
        'No SmartScreen or security-product compatibility promise is made. Do not disable security software.\n'
        'The application needs Microsoft Edge WebView2 Runtime; this package does not download/install it.\n',encoding='utf-8')
    manifest={'format':'FlashMonitor.package.v1','application':'FlashMonitor','version':version,
              'candidate':True,'release_acceptance':'pending','sha256':sha(payload),
              'scripts':{n:sha(package/n) for n in ('Install.ps1','Migrate-Data.ps1')},
              'source_sha256':hashes,'direct_bridge_included':False,'signed':False,
              'application_acceptance':acceptance}
    write_json(package/'package.json',manifest)
    zip_path=output/(package.name+'.zip')
    with zipfile.ZipFile(zip_path,'w',zipfile.ZIP_DEFLATED) as z:
        for path in sorted(package.rglob('*')):
            if path.is_file():z.write(path,path.relative_to(output))
    iscc=args.iscc
    if iscc is None:
        found=shutil.which('ISCC.exe')
        candidates=[Path(found)] if found else []
        candidates += [Path(os.environ.get(k,''))/'Inno Setup 6/ISCC.exe' for k in ('ProgramFiles(x86)','ProgramFiles')]
        iscc=next((p for p in candidates if p.is_file()),None)
    native=None
    if iscc:
        subprocess.run([str(iscc),f'/DPackageDir={package}',f'/DOutputDir={output}',f'/DAppVersion={version}',str(installer/'FlashMonitor.iss')],check=True)
        native=output/f'FlashMonitor-{version}-candidate-setup.exe'
    report={'version':version,'candidate':True,'source_sha256':hashes,'zip':str(zip_path),'zip_sha256':sha(zip_path),
            'application_sha256':sha(payload),'native_installer':str(native) if native else None,
            'native_installer_sha256':sha(native) if native else None,'signing':'Unsigned; publisher label is not a certificate',
            'native_blocker':None if native else 'Official Inno Setup 6 compiler not found; no software was installed.',
            'compiler_source':'https://jrsoftware.org/isdl.php','runtime_installed':False,'application_acceptance':acceptance,
            'release_blockers':(['Independent integrated acceptance is pending.'] if acceptance is None else [])+[
                                'Final candidate acceptance, including startup migration policy, must be recorded separately.',
                                'Native installer lifecycle has not been tested unless separately documented.']}
    write_json(output/'build-report.json',report)
    print(json.dumps(report,ensure_ascii=True,indent=2))


if __name__=='__main__':main()
