@echo off
cd /d "%~dp0"
echo Starting Graphical Texture Picker Server on http://localhost:8765
echo.
python otr_picker_server.py
pause
