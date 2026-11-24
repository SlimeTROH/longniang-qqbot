@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "OPENBLAS_NUM_THREADS=1"
for %%P in (".venv\Scripts\python.exe" "..\..\.venv\Scripts\python.exe" "..\..\..\venv\Scripts\python.exe") do (
  if exist "%%~P" (
    "%%~P" -c "import numpy" >nul 2>&1
    if not errorlevel 1 (
      "%%~P" -B app.py
      goto :done
    )
  )
)
py -3 -c "import numpy" >nul 2>&1
if not errorlevel 1 (
  py -3 -B app.py
  goto :done
)
python -c "import numpy" >nul 2>&1
if not errorlevel 1 (
  python -B app.py
  goto :done
)
echo Python 3.10+ and NumPy are required. Install with: py -3 -m pip install numpy
pause
exit /b 1
:done
if errorlevel 1 pause
