<#
    install.ps1 — установщик camera_bot (Windows).
    Ничего не знает о логинах и данных: только ставит зависимости,
    разворачивает пустой конфиг из шаблона и (по желанию) регистрирует автозапуск.

    Запуск:
        powershell -ExecutionPolicy Bypass -File .\install.ps1
        powershell -ExecutionPolicy Bypass -File .\install.ps1 -RegisterAutostart
#>
param(
    [switch]$RegisterAutostart,
    [string]$TaskName = "camera_bot"
)

$ErrorActionPreference = "Stop"
$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
Write-Host "== camera_bot installer ==" -ForegroundColor Cyan
Write-Host "Каталог: $Base"

# 1. Python
$py = $null
foreach ($c in @("py -3", "python")) {
    try { & $c.Split(" ")[0] $c.Split(" ")[1] --version *>$null; $py = $c; break } catch {}
}
if (-not $py) { Write-Error "Python 3.10+ не найден в PATH. Установите: winget install Python.Python.3.12"; exit 1 }
Write-Host "Python: $py" -ForegroundColor Green

# 2. Зависимости
Write-Host "Установка зависимостей (requirements.txt)..." -ForegroundColor Cyan
& $py.Split(" ")[0] $py.Split(" ")[1] -m pip install --upgrade pip *>$null
& $py.Split(" ")[0] $py.Split(" ")[1] -m pip install -r (Join-Path $Base "requirements.txt")
Write-Host "Зависимости установлены." -ForegroundColor Green

# 3. Конфиг из шаблона (не перезаписываем существующий)
$cfg = Join-Path $Base "tg_bot_config.json"
$tpl = Join-Path $Base "config\tg_bot_config.example.json"
if (Test-Path $cfg) {
    Write-Host "tg_bot_config.json уже есть — не трогаю." -ForegroundColor Yellow
} else {
    Copy-Item $tpl $cfg
    Write-Host "Создан tg_bot_config.json из шаблона." -ForegroundColor Green
    Write-Host "  -> ВПИШИТЕ токен от @BotFather в поле token." -ForegroundColor Yellow
    Write-Host "  -> Google-функции опциональны: sheet_id/drive_folder_id + service-account.json рядом." -ForegroundColor Yellow
}

# 4. Быстрая проверка компиляции
Write-Host "Проверка компиляции модулей..." -ForegroundColor Cyan
& $py.Split(" ")[0] $py.Split(" ")[1] -m py_compile (Get-ChildItem $Base -Filter "*.py" | ForEach-Object { $_.FullName })
Write-Host "Компиляция OK." -ForegroundColor Green

# 5. Автозапуск (по флагу)
if ($RegisterAutostart) {
    $runbot = Join-Path $Base "run_bot.cmd"
    Write-Host "Регистрирую задачу планировщика '$TaskName' (ONLOGON)..." -ForegroundColor Cyan
    schtasks /create /tn $TaskName /tr "`"$runbot`"" /sc onlogon /f | Out-Null
    Write-Host "Готово. Запуск сейчас: schtasks /run /tn $TaskName" -ForegroundColor Green
} else {
    Write-Host "`nАвтозапуск не регистрировался. Чтобы включить, перезапустите с -RegisterAutostart." -ForegroundColor Yellow
    Write-Host "Разовый запуск: .\run_bot.cmd" -ForegroundColor Yellow
}

Write-Host "`nСледующий шаг: впишите token в tg_bot_config.json и напишите боту в Telegram — первый написавший станет владельцем." -ForegroundColor Cyan
