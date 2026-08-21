; Inno Setup script for MCP DevBridge
; Compile with ISCC.exe (Inno Setup 6):  ISCC scripts\installer.iss
; Expects PyInstaller output in dist\MCPDevBridge

#define MyAppName "MCP DevBridge"
#define MyAppId "1A2B3C4D-5E6F-4A8B-9C0D-1E2F3A4B5C6D"
#define MyAppVersion "0.8.4"
#define MyAppPublisher "MCP DevBridge"
#define MyAppExeName "MCPDevBridge.exe"
#ifndef MySourceDir
#define MySourceDir "..\dist\MCPDevBridge"
#endif

[Setup]
AppId={{1A2B3C4D-5E6F-4A8B-9C0D-1E2F3A4B5C6D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\MCP DevBridge
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=no
OutputDir=..\release
OutputBaseFilename=MCPDevBridge-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
; Per-user install (no admin / UAC needed); use /CURRENTUSER to force.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#MySourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent