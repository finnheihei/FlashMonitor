; Preferred native Windows installer. Build only with official Inno Setup 6.x.
#ifndef PackageDir
  #error PackageDir is required
#endif
#ifndef OutputDir
  #error OutputDir is required
#endif
#ifndef AppVersion
  #error AppVersion is required
#endif

; Compile-time sandbox changes destinations/identity only; never shipped as the candidate.
#ifdef TestRoot
  #ifndef TestId
    #error TestId is required for isolated tests
  #endif
  #define InstallId TestId
  #define InstallHome TestRoot + "\FlashMonitor"
  #define MenuLink TestRoot + "\StartMenu\Flash Monitor"
  #define UninstallLink TestRoot + "\StartMenu\Uninstall Flash Monitor"
  #define RunSubkey "Software\FlashMonitorInstallerTests\" + TestId + "\Run"
  #define InstanceMutex "Local\" + TestId
  #define OutputName TestId + "-" + AppVersion + "-setup"
#else
  #define InstallId "FlashMonitor"
  #define InstallHome "{localappdata}\FlashMonitor"
  #define MenuLink "{userprograms}\Flash Monitor"
  #define UninstallLink "{group}\Uninstall Flash Monitor"
  #define RunSubkey "Software\Microsoft\Windows\CurrentVersion\Run"
  #define InstanceMutex "Local\FlashMonitor.SingleInstance"
  #ifdef ReleaseBuild
    #define OutputName "FlashMonitor-" + AppVersion + "-setup"
  #else
    #define OutputName "FlashMonitor-" + AppVersion + "-candidate-setup"
  #endif
#endif

[Setup]
AppId={#InstallId}
AppName=Flash Monitor
AppVersion={#AppVersion}
AppPublisher=finnheihei
AppPublisherURL=https://github.com/finnheihei/FlashMonitor
DefaultDirName={#InstallHome}
DisableDirPage=yes
DefaultGroupName=Flash Monitor
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\FlashMonitor.exe
AppMutex={#InstanceMutex}
CloseApplications=no
RestartApplications=no
AllowCancelDuringInstall=no
UsePreviousTasks=no
SetupLogging=yes
; No signing certificate has been configured. Hashes are integrity checks, not identity.

[Tasks]
Name: autostart; Description: "Start Flash Monitor when I sign in"; Flags: unchecked

[Files]
Source: "{#PackageDir}\payload\FlashMonitor.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#PackageDir}\Migrate-Data.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "INSTALLATION-native.txt"; DestDir: "{app}"; DestName: "INSTALLATION.txt"; Flags: ignoreversion
; Never include data/, configs, integration prototypes, caches or source-test directories.

[Icons]
Name: "{#MenuLink}"; Filename: "{app}\FlashMonitor.exe"; WorkingDir: "{app}"
Name: "{#UninstallLink}"; Filename: "{uninstallexe}"

[Registry]
Root: HKCU; Subkey: "{#RunSubkey}"; ValueType: string; ValueName: "FlashMonitor"; ValueData: """{app}\FlashMonitor.exe"" --gui --minimized --no-browser"; Tasks: autostart

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var InstalledVersion, CandidateVersion: Int64; RegisteredVersion: String;
begin
  Result := '';
  if CompareText(ExpandFileName(ExpandConstant('{app}')), ExpandFileName(ExpandConstant('{#InstallHome}'))) <> 0 then begin
    Result := 'This build installs only in its designated current-user directory.';
    Exit;
  end;
  if FileExists(ExpandConstant('{app}\installer-state.json')) then begin
    Result := 'A script-managed candidate is installed. Uninstall its application files first; user data is retained. Cross-installer upgrades are not yet validated.';
    Exit;
  end;
  if not StrToVersion('{#AppVersion}', CandidateVersion) then begin
    Result := 'Invalid candidate version.';
    Exit;
  end;
  if GetPackedVersion(ExpandConstant('{app}\FlashMonitor.exe'), InstalledVersion) then
    if ComparePackedVersion(InstalledVersion, CandidateVersion) > 0 then begin
      Result := 'Downgrade refused. Review data compatibility before restoring an older version.';
      Exit;
    end;
  if RegQueryStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#InstallId}_is1', 'DisplayVersion', RegisteredVersion) then
    if StrToVersion(RegisteredVersion, InstalledVersion) then
      if ComparePackedVersion(InstalledVersion, CandidateVersion) > 0 then
        Result := 'Downgrade refused. The installed package is newer.';
end;

function InitializeUninstall(): Boolean;
begin
  Result := not CheckForMutexes('{#InstanceMutex}');
  if not Result then SuppressibleMsgBox('Exit Flash Monitor from its tray before uninstalling. No process will be stopped.', mbError, MB_OK, IDOK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var Existing, Expected: String;
begin
  if CurUninstallStep = usUninstall then begin
    Expected := '"' + ExpandConstant('{app}\FlashMonitor.exe') + '"';
    if RegQueryStringValue(HKCU, '{#RunSubkey}', 'FlashMonitor', Existing) then
      if Pos(Lowercase(Expected), Lowercase(Existing)) = 1 then
        RegDeleteValue(HKCU, '{#RunSubkey}', 'FlashMonitor');
  end;
end;
// No [Run], [InstallDelete] or [UninstallDelete]: no launch, data migration or data purge.
