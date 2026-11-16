; TileArc Auto-Fill helper installer (STAGING).
; Output file: TileArc-AutoFill-Helper-Setup-staging.exe
; Per-user, no admin. Listens on 127.0.0.1:9410 after install and at login.
; Employees still need Google Chrome. This does not attach to daily Chrome.

#ifndef MyAppVersion
  #define MyAppVersion "0.5.0"
#endif

#define MyAppName "TileArc Auto-Fill Helper"
#define MyAppExeName "TileArcAutoFillHelper.exe"
#define MyAppPublisher "VK-TECH-LABS"

[Setup]
AppId={{7C2A9E14-5B36-4F88-A1D0-E9410C206A11}
AppName={#MyAppName} (STAGING)
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL=https://github.com/VK-TECH-LABS/Auto-Fill
AppSupportURL=https://github.com/VK-TECH-LABS/Auto-Fill
AppUpdatesURL=https://github.com/VK-TECH-LABS/Auto-Fill/releases/tag/tilearc-autofill-helper-staging
DefaultDirName={localappdata}\TileArc\AutoFill Helper
DefaultGroupName={#MyAppName} (STAGING)
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=TileArc-AutoFill-Helper-Setup-staging
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} (STAGING)
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\dist\TileArcAutoFillHelper\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\NOTICE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\WINDOWS_EMPLOYEE.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName} (STAGING)"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Comment: "Staging helper on 127.0.0.1:9410"
Name: "{userstartup}\{#MyAppName} (STAGING)"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Comment: "Start the staging helper at login"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Start TileArc Auto-Fill Helper"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/c taskkill /F /IM {#MyAppExeName} & exit /b 0"; Flags: runhidden

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  NeedsRestart := False;
  Exec(ExpandConstant('{cmd}'), '/c taskkill /F /IM {#MyAppExeName} & exit /b 0', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;
