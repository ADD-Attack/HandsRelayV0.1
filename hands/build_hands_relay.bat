@echo off
REM ============================================================
REM  Builds the STANDALONE hands app "HandsRelay.exe" (split setup:
REM  gateway and parser run elsewhere, e.g. under Docker) and puts
REM  it on your Desktop. Most setups want the packaged app instead:
REM  build_app.bat in the repo root builds "HandsRelay v2.exe".
REM
REM  Run it from this folder (hands\) of the repo checkout.
REM  It takes a few minutes.
REM
REM  Review E9: this used to build hands_relay_app.py with muse.ico,
REM  neither of which exists here, and did not bundle motion.py, which
REM  the hands load by path at startup.
REM ============================================================

cd /d %~dp0

if not exist hands_relay_hands.py (
    echo hands_relay_hands.py is not next to this file - run it from the repo's hands folder.
    goto :fail
)

echo Installing build tools and app dependencies...
python -m pip install --upgrade pyinstaller flask pyautogui pillow waitress
if errorlevel 1 goto :fail

rem The app looks for muse.ico beside itself; the repo ships muse-avatar.ico.
if not exist muse.ico if exist ..\muse-avatar.ico copy /Y ..\muse-avatar.ico muse.ico >nul

echo Building HandsRelay.exe ...
rem motion.py is loaded by path at runtime: bundle it under hands\ inside
rem the exe, where _load_motion() looks for it.
if exist muse.ico (
    python -m PyInstaller --onefile --noconsole --name "HandsRelay" --icon muse.ico --add-data "muse.ico;." --add-data "motion.py;hands" hands_relay_hands.py
) else (
    echo muse.ico not found - building without a custom icon.
    python -m PyInstaller --onefile --noconsole --name "HandsRelay" --add-data "motion.py;hands" hands_relay_hands.py
)
if errorlevel 1 goto :fail

copy /Y "dist\HandsRelay.exe" "%USERPROFILE%\Desktop\HandsRelay.exe" >nul

echo.
echo Done. "HandsRelay.exe" is on your Desktop.
echo Double-click it to start a session - no more terminals.
pause
exit /b 0

:fail
echo.
echo Something failed - take a screenshot of this window and send it to Muse.
pause
exit /b 1
