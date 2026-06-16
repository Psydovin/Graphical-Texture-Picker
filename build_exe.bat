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

    :: Strip any personal config/choices left over from testing this exe
    :: directly out of dist\ before it gets zipped for distribution.
    del /q "dist\Graphical Texture Picker\config.json" 2>nul
    del /q "dist\Graphical Texture Picker\choices.json" 2>nul
    del /q "dist\Graphical Texture Picker\choices.json.bak" 2>nul
    rmdir /s /q "dist\Graphical Texture Picker\master_output" 2>nul
    echo Cleaned any personal config.json / choices.json / master_output from dist\.
) else (
    echo Build FAILED - check output above.
)
pause
