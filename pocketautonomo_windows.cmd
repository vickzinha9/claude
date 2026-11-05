@echo off
rem pocketautonomo_windows.cmd - abre o painel do PocketAutonomo: analise, simulacao e execucao real.
rem Antes, uma vez: preparar_windows.cmd (cria a .venv com Playwright, Chromium e o OCR local).
rem O painel abre o navegador do programa; o login e feito por voce nele, uma vez. A execucao real
rem comeca DESARMADA: so o botao ARMAR, com confirmacao, deixa uma ordem sair.
setlocal
pushd "%~dp0" || exit /b 1
if not exist ".venv\Scripts\pythonw.exe" goto :sem_venv
start "" ".venv\Scripts\pythonw.exe" -m pocketautonomo.app %*
popd
exit /b 0

:sem_venv
echo [ERRO] .venv nao encontrada: rode preparar_windows.cmd primeiro.
popd
if not defined POCKETAUTONOMO_SEM_PAUSA pause
exit /b 1
