@echo off
setlocal
cd /d "%~dp0"

echo === Python do projeto ===
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys; print(sys.executable); print(sys.version)"
  if errorlevel 1 (
    echo ERRO: a .venv existe, mas o Python interno nao inicia.
    echo Rode run_api.bat para recriar automaticamente o ambiente.
  )
) else (
  echo .venv nao encontrada.
)

echo.
echo === Dependencias principais ===
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import fastapi, sqlalchemy, uvicorn, pydantic, openai; print('OK: dependencias principais instaladas')"
  if errorlevel 1 (
    echo Dependencias ausentes ou ambiente quebrado. Rode run_api.bat.
  )
) else (
  echo Nao foi possivel verificar dependencias sem .venv.
)

echo.
echo === Dica ===
echo Para iniciar o app, execute: run_api.bat
pause
