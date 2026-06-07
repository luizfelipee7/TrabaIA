@echo off
echo Iniciando a API do Banco Simulado...
.\.venv\Scripts\uvicorn.exe app.main:app --reload
pause
