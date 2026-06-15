@REM dependencies\libraries\py310\python.exe F:\repos\BallonsTranslator\ballontranslator
@REM @echo %PATH%

cd %~dp0

@echo off

:: Set the path for PaddleOCR and PyTorch libraries
set "PADDLE_PATH=%~dp0ballontrans_pylibs_win\Lib\site-packages\torch\lib"
set "PATH=%PADDLE_PATH%;%PATH%"

@REM if not defined PYTHON (set PATH=pylibs;pylibs\Scripts;%%PATH%%
set PATH=ballontrans_pylibs_win;ballontrans_pylibs_win\Scripts;PortableGit\cmd;%PATH%

@REM Locate a working Python 3.12 interpreter -- the version the dependencies are
@REM installed under. Try, in order: the bundled portable build (packaged release),
@REM the py launcher pinned to 3.12, then python.exe on PATH. We deliberately never fall
@REM back to a bare "py -3" / unpinned python: that resolves to 3.14 on this machine,
@REM which has none of the deps and would trigger a full reinstall. Each candidate is
@REM actually run before being accepted, so a broken Microsoft Store alias or a stale
@REM PATH that lacks Python falls through to the next option instead of aborting with
@REM "Couldn't launch python".
set "PYTHON="
call :try_python "%~dp0ballontrans_pylibs_win\python.exe"
if not defined PYTHON for /f "delims=" %%P in ('py -3.12 -c "import sys;print(sys.executable)" 2^>nul') do if not defined PYTHON call :try_python "%%P"
if not defined PYTHON for %%P in (python.exe) do if not defined PYTHON call :try_python "%%~$PATH:P"
if not defined PYTHON (
    echo Couldn't find a working Python 3.12 interpreter.
    echo Install Python 3.12 ^(so "py -3.12" works^) or add it to PATH, or place a portable build in ballontrans_pylibs_win.
    goto :endofscript
)
echo Using Python: %PYTHON%

set ERROR_REPORTING=FALSE

mkdir tmp 2>NUL

"%PYTHON%" -c "" >tmp/stdout.txt 2>tmp/stderr.txt
if %ERRORLEVEL% == 0 goto :check_pip
echo Couldn't launch python
goto :show_stdout_stderr

:check_pip
"%PYTHON%" -mpip --help >tmp/stdout.txt 2>tmp/stderr.txt
if %ERRORLEVEL% == 0 goto :launch
if "%PIP_INSTALLER_LOCATION%" == "" goto :show_stdout_stderr
"%PYTHON%" "%PIP_INSTALLER_LOCATION%" >tmp/stdout.txt 2>tmp/stderr.txt
if %ERRORLEVEL% == 0 goto :launch
echo Couldn't install pip
goto :show_stdout_stderr


:launch
"%PYTHON%" launch.py  %*
pause
exit /b


:show_stdout_stderr

echo.
echo exit code: %errorlevel%

for /f %%i in ("tmp\stdout.txt") do set size=%%~zi
if %size% equ 0 goto :show_stderr
echo.
echo stdout:
type tmp\stdout.txt

:show_stderr
for /f %%i in ("tmp\stderr.txt") do set size=%%~zi
if %size% equ 0 goto :show_stderr
echo.
echo stderr:
type tmp\stderr.txt

:endofscript

echo.
echo Launch unsuccessful. Exiting.
pause
goto :eof


:try_python
@REM Accept the given interpreter only if it exists and actually runs a trivial command.
@REM This filters out non-existent paths and the broken Microsoft Store "python.exe" alias
@REM (which exits non-zero), letting detection fall through to the next candidate.
if "%~1"=="" exit /b
if not exist "%~1" exit /b
"%~1" -c "" >nul 2>nul && set "PYTHON=%~1"
exit /b