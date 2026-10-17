@echo off
rem Starts App Timer without a console window.
where pyw >nul 2>nul && (start "" pyw "%~dp0app_timer.pyw" & exit /b)
where pythonw >nul 2>nul && (start "" pythonw "%~dp0app_timer.pyw" & exit /b)
echo Python was not found on this PC.
echo Please install Python first - see SETUP.txt for the steps.
pause
