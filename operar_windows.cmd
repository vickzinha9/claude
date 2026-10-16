@echo off
rem operar_windows.cmd - operacao SIMULADA do PocketAutonomo: analise, coordenador e
rem executor simulado. Nenhuma ordem vai para a Pocket Option e nenhum dinheiro e movimentado.
rem
rem Usa o Python de .venv, criado por preparar_windows.cmd, para rodar
rem     python -m pocketautonomo.operate --configure
rem O programa abre o navegador para o seu login e faz a varredura dos favoritos. Depois de
rem cada visita, a analise local responde CALL, PUT ou AGUARDAR. So uma decisao validada,
rem atual e com payout de 90% ou mais inicia um ciclo: R$ 5, R$ 10, R$ 20 e R$ 40, no mesmo
rem ativo e direcao, num simulador deste computador. Durante o ciclo, a varredura para no
rem ativo do ciclo, sem cliques. Os estados aparecem no terminal; Ctrl+C encerra.
rem Exemplos:
rem     operar_windows.cmd --demonstracao
rem     operar_windows.cmd --cycles 2
rem --demonstracao roda cenas com dados ARTIFICIAIS, sem navegador. O programa reutiliza
rem runtime\ (perfil do navegador, seletores, CSV e bancos) sem apagar nada.
rem Funciona com dois cliques ou pelo terminal, de qualquer pasta: ele entra na pasta onde
rem esta. No final, a janela espera uma tecla, a menos que POCKETAUTONOMO_SEM_PAUSA exista.

setlocal EnableExtensions
set "CODIGO=1"
pushd "%~dp0" || goto :sem_pasta
if not exist ".venv\Scripts\python.exe" goto :sem_venv

".venv\Scripts\python.exe" -m pocketautonomo.operate --configure %*
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
echo operar_windows.cmd terminou com codigo %CODIGO%.
echo 0 = concluido (ou demonstracao conferida); 1 = falha ou parada, estado preservado;
echo 2 = argumento invalido ou configuracao incompleta; 130 = Ctrl+C.

:pausa
if not defined POCKETAUTONOMO_SEM_PAUSA pause
exit /b %CODIGO%
