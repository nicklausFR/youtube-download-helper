@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
    py -3 build_exe.py
    goto done
)
if exist "C:\Program Files\Python311\python.exe" (
    "C:\Program Files\Python311\python.exe" build_exe.py
    goto done
)
python build_exe.py
:done
if errorlevel 1 (
    echo Compilation echouee. Voir le message ci-dessus.
    pause
    exit /b 1
)
echo Compilation terminee : youtube-download-helper.exe
pause
