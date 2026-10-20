# Explicit, copy-only migration. Default action only previews filenames, sizes and hashes.
[CmdletBinding()]
param(
    [ValidateSet('Preview','Apply','Rollback')][string]$Action='Preview',
    [string]$Source='D:\Daily\Commen\Tun_monitor\data',
    [string]$Destination=(Join-Path $env:LOCALAPPDATA 'FlashMonitor\data'),
    [string]$PlanPath,
    [string]$JournalPath,
    [switch]$ConfirmMigration
)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
function Full([string]$p){return [IO.Path]::GetFullPath($p).TrimEnd('\')}
function NoLinks([string]$p){
    $p=Full $p
    while($p){
        if((Test-Path -LiteralPath $p) -and ((Get-Item -LiteralPath $p -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'Reparse points are not supported.'}
        $next=Split-Path $p -Parent;if($next -eq $p){break};$p=$next
    }
}
function Child([string]$root,[string]$relative){
    if([IO.Path]::IsPathRooted($relative)){throw 'Absolute manifest entry refused.'}
    $p=Full (Join-Path $root $relative)
    if(-not $p.StartsWith((Full $root)+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Manifest escaped the data directory.'}
    NoLinks $p;return $p
}
function CheckDestination([string]$path){
    $p=Full $path;NoLinks $p
    $standard=Full (Join-Path $env:LOCALAPPDATA 'FlashMonitor\data')
    $temp=(Full $env:TEMP)+'\'
    $test=$false
    if($p.StartsWith($temp,[StringComparison]::OrdinalIgnoreCase)){
        $first=$p.Substring($temp.Length).Split('\')[0]
        $test=$first -like 'fm-installer-test-*' -and (Split-Path $p -Leaf) -eq 'data'
    }
    if($p -ne $standard -and -not $test){throw 'Destination must be the current-user FlashMonitor data directory or an isolated fm-installer-test-* data directory.'}
    return $p
}
function Hash([string]$p){return (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant()}
function Inventory([string]$root){
    NoLinks $root
    if(-not(Test-Path -LiteralPath $root -PathType Container)){throw 'Source data directory does not exist.'}
    $all=New-Object Collections.Generic.List[object]
    $todo=New-Object Collections.Generic.Stack[string];$todo.Push($root)
    while($todo.Count){
        foreach($entry in Get-ChildItem -LiteralPath $todo.Pop() -Force){
            if($entry.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Linked data is not supported.'}
            $all.Add($entry)
            if($entry.PSIsContainer){$todo.Push($entry.FullName)}
        }
    }
    return @($all | Where-Object {-not $_.PSIsContainer} | Sort-Object FullName | ForEach-Object {
        [ordered]@{name=$_.FullName.Substring((Full $root).Length+1);size=$_.Length;sha256=(Hash $_.FullName)}
    })
}
function SaveJson($value,[string]$path){$value | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $path -Encoding UTF8}

if($Action -eq 'Preview'){
    $dest=CheckDestination $Destination;$src=Full $Source
    if($src -eq $dest -or $src.StartsWith($dest+'\',[StringComparison]::OrdinalIgnoreCase) -or $dest.StartsWith($src+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Source and destination must be separate directories.'}
    $files=@(Inventory $src)
    $occupied=(Test-Path -LiteralPath $dest) -and @(Get-ChildItem -LiteralPath $dest -Force).Count -gt 0
    $plan=[ordered]@{format='FlashMonitor.migration.v1';source=$src;destination=$dest;files=$files;
        source_retained=$true;can_apply=(-not $occupied -and $files.Count -gt 0);
        blocker=if($occupied){'Destination is not empty. Automatic merge/overwrite is refused.'}elseif(-not $files.Count){'Source is empty.'}else{''}}
    if($PlanPath){SaveJson $plan (Full $PlanPath)}
    $plan | ConvertTo-Json -Depth 10
    return
}
if(-not $ConfirmMigration){throw 'Apply/Rollback requires -ConfirmMigration after reviewing the preview or journal.'}
if($Action -eq 'Apply'){
    if(-not $PlanPath){throw 'Apply requires the saved preview PlanPath.'}
    $record=Get-Content -LiteralPath $PlanPath -Raw | ConvertFrom-Json
}else{
    if(-not $JournalPath){throw 'Rollback requires JournalPath.'}
    $record=Get-Content -LiteralPath $JournalPath -Raw | ConvertFrom-Json
}
if($record.format -ne 'FlashMonitor.migration.v1'){throw 'Unknown migration record.'}
$dest=CheckDestination $record.destination
$src=Full $record.source
if($src -eq $dest -or $src.StartsWith($dest+'\',[StringComparison]::OrdinalIgnoreCase) -or $dest.StartsWith($src+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Overlapping directories refused.'}
$files=@($record.files)
if(-not $files.Count){throw 'Empty migration refused.'}
if(@($files.name | Sort-Object -Unique).Count -ne $files.Count){throw 'Duplicate manifest entries refused.'}
foreach($file in $files){$null=Child $dest $file.name}
$test=$dest.StartsWith((Full $env:TEMP)+'\',[StringComparison]::OrdinalIgnoreCase)
$name='Local\FlashMonitor.SingleInstance'
if($test){$name='Local\FlashMonitorMigration.Test.'+[IO.Path]::GetFileName((Split-Path $dest -Parent))}
$created=$false;$mutex=New-Object Threading.Mutex($true,$name,[ref]$created)
if(-not $created){$mutex.Dispose();throw 'Exit Flash Monitor before migration; no process will be stopped.'}
$streams=@{}
try{
    if($Action -eq 'Rollback'){
        if($record.state -notin @('applied','prepared')){throw 'Journal is not an applied or interrupted migration.'}
        $current=@(Inventory $dest)
        if($current.Count -ne $files.Count){throw 'Destination changed since migration; rollback refused without deleting anything.'}
        foreach($file in $files){
            $p=Child $dest $file.name
            if(-not(Test-Path -LiteralPath $p) -or (Hash $p) -ne $file.sha256){throw 'Migrated data changed; rollback refused without deleting anything.'}
            $streams[$p]=[IO.File]::Open($p,'Open','Read','None')
        }
        foreach($stream in $streams.Values){$stream.Dispose()};$streams=@{}
        # Delete only the unchanged files that this migration created, never the source.
        foreach($file in $files){Remove-Item -LiteralPath (Child $dest $file.name) -Force}
        $record.state='rolled_back';SaveJson $record (Full $JournalPath)
        Write-Output 'Rolled back unchanged migrated copies. Original source retained.'
        return
    }
    if((Test-Path -LiteralPath $dest) -and @(Get-ChildItem -LiteralPath $dest -Force).Count){throw 'Destination is not empty; no data was overwritten.'}
    $current=@(Inventory $src)
    if($current.Count -ne $files.Count){throw 'Source changed since preview; preview again.'}
    foreach($file in $files){
        $p=Child $src $file.name
        $stream=[IO.File]::Open($p,'Open','Read','None');$streams[$file.name]=$stream
        $sha=[Security.Cryptography.SHA256]::Create()
        try{$hash=([BitConverter]::ToString($sha.ComputeHash($stream))).Replace('-','').ToLowerInvariant()}finally{$sha.Dispose()}
        $stream.Position=0
        if($hash -ne $file.sha256 -or $stream.Length -ne $file.size){throw 'Source changed since preview; no migration performed.'}
    }
    # Source files (including SQLite WAL/SHM) remain exclusively locked until every copy is verified.
    $parent=Split-Path $dest -Parent;NoLinks $parent
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $id=[guid]::NewGuid().ToString('N')
    $staging=Join-Path $parent ('migration-staging-'+$id)
    New-Item -ItemType Directory -Path $staging | Out-Null
    foreach($file in $files){
        $p=Child $staging $file.name
        New-Item -ItemType Directory -Force -Path (Split-Path $p -Parent) | Out-Null
        $out=[IO.File]::Open($p,'CreateNew','Write','None')
        try{$streams[$file.name].CopyTo($out);$out.Flush($true)}finally{$out.Dispose()}
        if((Hash $p) -ne $file.sha256){throw 'Copied data checksum mismatch; staging retained for inspection.'}
    }
    $journalDir=Join-Path $parent 'migration-journals';New-Item -ItemType Directory -Force -Path $journalDir | Out-Null
    $journal=Join-Path $journalDir ($id+'.json')
    $entry=[ordered]@{format=$record.format;source=$src;destination=$dest;files=$files;source_retained=$true;state='prepared';staging=$staging}
    SaveJson $entry $journal
    if(Test-Path -LiteralPath $dest){[IO.Directory]::Delete($dest,$false)}
    [IO.Directory]::Move($staging,$dest)
    $entry.state='applied';SaveJson $entry $journal
    Write-Output ('Migration applied by copy. Rollback journal: '+$journal)
}finally{
    foreach($stream in $streams.Values){$stream.Dispose()}
    $mutex.ReleaseMutex();$mutex.Dispose()
}
