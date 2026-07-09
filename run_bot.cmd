@echo off
rem Самоперезапускающаяся обёртка для camera_bot.py.
rem Переносимая: BASE = папка этого файла, python ищется автоматически.
setlocal enabledelayedexpansion
set "BASE=%~dp0"
if "%BASE:~-1%"=="\" set "BASE=%BASE:~0,-1%"

rem поиск интерпретатора: py -3 -> python -> явный PY из окружения
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
  echo [ERROR] Python не найден в PATH. Установите Python 3.10+ или задайте переменную PY.
  exit /b 9009
)

set FAST=0
:loop
for /f %%t in ('powershell -NoProfile -Command "[DateTimeOffset]::Now.ToUnixTimeSeconds()"') do set T0=%%t
echo [%date% %time%] starting camera_bot >> "%BASE%\bot_stderr.log"
%PY% "%BASE%\camera_bot.py" >> "%BASE%\bot_stderr.log" 2>&1
set EC=%errorlevel%
for /f %%t in ('powershell -NoProfile -Command "[DateTimeOffset]::Now.ToUnixTimeSeconds()"') do set T1=%%t
set /a LIFE=T1-T0
rem эскалация паузы при серии быстрых падений
if %LIFE% GEQ 60 (set FAST=0) else (set /a FAST+=1)
set PAUSE_S=3
if %FAST% GEQ 3 set PAUSE_S=30
if %FAST% GEQ 6 set PAUSE_S=300
if not exist "%BASE%\restarts.csv" echo ts;exit_code;life_s;pause_s>> "%BASE%\restarts.csv"
for /f %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-ddTHH:mm:ss"') do set TS=%%t
echo %TS%;%EC%;%LIFE%;%PAUSE_S%>> "%BASE%\restarts.csv"
echo [%date% %time%] camera_bot exited (code %EC%, life %LIFE%s), restart in %PAUSE_S%s >> "%BASE%\bot_stderr.log"
set /a PINGN=PAUSE_S+1
ping -n %PINGN% 127.0.0.1 >nul
goto loop
