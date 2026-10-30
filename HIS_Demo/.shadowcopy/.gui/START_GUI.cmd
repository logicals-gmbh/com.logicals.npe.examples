@echo off
setlocal
where py >nul 2>nul
if not errorlevel 1 (
  py -3 "%~dp0his_gui.py"
  goto done
)
where python >nul 2>nul
if not errorlevel 1 (
  python "%~dp0his_gui.py"
  goto done
)
if exist "C:\LC3ENV\tools\python-3.11.4-win64\python.exe" (
  "C:\LC3ENV\tools\python-3.11.4-win64\python.exe" "%~dp0his_gui.py"
  goto done
)
echo Python 3.10 or later with Tkinter was not found.
:done
if errorlevel 1 pause
endlocal
