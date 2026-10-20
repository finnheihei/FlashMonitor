[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Compiler,
    [Parameter(Mandatory=$true)][string]$PackageRoot
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
# Shell shortcuts expand Windows 8.3 aliases; use that same canonical test root.
$tempBase=& python -B -c 'import os; print(os.path.realpath(os.environ["TEMP"]))'
if($LASTEXITCODE -ne 0 -or -not $tempBase){throw 'Cannot resolve installer test directory'}
$root=Join-Path $tempBase ('fm-installer-test-native-'+[guid]::NewGuid().ToString('N'))
$id='FlashMonitorTest'+[guid]::NewGuid().ToString('N')
$app=Join-Path $root 'FlashMonitor'
$output=Join-Path $root 'build'
$arp='HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\'+$id+'_is1'
$testRun='HKCU:\Software\FlashMonitorInstallerTests\'+$id+'\Run'
$passed=New-Object Collections.Generic.List[string]
$runNo=0
New-Item -ItemType Directory -Path $output -Force | Out-Null
function Assert($value,[string]$message){if(-not $value){throw $message}}
function Digest([string]$path){return (Get-FileHash -LiteralPath $path).Hash.ToLowerInvariant()}
function RealState {
    $state=[ordered]@{}
    $paths=@('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FlashMonitor_is1','HKCU:\Software\Microsoft\Windows\CurrentVersion\Run')
    foreach($path in $paths){
        $values=[ordered]@{}
        if(Test-Path -LiteralPath $path){
            $key=Get-Item -LiteralPath $path
            $names=if($path.EndsWith('\Run')){@('FlashMonitor')}else{@($key.GetValueNames() | Sort-Object)}
            foreach($name in $names){$values[$name]=$key.GetValue($name,$null)}
        }
        $state[$path]=$values
    }
    $link=Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Flash Monitor.lnk'
    $state['shortcut']=if(Test-Path -LiteralPath $link){Digest $link}else{$null}
    return ($state | ConvertTo-Json -Depth 5 -Compress)
}
function RunSetup([string]$exe,[string[]]$extra=@(),[bool]$expectSuccess=$true){
    $script:runNo++
    $log=Join-Path $root ('run-'+$script:runNo+'.log')
    $arguments=@('/VERYSILENT','/SUPPRESSMSGBOXES','/SP-','/NORESTART','/NOCLOSEAPPLICATIONS','/NORESTARTAPPLICATIONS',('/LOG="'+$log+'"'))+$extra
    $p=Start-Process -FilePath $exe -ArgumentList $arguments -WindowStyle Hidden -PassThru
    if(-not $p.WaitForExit(30000)){
        # This exact child was launched by this test; no other process is stopped.
        $p.Kill();throw ('Test installer timed out: '+$log)
    }
    if($expectSuccess){Assert ($p.ExitCode -eq 0) ('Installer failed '+$p.ExitCode+': '+$log)}
    else{Assert ($p.ExitCode -ne 0) ('Unsafe operation was accepted: '+$log)}
}
function Compile([string]$version){
    $args=@(('/DPackageDir='+$PackageRoot),('/DOutputDir='+$output),('/DAppVersion='+$version),('/DTestRoot='+$root),('/DTestId='+$id),(Join-Path $PSScriptRoot 'FlashMonitor.iss'))
    & $Compiler @args *> (Join-Path $root ('compile-'+$version+'.log'))
    if($LASTEXITCODE -ne 0){throw 'Test installer compilation failed'}
    return (Join-Path $output ($id+'-'+$version+'-setup.exe'))
}
$before=RealState
try {
    $baseVersion=[string](Get-Content -LiteralPath (Join-Path $PackageRoot 'package.json') -Raw | ConvertFrom-Json).version
    $parts=$baseVersion.Split('.')
    Assert ($parts.Count -eq 3 -and $baseVersion -match '^\d+\.\d+\.\d+$') 'Invalid package version'
    $upgradeVersion=('{0}.{1}.{2}' -f $parts[0],$parts[1],([int]$parts[2]+1))
    $base=Compile $baseVersion
    $upgrade=Compile $upgradeVersion
    $payload=Join-Path $PackageRoot 'payload\FlashMonitor.exe'
    $expected=Digest $payload
    $exe=Join-Path $app 'FlashMonitor.exe'
    $outside=Join-Path $root 'outside'
    RunSetup $base @(('/DIR="'+$outside+'"'),'/TASKS=""') $false
    Assert (-not(Test-Path -LiteralPath $outside)) 'Directory override escaped designated install root'
    $passed.Add('native_directory_override_refused')
    New-Item -ItemType Directory -Path $app -Force | Out-Null
    $scriptMarker=Join-Path $app 'installer-state.json'
    [IO.File]::WriteAllText($scriptMarker,'{"fixture":"script-managed"}')
    RunSetup $base @('/TASKS=""') $false
    Assert (-not(Test-Path -LiteralPath $exe)) 'Script-managed install was overwritten'
    Remove-Item -LiteralPath $scriptMarker
    $passed.Add('native_cross_installer_upgrade_refused')
    # A running-test mutex must block both installation and uninstallation.
    $created=$false;$held=New-Object Threading.Mutex($true,('Local\'+$id),[ref]$created)
    try{RunSetup $base @('/TASKS=""') $false}finally{$held.ReleaseMutex();$held.Dispose()}
    Assert (-not(Test-Path -LiteralPath $exe)) 'Blocked install changed payload'
    $passed.Add('running_instance_blocks_install')

    RunSetup $base @('/TASKS=""')
    Assert ((Digest $exe) -eq $expected) 'Installed real payload mismatch'
    Assert ((Get-ItemProperty -LiteralPath $arp).InstallLocation.TrimEnd('\') -eq $app) 'Uninstall location mismatch'
    Assert (-not(Test-Path -LiteralPath $testRun)) 'Autostart was enabled by default'
    $ws=New-Object -ComObject WScript.Shell
    $shortcutTarget=$ws.CreateShortcut((Join-Path $root 'StartMenu\Flash Monitor.lnk')).TargetPath
    Assert ($shortcutTarget -eq $exe) ('Start Menu target mismatch: expected '+$exe+', found '+$shortcutTarget)
    $passed.Add('native_install_payload_shortcut_uninstall_registration_no_default_autostart')

    $data=Join-Path $app 'data';New-Item -ItemType Directory -Path $data -Force | Out-Null
    $db=Join-Path $data 'flashmonitor.db'
    & python -B -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute("CREATE TABLE totals (bytes INTEGER)"); c.execute("INSERT INTO totals VALUES (1234567)"); c.commit(); c.close()' $db
    if($LASTEXITCODE -ne 0){throw 'SQLite fixture creation failed'}
    $settings=Join-Path $data 'settings.json';[IO.File]::WriteAllText($settings,'{"fixture":true,"autostart":false}')
    $dbHash=Digest $db;$settingsHash=Digest $settings
    RunSetup $base @('/TASKS=autostart')
    $run=(Get-ItemProperty -LiteralPath $testRun).FlashMonitor
    Assert ($run.StartsWith('"'+$exe+'"')) 'Explicit autostart not registered to test payload'
    RunSetup $upgrade @('/TASKS=""')
    Assert ((Get-ItemProperty -LiteralPath $arp).DisplayVersion -eq $upgradeVersion) 'Upgrade registration missing'
    Assert ((Digest $db) -eq $dbHash -and (Digest $settings) -eq $settingsHash) 'Upgrade changed user data'
    Assert ((Get-ItemProperty -LiteralPath $testRun).FlashMonitor -eq $run) 'Upgrade changed autostart preference'
    $passed.Add('native_upgrade_preserves_sqlite_settings_and_opted_in_autostart')

    RunSetup $base @('/TASKS=""') $false
    Assert ((Get-ItemProperty -LiteralPath $arp).DisplayVersion -eq $upgradeVersion) 'Downgrade changed registration'
    Assert ((Digest $exe) -eq $expected) 'Downgrade changed payload'
    $passed.Add('native_downgrade_refused')

    $uninstall=Join-Path $app 'unins000.exe'
    $created=$false;$held=New-Object Threading.Mutex($true,('Local\'+$id),[ref]$created)
    try{RunSetup $uninstall @() $false}finally{$held.ReleaseMutex();$held.Dispose()}
    Assert (Test-Path -LiteralPath $exe) 'Blocked uninstall removed payload'
    $passed.Add('running_instance_blocks_uninstall')

    RunSetup $uninstall
    Assert (-not(Test-Path -LiteralPath $exe)) 'Uninstall retained app'
    Assert (-not(Test-Path -LiteralPath $arp)) 'Uninstall registration retained'
    Assert (-not(Test-Path -LiteralPath (Join-Path $root 'StartMenu\Flash Monitor.lnk'))) 'Shortcut retained'
    Assert ((Digest $db) -eq $dbHash -and (Digest $settings) -eq $settingsHash) 'Uninstall changed data'
    $runAfter=if(Test-Path -LiteralPath $testRun){(Get-Item -LiteralPath $testRun).GetValue('FlashMonitor',$null)}else{$null}
    Assert ($null -eq $runAfter) 'Owned autostart value retained'
    $passed.Add('native_uninstall_preserves_data_and_removes_owned_registration')

    RunSetup $base @('/TASKS=""')
    Assert ((Digest $db) -eq $dbHash -and (Digest $settings) -eq $settingsHash) 'Reinstall changed data'
    & python -B -c 'import sqlite3,sys; c=sqlite3.connect("file:"+sys.argv[1]+"?mode=ro",uri=True); assert c.execute("SELECT bytes FROM totals").fetchone()==(1234567,); assert c.execute("PRAGMA integrity_check").fetchone()==("ok",); c.close()' $db
    if($LASTEXITCODE -ne 0){throw 'Retained SQLite integrity check failed'}
    New-Item -Path $testRun -Force | Out-Null
    $externalRun='"C:\SyntheticExternalOwner\other.exe"'
    New-ItemProperty -LiteralPath $testRun -Name FlashMonitor -Value $externalRun -PropertyType String -Force | Out-Null
    RunSetup (Join-Path $app 'unins000.exe')
    Assert (-not(Test-Path -LiteralPath $arp)) 'Final uninstall registration retained'
    Assert (-not(Test-Path -LiteralPath $exe)) 'Final uninstall retained app'
    Assert (-not(Test-Path -LiteralPath (Join-Path $root 'StartMenu\Flash Monitor.lnk'))) 'Final shortcut retained'
    Assert ((Get-ItemProperty -LiteralPath $testRun).FlashMonitor -eq $externalRun) 'Uninstall removed externally owned autostart value'
    $passed.Add('native_uninstall_preserves_externally_changed_run_value')
    Remove-ItemProperty -LiteralPath $testRun -Name FlashMonitor
    $passed.Add('native_reinstall_retains_readable_sqlite')
    foreach($keyPath in @($testRun,(Split-Path $testRun -Parent))){
        if(Test-Path -LiteralPath $keyPath){
            $key=Get-Item -LiteralPath $keyPath
            if($key.GetValueNames().Count -eq 0 -and $key.GetSubKeyNames().Count -eq 0){Remove-Item -LiteralPath $keyPath}
        }
    }
} finally {
    $unchanged=($before -ceq (RealState))
    $report=[ordered]@{passed=$passed.Count;checks=@($passed);root=$root;test_id=$id;test_registration=$arp;test_autostart_key=$testRun;real_monitor_registration_unchanged=$unchanged;real_application_started=$false;payload_sha256=if(Test-Path -LiteralPath (Join-Path $PackageRoot 'payload\FlashMonitor.exe')){Digest (Join-Path $PackageRoot 'payload\FlashMonitor.exe')}else{$null};scope='Native test build from same ISS and payload; compile-time destination and identity isolation. Production installer is not installed.'}
    $report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $root 'report.json') -Encoding UTF8
    $report | ConvertTo-Json -Depth 5
    if(-not $unchanged){throw 'Real Monitor registration changed'}
}
# Keep bounded test fixtures and logs. No recursive deletion or real-application cleanup.
