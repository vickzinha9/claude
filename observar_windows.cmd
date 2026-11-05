@echo off
rem observar_windows.cmd - configura e testa o coletor somente leitura do PocketAutonomo.
rem
rem Usa o Python de .venv, criado por preparar_windows.cmd, para rodar
rem     python -m pocketautonomo.observe --configure --count 3
rem O programa pede o link (se voce nao passar --url), abre o navegador para o seu login,
rem pede as fontes do ativo, da cotacao do ativo e do payout, com previa, e faz 3
rem observacoes. A cotacao pode vir de um seletor, da busca em texto (buscar) ou da
rem leitura visual do marcador no grafico, com OCR local (ocr).
rem Argumentos extras vao para o programa, por exemplo: observar_windows.cmd --interval 10
rem O coletor nao envia ordens, nao clica em CALL/PUT e reutiliza runtime\ (perfil do
rem navegador, seletores e CSV) sem apagar nada.
rem Funciona com dois cliques ou pelo terminal, de qualquer pasta: ele entra na pasta onde
rem esta. No final, a janela espera uma tecla, a menos que POCKETAUTONOMO_SEM_PAUSA exista.

setlocal EnableExtensions
set "CODIGO=1"
pushd "%~dp0" || goto :sem_pasta
if not exist ".venv\Scripts\python.exe" goto :sem_venv

".venv\Scripts\python.exe" -m pocketautonomo.observe --configure --count 3 %*
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
echo observar_windows.cmd terminou com codigo %CODIGO%.
echo 0 = ao menos uma observacao valida; 1 = falha ou nenhuma observacao valida;
echo 2 = link recusado ou configuracao incompleta; 130 = Ctrl+C.

:pausa
if not defined POCKETAUTONOMO_SEM_PAUSA pause
exit /b %CODIGO%
