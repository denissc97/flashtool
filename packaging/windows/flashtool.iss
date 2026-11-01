; Inno Setup script for the Windows installer (build dist\flashtool first with packaging\build.py).
; Signing: configure SignTool in Inno Setup with the company code-signing certificate.
#define AppVersion "0.1.0"

[Setup]
AppName=FlashTool
AppVersion={#AppVersion}
AppPublisher=FlashTool
DefaultDirName={autopf}\FlashTool
DefaultGroupName=FlashTool
OutputBaseFilename=FlashTool-{#AppVersion}-setup
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
; SignTool=signtool

[Files]
Source: "..\..\dist\flashtool\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "..\..\dist\sbom.cdx.json"; DestDir: "{app}\docs"
Source: "..\..\docs\*"; DestDir: "{app}\docs"; Flags: recursesubdirs

[Icons]
Name: "{group}\FlashTool"; Filename: "{app}\flashtool.exe"
Name: "{autodesktop}\FlashTool"; Filename: "{app}\flashtool.exe"; Tasks: desktopicon

[Tasks]
Name: desktopicon; Description: "Create a desktop shortcut"; Flags: unchecked

[Messages]
WelcomeLabel2=FlashTool needs the Vector driver setup (XL Driver Library) for VN interfaces. Install it separately from vector.com.
