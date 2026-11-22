; Inno Setup 6 script: ARES-Battery-Guardian-V2-Beta-Setup.exe
;
; Build (packaging\build_installer.bat does this after PyInstaller):
;   "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" packaging\ARES-Battery-Guardian-V2-Beta.iss
; Input : dist_v2beta\ARES-Battery-Guardian\  (PyInstaller one-folder build)
; Output: installer_output\ARES-Battery-Guardian-V2-Beta-Setup.exe
;
; USER DATA IS NEVER TOUCHED. Settings, history, sessions, events, custom alarm paths and the
; database live in %LOCALAPPDATA%\ARES Battery Guardian (see app/paths.py). This script installs
; only into the PROGRAM folder {app} (%LOCALAPPDATA%\Programs\ARES Battery Guardian by default),
; has no [UninstallDelete] section, and refuses a program folder that is inside (or contains) the
; data folder, so install, update, uninstall and reinstall cannot delete or overwrite user data.

#define AppName      "ARES Battery Guardian"
#define AppVersion   "2.0.0 Beta 1"
#define AppFileVer   "2.0.0.1"
#define AppExe       "ARES-Battery-Guardian.exe"
#define DistDir      "..\dist_v2beta\ARES-Battery-Guardian"

[Setup]
; Keep this AppId for every later V2 build so updates replace this install in place.
AppId={{10EAE2D6-A69E-4449-BBA4-73FE0626C524}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
VersionInfoVersion={#AppFileVer}
VersionInfoProductVersion={#AppFileVer}
VersionInfoProductTextVersion={#AppVersion}
VersionInfoDescription={#AppName} {#AppVersion} Setup
; Per-user install: no administrator rights. {autopf} = %LOCALAPPDATA%\Programs here.
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\installer_output
OutputBaseFilename=ARES-Battery-Guardian-V2-Beta-Setup
SetupIconFile=..\assets\ares.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName} {#AppVersion}
InfoBeforeFile=BETA_NOTES.txt
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; A running ARES is asked to exit cleanly in PrepareToInstall; Restart Manager is the fallback.
CloseApplications=yes
RestartApplications=no
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; Old program libraries only (an update must not mix DLLs from two builds). {app} is the program
; folder, never the data folder (checked in PrepareToInstall).
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "BETA_NOTES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Comment: "{#AppName} {#AppVersion}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Comment: "{#AppName} {#AppVersion}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; 1) ask a running ARES to exit cleanly (it saves and closes its database itself);
; 2) remove the Windows sign-in entry and Smart Wake-Up task that run THIS copy. Neither opens the
;    database or writes into the data folder (tests/test_beta_packaging.py checks this).
Filename: "{app}\{#AppExe}"; Parameters: "--exit"; Flags: runhidden waituntilterminated; RunOnceId: "AresExit"
Filename: "{app}\{#AppExe}"; Parameters: "--uninstall-cleanup"; Flags: runhidden waituntilterminated; RunOnceId: "AresCleanup"

; There is intentionally NO [UninstallDelete] section: the data folder
; %LOCALAPPDATA%\ARES Battery Guardian is kept after uninstalling.

[Code]
function DataRoot(): String;
begin
  Result := AddBackslash(ExpandConstant('{localappdata}')) + '{#AppName}';
end;

function IsInside(Path, Folder: String): Boolean;
var
  P, F: String;
begin
  P := Lowercase(AddBackslash(Path));
  F := Lowercase(AddBackslash(Folder));
  Result := Copy(P, 1, Length(F)) = F;
end;

function OverlapsData(Dir: String): Boolean;
begin
  Result := IsInside(Dir, DataRoot()) or IsInside(DataRoot(), Dir);
end;

function DataFolderMessage(): String;
begin
  Result := 'The program cannot be installed in or above the ARES data folder' + #13#10 +
            DataRoot() + #13#10 +
            'because that folder holds your settings and battery history. Choose another folder.';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpSelectDir) and OverlapsData(WizardDirValue()) then
  begin
    MsgBox(DataFolderMessage(), mbError, MB_OK);
    Result := False;
  end;
end;

procedure AskAresToExit(Exe: String);
var
  RC: Integer;
begin
  if FileExists(Exe) then
  begin
    Log('Asking a running ARES to exit: ' + Exe + ' --exit');
    if Exec(Exe, '--exit', '', SW_HIDE, ewWaitUntilTerminated, RC) then
      Log('--exit finished with code ' + IntToStr(RC))
    else
      Log('--exit could not be started: ' + SysErrorMessage(RC));
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if OverlapsData(ExpandConstant('{app}')) then
  begin
    Result := DataFolderMessage();
    exit;
  end;
  // Update: let the installed ARES close itself (and its database) before files are replaced.
  AskAresToExit(ExpandConstant('{app}\{#AppExe}'));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  // A copy of ARES running from another folder (e.g. an older release\ build) uses the same
  // data folder; ask it to exit too, so "Launch" starts this version and not the old one.
  if CurStep = ssPostInstall then
    AskAresToExit(ExpandConstant('{app}\{#AppExe}'));
end;
