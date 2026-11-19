; 双击安装，不要求使用者安装 Python 或 Node。
; 编译：iscc installer\FitnessPro.iss

#define AppVersion "0.1.0"

[Setup]
AppId={{8F3A6B1E-4C2D-4A91-9E77-1B6C0A5D2F44}
AppName=FitnessPro
AppVersion={#AppVersion}
AppPublisher=FitnessPro
DefaultDirName={localappdata}\Programs\FitnessPro
DefaultGroupName=FitnessPro
DisableProgramGroupPage=yes
OutputDir=..\release
OutputBaseFilename=FitnessPro-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayIcon={app}\FitnessPro.exe
SetupLogging=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Files]
Source: "..\release\FitnessPro\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\FitnessPro"; Filename: "{app}\FitnessPro.exe"
Name: "{autodesktop}\FitnessPro"; Filename: "{app}\FitnessPro.exe"

[Run]
Filename: "{app}\FitnessPro.exe"; Description: "启动 FitnessPro"; Flags: nowait postinstall skipifsilent
