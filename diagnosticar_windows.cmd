@echo off
rem diagnosticar_windows.cmd - diagnostico A/B da captura do grafico do PocketAutonomo.
rem
rem Usa o Python de .venv, criado por preparar_windows.cmd, para rodar
rem     python -m pocketautonomo.capture_check
rem O programa pede o link (se voce nao passar --url), abre o navegador com o mesmo perfil
rem do coletor e, depois do Enter, so le a cotacao do ativo que esta na tela, pelo grafico,
rem com a fonte gravada por observar_windows.cmd. Sao quatro fases, cada uma comecada
rem com Enter: navegador visivel, coberto por este terminal maximizado, visivel de novo e
rem minimizado. No fim, ele mostra as medianas da etapa tela e a conclusao.
rem O diagnostico nao envia ordens, nao clica e nao troca de ativo. Ele grava so um CSV novo
rem em runtime\, sem apagar nada. Funciona com dois cliques ou pelo terminal, de qualquer
rem pasta: ele entra na pasta onde esta. No final, a janela espera uma tecla, a menos que
rem POCKETAUTONOMO_SEM_PAUSA exista.

setlocal EnableExtensions
set "CODIGO=1"
pushd "%~dp0" || goto :sem_pasta
if not exist ".venv\Scripts\python.exe" goto :sem_venv

".venv\Scripts\python.exe" -m pocketautonomo.capture_check %*
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
echo diagnosticar_windows.cmd terminou com codigo %CODIGO%.
echo 0 = diagnostico concluido; 1 = falha ou parada; 2 = link ou configuracao invalidos;
echo 130 = Ctrl+C.

:pausa
if not defined POCKETAUTONOMO_SEM_PAUSA pause
exit /b %CODIGO%
