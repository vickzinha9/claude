@echo off
rem grafico_windows.cmd - leitura das velas do grafico do PocketAutonomo, sem cliques e sem ordens.
rem
rem Usa o Python de .venv, criado por preparar_windows.cmd, para rodar
rem     python -m pocketautonomo.chart --configure
rem O programa abre o navegador para o seu login e calibra a leitura das velas: acha as cores
rem das velas de alta e de baixa, pergunta o periodo de cada vela (60 = M1), le as velas e
rem mostra uma imagem com elas numeradas e os precos lidos, para voce comparar com o grafico.
rem Depois, le o grafico do ativo que esta na tela a cada poucos segundos, sem clicar, e mostra
rem a leitura exploratoria (alta, baixa ou indefinido, com os sinais) e a decisao validada
rem (CALL, PUT ou AGUARDAR). A leitura exploratoria nao e sinal de entrada. Exemplos:
rem     grafico_windows.cmd
rem     grafico_windows.cmd --count 12 --interval 10
rem O programa reutiliza runtime\ (perfil do navegador, seletores e velas gravadas) sem apagar
rem nada. Funciona com dois cliques ou pelo terminal, de qualquer pasta: ele entra na pasta onde
rem esta. No final, a janela espera uma tecla, a menos que POCKETAUTONOMO_SEM_PAUSA exista.

setlocal EnableExtensions
set "CODIGO=1"
pushd "%~dp0" || goto :sem_pasta
if not exist ".venv\Scripts\python.exe" goto :sem_venv

".venv\Scripts\python.exe" -m pocketautonomo.chart --configure %*
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
echo grafico_windows.cmd terminou com codigo %CODIGO%.
echo 0 = ao menos uma leitura das velas funcionou; 1 = nenhuma funcionou ou falha;
echo 2 = argumento invalido, calibracao cancelada ou configuracao incompleta; 130 = Ctrl+C.

:pausa
if not defined POCKETAUTONOMO_SEM_PAUSA pause
exit /b %CODIGO%
