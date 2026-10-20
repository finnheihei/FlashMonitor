# Runs only in a fresh TEMP\fm-installer-test-* tree. Never touches actual HKCU app keys or user shortcuts.
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
$root=Join-Path $env:TEMP ('fm-installer-test-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $root | Out-Null
$package=Join-Path $root 'package';New-Item -ItemType Directory -Path (Join-Path $package 'payload') -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Install.ps1'),(Join-Path $PSScriptRoot 'Migrate-Data.ps1') -Destination $package
$install=Join-Path $package 'Install.ps1';$migrate=Join-Path $package 'Migrate-Data.ps1'
$passed=New-Object Collections.Generic.List[string]
function Assert($condition,[string]$message){if(-not $condition){throw $message}}
function FileDigest([string]$p){return (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant()}
function Package([string]$version,[string]$bytes){
    [IO.File]::WriteAllText((Join-Path $package 'payload\FlashMonitor.exe'),$bytes)
    [ordered]@{format='FlashMonitor.package.v1';application='FlashMonitor';version=$version;
        sha256=(FileDigest (Join-Path $package 'payload\FlashMonitor.exe'));
        scripts=@{'Install.ps1'=(FileDigest $install);'Migrate-Data.ps1'=(FileDigest $migrate)}} |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $package 'package.json') -Encoding UTF8
}
function Refused([scriptblock]$operation,[string]$message){
    $failed=$false
    try{& $operation | Out-Null}catch{$failed=$true}
    Assert $failed $message
}

Package '1.0.0' 'synthetic application version one - never executed'
& $install -PackageRoot $package -SandboxRoot $root | Out-Null
$app=Join-Path $root 'FlashMonitor';$exe=Join-Path $app 'FlashMonitor.exe'
Assert ((FileDigest $exe) -eq (FileDigest (Join-Path $package 'payload\FlashMonitor.exe'))) 'Fresh payload mismatch'
$reg=Get-Content -LiteralPath (Join-Path $root 'registry.json') -Raw | ConvertFrom-Json
Assert ($null -eq $reg.run) 'Autostart changed by default'
$ws=New-Object -ComObject WScript.Shell
Assert ($ws.CreateShortcut((Join-Path $root 'StartMenu\Flash Monitor.lnk')).TargetPath -eq $exe) 'Shortcut target mismatch'
$passed.Add('fresh_install_start_menu_default_no_autostart')

New-Item -ItemType Directory -Path (Join-Path $app 'data'),(Join-Path $app 'webview2') -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $app 'data\settings.json'),'synthetic settings')
[IO.File]::WriteAllText((Join-Path $app 'data\flashmonitor.db'),'synthetic cumulative record')
[IO.File]::WriteAllText((Join-Path $app 'webview2\state'),'synthetic UI state')
$settingsHash=FileDigest (Join-Path $app 'data\settings.json');$dataHash=FileDigest (Join-Path $app 'data\flashmonitor.db');$oldHash=FileDigest $exe
& $install -PackageRoot $package -SandboxRoot $root -EnableAutostart | Out-Null
$oldRun=(Get-Content -LiteralPath (Join-Path $root 'registry.json') -Raw | ConvertFrom-Json).run
Assert (-not [string]::IsNullOrWhiteSpace($oldRun)) 'Explicit autostart was not recorded'
Package '1.1.0' 'synthetic application version two - never executed'
& $install -PackageRoot $package -SandboxRoot $root | Out-Null
$state=Get-Content -LiteralPath (Join-Path $app 'installer-state.json') -Raw | ConvertFrom-Json
Assert ((FileDigest (Join-Path $state.backup 'FlashMonitor.exe')) -eq $oldHash) 'Upgrade backup missing'
Assert ((Get-Content -LiteralPath (Join-Path $root 'registry.json') -Raw | ConvertFrom-Json).run -eq $oldRun) 'Upgrade modified autostart preference'
Assert ((FileDigest (Join-Path $app 'data\settings.json')) -eq $settingsHash) 'Upgrade changed settings'
Assert ((FileDigest (Join-Path $app 'data\flashmonitor.db')) -eq $dataHash) 'Upgrade changed cumulative data'
$passed.Add('upgrade_preserves_data_settings_autostart_and_old_binary')

$currentHash=FileDigest $exe
Package '0.9.0' 'older application'
Refused {& $install -PackageRoot $package -SandboxRoot $root} 'Downgrade allowed'
Assert ((FileDigest $exe) -eq $currentHash) 'Downgrade changed installed binary'
Package '1.2.0' 'new application'
Add-Content -LiteralPath (Join-Path $package 'payload\FlashMonitor.exe') -Value 'tampered'
Refused {& $install -PackageRoot $package -SandboxRoot $root} 'Tampered package allowed'
Assert ((FileDigest $exe) -eq $currentHash) 'Tampered package changed binary'
$passed.Add('downgrade_and_checksum_fail_before_changes')

Package '1.2.0' 'new application'
$created=$false;$held=New-Object Threading.Mutex($true,('Local\FlashMonitorInstaller.Test.'+(Split-Path $root -Leaf)),[ref]$created)
try{Refused {& $install -PackageRoot $package -SandboxRoot $root} 'Busy application was not refused'}finally{$held.ReleaseMutex();$held.Dispose()}
Assert ((FileDigest $exe) -eq $currentHash) 'Busy application changed'
$passed.Add('running_instance_refused_without_stopping')

# Hold the destination executable open to exercise safe replacement failure.
$locked=[IO.File]::Open($exe,'Open','Read','None')
try{Refused {& $install -PackageRoot $package -SandboxRoot $root} 'Locked executable replacement allowed'}finally{$locked.Dispose()}
Assert ((FileDigest $exe) -eq $currentHash) 'Locked replacement corrupted old binary'
$passed.Add('locked_upgrade_preserves_old_binary')

& (Join-Path $app 'Uninstall.ps1') -Action Uninstall -SandboxRoot $root | Out-Null
Assert (-not(Test-Path -LiteralPath $exe)) 'Uninstall retained executable'
Assert ((FileDigest (Join-Path $app 'data\settings.json')) -eq $settingsHash) 'Uninstall changed settings'
Assert ((FileDigest (Join-Path $app 'data\flashmonitor.db')) -eq $dataHash) 'Uninstall changed data'
Assert (Test-Path -LiteralPath (Join-Path $app 'webview2\state')) 'Uninstall removed UI state'
Assert (Test-Path -LiteralPath $state.backup) 'Uninstall removed upgrade backup'
$reg=Get-Content -LiteralPath (Join-Path $root 'registry.json') -Raw | ConvertFrom-Json
Assert ($null -eq $reg.run -and $null -eq $reg.uninstall) 'Owned registrations not removed'
$passed.Add('uninstall_removes_only_owned_files_and_retains_data')

$source=Join-Path $root 'old-source\data';$dest=Join-Path $root 'migration-target\data'
New-Item -ItemType Directory -Path $source -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $source 'settings.json'),'synthetic old settings')
[IO.File]::WriteAllText((Join-Path $source 'flashmonitor.db'),'synthetic database bytes')
[IO.File]::WriteAllText((Join-Path $source 'flashmonitor.db-wal'),'synthetic WAL bytes')
$plan=Join-Path $root 'preview.json'
& $migrate -Source $source -Destination $dest -PlanPath $plan | Out-Null
Assert (-not(Test-Path -LiteralPath $dest)) 'Preview created target data'
Refused {& $migrate -Action Apply -PlanPath $plan} 'Migration applied without explicit confirmation'
$passed.Add('migration_preview_readonly_and_apply_explicit')

$stream=[IO.File]::Open((Join-Path $source 'flashmonitor.db'),'Open','Read','None')
try{Refused {& $migrate -Action Apply -PlanPath $plan -ConfirmMigration} 'Active data was migrated'}finally{$stream.Dispose()}
Assert (-not(Test-Path -LiteralPath $dest)) 'Locked source migration wrote target'
[IO.File]::AppendAllText((Join-Path $source 'settings.json'),'changed')
Refused {& $migrate -Action Apply -PlanPath $plan -ConfirmMigration} 'Stale preview was applied'
Assert (-not(Test-Path -LiteralPath $dest)) 'Stale preview changed target'
$passed.Add('migration_refuses_locked_source_and_stale_preview')

& $migrate -Source $source -Destination $dest -PlanPath $plan | Out-Null
& $migrate -Action Apply -PlanPath $plan -ConfirmMigration | Out-Null
foreach($file in Get-ChildItem -LiteralPath $source -File){Assert ((FileDigest $file.FullName) -eq (FileDigest (Join-Path $dest $file.Name))) 'Copy mismatch'}
$journal=(Get-ChildItem -LiteralPath (Join-Path (Split-Path $dest -Parent) 'migration-journals') -File | Select-Object -First 1).FullName
Refused {& $migrate -Action Apply -PlanPath $plan -ConfirmMigration} 'Nonempty destination was overwritten'
[IO.File]::AppendAllText((Join-Path $dest 'settings.json'),'new application data')
Refused {& $migrate -Action Rollback -JournalPath $journal -ConfirmMigration} 'Modified data was deleted on rollback'
Assert (Test-Path -LiteralPath (Join-Path $dest 'flashmonitor.db')) 'Rollback conflict partially deleted data'
Copy-Item -LiteralPath (Join-Path $source 'settings.json') -Destination (Join-Path $dest 'settings.json') -Force
& $migrate -Action Rollback -JournalPath $journal -ConfirmMigration | Out-Null
Assert (@(Get-ChildItem -LiteralPath $dest -File).Count -eq 0) 'Rollback left migrated files'
Assert (@(Get-ChildItem -LiteralPath $source -File).Count -eq 3) 'Rollback touched original source'
$passed.Add('migration_copy_wal_nonempty_guard_and_conflict_safe_rollback')

$report=[ordered]@{passed=$passed.Count;checks=@($passed);root=$root;real_registry_modified=$false;real_application_started=$false;native_inno_installer_tested=$false}
$report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $root 'report.json') -Encoding UTF8
$report | ConvertTo-Json -Depth 5
# Retain the bounded synthetic fixture directory as auditable evidence; no recursive cleanup.
