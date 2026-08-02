; Inno Setup script for George.
;
; Build:  iscc /DVersion=3.4.0 packaging\george.iss
; Expects PyInstaller's dist\George\ to exist already.
;
; This is the Windows equivalent of install.sh: it puts George somewhere
; sensible, gives him a Start Menu entry with his own icon, offers to
; fetch Ollama if it is missing, and leaves a working uninstaller.
; Per-user by default so it needs no administrator rights.

#ifndef Version
  #define Version "3.4.0"
#endif

#define AppName "George"
#define AppId "com.thepriest.george"
#define Publisher "the-priest"
#define AppURL "https://github.com/the-priest/George"

[Setup]
AppId={{7B2C9A14-4E6D-4C8B-9F31-6A0D5E2C8B77}
AppName={#AppName}
AppVersion={#Version}
AppPublisher={#Publisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=George-Setup-{#Version}-x64
SetupIconFile=george.ico
UninstallDisplayIcon={app}\George.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Per-user install: no UAC prompt, and George only ever writes to the
; user's own AppData anyway.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
  GroupDescription: "Shortcuts:"
Name: "safegraphics"; Description: \
  "Also add a ""safe graphics"" shortcut (use if the window comes up black)"; \
  GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\George\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\George.exe"; \
  IconFilename: "{app}\George.exe"; AppUserModelID: "{#AppId}"
Name: "{group}\{#AppName} (safe graphics)"; \
  Filename: "{app}\george-safe.cmd"; \
  IconFilename: "{app}\George.exe"; Tasks: safegraphics
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\George.exe"; \
  IconFilename: "{app}\George.exe"; AppUserModelID: "{#AppId}"; \
  Tasks: desktopicon

[Run]
Filename: "{app}\George.exe"; Description: "Start {#AppName} now"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; The cache is ours and regenerates; config, notes and chats are HIS and
; stay put. An uninstaller that silently deletes a year of notes is a
; bug, not a feature.
Type: filesandordirs; Name: "{localappdata}\George\cache"

[Code]
function OllamaInstalled(): Boolean;
var
  Names: TArrayOfString;
  I: Integer;
begin
  Result := False;
  SetArrayLength(Names, 3);
  Names[0] := ExpandConstant('{localappdata}\Programs\Ollama\ollama.exe');
  Names[1] := ExpandConstant('{commonpf}\Ollama\ollama.exe');
  Names[2] := ExpandConstant('{userpf}\Ollama\ollama.exe');
  for I := 0 to GetArrayLength(Names) - 1 do
    if FileExists(Names[I]) then
    begin
      Result := True;
      Exit;
    end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrorCode: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    if not OllamaInstalled() then
    begin
      if MsgBox('George needs Ollama to think, and it is not installed yet.'
        + #13#10#13#10
        + 'Open the Ollama download page now?' + #13#10
        + 'Once it is installed, George starts and stops it for you.',
        mbConfirmation, MB_YESNO) = IDYES then
        ShellExec('open', 'https://ollama.com/download/windows', '', '',
                  SW_SHOW, ewNoWait, ErrorCode);
    end;
  end;
end;
