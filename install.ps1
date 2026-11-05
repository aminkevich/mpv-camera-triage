<#
  install.ps1 — installs the mpv settings from this repository into %APPDATA%\mpv

  WHAT IT DOES
    Puts the files from the repository's mpv\ folder into %APPDATA%\mpv. Two modes:

    1. Copy (default)
       The files are copied. After updating the repository (git pull) run the
       script again, otherwise mpv keeps using the old copies.

    2. Symbolic links (-Link switch)
       Links to the repository files are created in %APPDATA%\mpv. After git pull mpv
       sees the new version right away, no need to run the script again.
       Requires ONE of:
       - Windows "Developer Mode" turned on
         (Settings → System → For developers → Developer Mode), or
       - PowerShell run as administrator.
       The script first checks that links can be created and changes nothing if they cannot.
       Afterwards the repository folder must not be moved or deleted — the links would break.

    In both modes:
    - files in %APPDATA%\mpv that differ are saved before replacement to
      %APPDATA%\mpv\_backup_<date-time>\ (identical ones are left alone);
    - the old version of the script (gopro_triage.*) is moved there too, otherwise mpv loads
      both scripts and every key fires twice;
    - if DaVinci Resolve is installed, Workspace → Scripts gets the camera_triage entries
      (markers on clips from _triage.csv, tools\resolve_markers.py).

  HOW TO RUN
    Open PowerShell in the repository folder (Shift + right-click the folder →
    "Open PowerShell window here", or cd into the folder).

    By default Windows does not allow running .ps1 scripts. Allow it for the
    current PowerShell window only (the restriction returns when the window closes):

        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

    Then one of:

        .\install.ps1          # 1. copy
        .\install.ps1 -Link    # 2. symbolic links

    The same in one command, without changing the policy:

        powershell -ExecutionPolicy Bypass -File .\install.ps1
        powershell -ExecutionPolicy Bypass -File .\install.ps1 -Link
#>
param([switch]$Link)

$ErrorActionPreference = "Stop"
$src    = Join-Path $PSScriptRoot "mpv"
$dst    = Join-Path $env:APPDATA "mpv"
$stamp  = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = Join-Path $dst "_backup_$stamp"

# Symbolic link via mklink: unlike New-Item in Windows PowerShell 5.1,
# mklink works without administrator rights when "Developer Mode" is on.
function New-Symlink($path, $target) {
    $out = & cmd /c "mklink `"$path`" `"$target`" 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "mklink: $out" }
}

function Backup-File($path) {
    $rel = $path.Substring($dst.Length).TrimStart('\')
    $target = Join-Path $backup $rel
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    Move-Item -Force $path $target
    Write-Host "  saved to backup: $rel"
}

# Link mode: first check that links can be created at all — BEFORE any changes
if ($Link) {
    $probe = Join-Path $env:TEMP "camera_triage_linktest_$stamp"
    try { New-Symlink $probe (Join-Path $PSScriptRoot "README.md") } catch { }
    if (Test-Path $probe) {
        Remove-Item -Force $probe
    } else {
        Write-Host "Cannot create symbolic links — nothing was changed." -ForegroundColor Red
        Write-Host "Options:"
        Write-Host "  - turn on Developer Mode: Settings → System → For developers,"
        Write-Host "    then run .\install.ps1 -Link again"
        Write-Host "  - or run PowerShell as administrator and repeat"
        Write-Host "  - or install as copies: .\install.ps1"
        exit 1
    }
}

New-Item -ItemType Directory -Force -Path $dst | Out-Null

# old version of the script
foreach ($old in @("scripts\gopro_triage.lua", "script-opts\gopro_triage.conf")) {
    $p = Join-Path $dst $old
    if (Test-Path $p) { Backup-File $p }
}

Get-ChildItem -Path $src -Recurse -File | ForEach-Object {
    $rel    = $_.FullName.Substring($src.Length).TrimStart('\')
    $source = $_.FullName
    $target = Join-Path $dst $rel
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null

    if (Test-Path $target) {
        $item = Get-Item -Force $target
        if ($item.LinkType) {
            if ($Link -and ($item.Target -contains $source)) { Write-Host "= $rel (link already exists)"; return }
            Remove-Item -Force $target                     # old/foreign link — replace it
        } elseif ((Get-FileHash $target).Hash -eq (Get-FileHash $source).Hash) {
            if (-not $Link) { Write-Host "= $rel (unchanged)"; return }
            Remove-Item -Force $target                     # identical copy — no backup needed
        } else {
            Backup-File $target                            # differs — to the backup
        }
    }

    if ($Link) {
        New-Symlink $target $source
        Write-Host "→ $rel (link)"
    } else {
        Copy-Item -Force $source $target
        Write-Host "+ $rel"
    }
}

# DaVinci Resolve: Workspace → Scripts → camera_triage … (markers on clips from _triage.csv).
# Small launchers that run tools\resolve_markers.py from this repository (so the repository
# folder must stay where it is). Skipped if Resolve is not installed.
$resolveScripts = Join-Path $env:APPDATA "Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts"
if (Test-Path (Split-Path $resolveScripts)) {
    $utility = Join-Path $resolveScripts "Utility"
    New-Item -ItemType Directory -Force -Path $utility | Out-Null
    $tools = Join-Path $PSScriptRoot "tools"
    $entries = [ordered]@{
        "camera_triage - timeline markers" = "timeline"
        "camera_triage - media pool markers" = "mediapool"
        "camera_triage - remove markers" = "remove"
    }
    foreach ($name in $entries.Keys) {
        $launcher = @"
# Generated by install.ps1 (mpv-camera-triage): runs tools\resolve_markers.py from the repository.
import importlib, sys
sys.path.insert(0, r"$tools")
import triage_plan, make_timeline, resolve_markers
for module in (triage_plan, make_timeline, resolve_markers):
    importlib.reload(module)
resolve_markers.run(resolve, "$($entries[$name])")
"@
        $path = Join-Path $utility "$name.py"
        [System.IO.File]::WriteAllText($path, $launcher.Replace("`r`n", "`n"), (New-Object System.Text.UTF8Encoding $false))
        Write-Host "+ Resolve: Workspace → Scripts → $name"
    }
}

Write-Host ""
Write-Host "Done: $dst"
if (Test-Path $backup) { Write-Host "Backup of previous files: $backup" }
