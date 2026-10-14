<#
  install.ps1 — устанавливает настройки mpv из этого репозитория в %APPDATA%\mpv

  ЧТО ДЕЛАЕТ
    Ставит файлы из папки mpv\ репозитория в %APPDATA%\mpv. Два режима:

    1. Копирование (по умолчанию)
       Файлы копируются. После обновления репозитория (git pull) скрипт
       нужно запустить ещё раз, иначе mpv продолжит работать со старыми копиями.

    2. Символические ссылки (ключ -Link)
       В %APPDATA%\mpv создаются ссылки на файлы репозитория. После git pull mpv
       сразу видит новую версию, повторно запускать скрипт не нужно.
       Требует включённого «Режима разработчика» Windows
       (Параметры → Система → Для разработчиков) или запуска PowerShell от администратора.
       Папку репозитория после этого нельзя перемещать или удалять — ссылки сломаются.

    В обоих режимах:
    - отличающиеся файлы в %APPDATA%\mpv перед заменой сохраняются в
      %APPDATA%\mpv\_backup_<дата-время>\ (одинаковые не трогаются);
    - старая версия скрипта (gopro_triage.*) убирается туда же, иначе mpv загрузит
      оба скрипта и каждая клавиша будет срабатывать дважды.

  КАК ЗАПУСТИТЬ
    Открыть PowerShell в папке репозитория (Shift + правый клик по папке →
    «Открыть окно PowerShell здесь», или cd в эту папку).

    Windows по умолчанию запрещает запуск скриптов .ps1. Разрешить только для
    текущего окна PowerShell (после закрытия окна запрет вернётся):

        Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

    Затем один из вариантов:

        .\install.ps1          # 1. копирование
        .\install.ps1 -Link    # 2. символические ссылки

    То же одной командой, без изменения политики:

        powershell -ExecutionPolicy Bypass -File .\install.ps1
        powershell -ExecutionPolicy Bypass -File .\install.ps1 -Link
#>
param([switch]$Link)

$ErrorActionPreference = "Stop"
$src    = Join-Path $PSScriptRoot "mpv"
$dst    = Join-Path $env:APPDATA "mpv"
$stamp  = Get-Date -Format "yyyyMMdd-HHmmss"
$backup = Join-Path $dst "_backup_$stamp"

function Backup-File($path) {
    $rel = $path.Substring($dst.Length).TrimStart('\')
    $target = Join-Path $backup $rel
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    Move-Item -Force $path $target
    Write-Host "  сохранено в бэкап: $rel"
}

New-Item -ItemType Directory -Force -Path $dst | Out-Null

# старая версия скрипта
foreach ($old in @("scripts\gopro_triage.lua", "script-opts\gopro_triage.conf")) {
    $p = Join-Path $dst $old
    if (Test-Path $p) { Backup-File $p }
}

Get-ChildItem -Path $src -Recurse -File | ForEach-Object {
    $rel    = $_.FullName.Substring($src.Length).TrimStart('\')
    $target = Join-Path $dst $rel
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null

    if (Test-Path $target) {
        $item = Get-Item $target
        $isLinkToUs = $item.LinkType -eq "SymbolicLink" -and $item.Target -contains $_.FullName
        if ($isLinkToUs) { Write-Host "= $rel (ссылка уже есть)"; return }
        if (-not $Link -and -not $item.LinkType -and
            (Get-FileHash $target).Hash -eq (Get-FileHash $_.FullName).Hash) {
            Write-Host "= $rel (без изменений)"; return
        }
        if ($item.LinkType) { Remove-Item -Force $target } else { Backup-File $target }
    }

    if ($Link) {
        New-Item -ItemType SymbolicLink -Path $target -Target $_.FullName | Out-Null
        Write-Host "→ $rel (ссылка)"
    } else {
        Copy-Item -Force $_.FullName $target
        Write-Host "+ $rel"
    }
}

Write-Host ""
Write-Host "Готово: $dst"
if (Test-Path $backup) { Write-Host "Бэкап прежних файлов: $backup" }
