; Inno Setup script for Argus on Windows.
; UNTESTED — authored on Linux, cannot run ISCC.exe / Inno Setup here.
; See packaging/windows/README.md for the exact build sequence
; (PyInstaller first, then this script).
;
; Assumes `pyinstaller argus.spec` has already produced the onedir build
; at packaging\windows\dist\argus\ (containing Argus.exe + all bundled
; deps/data). This script packages that directory as-is.

#define MyAppName "Argus"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "Argus"
#define MyAppExeName "Argus.exe"

[Setup]
AppId={{6E6D8C6B-6E1A-4E60-9F0B-ARGUS00000001}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputBaseFilename=argus-setup-{#MyAppVersion}
Compression=lzma
SolidCompression=yes
; Per-user install avoids requiring admin rights, matching the "no manual
; steps, just double-click" install experience the spec asks for, and
; matches the LogonTrigger-based Task Scheduler task registered below
; (which runs in the interactive user session, not as SYSTEM).
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; The full PyInstaller onedir output tree (Argus.exe + all bundled
; dependencies + the dashboard templates data dir under
; argus\dashboard\templates\, per argus.spec's `datas=`).
Source: "dist\argus\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
; Register the always-on-at-logon Task Scheduler task by reusing
; packaging/windows/Argus.xml as a template. Approach: Inno's [Code]
; section (below) rewrites a copy of Argus.xml at install time,
; substituting the real installed exe path (Inno's {app} constant,
; expanded to a plain string before Pascal code runs — Inno macros are
; NOT available inside [Code], so ExpandConstant('{app}') is used there
; instead), then this [Run] entry imports that rewritten XML via
; schtasks.exe, exactly the same command `argus service install` runs by
; hand on Windows (see src/argus/service.py install_windows()).
Filename: "{sys}\schtasks.exe"; \
  Parameters: "/Create /TN ""Argus"" /XML ""{app}\Argus.xml"" /F"; \
  Flags: runhidden; StatusMsg: "Registering Argus to start at logon..."

; "Launch Argus" checkbox, offered after install completes.
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\schtasks.exe"; Parameters: "/End /TN ""Argus"""; Flags: runhidden; RunOnceId: "StopArgusTask"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""Argus"" /F"; Flags: runhidden; RunOnceId: "DeleteArgusTask"

[Code]
// Rewrite Argus.xml (bundled below via [Files]-less "Source" — actually
// generated inline here to keep the substitution trivial and avoid
// UTF-16 file-encoding gymnastics in Pascal Script): read the template
// committed at packaging/windows/Argus.xml, replace its placeholder
// <Command>/<Arguments> with the real installed exe, and write the
// result into {app}\Argus.xml before the [Run] schtasks step above
// imports it.
//
// NOTE: this reimplements, in Inno's Pascal Script, exactly what
// src/argus/service.py's install_windows() does in Python (format the
// same XML template with the resolved interpreter/exe path). Keeping
// both is intentional: `argus service install` is also usable standalone
// (e.g. for a manual/dev install without the .exe installer), but the
// shipped installer must not depend on invoking the installed app once
// just to self-register — it registers the task as part of Setup itself.
procedure CreateArgusTaskXml();
var
  Lines: TArrayOfString;
  Xml: AnsiString;
  ExePath: String;
begin
  ExePath := ExpandConstant('{app}\{#MyAppExeName}');

  Xml :=
    '<?xml version="1.0" encoding="UTF-16"?>' + #13#10 +
    '<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">' + #13#10 +
    '  <RegistrationInfo>' + #13#10 +
    '    <Description>Argus - always-on activity tracker (starts at logon, restarts on crash)</Description>' + #13#10 +
    '  </RegistrationInfo>' + #13#10 +
    '  <Triggers>' + #13#10 +
    '    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>' + #13#10 +
    '  </Triggers>' + #13#10 +
    '  <Principals>' + #13#10 +
    '    <Principal id="Author">' + #13#10 +
    '      <LogonType>InteractiveToken</LogonType>' + #13#10 +
    '      <RunLevel>LeastPrivilege</RunLevel>' + #13#10 +
    '    </Principal>' + #13#10 +
    '  </Principals>' + #13#10 +
    '  <Settings>' + #13#10 +
    '    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>' + #13#10 +
    '    <AllowHardTerminate>true</AllowHardTerminate>' + #13#10 +
    '    <StartWhenAvailable>true</StartWhenAvailable>' + #13#10 +
    '    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>' + #13#10 +
    '    <Enabled>true</Enabled>' + #13#10 +
    '    <Hidden>false</Hidden>' + #13#10 +
    '    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>' + #13#10 +
    '    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>' + #13#10 +
    '  </Settings>' + #13#10 +
    '  <Actions Context="Author">' + #13#10 +
    '    <Exec>' + #13#10 +
    '      <Command>' + ExePath + '</Command>' + #13#10 +
    '      <Arguments></Arguments>' + #13#10 +
    '    </Exec>' + #13#10 +
    '  </Actions>' + #13#10 +
    '</Task>' + #13#10;

  // schtasks /XML requires UTF-16; SaveStringToUTF8File would be wrong
  // here. Inno's SaveStringsToFile/SaveStringToFile write ANSI/UTF-8 by
  // default — verify on real Windows whether schtasks accepts the UTF-8
  // form (it often does despite the XML declaration) or whether this
  // needs an explicit UTF-16LE BOM write via a small helper DLL/exec.
  // Flagged here rather than silently assumed correct: this is exactly
  // the kind of thing that only shows up on a real Windows run.
  SaveStringToFile(ExpandConstant('{app}\Argus.xml'), Xml, False);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    CreateArgusTaskXml();
  end;
end;
