@echo off
setlocal
cd /d "%~dp0"
echo Iniciando a API do Banco Simulado...

if not exist ".venv\Scripts\python.exe" (
  echo Ambiente virtual nao encontrado. Criando .venv...
  py -3 -m venv .venv
)

echo Validando ambiente virtual...
".venv\Scripts\python.exe" -c "print('ok')" >nul 2>nul
if errorlevel 1 (
  echo Ambiente virtual corrompido ou inacessivel. Recriando .venv...
  rmdir /s /q ".venv"
  py -3 -m venv .venv
  if errorlevel 1 (
    echo Falha ao recriar a .venv. Verifique a instalacao do Python.
    pause
    exit /b 1
  )
)

echo Verificando dependencias...
".venv\Scripts\python.exe" -c "import fastapi, sqlalchemy, uvicorn" >nul 2>nul
if errorlevel 1 (
  echo Instalando dependencias do requirements.txt...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo Falha ao usar o Python da .venv.
    echo Se aparecer erro de WindowsApps ou "sessao de logon", apague a pasta .venv e recrie com:
    echo   py -3 -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
  )
)

echo Abrindo em http://127.0.0.1:8000/assistente
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
pause
