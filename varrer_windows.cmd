@echo off
rem varrer_windows.cmd - varredura automatica dos ativos favoritos do PocketAutonomo.
rem
rem Usa o Python de .venv, criado por preparar_windows.cmd, para rodar
rem     python -m pocketautonomo.scan --configure
rem O programa pede o link (se voce nao passar --url), abre o navegador para o seu login,
rem lista os favoritos que o seletor gravado encontra (ou pede o seletor) e passa por eles,
rem um de cada vez: clica so no favorito, confere o ativo selecionado, espera os dados e
rem le a cotacao e o payout com as fontes gravadas por observar_windows.cmd.
rem Sem --cycles, a varredura continua ate Ctrl+C. Exemplos:
rem     varrer_windows.cmd --cycles 2
rem     varrer_windows.cmd --interval 30
rem A varredura nao envia ordens, nao clica em BUY, SELL, CALL ou PUT e nao mexe no
rem Amount. Ela reutiliza runtime\ (perfil do navegador, seletores e CSV) sem apagar nada.
rem Funciona com dois cliques ou pelo terminal, de qualquer pasta: ele entra na pasta onde
rem esta. No final, a janela espera uma tecla, a menos que POCKETAUTONOMO_SEM_PAUSA exista.

setlocal EnableExtensions
set "CODIGO=1"
pushd "%~dp0" || goto :sem_pasta
if not exist ".venv\Scripts\python.exe" goto :sem_venv

".venv\Scripts\python.exe" -m pocketautonomo.scan --configure %*
set "CODIGO=%ERRORLEVEL%"
goto :fim

:sem_venv
echo [ERRO] Nao encontrei .venv\Scripts\python.exe nesta pasta.
echo Rode primeiro preparar_windows.cmd.
goto :fim

:sem_pasta
echo [ERRO] Nao foi possivel entrar na pasta do projeto: "%~dp0"
goto :pausa

:fim
popd
echo.
echo varrer_windows.cmd terminou com codigo %CODIGO%.
echo 0 = ao menos uma linha com dados gravada; 1 = falha ou nenhuma linha com dados;
echo 2 = link recusado ou configuracao incompleta; 130 = Ctrl+C.

:pausa
if not defined POCKETAUTONOMO_SEM_PAUSA pause
exit /b %CODIGO%
