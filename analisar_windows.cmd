@echo off
rem analisar_windows.cmd - analise local CALL/PUT das cotacoes gravadas pela varredura.
rem
rem Usa o Python de .venv, criado por preparar_windows.cmd, para rodar
rem     python -m pocketautonomo.analysis
rem O programa le os CSV da varredura em runtime\ (so as linhas com dados para analise),
rem testa regras fixas em dados separados e mostra, para cada ativo, CALL, PUT ou AGUARDAR,
rem com o motivo. Sem regra validada, a resposta e AGUARDAR. Exemplo:
rem     analisar_windows.cmd --horizon 60
rem A analise nao abre o navegador, nao envia ordens e nao usa IA paga. Ela so le runtime\,
rem sem gravar nem apagar nada. Funciona com dois cliques ou pelo terminal, de qualquer
rem pasta: ele entra na pasta onde esta. No final, a janela espera uma tecla, a menos que
rem POCKETAUTONOMO_SEM_PAUSA exista.

setlocal EnableExtensions
set "CODIGO=1"
pushd "%~dp0" || goto :sem_pasta
if not exist ".venv\Scripts\python.exe" goto :sem_venv

".venv\Scripts\python.exe" -m pocketautonomo.analysis %*
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
echo analisar_windows.cmd terminou com codigo %CODIGO%.
echo 0 = analise mostrada; 1 = falha na leitura dos CSV; 2 = argumento invalido.

:pausa
if not defined POCKETAUTONOMO_SEM_PAUSA pause
exit /b %CODIGO%
