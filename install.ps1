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
       Нужен ОДИН из вариантов:
       - включён «Режим разработчика» Windows
         (Параметры → Система → Для разработчиков → Режим разработчика), или
       - PowerShell запущен от имени администратора.
       Скрипт сначала проверяет, можно ли создавать ссылки, и ничего не меняет, если нельзя.
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

# Символическая ссылка через mklink: в отличие от New-Item в Windows PowerShell 5.1,
# mklink работает без прав администратора, если включён «Режим разработчика».
function New-Symlink($path, $target) {
    $out = & cmd /c "mklink `"$path`" `"$target`" 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "mklink: $out" }
}

function Backup-File($path) {
    $rel = $path.Substring($dst.Length).TrimStart('\')
    $target = Join-Path $backup $rel
    New-Item -ItemType Directory -Force -Path (Split-Path $target) | Out-Null
    Move-Item -Force $path $target
    Write-Host "  сохранено в бэкап: $rel"
}

# Режим ссылок: сначала проверить, что ссылки вообще можно создавать, — ДО любых изменений
if ($Link) {
    $probe = Join-Path $env:TEMP "camera_triage_linktest_$stamp"
    try { New-Symlink $probe (Join-Path $PSScriptRoot "README.md") } catch { }
    if (Test-Path $probe) {
        Remove-Item -Force $probe
    } else {
        Write-Host "Нельзя создать символические ссылки — ничего не изменено." -ForegroundColor Red
        Write-Host "Варианты:"
        Write-Host "  - включить «Режим разработчика»: Параметры → Система → Для разработчиков,"
        Write-Host "    затем снова .\install.ps1 -Link"
        Write-Host "  - или запустить PowerShell от имени администратора и повторить"
        Write-Host "  - или поставить копиями: .\install.ps1"
        exit 1
    }
}

New-Item -ItemType Directory -Force -Path $dst | Out-Null

# старая версия скрипта
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
            if ($Link -and ($item.Target -contains $source)) { Write-Host "= $rel (ссылка уже есть)"; return }
            Remove-Item -Force $target                     # старая/чужая ссылка — заменить
        } elseif ((Get-FileHash $target).Hash -eq (Get-FileHash $source).Hash) {
            if (-not $Link) { Write-Host "= $rel (без изменений)"; return }
            Remove-Item -Force $target                     # та же копия — бэкап не нужен
        } else {
            Backup-File $target                            # отличается — в бэкап
        }
    }

    if ($Link) {
        New-Symlink $target $source
        Write-Host "→ $rel (ссылка)"
    } else {
        Copy-Item -Force $source $target
        Write-Host "+ $rel"
    }
}

Write-Host ""
Write-Host "Готово: $dst"
if (Test-Path $backup) { Write-Host "Бэкап прежних файлов: $backup" }
