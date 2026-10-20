@echo off
cd /d "%~dp0"
if not exist "pulo.py" (
  echo File pulo.py tidak ditemukan di folder ini:
  echo %CD%
  echo.
  echo File .py yang ada di folder ini:
  dir /b *.py* 2^>nul
  echo.
  echo Ganti namanya jadi pulo.py, atau pindahkan pulo.py ke folder ini.
  pause
  exit /b 1
)
echo [1/4] Mencari Python...
rem Pilih Python yang punya paket siap pakai (3.13 paling aman).
set "PY="
for %%v in (3.13 3.12 3.11 3.14) do if not defined PY (py -%%v --version >nul 2>&1 && set "PY=py -%%v" && set "PYW=pyw -%%v")
if not defined PY (python --version >nul 2>&1 && set "PY=python" && set "PYW=pythonw")
if not defined PY (echo Python tidak ditemukan. Pasang Python 3.13 dari python.org, centang "Add python.exe to PATH". & pause & exit /b 1)
echo Memakai: %PY%
%PY% --version
%PY% -c "import sys; sys.exit(0 if sys.version_info < (3,15) else 1)" || (echo. & echo Python ini terlalu baru untuk paket Pulo. Pasang Python 3.13 dari python.org lalu jalankan lagi. & pause & exit /b 1)
echo.
echo [2/4] Memasang PySide6 dan PyInstaller. Bisa 5-15 menit...
%PY% -m pip install -r requirements.txt pyinstaller --progress-bar on
if errorlevel 1 (echo Gagal memasang paket. Cek internet lalu coba lagi. & pause & exit /b 1)
echo.
echo [3/4] Memasang pembaca judul lagu (opsional)...
%PY% -m pip install -r requirements-musik.txt --only-binary=:all: --progress-bar on
if errorlevel 1 (echo Lewati: judul lagu tidak tampil, tombol musik tetap jalan.)
echo.
echo [4/4] Merakit Pulo.exe, sekitar 1-3 menit...
set "ICON="
if exist "pulo.ico" set "ICON=--icon pulo.ico"
%PY% -m PyInstaller --noconsole --onefile --name Pulo %ICON% --collect-submodules winrt pulo.py
if errorlevel 1 (echo Gagal merakit exe. Salin pesan di atas lalu kirim. & pause & exit /b 1)
if not exist "dist\Pulo.exe" (echo Pulo.exe tidak ditemukan. Salin pesan di atas lalu kirim. & pause & exit /b 1)
echo.
echo Selesai: dist\Pulo.exe
echo.
echo Membuat shortcut di Desktop dan Start Menu...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0buat-shortcut.ps1"
pause
