@echo off
setlocal
set "ROOT=%~dp0"
set "VENV_PY=%ROOT%.venv\Scripts\python.exe"
set "SITE_PKGS=%ROOT%.venv\Lib\site-packages"
set "SRC_DIR=%ROOT%src"

if exist "%VENV_PY%" (
    "%VENV_PY%" -c "import ssl" >nul 2>&1
    if not errorlevel 1 (
        "%VENV_PY%" -m biliyt %*
        exit /b %ERRORLEVEL%
    )
)

set "PYTHONPATH=%SITE_PKGS%;%SRC_DIR%"
python -m biliyt %*
exit /b %ERRORLEVEL%
