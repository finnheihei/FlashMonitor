# Offline, current-user installer. No downloads, elevation, process termination or data migration.
[CmdletBinding()]
param(
    [ValidateSet('Install','Uninstall')][string]$Action='Install',
    [string]$PackageRoot=$PSScriptRoot,
    [string]$SandboxRoot,
    [switch]$EnableAutostart
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2

function Full([string]$Path) { return [IO.Path]::GetFullPath($Path).TrimEnd('\') }
function Child([string]$Root,[string]$Path) {
    $p=Full $Path; $r=Full $Root
    if (-not $p.StartsWith($r+'\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Path escaped the managed directory.' }
    return $p
}
function NoLinks([string]$Path) {
    $p=Full $Path
    while ($p) {
        if ((Test-Path -LiteralPath $p) -and ((Get-Item -LiteralPath $p -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Reparse points are not supported.' }
        $next=Split-Path $p -Parent
        if ($next -eq $p) { break }; $p=$next
    }
}
function JsonFile($Value,[string]$Path) {
    $Value | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $Path -Encoding UTF8
}
function Hash([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }

$sandbox= -not [string]::IsNullOrWhiteSpace($SandboxRoot)
if ($sandbox) {
    $sandboxPath=Child $env:TEMP $SandboxRoot
    if ((Split-Path $sandboxPath -Leaf) -notlike 'fm-installer-test-*') { throw 'Sandbox must be TEMP\fm-installer-test-*.' }
    NoLinks $sandboxPath
    $app=Join-Path $sandboxPath 'FlashMonitor'
    $menu=Join-Path $sandboxPath 'StartMenu'
    $registryFile=Join-Path $sandboxPath 'registry.json'
    $mutexName='Local\FlashMonitorInstaller.Test.'+(Split-Path $sandboxPath -Leaf)
} else {
    $app=Join-Path $env:LOCALAPPDATA 'FlashMonitor'
    $menu=Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
    $mutexName='Local\FlashMonitor.SingleInstance'
}
$app=Full $app; NoLinks $app; NoLinks $menu
$exe=Child $app (Join-Path $app 'FlashMonitor.exe')
$statePath=Child $app (Join-Path $app 'installer-state.json')
$shortcut=Join-Path $menu 'Flash Monitor.lnk'
NoLinks $shortcut
$uninstallKey='HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FlashMonitor_is1'
$runKey='HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$newMutex=$false
$mutex=New-Object System.Threading.Mutex($true,$mutexName,[ref]$newMutex)
if (-not $newMutex) { $mutex.Dispose(); throw 'Flash Monitor is running. Exit it from its tray and retry; the installer will not stop it.' }

function ReadRegistration {
    if ($sandbox) {
        if (Test-Path -LiteralPath $registryFile) { return Get-Content -LiteralPath $registryFile -Raw | ConvertFrom-Json }
        return [pscustomobject]@{uninstall=$null;run=$null}
    }
    $registration=$null; $run=$null
    if (Test-Path -LiteralPath $uninstallKey) {
        $key=Get-Item -LiteralPath $uninstallKey; $values=@{}
        foreach($name in $key.GetValueNames()) { $values[$name]=$key.GetValue($name) }
        $registration=[pscustomobject]$values
    }
    if (Test-Path -LiteralPath $runKey) { $run=(Get-Item -LiteralPath $runKey).GetValue('FlashMonitor',$null) }
    return [pscustomobject]@{uninstall=$registration;run=$run}
}
function WriteRegistration($Registration) {
    if ($sandbox) { JsonFile $Registration $registryFile; return }
    if ($null -eq $Registration.uninstall) {
        if(Test-Path -LiteralPath $uninstallKey){ Remove-Item -LiteralPath $uninstallKey -Recurse -Force }
    } else {
        New-Item -Path $uninstallKey -Force | Out-Null
        foreach($p in $Registration.uninstall.PSObject.Properties) {
            $type='String'; if($p.Name -in @('NoModify','NoRepair')){$type='DWord'}
            New-ItemProperty -LiteralPath $uninstallKey -Name $p.Name -Value $p.Value -PropertyType $type -Force | Out-Null
        }
    }
    # Preserve an existing autostart value unless the caller explicitly changed it.
    $old=$null
    if(Test-Path -LiteralPath $runKey){$old=(Get-Item -LiteralPath $runKey).GetValue('FlashMonitor',$null)}
    if($old -ne $Registration.run){
        if($null -eq $Registration.run){Remove-ItemProperty -LiteralPath $runKey -Name FlashMonitor -ErrorAction SilentlyContinue}
        else {New-Item -Path $runKey -Force | Out-Null; New-ItemProperty -LiteralPath $runKey -Name FlashMonitor -Value $Registration.run -PropertyType String -Force | Out-Null}
    }
}

try {
    if($Action -eq 'Uninstall') {
        if(-not(Test-Path -LiteralPath $statePath)){throw 'No managed script installation found; refusing broad deletion.'}
        $state=Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        if($state.format -ne 'FlashMonitor.install.v1' -or (Full $state.install_dir) -ne $app){throw 'Installation identity mismatch.'}
        $registration=ReadRegistration
        if($null -ne $registration.uninstall -and $registration.uninstall.InstallLocation -ne $app){throw 'A different installer owns the current uninstall registration.'}
        $managed=@('FlashMonitor.exe','Uninstall.ps1','Migrate-Data.ps1','installer-state.json')
        # No directory-wide cleanup: data, WebView state and backups are retained.
        foreach($name in $managed){$path=Child $app (Join-Path $app $name);NoLinks $path}
        if(Test-Path -LiteralPath $exe){$lock=[IO.File]::Open($exe,'Open','Read','None');$lock.Dispose()}
        if(Test-Path -LiteralPath $shortcut){
            $ws=New-Object -ComObject WScript.Shell
            if((Full $ws.CreateShortcut($shortcut).TargetPath) -eq $exe){Remove-Item -LiteralPath $shortcut -Force}
        }
        $registration.uninstall=$null
        if($null -ne $registration.run -and $registration.run.StartsWith('"'+$exe+'"',[StringComparison]::OrdinalIgnoreCase)){$registration.run=$null}
        WriteRegistration $registration
        foreach($name in $managed){$path=Child $app (Join-Path $app $name);if(Test-Path -LiteralPath $path){Remove-Item -LiteralPath $path -Force}}
        Write-Output 'Uninstalled application files. User data and installer backups were retained.'
        return
    }

    $manifestPath=Join-Path $PackageRoot 'package.json'
    $manifest=Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if($manifest.format -ne 'FlashMonitor.package.v1' -or $manifest.application -ne 'FlashMonitor'){throw 'Unknown package format.'}
    $version=[version]$manifest.version
    $payload=Join-Path $PackageRoot 'payload\FlashMonitor.exe'
    if((Hash $payload) -ne $manifest.sha256){throw 'Application checksum mismatch; nothing installed.'}
    $sourceScripts=@{'Uninstall.ps1'=(Join-Path $PackageRoot 'Install.ps1');'Migrate-Data.ps1'=(Join-Path $PackageRoot 'Migrate-Data.ps1')}
    foreach($name in $sourceScripts.Keys){
        $source=$sourceScripts[$name]
        if((Hash $source) -ne $manifest.scripts.((Split-Path $source -Leaf))){throw 'Installer script checksum mismatch.'}
    }
    if(Test-Path -LiteralPath $statePath){
        $previous=Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        if($previous.format -ne 'FlashMonitor.install.v1' -or (Full $previous.install_dir) -ne $app){throw 'Installation identity mismatch.'}
        if([version]$previous.version -gt $version){throw 'Downgrade refused; use the preserved package/backup after reviewing data compatibility.'}
    }
    if(Test-Path -LiteralPath (Join-Path $app 'unins000.exe')){throw 'A native installer manages this directory. Cross-installer upgrades have not been validated.'}
    if(Test-Path -LiteralPath $exe){
        $installedVersion=$null
        $versionText=[Diagnostics.FileVersionInfo]::GetVersionInfo($exe).FileVersion
        if([version]::TryParse($versionText,[ref]$installedVersion) -and $installedVersion -gt $version){throw 'Downgrade of the existing application refused.'}
    }
    $before=ReadRegistration
    if($null -ne $before.uninstall -and $before.uninstall.InstallLocation -ne $app){throw 'A different installation owns this application registration.'}
    New-Item -ItemType Directory -Force -Path $app,$menu | Out-Null
    $backup=Child $app (Join-Path $app ('installer-backups\'+[guid]::NewGuid().ToString('N')))
    NoLinks $backup
    New-Item -ItemType Directory -Path $backup -Force | Out-Null
    JsonFile $before (Join-Path $backup 'registration.json')
    $targets=@('FlashMonitor.exe','Uninstall.ps1','Migrate-Data.ps1','installer-state.json')
    $existed=@{}
    foreach($name in $targets){
        $path=Child $app (Join-Path $app $name); NoLinks $path
        $existed[$name]=Test-Path -LiteralPath $path
        if($existed[$name]){Copy-Item -LiteralPath $path -Destination (Join-Path $backup $name)}
    }
    $hadShortcut=Test-Path -LiteralPath $shortcut
    if($hadShortcut){Copy-Item -LiteralPath $shortcut -Destination (Join-Path $backup 'shortcut.lnk')}
    $incoming=Child $app (Join-Path $app ('incoming-'+[guid]::NewGuid().ToString('N')+'.exe'))
    try {
        Copy-Item -LiteralPath $payload -Destination $incoming
        if((Hash $incoming) -ne $manifest.sha256){throw 'Staged application checksum mismatch.'}
        if(Test-Path -LiteralPath $exe){
            $lock=[IO.File]::Open($exe,'Open','Read','None');$lock.Dispose()
            [IO.File]::Replace($incoming,$exe,(Join-Path $backup 'replaced.exe'))
        }else{[IO.File]::Move($incoming,$exe)}
        foreach($name in $sourceScripts.Keys){Copy-Item -LiteralPath $sourceScripts[$name] -Destination (Join-Path $app $name) -Force}
        $ws=New-Object -ComObject WScript.Shell; $link=$ws.CreateShortcut($shortcut)
        $link.TargetPath=$exe; $link.WorkingDirectory=$app; $link.Description='Flash Monitor'; $link.IconLocation=$exe+',0'; $link.Save()
        $registration=[pscustomobject]@{uninstall=[pscustomobject]@{
            DisplayName='Flash Monitor';DisplayVersion=$manifest.version;Publisher='finnheihei';InstallLocation=$app;
            DisplayIcon=$exe;UninstallString='powershell.exe -NoProfile -ExecutionPolicy Bypass -File "'+(Join-Path $app 'Uninstall.ps1')+'" -Action Uninstall';
            NoModify=1;NoRepair=1
        };run=$before.run}
        if($EnableAutostart){$registration.run='"'+$exe+'" --gui --minimized --no-browser'}
        WriteRegistration $registration
        JsonFile ([ordered]@{format='FlashMonitor.install.v1';install_dir=$app;version=$manifest.version;sha256=$manifest.sha256;backup=$backup}) $statePath
    }catch{
        $failure=$_
        foreach($name in $targets){
            $path=Child $app (Join-Path $app $name)
            if($existed[$name]){Copy-Item -LiteralPath (Join-Path $backup $name) -Destination $path -Force}
            elseif(Test-Path -LiteralPath $path){Remove-Item -LiteralPath $path -Force}
        }
        if($hadShortcut){Copy-Item -LiteralPath (Join-Path $backup 'shortcut.lnk') -Destination $shortcut -Force}
        elseif(Test-Path -LiteralPath $shortcut){Remove-Item -LiteralPath $shortcut -Force}
        WriteRegistration $before
        throw $failure
    }finally{if(Test-Path -LiteralPath $incoming){Remove-Item -LiteralPath $incoming -Force}}
    Write-Output ('Installed '+$manifest.version+' to '+$app+'. Data unchanged; application was not started.')
} finally {
    $mutex.ReleaseMutex();$mutex.Dispose()
}
