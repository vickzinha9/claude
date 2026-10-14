@echo off
rem preparar_windows.cmd - prepara o coletor somente leitura do PocketAutonomo no Windows.
rem
rem 1. Procura um Python 3.10 ou mais novo, de 64 bits: py -3.12, py -3 e python, nessa ordem.
rem 2. Com esse Python, roda "python -m pocketautonomo.setup_observer", que cria ou reutiliza
rem    .venv, instala a versao do Playwright definida em pocketautonomo\observe.py
rem    (PLAYWRIGHT_REQUIREMENT) e o Chromium dele, e testa a instalacao.
rem Um candidato so e aceito se o teste dele terminar com codigo exatamente 0: um codigo
rem negativo, como o -1610612730 que o py devolve sem o Python pedido, tambem e falha.
rem Os candidatos e o Python escolhido rodam com "call", para que um py.cmd ou python.bat no
rem PATH (como os atalhos do pyenv-win) devolva o controle a este arquivo.
rem Cada etapa para com uma mensagem clara se falhar. Nada em runtime\ (perfil do navegador,
rem seletores e CSV) e lido ou alterado, e uma .venv existente nunca e apagada.
rem Funciona com dois cliques ou pelo terminal, de qualquer pasta: ele entra na pasta onde
rem esta. No final, a janela espera uma tecla, a menos que POCKETAUTONOMO_SEM_PAUSA exista.

setlocal EnableExtensions
set "CODIGO=1"
pushd "%~dp0" || goto :sem_pasta

set "PYTHON_CMD="
call :experimentar py -3.12
if not defined PYTHON_CMD call :experimentar py -3
if not defined PYTHON_CMD call :experimentar python
if not defined PYTHON_CMD goto :sem_python

echo Python escolhido: %PYTHON_CMD%
call %PYTHON_CMD% -m pocketautonomo.setup_observer
set "CODIGO=%ERRORLEVEL%"
goto :fim

:experimentar
call %* -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) and sys.maxsize > 2**32 else 1)" <nul >nul 2>&1
if "%ERRORLEVEL%"=="0" set "PYTHON_CMD=%*"
if not "%ERRORLEVEL%"=="0" echo %*: nao serve (codigo %ERRORLEVEL%).
exit /b 0

:sem_python
echo.
echo [ERRO] Nenhum Python 3.10 ou mais novo, de 64 bits, foi encontrado.
echo Procurei: py -3.12, py -3 e python.
echo Instale o Python 3.12 de 64 bits de https://www.python.org/downloads/windows/
echo e rode preparar_windows.cmd de novo.
goto :fim

:sem_pasta
echo [ERRO] Nao foi possivel entrar na pasta do projeto: "%~dp0"
goto :pausa

:fim
popd
echo.
if "%CODIGO%"=="0" echo preparar_windows.cmd terminou: tudo pronto. Agora rode observar_windows.cmd.
if not "%CODIGO%"=="0" echo preparar_windows.cmd falhou com codigo %CODIGO%. Leia a mensagem acima.

:pausa
if not defined POCKETAUTONOMO_SEM_PAUSA pause
exit /b %CODIGO%
