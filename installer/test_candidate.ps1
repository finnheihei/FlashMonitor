[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$ZipPath)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
$root=Join-Path $env:TEMP ('fm-installer-test-'+[guid]::NewGuid().ToString('N'))
$extract=Join-Path $root 'extracted'
New-Item -ItemType Directory -Path $extract -Force | Out-Null
Expand-Archive -LiteralPath $ZipPath -DestinationPath $extract
$packages=@(Get-ChildItem -LiteralPath $extract -Directory)
if($packages.Count -ne 1){throw 'Expected one package root'}
$package=$packages[0].FullName
$manifest=Get-Content -LiteralPath (Join-Path $package 'package.json') -Raw | ConvertFrom-Json
if(-not $manifest.candidate -or $manifest.direct_bridge_included){throw 'Wrong candidate metadata'}
$expected=@('Install.cmd','Install.ps1','Migrate-Data.ps1','INSTALLATION.txt','package.json','payload\FlashMonitor.exe')
$actual=@(Get-ChildItem -LiteralPath $package -Recurse -File | ForEach-Object {$_.FullName.Substring($package.Length+1)})
if(@(Compare-Object $expected $actual).Count){throw 'Unexpected package contents'}
$install=Join-Path $package 'Install.ps1'
& $install -PackageRoot $package -SandboxRoot $root | Out-Null
$app=Join-Path $root 'FlashMonitor'
$exe=Join-Path $app 'FlashMonitor.exe'
if((Get-FileHash -LiteralPath $exe).Hash.ToLowerInvariant() -ne $manifest.sha256){throw 'Installed payload mismatch'}
$data=Join-Path $app 'data'
New-Item -ItemType Directory -Path $data | Out-Null
[IO.File]::WriteAllText((Join-Path $data 'settings.json'),'{"synthetic":true}')
[IO.File]::WriteAllBytes((Join-Path $data 'flashmonitor.db'),[byte[]](0,1,2,3,4,5))
$hashes=@{}
foreach($f in Get-ChildItem -LiteralPath $data -File){$hashes[$f.Name]=(Get-FileHash -LiteralPath $f.FullName).Hash}
# Reinstall the same candidate, exercising File.Replace and installed payload backup.
& $install -PackageRoot $package -SandboxRoot $root | Out-Null
$state=Get-Content -LiteralPath (Join-Path $app 'installer-state.json') -Raw | ConvertFrom-Json
if((Get-FileHash -LiteralPath (Join-Path $state.backup 'FlashMonitor.exe')).Hash.ToLowerInvariant() -ne $manifest.sha256){throw 'Payload backup mismatch'}
& (Join-Path $app 'Uninstall.ps1') -Action Uninstall -SandboxRoot $root | Out-Null
if(Test-Path -LiteralPath $exe){throw 'Uninstall retained payload'}
foreach($name in $hashes.Keys){if((Get-FileHash -LiteralPath (Join-Path $data $name)).Hash -ne $hashes[$name]){throw 'Data changed'}}
$report=[ordered]@{passed=4;checks=@('zip_allowlist','real_payload_install_hash','real_payload_reinstall_backup','uninstall_preserves_fixture_data');zip_sha256=(Get-FileHash -LiteralPath $ZipPath).Hash.ToLowerInvariant();root=$root;real_registry_modified=$false;real_application_started=$false;native_inno_installer_tested=$false}
$report | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $root 'report.json') -Encoding UTF8
$report | ConvertTo-Json -Depth 5
