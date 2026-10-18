@echo off
rem continuo_windows.cmd - operacao SIMULADA do PocketAutonomo em modo continuo.
rem Nenhuma ordem vai para a Pocket Option e nenhum dinheiro e movimentado.
rem
rem Usa o Python de .venv, criado por preparar_windows.cmd, para rodar
rem     python -m pocketautonomo.operate --continuo
rem E a mesma operacao simulada de operar_windows.cmd, uma execucao depois da outra, ate
rem Ctrl+C ou ate voce fechar a janela do navegador. Na primeira, faca o login e pressione
rem Enter, como no operar_windows.cmd. Depois de uma falha do navegador, da sessao
rem desconectada ou de ciclos sem leitura completa, uma execucao nova abre o navegador de
rem novo e espera sozinha a tela de operacoes, so lendo a pagina, sem clicar; 5 execucoes
rem seguidas sem leitura completa, ou mais de 6 execucoes novas em 1 hora, param o modo
rem continuo. Ele nunca recomeca sozinho depois
rem de uma PAUSA (troca de ativo nao confirmada) ou de uma parada por seguranca: a PAUSA
rem fica gravada, e so voce a libera, digitando LIBERAR depois do login, quando abrir de
rem novo. Tudo o que aparece na janela tambem vai para um log, e o Windows nao suspende
rem enquanto ele roda. Exemplos:
rem     continuo_windows.cmd
rem     continuo_windows.cmd --configure
rem O programa reutiliza runtime\ (perfil do navegador, seletores, CSV, bancos, estado do
rem modo continuo e log) sem apagar nada.
rem Funciona com dois cliques ou pelo terminal, de qualquer pasta: ele entra na pasta onde
rem esta. No final, a janela espera uma tecla, a menos que POCKETAUTONOMO_SEM_PAUSA exista.

setlocal EnableExtensions
set "CODIGO=1"
pushd "%~dp0" || goto :sem_pasta
if not exist ".venv\Scripts\python.exe" goto :sem_venv

".venv\Scripts\python.exe" -m pocketautonomo.operate --continuo %*
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
echo continuo_windows.cmd terminou com codigo %CODIGO%.
echo 0 = voce fechou a janela do navegador, sem PAUSA e sem ciclo aberto;
echo 1 = PAUSA, parada por seguranca ou falhas seguidas, estado preservado;
echo 2 = argumento invalido ou configuracao incompleta; 130 = Ctrl+C.

:pausa
if not defined POCKETAUTONOMO_SEM_PAUSA pause
exit /b %CODIGO%
