; Inno Setup script for the Shared Knowledge desktop app.
;
; Build the PyInstaller bundle first (build_installer.ps1 does this), then:
;   iscc installer\SharedKnowledge.iss
;
; Installs the onedir bundle into {autopf}\SharedKnowledge and creates
; Start Menu + desktop shortcuts. User data (profiles) is NOT installed —
; it lives in %APPDATA%\SharedKnowledge, so updates never touch it.

#define MyAppName "Shared Knowledge"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Shared Knowledge"
#define MyAppExeName "SharedKnowledge.exe"

[Setup]
; NOTE: the changes made in this installer will not be persisted on the
; machine unless the user runs it elevated (per-machine install).
AppId={{8E4C1B2A-7D3F-4A6E-9C1B-5F0A2D3E4C5D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\SharedKnowledge
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=output
OutputBaseFilename=SharedKnowledge-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; the app needs the WebView2 Runtime; on Win10/11 it's preinstalled, but
; check and offer the bootstrapper if missing
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\SharedKnowledge\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
