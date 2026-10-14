# ilias-sync für Windows installieren (PowerShell):
#   irm https://raw.githubusercontent.com/haucky/ilias-sync/main/install.ps1 | iex
#
# Lädt das fertige Programm von der neuesten GitHub-Release nach
# %LOCALAPPDATA%\Programs\ilias-sync und nimmt den Ordner in den PATH auf. Kein Python nötig.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # macht Invoke-WebRequest deutlich schneller

$repo  = 'haucky/ilias-sync'
$asset = 'ilias-sync-windows-x64.exe'
$dir   = Join-Path $env:LOCALAPPDATA 'Programs\ilias-sync'
$exe   = Join-Path $dir 'ilias-sync.exe'

New-Item -ItemType Directory -Force -Path $dir | Out-Null
Write-Host "Lade $asset ..."
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$tmp = "$exe.download"
Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/$repo/releases/latest/download/$asset" -OutFile $tmp
Move-Item -Force $tmp $exe
Unblock-File $exe

# Startmenü-Eintrag – Doppelklick richtet beim ersten Mal ein und synchronisiert danach
$lnk = Join-Path ([Environment]::GetFolderPath('Programs')) 'ILIAS-Sync.lnk'
$sc = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$sc.TargetPath = $exe
$sc.WorkingDirectory = $dir
$sc.Description = 'myILIAS-Kursdateien synchronisieren'
$sc.Save()

$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not (($userPath -split ';') -contains $dir)) {
    [Environment]::SetEnvironmentVariable('Path', ($userPath.TrimEnd(';') + ";$dir").TrimStart(';'), 'User')
    $env:Path += ";$dir"
}

Write-Host "OK - installiert: $exe ($(& $exe --version))" -ForegroundColor Green
Write-Host 'Startmenue: "ILIAS-Sync" (Doppelklick = einrichten bzw. synchronisieren)'
Write-Host 'Oder im Terminal:  ilias-sync setup'
