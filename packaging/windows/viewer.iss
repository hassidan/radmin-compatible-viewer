#ifndef SourceDir
  #error SourceDir must point to the PyInstaller onedir bundle
#endif
#ifndef OutputDir
  #error OutputDir must be provided
#endif
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
[Setup]
AppId=IndependentViewer.RadminCompatibleViewer
AppName=Radmin Compatible Viewer
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\Radmin Compatible Viewer
DefaultGroupName=Radmin Compatible Viewer
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=radmin-compatible-viewer-{#AppVersion}-windows-x86_64-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\radmin-compatible-viewer.exe
SetupIconFile={#IconFile}
LicenseFile={#LicenseFile}
[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\Radmin Compatible Viewer"; Filename: "{app}\radmin-compatible-viewer.exe"; AppUserModelID: "IndependentViewer.RadminCompatibleViewer"
[Run]
Filename: "{app}\radmin-compatible-viewer.exe"; Description: "Launch Radmin Compatible Viewer"; Flags: nowait postinstall skipifsilent
