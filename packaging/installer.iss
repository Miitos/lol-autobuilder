; Installateur Windows de lolbuilds (Inno Setup 6), construit par la CI :
;   iscc /DAppVersion=0.1.0 packaging\installer.iss
; Installation par utilisateur (aucun droit administrateur), dans %LOCALAPPDATA%\Programs\lolbuilds.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6E1C3A52-8D4B-4F0E-9B7A-2C5D7F1A4B90}
AppName=lolbuilds
AppVersion={#AppVersion}
AppVerName=lolbuilds {#AppVersion}
AppPublisher=Miitos
AppPublisherURL=https://github.com/Miitos/lol-autobuilder
AppSupportURL=https://github.com/Miitos/lol-autobuilder/issues
AppUpdatesURL=https://github.com/Miitos/lol-autobuilder/releases
DefaultDirName={autopf}\lolbuilds
DefaultGroupName=lolbuilds
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist\installer
OutputBaseFilename=lolbuilds-setup-{#AppVersion}
SetupIconFile=lolbuilds.ico
UninstallDisplayIcon={app}\lolbuilds.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
fr.StartupTask=Lancer lolbuilds au démarrage de Windows
en.StartupTask=Start lolbuilds when Windows starts
fr.LaunchNow=Lancer lolbuilds maintenant
en.LaunchNow=Launch lolbuilds now

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "startup"; Description: "{cm:StartupTask}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\lolbuilds\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\lolbuilds"; Filename: "{app}\lolbuilds.exe"
Name: "{autodesktop}\lolbuilds"; Filename: "{app}\lolbuilds.exe"; Tasks: desktopicon
Name: "{userstartup}\lolbuilds"; Filename: "{app}\lolbuilds.exe"; Tasks: startup

[Run]
Filename: "{app}\lolbuilds.exe"; Description: "{cm:LaunchNow}"; Flags: nowait postinstall skipifsilent
; Mise à jour depuis l'appli (installateur lancé en silencieux) : on rouvre lolbuilds automatiquement.
Filename: "{app}\lolbuilds.exe"; Parameters: "--after-update"; Flags: nowait skipifnotsilent

[UninstallRun]
; lolbuilds ouvert pendant la désinstallation : on le ferme, sinon l'exe et son dossier restent.
; (Pas d'AppMutex : la mise à jour silencieuse démarre l'installateur avant que l'appli ait quitté, il s'arrêterait.)
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM lolbuilds.exe"; Flags: runhidden; RunOnceId: "CloseLolbuilds"

[UninstallDelete]
; Les builds en cache et les réglages (%USERPROFILE%\.lolbuilds) sont conservés volontairement.
Type: filesandordirs; Name: "{app}\_internal"
