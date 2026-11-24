@echo off
setlocal
cd /d "%~dp0"

set "PY_LAUNCHER=python"
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.11 or newer and retry.
  exit /b 1
)
py -3 --version >nul 2>nul
if not errorlevel 1 set "PY_LAUNCHER=py -3"

for /f "delims=" %%P in ('%PY_LAUNCHER% -c "import sys; print(sys.executable)"') do set "GLOBAL_PYTHON=%%P"
set "PYTHON_EXE="
"%GLOBAL_PYTHON%" -c "import importlib.util as u; required = ('mcp','uvicorn','starlette','PyInstaller','PySide6.QtWebEngineWidgets'); raise SystemExit(any(u.find_spec(n) is None for n in required))" >nul 2>nul
if not errorlevel 1 set "PYTHON_EXE=%GLOBAL_PYTHON%"

if not defined PYTHON_EXE (
  if not exist ".venv-build\Scripts\python.exe" (
    %PY_LAUNCHER% -m venv .venv-build
    if errorlevel 1 exit /b 1
  )
  set "PYTHON_EXE=%CD%\.venv-build\Scripts\python.exe"
)
"%PYTHON_EXE%" -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
  echo Python 3.11 or newer is required.
  exit /b 1
)
"%PYTHON_EXE%" -c "import importlib.util as u; required = ('mcp','uvicorn','starlette','PyInstaller','PySide6.QtWebEngineWidgets'); raise SystemExit(any(u.find_spec(n) is None for n in required))" >nul 2>nul
if errorlevel 1 (
  "%PYTHON_EXE%" -m pip install -r requirements.txt
  if errorlevel 1 exit /b 1
)

set "OUTPUT_DIR=%CD%\dist\company"
set "OUTPUT_NUMBER=1"
:choose_output_dir
if exist "%OUTPUT_DIR%" goto increment_output_dir
goto output_dir_ready
:increment_output_dir
set /a OUTPUT_NUMBER+=1
set "OUTPUT_DIR=%CD%\dist\company-%OUTPUT_NUMBER%"
goto choose_output_dir
:output_dir_ready
set "OUTPUT_TAG=company-%OUTPUT_NUMBER%"
mkdir "%OUTPUT_DIR%"
if errorlevel 1 exit /b 1

if not exist "build\%OUTPUT_TAG%\main" mkdir "build\%OUTPUT_TAG%\main"
if not exist "build\%OUTPUT_TAG%\hook" mkdir "build\%OUTPUT_TAG%\hook"

"%PYTHON_EXE%" -m PyInstaller --noconfirm --clean ^
  --distpath "%OUTPUT_DIR%" ^
  --workpath "build\%OUTPUT_TAG%\main" ^
  "%CD%\AgentBridge.spec"
if errorlevel 1 exit /b 1

"%PYTHON_EXE%" -m PyInstaller --noconfirm --clean --onedir --console ^
  --name AgentBridgeHook ^
  --icon "%CD%\assets\agentbridge_hook.ico" ^
  --distpath "%OUTPUT_DIR%" ^
  --workpath "build\%OUTPUT_TAG%\hook" ^
  --specpath "build\%OUTPUT_TAG%\hook" ^
  "%CD%\agentbridge_hook.py"
if errorlevel 1 exit /b 1

echo.
echo Built: %OUTPUT_DIR%\AgentBridge\AgentBridge.exe
echo Built: %OUTPUT_DIR%\AgentBridgeHook\AgentBridgeHook.exe
endlocal
