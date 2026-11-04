@echo off
title Build HandsRelay v2 App
cd /d %~dp0

rem Install PyInstaller if it is not there yet.
python -c "import PyInstaller" 2>nul
if errorlevel 1 (
  echo Installing PyInstaller...
  python -m pip install pyinstaller
  if errorlevel 1 (
    echo PyInstaller install failed. This window will close.
    pause
    exit /b 1
  )
)

rem pyautogui drives the bundled hands; PyInstaller must be able to see it.
python -c "import pyautogui" 2>nul
if errorlevel 1 (
  echo Installing pyautogui...
  python -m pip install pyautogui
  if errorlevel 1 (
    echo pyautogui install failed. This window will close.
    pause
    exit /b 1
  )
)

rem pywebview is the native window shell around the relay's local web UI.
python -c "import webview" 2>nul
if errorlevel 1 (
  echo Installing pywebview...
  python -m pip install pywebview
  if errorlevel 1 (
    echo pywebview install failed. This window will close.
    pause
    exit /b 1
  )
)

rem Optional: pywin32 adds process names to /v1/windows. Focus and the
rem held-act window pin work without it (ctypes). PyInstaller bundles it
rem when installed because hands_relay_v2_app.py imports it optionally.
python -c "import win32gui" 2>nul
if errorlevel 1 (
  echo Installing pywin32 - optional, for process names in the window list...
  python -m pip install pywin32
  if errorlevel 1 echo pywin32 install failed - building without it.
)

rem The app loads the three service modules from files at runtime, so
rem bundle them inside the exe: hands, gateway, and parser. The exe then
rem runs standalone from any folder — one icon, one Start button.
rem NOTE: PyInstaller cannot see imports inside these data files; any new
rem third-party import in a service module must also be imported (or
rem optionally imported) in hands_relay_v2_app.py to be bundled.
python -m PyInstaller --onefile --windowed --name "HandsRelay v2" --icon relay.ico --hidden-import pyautogui --hidden-import webview --add-data "ui;ui" --add-data "relay.ico;." --add-data "services\gateway\app.py;services\gateway" --add-data "services\parser\app.py;services\parser" --add-data "hands\hands_relay_hands.py;hands" --add-data "hands\motion.py;hands" hands_relay_v2_app.py
if errorlevel 1 (
  echo Build failed. This window will close.
  pause
  exit /b 1
)

copy /Y "dist\HandsRelay v2.exe" "HandsRelay v2.exe" >nul
echo.
echo Built: %CD%\HandsRelay v2.exe
