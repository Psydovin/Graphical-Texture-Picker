@echo off
cd /d "%~dp0"
echo === OTR Pack Master ===
echo.

:: Find Python
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found in PATH.
    echo Install Python from https://python.org and make sure to check "Add to PATH".
    pause
    exit /b 1
)

:: Install mpyq if needed
python -c "import mpyq" >nul 2>&1
if errorlevel 1 (
    echo Installing mpyq...
    python -m pip install mpyq --quiet
)

:: Check if the picker app (dev script or packaged exe) is already running —
:: it has the master archive open for reading, which can lock the file.
call :CheckPickerRunning
if "%PICKER_RUNNING%"=="0" goto :picker_not_running
echo WARNING: Graphical Texture Picker appears to be running.
echo The output file may be locked and the build will fail.
echo.
echo Please close it, then press any key to continue.
pause >nul
echo.
call :CheckPickerRunning
if not "%PICKER_RUNNING%"=="0" (
    echo Still running. Exiting.
    pause
    exit /b 1
)
echo Closed. Continuing...
echo.
:picker_not_running

:: Run the packer
echo Running otr_pack_master.py...
echo.
python otr_pack_master.py
if errorlevel 1 (
    echo.
    echo Script failed. See error above.
    pause
    exit /b 1
)

:: Copy output to mods folder (destination comes from config.json's
:: master_dir, the same setting the picker UI's Settings panel writes)
set SRC=%~dp0master_output\999_Master.o2r
set SRC_NEW=%~dp0master_output\999_Master_new.o2r
set DEST_DIR=
for /f "delims=" %%i in ('python -c "import json; c=json.load(open('config.json')); g=c.get('games',{}); a=c.get('active_game','soh'); print(g.get(a,{}).get('master_dir',''))" 2^>nul') do set DEST_DIR=%%i

if "%DEST_DIR%"=="" (
    echo.
    echo WARNING: No output destination configured. Set it in the picker UI's
    echo Settings panel ^(Ship of Harkinian mods folder^), then re-run this.
    pause
    exit /b 1
)
set DEST=%DEST_DIR%\999_Master.o2r

if exist "%SRC_NEW%" (
    echo.
    echo Copying 999_Master_new.o2r to mods folder as 999_Master.o2r...
    copy /y "%SRC_NEW%" "%DEST%"
) else if exist "%SRC%" (
    echo.
    echo Copying 999_Master.o2r to mods folder...
    copy /y "%SRC%" "%DEST%"
) else (
    echo.
    echo WARNING: Could not find output file to copy.
)

echo.
echo Done! 999_Master.o2r is in your mods folder.
pause
exit /b 0

:CheckPickerRunning
set PICKER_RUNNING=0
for /f "delims=" %%i in ('powershell -NoProfile -Command "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*otr_picker_server.py*' } | Measure-Object).Count + (Get-Process 'Graphical Texture Picker' -ErrorAction SilentlyContinue | Measure-Object).Count"') do set PICKER_RUNNING=%%i
exit /b 0
