#define AppName "ZAIA PDF"
#define AppVersion "1.1.0"

[Setup]
AppId={{8F3A6C2E-1B7D-4E95-A4C0-5D2E9B7F1A63}
AppName={#AppName}
AppVersion={#AppVersion}
; per-user dir so the in-app updater can write files without admin
DefaultDirName={localappdata}\Programs\ZAIA PDF
DefaultGroupName=ZAIA PDF
PrivilegesRequired=lowest
OutputBaseFilename=ZAIA_PDF_Setup_{#AppVersion}
SetupIconFile=zaia.ico
UninstallDisplayIcon={app}\zaia.ico
Compression=lzma2/fast
SolidCompression=no
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "Desktop shortcut"; Flags: unchecked

[Files]
Source: "ZAIA_PDF.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "ZAIA_PDF.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "zaia_pdf.py"; DestDir: "{app}"; Flags: ignoreversion
Source: "zaia.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "logo.png"; DestDir: "{app}"; Flags: ignoreversion
Source: "setup_rev.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\ZAIA PDF"; Filename: "{app}\ZAIA_PDF.exe"; IconFilename: "{app}\zaia.ico"
Name: "{autodesktop}\ZAIA PDF"; Filename: "{app}\ZAIA_PDF.exe"; IconFilename: "{app}\zaia.ico"; Tasks: desktopicon

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"
Type: files; Name: "{app}\crash.log"
Type: files; Name: "{app}\start.log"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\.update_tmp"

[Run]
Filename: "{app}\ZAIA_PDF.exe"; Description: "Launch ZAIA PDF"; Flags: nowait postinstall skipifsilent
