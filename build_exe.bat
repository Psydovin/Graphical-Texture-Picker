@echo off
echo Installing / updating build dependencies...
pip install pywebview pyinstaller --quiet

echo.
echo Building Graphical Texture Picker.exe ...
pyinstaller ^
  --name "Graphical Texture Picker" ^
  --noconsole ^
  --onedir ^
  --hidden-import webview.platforms.winforms ^
  --hidden-import clr ^
  --add-data "soh.png;." ^
  otr_picker_server.py

echo.
if exist "dist\Graphical Texture Picker\Graphical Texture Picker.exe" (
    echo Build succeeded: dist\Graphical Texture Picker\Graphical Texture Picker.exe
) else (
    echo Build FAILED - check output above.
)
pause
