@echo off
setlocal enabledelayedexpansion

echo.
echo  Universal Radio Bridge v3.2
echo  ================================
echo.

:: --- Find game EXE directory (MS Store / WindowsApps) ---
set "GAME_EXE_DIR="
for /d %%F in ("C:\Program Files\WindowsApps\Microsoft.ForteBaseGame*") do (
    if exist "%%F\forzahorizon6.exe" set "GAME_EXE_DIR=%%F"
)
if "%GAME_EXE_DIR%"=="" (
    echo  [!] Game EXE not found in WindowsApps.
    echo  Enter full path to folder containing forzahorizon6.exe:
    set /p "GAME_EXE_DIR=Path: "
)
if not exist "%GAME_EXE_DIR%\forzahorizon6.exe" (
    echo  [ERROR] forzahorizon6.exe not found in: %GAME_EXE_DIR%
    pause & exit /b 1
)
echo  [OK] Game EXE: %GAME_EXE_DIR%

:: --- Find game data directory ---
set "GAME_DATA_DIR="
for %%D in (C D E F G H) do (
    if exist "%%D:\XboxGames\Forza Horizon 6\Content\forzahorizon6.exe" (
        set "GAME_DATA_DIR=%%D:\XboxGames\Forza Horizon 6\Content"
    )
)
if "%GAME_DATA_DIR%"=="" set "GAME_DATA_DIR=%GAME_EXE_DIR%"
echo  [OK] Game data: %GAME_DATA_DIR%

:: --- Find Python (always resolve to an absolute pythonw.exe path so the
::     proxy DLL can spawn it via CreateProcess without relying on PATH) ---
set "PYTHON_EXE="
:: Prefer per-user installs first (CPython 3.13/3.12/...)
for %%V in (313 312 311 310) do (
    if not defined PYTHON_EXE if exist "%LOCALAPPDATA%\Programs\Python\Python%%V\pythonw.exe" (
        set "PYTHON_EXE=%LOCALAPPDATA%\Programs\Python\Python%%V\pythonw.exe"
    )
)
:: System-wide installs C:\PythonXYZ, D:\PythonXYZ, ...
if not defined PYTHON_EXE for %%D in (C D E) do for %%V in (313 312 311 310) do (
    if not defined PYTHON_EXE if exist "%%D:\Python%%V\pythonw.exe" (
        set "PYTHON_EXE=%%D:\Python%%V\pythonw.exe"
    )
)
:: As a last resort, resolve the `py` launcher to a real interpreter path
if not defined PYTHON_EXE (
    py --version > nul 2>&1
    if not errorlevel 1 (
        for /f "usebackq tokens=*" %%P in (`py -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" 2^>nul`) do (
            if exist "%%P" set "PYTHON_EXE=%%P"
        )
        if not defined PYTHON_EXE (
            for /f "usebackq tokens=*" %%P in (`py -c "import sys;print(sys.executable)" 2^>nul`) do (
                if exist "%%P" set "PYTHON_EXE=%%P"
            )
        )
    )
)
:: Fall back to plain `python`/`python3` on PATH
if not defined PYTHON_EXE (
    for %%C in (python.exe python3.exe) do (
        if not defined PYTHON_EXE for /f "tokens=*" %%P in ('where %%C 2^>nul') do (
            if not defined PYTHON_EXE if exist "%%P" set "PYTHON_EXE=%%P"
        )
    )
)
if not defined PYTHON_EXE (
    echo  [ERROR] Python not found. Get it from https://python.org
    pause & exit /b 1
)
:py_ok
for /f "tokens=*" %%V in ('"%PYTHON_EXE%" --version 2^>^&1') do set "PYVER=%%V"
echo  [OK] %PYVER% at %PYTHON_EXE%

:: --- Install Python deps ---
:: Python 3.13 no longer supports `winsdk`; use PyWinRT (winrt-*) instead.
:: We install both — pip will skip whichever isn't compatible.
echo  [*] Installing dependencies...
"%PYTHON_EXE%" -m pip install --upgrade pip --quiet 2>nul
"%PYTHON_EXE%" -m pip install pyaudiowpatch pycaw comtypes --quiet 2>nul
"%PYTHON_EXE%" -m pip install winrt-runtime "winrt-Windows.Media.Control" "winrt-Windows.Foundation" "winrt-Windows.Foundation.Collections" --quiet 2>nul
if errorlevel 1 (
    echo  [!] PyWinRT install failed, falling back to legacy winsdk
    "%PYTHON_EXE%" -m pip install winsdk --quiet 2>nul
)
echo  [OK] Dependencies done

:: --- Check VB-Cable ---
reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Render" /s /f "CABLE" 2>nul | find "CABLE" > nul
if errorlevel 1 (
    echo.
    echo  [!] VB-Cable NOT installed.
    echo      Download FREE: https://vb-audio.com/Cable/
    echo      Required for virtual speaker feature.
    echo.
)

:: --- Check ffmpeg ---
ffmpeg -version > nul 2>&1
if errorlevel 1 (
    echo  [!] ffmpeg not found - install if you want DLNA stream relay
) else (
    echo  [OK] ffmpeg found
)

:: --- Copy files ---
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"

echo  [*] Taking ownership of game EXE folder...
takeown /f "%GAME_EXE_DIR%" /r /d y > nul 2>&1
icacls "%GAME_EXE_DIR%" /grant "%USERNAME%:(OI)(CI)F" /t /q > nul 2>&1

:: --- IMPORTANT: rename original version.dll to spotify_radio_bridge.dll ---
echo  [*] Setting up DLL chain...
if exist "%GAME_EXE_DIR%\version.dll" (
    :: Check if it's the original mod (large file) or our proxy (small)
    for %%F in ("%GAME_EXE_DIR%\version.dll") do set "ORIG_SIZE=%%~zF"
    if !ORIG_SIZE! GTR 100000 (
        :: It's the original mod DLL - rename it
        if not exist "%GAME_EXE_DIR%\spotify_radio_bridge.dll" (
            rename "%GAME_EXE_DIR%\version.dll" "spotify_radio_bridge.dll" 2>nul
            if errorlevel 1 (
                copy /Y "%GAME_EXE_DIR%\version.dll" "%GAME_EXE_DIR%\spotify_radio_bridge.dll" > nul
            )
            echo  [OK] Original mod: version.dll -> spotify_radio_bridge.dll
        ) else (
            echo  [OK] spotify_radio_bridge.dll already exists
        )
    ) else (
        echo  [OK] Our proxy already in place (small file)
    )
) else (
    :: No version.dll yet - copy original mod DLL
    if exist "%SCRIPT_DIR%\spotify_radio_bridge.dll" (
        copy /Y "%SCRIPT_DIR%\spotify_radio_bridge.dll" "%GAME_EXE_DIR%\spotify_radio_bridge.dll" > nul
        echo  [OK] spotify_radio_bridge.dll copied
    )
)

:: Copy our proxy version.dll
copy /Y "%SCRIPT_DIR%\version.dll" "%GAME_EXE_DIR%\version.dll" > nul
if errorlevel 1 (
    echo  [ERROR] Cannot copy version.dll - run as Administrator
    pause & exit /b 1
)
echo  [OK] version.dll (proxy) -> %GAME_EXE_DIR%\

:: Copy bridge script to data dir
if not exist "%GAME_DATA_DIR%\universal-radio" mkdir "%GAME_DATA_DIR%\universal-radio"
copy /Y "%SCRIPT_DIR%\universal-radio\universal_radio_bridge.py" "%GAME_DATA_DIR%\universal-radio\universal_radio_bridge.py" > nul
echo  [OK] universal_radio_bridge.py -> %GAME_DATA_DIR%\universal-radio\

:: Copy to EXE dir too
if not exist "%GAME_EXE_DIR%\universal-radio" mkdir "%GAME_EXE_DIR%\universal-radio" 2>nul
copy /Y "%SCRIPT_DIR%\universal-radio\universal_radio_bridge.py" "%GAME_EXE_DIR%\universal-radio\universal_radio_bridge.py" > nul 2>&1

:: Save Python path in both locations
echo %PYTHON_EXE%> "%GAME_DATA_DIR%\universal-radio\python_path.txt"
echo %PYTHON_EXE%> "%GAME_EXE_DIR%\universal-radio\python_path.txt" 2>nul
echo  [OK] Python path saved

:: --- Firewall ---
echo  [*] Firewall rules...
for %%R in (URB-UDP-1900 URB-UDP-5353 URB-TCP-8104 URB-TCP-8008) do (
    netsh advfirewall firewall delete rule name="%%R" > nul 2>&1
)
netsh advfirewall firewall add rule name="URB-UDP-1900" dir=in action=allow protocol=UDP localport=1900 > nul
netsh advfirewall firewall add rule name="URB-UDP-5353" dir=in action=allow protocol=UDP localport=5353 > nul
netsh advfirewall firewall add rule name="URB-TCP-8104" dir=in action=allow protocol=TCP localport=8104 > nul
netsh advfirewall firewall add rule name="URB-TCP-8008" dir=in action=allow protocol=TCP localport=8008 > nul
echo  [OK] Firewall rules added

echo.
echo  ================================
echo  Done! Restart Forza Horizon 6.
echo  ================================
echo.
echo  DLL chain: version.dll (proxy) -> spotify_radio_bridge.dll (original mod)
echo  Bridge starts automatically 5s after game launch.
echo  Dashboard: http://localhost:8104
echo.
echo  To use VB-Cable as FH6 Radio:
echo  In Spotify/Yandex/browser: change output to "CABLE Input"
echo  OR use App Routing buttons at http://localhost:8104
echo.
pause
