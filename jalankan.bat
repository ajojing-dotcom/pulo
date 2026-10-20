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
rem Pilih Python yang punya paket siap pakai (3.13 paling aman).
set "PY="
for %%v in (3.13 3.12 3.11 3.14) do if not defined PY (py -%%v --version >nul 2>&1 && set "PY=py -%%v" && set "PYW=pyw -%%v")
if not defined PY (python --version >nul 2>&1 && set "PY=python" && set "PYW=pythonw")
if not defined PY (echo Python tidak ditemukan. Pasang Python 3.13 dari python.org, centang "Add python.exe to PATH". & pause & exit /b 1)
echo Memakai: %PY%
%PY% --version
%PY% -c "import sys; sys.exit(0 if sys.version_info < (3,15) else 1)" || (echo. & echo Python ini terlalu baru untuk paket Pulo. Pasang Python 3.13 dari python.org lalu jalankan lagi. & pause & exit /b 1)
echo Memasang paket (sekali saja). Bisa 5-15 menit...
%PY% -m pip install -r requirements.txt --progress-bar on
if errorlevel 1 (echo Gagal memasang paket. & pause & exit /b 1)
%PY% -m pip install -r requirements-musik.txt --only-binary=:all: --progress-bar on
if errorlevel 1 (echo Lewati: judul lagu tidak tampil, tombol musik tetap jalan.)
start "" %PYW% pulo.py
