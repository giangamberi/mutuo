; Inno Setup script for the Mutuo desktop app.
;
; Build the PyInstaller bundle first (build_installer.ps1 does this), then:
;   iscc installer\Mutuo.iss
;
; Installs the onedir bundle into {autopf}\Mutuo and creates
; Start Menu + desktop shortcuts. User data (profiles) is NOT installed —
; it lives in %APPDATA%\Mutuo, so updates never touch it.

#define MyAppName "Mutuo"
#define MyAppVersion "0.7.0"
#define MyAppPublisher "Mutuo"
#define MyAppExeName "Mutuo.exe"

[Setup]
; NOTE: the changes made in this installer will not be persisted on the
; machine unless the user runs it elevated (per-machine install).
AppId={{8E4C1B2A-7D3F-4A6E-9C1B-5F0A2D3E4C5D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\Mutuo
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=output
OutputBaseFilename=Mutuo-Setup-{#MyAppVersion}
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
Source: "..\dist\Mutuo\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
