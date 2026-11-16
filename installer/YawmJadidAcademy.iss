; Inno Setup 6 script - per-user installer (no admin rights needed).
; STATUS: written, NOT COMPILED here (no Windows). Compile on the build machine:  iscc installer\YawmJadidAcademy.iss
; Input : dist\YawmJadidAcademy\   (output of: pyinstaller YawmJadidAcademy.spec --noconfirm)
; Output: installer\Output\YawmJadidAcademy-Setup-1.2.0.exe
; DATA SAFETY: the database lives in %LOCALAPPDATA%\AlAhdAcademySuite (outside the install folder). Neither upgrade nor
; uninstall touches it - do NOT add [UninstallDelete] entries for that folder.

#define MyAppName "Yawm Jadid Academy"
#define MyAppArName "أكاديمية يوم جديد"
#define MyAppVersion "1.2.0"
#define MyAppExe "YawmJadidAcademy.exe"

[Setup]
; Fixed GUID: keeps upgrades in place. Never change it between releases.
AppId={{6F2C7B1E-3A54-4D8B-9E27-5B1C8A0D4F63}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Yawm Jadid Academy
DefaultDirName={autopf}\YawmJadidAcademy
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=YawmJadidAcademy-Setup-{#MyAppVersion}
SetupIconFile=..\alahd_erp\icon.ico
UninstallDisplayIcon={app}\{#MyAppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\YawmJadidAcademy\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
// The desktop window uses Microsoft Edge WebView2 (preinstalled on current Windows 10/11). Warn, do not block.
function WebView2Present(): Boolean;
var v: String;
begin
  Result := RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v)
         or RegQueryStringValue(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
  if not WebView2Present() then
    MsgBox('Microsoft Edge WebView2 Runtime was not detected. The app falls back to opening in your browser, but for the ' +
           'best experience install the "Evergreen" WebView2 Runtime from Microsoft (free), then run this setup again.',
           mbInformation, MB_OK);
end;
