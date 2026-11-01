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

rem F7 capture backend: Pillow's ImageGrab is the all-screens surface.
python -m pip install -r hands\requirements-capture.txt
if errorlevel 1 (
  echo Capture backend install failed. This window will close.
  pause
  exit /b 1
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

rem The app loads the three service modules from files at runtime, so
rem bundle them inside the exe: hands, gateway, and parser. The exe then
rem runs standalone from any folder — one icon, one Start button.
python -m PyInstaller --onefile --windowed --name "HandsRelay v2" --icon relay.ico --hidden-import pyautogui --hidden-import webview --hidden-import PIL.ImageGrab --add-data "ui;ui" --add-data "relay.ico;." --add-data "services\gateway\app.py;services\gateway" --add-data "services\parser\app.py;services\parser" --add-data "hands\hands_relay_hands.py;hands" --add-data "hands\motion.py;hands" --add-data "hands\claim_sync.py;hands" hands_relay_v2_app.py
if errorlevel 1 (
  echo Build failed. This window will close.
  pause
  exit /b 1
)

copy /Y "dist\HandsRelay v2.exe" "HandsRelay v2.exe" >nul
echo.
echo Built: %CD%\HandsRelay v2.exe
