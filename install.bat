@echo off
chcp 65001 >nul
echo ========================================
echo BalloonsTranslator - Install Dependencies
echo ========================================
echo.

:: Change to script directory
cd /d "%~dp0"

:: Check if Python is installed
echo [1/6] Checking Python installation...
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python is not installed or not in PATH!
    echo Please install Python from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version 2^>^&1') do set PYTHON_VERSION=%%i
echo Found: %PYTHON_VERSION%
echo.

:: Check if pip is installed
echo [2/6] Checking pip installation...
python -m pip --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: pip is not installed!
    echo Attempting to install pip...
    python -m ensurepip --upgrade
    if %ERRORLEVEL% NEQ 0 (
        echo ERROR: Could not install pip automatically!
        pause
        exit /b 1
    )
)
for /f "tokens=*" %%i in ('python -m pip --version 2^>^&1') do set PIP_VERSION=%%i
echo Found: %PIP_VERSION%
echo.

:: Upgrade pip and setuptools
echo [3/6] Upgrading pip and setuptools...
python -m pip install --upgrade pip setuptools wheel --disable-pip-version-check
echo.

:: Install packaging (required by launch.py for dependency checking)
echo [4/6] Installing packaging (required for dependency checking)...
python -m pip install packaging --disable-pip-version-check
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Failed to install packaging!
    pause
    exit /b 1
)
echo.

:: Detect GPU type for PyTorch installation
echo [5/6] Detecting GPU type for PyTorch installation...
set TORCH_COMMAND=
set IS_AMD=0

:: Check for AMD GPU on Windows
if /i "%OS%"=="Windows_NT" (
    echo Checking for AMD GPU...
    wmic path win32_VideoController get name 2>nul | findstr /i "AMD Radeon" >nul
    if %ERRORLEVEL% EQU 0 (
        set IS_AMD=1
        echo Detected: AMD GPU
        echo Will install PyTorch with CUDA 11.8 support
        echo Note: PyTorch with CUDA 11.8 for AMD GPUs
        set TORCH_COMMAND=python -m pip install torch==2.2.2 torchvision==0.17.2 torchaudio==2.2.2 --index-url https://download.pytorch.org/whl/cu118 --disable-pip-version-check
    ) else (
        echo Detected: NVIDIA GPU or CPU only
        echo Will install PyTorch with CUDA 12.8 support
        set TORCH_COMMAND=python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128 --disable-pip-version-check
    )
) else (
    echo Detected: Non-Windows system
    echo Will install PyTorch with CUDA 12.8 support
    set TORCH_COMMAND=python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128 --disable-pip-version-check
)

echo.
echo Installing PyTorch, torchvision, and torchaudio...
echo This may take several minutes. Please wait...
%TORCH_COMMAND%
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo WARNING: PyTorch installation may have failed!
    echo You can manually install PyTorch later using:
    echo   For AMD GPU: python -m pip install torch==2.2.2 torchvision==0.17.2 torchaudio==2.2.2 --index-url https://download.pytorch.org/whl/cu118
    echo   For NVIDIA/CPU: python -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
    echo   For installation guide, visit: https://pytorch.org/
    echo.
    echo Continuing with other dependencies...
)
echo.

:: Install requirements from requirements.txt
echo [6/6] Installing dependencies from requirements.txt...
if not exist "requirements.txt" (
    echo ERROR: requirements.txt not found!
    echo Please make sure you are running install.bat from the BalloonsTranslator directory.
    pause
    exit /b 1
)

echo Installing all required packages...
echo This may take several minutes depending on your internet connection. Please wait...
python -m pip install -r requirements.txt --prefer-binary --disable-pip-version-check --no-warn-script-location
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo WARNING: Some dependencies may have failed to install!
    echo You can try running the installation again:
    echo   install.bat
    echo.
    echo Or manually install requirements:
    echo   python -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
echo.

:: Final check
echo ========================================
echo Installation completed!
echo ========================================
echo.
echo You can now run the application using:
echo   launch_win.bat
echo.
echo Or directly with:
echo   python launch.py
echo.
pause

