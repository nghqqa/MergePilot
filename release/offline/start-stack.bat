@echo off
rem MergePilot offline stack launcher (two-phase: measure postgres bridge IP,
rem then start the full stack). ASCII + CRLF only - no chcp, no UTF-8 text.
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist .env (
  echo [1/3] placeholder .env ...
  > .env echo MERGEPILOT_RUN_ID=offline-demo
  >> .env echo MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES=0.0.0.0
)

echo [1/3] starting postgres to measure the bridge IP ...
docker compose up -d --no-deps postgres
if errorlevel 1 ( echo FAIL: docker compose up postgres & exit /b 1 )

rem wait for healthy
set /a tries=0
:waitpg
set /a tries+=1
if %tries% gtr 30 ( echo FAIL: postgres not healthy & exit /b 1 )
for /f %%i in ('docker inspect mergepilot-isolated-postgres-1 --format "{{.State.Health.Status}}" 2^>nul') do set H=%%i
if not "!H!"=="healthy" ( timeout /t 2 /nobreak >nul & goto waitpg )
echo postgres healthy

echo [2/3] measuring postgres bridge IP ...
for /f %%i in ('docker inspect mergepilot-isolated-postgres-1 --format "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"') do set PGIP=%%i
echo measured IP: !PGIP!
> .env echo MERGEPILOT_RUN_ID=offline-demo
>> .env echo MERGEPILOT_PG_EXPECTED_SERVER_ADDRESSES=!PGIP!

echo [3/3] starting full stack ...
docker compose up -d --no-build
if errorlevel 1 ( echo FAIL: docker compose up & exit /b 1 )

echo.
echo Done. preflight runs once; check:  docker compose ps -a
echo console:  http://127.0.0.1:8600     webhook health: http://127.0.0.1:8090/healthz
endlocal
