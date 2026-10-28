@echo off
:: Build the Automated Test Suite into a distributable folder.
::
:: Output: dist\AutomatedTestSuite\AutomatedTestSuite.exe
::
:: Prerequisites:
::   pip install pyinstaller
::
:: To share: zip the entire dist\AutomatedTestSuite folder and send it.
:: Recipients do NOT need Python or any packages installed.

echo Building Automated Test Suite...
python -m PyInstaller app.spec --noconfirm
if %ERRORLEVEL% NEQ 0 (
    echo BUILD FAILED
    pause
    exit /b 1
)

echo.
echo Build complete!
echo Output: dist\AutomatedTestSuite\AutomatedTestSuite.exe
echo.
echo To distribute: zip the entire dist\AutomatedTestSuite\ folder.
pause
