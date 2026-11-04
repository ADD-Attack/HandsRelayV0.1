@echo off
title HandsRelay v2 Launcher (split mode)
setlocal
rem Most setups want the packaged app instead (build_app.bat builds
rem "HandsRelay v2.exe", which runs hands + gateway + parser in one window).
rem This launcher is for the split setup: the standalone hands app runs
rem separately and shows its per-session secret in its own window.
rem Review E9: ROOT is this script's folder (it used to assume
rem %USERPROFILE%\HandsRelayV0.1), and the secret is asked for (it used to
rem be scraped out of the retired v1 Downloads\cursor_relay.py).
set "ROOT=%~dp0"

set "HANDS_SECRET="
set /p "HANDS_SECRET=Paste the hands app's session secret (shown in its window): "
if "%HANDS_SECRET%"=="" (
  echo No hands secret given. This window will close.
  pause
  exit /b 1
)

rem Fresh gateway token for this launch only. It is printed at the top
rem of the gateway window; it is not saved anywhere.
powershell -NoProfile -Command "[guid]::NewGuid().ToString('N')" > "%TEMP%\handsrelay-v2-token.txt"
set /p GATEWAY_TOKEN=<"%TEMP%\handsrelay-v2-token.txt"
del "%TEMP%\handsrelay-v2-token.txt" >nul 2>&1

rem Review E9: the quoted set "NAME=value" form matters. The old
rem `set NAME=value && ...` kept the space before && in every value, so
rem the token and the hands secret never matched and HANDS_URL was invalid.
start "HandsRelay v2 - Parser (port 5011)" cmd /k "cd /d "%ROOT%services\parser" && python app.py"
start "HandsRelay v2 - Gateway (port 5010)" cmd /k "cd /d "%ROOT%services\gateway" && set "RELAY_GATEWAY_TOKEN=%GATEWAY_TOKEN%" && set "HANDS_URL=http://127.0.0.1:5000" && set "HANDS_SECRET=%HANDS_SECRET%" && set "PARSER_URL=http://127.0.0.1:5011" && echo Gateway token for this launch: %GATEWAY_TOKEN% && python app.py"

echo.
echo HandsRelay v2 starting: parser on port 5011, gateway on port 5010.
echo The hands must also be running - the standalone HandsRelay app.
echo If a service window shows "address already in use", that service was
echo already running - just close the duplicate window.
echo This launcher window will close in 8 seconds.
timeout /t 8 >nul
endlocal
