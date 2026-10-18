"""Prepare the read-only collector: python -m pocketautonomo.setup_observer.

preparar_windows.cmd runs this module with the Python that it found. The work happens in
the current directory, the project folder:

1. this Python must be 3.11 or newer, because onnxruntime 1.31.0, the OCR's engine, has
   no package for older versions, and, on Windows, 64 bits, because greenlet, which
   Playwright needs, ships ready-made Windows packages only for 64 bits;
2. the virtual environment .venv is created, or reused when its Python works and meets the
   same rule. An unusable .venv is never deleted or replaced: the message says what to do;
3. the pip of .venv installs PLAYWRIGHT_REQUIREMENT, the Playwright version pinned in
   pocketautonomo/observe.py, and OCR_REQUIREMENTS, the local OCR pinned in
   pocketautonomo/quote_ocr.py (rapidocr, with its models, and onnxruntime);
4. that Playwright installs its Chromium (python -m playwright install chromium);
5. .venv must import that Playwright version, its Chromium must start without a window
   and open a blank page, without visiting any site, and the OCR must read 1.26543 in an
   image drawn here.

Each step stops the run with a clear message when it fails. Nothing here reads or writes
runtime/, where the collector keeps the browser profile, the selectors and the CSV files.

Exit codes: 0 when the collector is ready and 1 when a step failed.
"""

import argparse
import os
import subprocess
import sys
import venv
from dataclasses import dataclass
from pathlib import Path

from pocketautonomo.observe import PLAYWRIGHT_REQUIREMENT
from pocketautonomo.quote_ocr import OCR_REQUIREMENTS

VENV_DIR = Path(".venv")  # relative to the current directory, the project folder
MIN_PYTHON = (3, 11)
PLAYWRIGHT_VERSION = PLAYWRIGHT_REQUIREMENT.split("==", 1)[1]
PYTHON_DOWNLOAD = "https://www.python.org/downloads/windows/"

INTERPRETER_CHECK = (
    "import sys; print('%d.%d.%d' % sys.version_info[:3], 64 if sys.maxsize > 2**32 else 32)"
)
PLAYWRIGHT_CHECK = (
    "from importlib.metadata import version; import playwright.sync_api;"
    " print(version('playwright'))"
)
OCR_SAMPLE = "1.26543"
OCR_CHECK = """\
from PIL import Image, ImageDraw, ImageFont
from pocketautonomo.quote_ocr import RapidOcrEngine
from pocketautonomo.quote_reader import GrayImage
picture = Image.new("L", (180, 40), 255)
font = ImageFont.load_default(size=24)
ImageDraw.Draw(picture).text((10, 20), "%s", font=font, fill=0, anchor="lm")
gray = GrayImage(picture.width, picture.height, picture.tobytes())
text, score = RapidOcrEngine().recognize(gray)
print(text, round(score, 3))
""" % OCR_SAMPLE
VC_REDIST = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
CHROMIUM_CHECK = """\
from playwright.sync_api import sync_playwright
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    browser.new_page().close()
    browser.close()
"""


@dataclass(frozen=True)
class Interpreter:
    version: tuple  # (major, minor, micro)
    bits: int
    executable: str

    def __str__(self):
        return f"{'.'.join(str(part) for part in self.version)} de {self.bits} bits"


class SetupFailed(Exception):
    """A step failed; the message says which one and what to do."""


def main(argv=None, *, run=subprocess.run, create_venv=None, interpreter=None, windows=None):
    """Prepare .venv and return the exit code.

    run, create_venv, interpreter and windows exist for tests. They replace subprocess.run,
    the creation of .venv by the venv module, the Python running this module and the
    check for Windows.
    """
    argparse.ArgumentParser(
        prog="python -m pocketautonomo.setup_observer",
        description=(
            "Prepara o coletor somente leitura: cria ou reutiliza .venv, instala"
            f" {PLAYWRIGHT_REQUIREMENT}, o Chromium dele e o OCR local"
            f" ({' e '.join(OCR_REQUIREMENTS)}) e testa a instalação. Não mexe em runtime/."
        ),
    ).parse_args(argv)
    interpreter = running_interpreter() if interpreter is None else interpreter
    windows = os.name == "nt" if windows is None else windows
    create_venv = create_with_venv if create_venv is None else create_venv
    say(
        f"PREPARAÇÃO do coletor somente leitura: .venv, {PLAYWRIGHT_REQUIREMENT}, o Chromium"
        f" dele e o OCR local ({' e '.join(OCR_REQUIREMENTS)}). Nada em runtime/ (perfil do"
        " navegador, seletores e CSV) é lido ou alterado."
    )
    try:
        check_interpreter(interpreter, windows)
        python = prepare_venv(VENV_DIR, interpreter, run, create_venv, windows)
        run_step(
            run,
            f"Instalando {PLAYWRIGHT_REQUIREMENT} em {VENV_DIR}",
            [python, "-m", "pip", "install", "--disable-pip-version-check", PLAYWRIGHT_REQUIREMENT],
            f"a instalação de {PLAYWRIGHT_REQUIREMENT} falhou",
            "Veja a mensagem do pip acima, confira a conexão com a internet e rode de novo; se"
            f" o erro continuar, apague a pasta {VENV_DIR} e rode mais uma vez.",
        )
        run_step(
            run,
            f"Instalando o OCR local ({' e '.join(OCR_REQUIREMENTS)}) em {VENV_DIR}",
            [python, "-m", "pip", "install", "--disable-pip-version-check", *OCR_REQUIREMENTS],
            "a instalação do OCR local falhou",
            "Veja a mensagem do pip acima, confira a conexão com a internet e rode de novo; se"
            f" o erro continuar, apague a pasta {VENV_DIR} e rode mais uma vez.",
        )
        run_step(
            run,
            "Instalando o Chromium do Playwright (python -m playwright install chromium)",
            [python, "-m", "playwright", "install", "chromium"],
            "a instalação do Chromium do Playwright falhou",
            "Veja a mensagem acima, confira a conexão com a internet e o espaço em disco e"
            " rode de novo.",
        )
        check_installation(run, python)
        check_ocr(run, python)
    except SetupFailed as error:
        warn(f"FALHA: {error}")
        return 1
    say(
        f"Pronto: {VENV_DIR} tem o Playwright {PLAYWRIGHT_VERSION}, o Chromium dele abriu sem"
        f" janela uma página em branco e o OCR local leu {OCR_SAMPLE} numa imagem de teste."
    )
    say(
        "Próximo passo: observar_windows.cmd, que pede o link, abre o navegador para o seu"
        " login e configura os seletores."
    )
    return 0


def running_interpreter():
    bits = 64 if sys.maxsize > 2**32 else 32
    return Interpreter(tuple(sys.version_info[:3]), bits, sys.executable)


def interpreter_problem(interpreter, windows):
    """Why interpreter cannot run the collector, or None when it can."""
    if interpreter.version[:2] < MIN_PYTHON:
        return (
            "o coletor exige Python 3.11 ou mais novo, porque o onnxruntime 1.31.0 do OCR local"
            " não tem pacote para versões anteriores"
        )
    if windows and interpreter.bits != 64:
        return (
            "no Windows, o greenlet, de que o Playwright precisa, só tem pacote pronto de 64"
            " bits"
        )
    return None


def check_interpreter(interpreter, windows):
    problem = interpreter_problem(interpreter, windows)
    if problem is not None:
        raise SetupFailed(
            f"este Python é o {interpreter} ({interpreter.executable}), e {problem}. Instale"
            f" o Python 3.12 de 64 bits (py install 3.12, ou {PYTHON_DOWNLOAD}) e rode de novo."
        )
    say(f"Python {interpreter}: {interpreter.executable}")


def venv_python(path, windows):
    return path / "Scripts" / "python.exe" if windows else path / "bin" / "python"


def create_with_venv(path):
    venv.EnvBuilder(with_pip=True).create(str(path))


def prepare_venv(path, interpreter, run, create_venv, windows):
    """The Python of the .venv at path: a new one, or the existing one when it is usable."""
    python = venv_python(path, windows)
    if not path.exists():
        say(f"Criando {path} com o Python {interpreter}.")
        try:
            create_venv(path)
        except Exception as error:  # OSError, or CalledProcessError from ensurepip
            raise SetupFailed(
                f"não foi possível criar {path} ({type(error).__name__}: {error})."
                " Confira o espaço em disco e as permissões da pasta e rode de novo."
            ) from error
        if not python.exists():
            raise SetupFailed(f"{path} foi criado, mas sem {python}. Apague {path} e rode de novo.")
        return absolute(python)
    keep = f"A pasta {path} não foi apagada: apague-a você mesmo e rode de novo."
    if not python.exists():
        raise SetupFailed(f"a pasta {path} já existe, mas não tem {python}. {keep}")
    found = venv_interpreter(run, python)
    if found is None:
        raise SetupFailed(f"a pasta {path} já existe, mas o Python dela não funciona. {keep}")
    problem = interpreter_problem(found, windows)
    if problem is not None:
        raise SetupFailed(f"a pasta {path} já existe e usa o Python {found}, e {problem}. {keep}")
    say(f"Reutilizando {path}, com o Python {found}.")
    return absolute(python)


def absolute(path):
    """path made absolute without following links: in Linux, .venv/bin/python is a link to
    the base Python, which does not see the packages of .venv."""
    return Path(os.path.abspath(path))


def venv_interpreter(run, python):
    """The Interpreter of the existing .venv, or None when it does not run."""
    try:
        result = run(
            [str(python), "-c", INTERPRETER_CHECK], capture_output=True, text=True, errors="replace"
        )
    except OSError:
        return None
    try:
        version, bits = result.stdout.split()
        found = Interpreter(tuple(int(part) for part in version.split(".")), int(bits), str(python))
    except ValueError:
        return None
    return found if result.returncode == 0 else None


def run_step(run, description, command, failure, advice):
    """Run command with its output on the terminal; SetupFailed when it does not succeed."""
    say(f"{description}...")
    try:
        result = run([str(part) for part in command])
    except OSError as error:
        raise SetupFailed(f"{failure} ({error}). {advice}") from error
    if result.returncode != 0:
        raise SetupFailed(f"{failure} (código {result.returncode}). {advice}")


def check_installation(run, python):
    say(f"Conferindo o Playwright de {VENV_DIR}...")
    result = run(
        [str(python), "-c", PLAYWRIGHT_CHECK], capture_output=True, text=True, errors="replace"
    )
    installed = result.stdout.strip() if result.returncode == 0 else ""
    if installed != PLAYWRIGHT_VERSION:
        raise SetupFailed(
            f"{VENV_DIR} não importa o Playwright {PLAYWRIGHT_VERSION} (encontrado:"
            f" {installed or 'nenhum'}; {last_line(result.stderr)}). Rode de novo; se repetir,"
            f" apague {VENV_DIR} e rode mais uma vez."
        )
    say("Abrindo o Chromium sem janela, numa página em branco...")
    result = run(
        [str(python), "-c", CHROMIUM_CHECK], capture_output=True, text=True, errors="replace"
    )
    if result.returncode != 0:
        raise SetupFailed(
            f"o Chromium do Playwright não abriu ({last_line(result.stderr)}). Rode de novo,"
            " para repetir a instalação do Chromium."
        )


def check_ocr(run, python):
    say(f"Lendo {OCR_SAMPLE} com o OCR local, numa imagem desenhada aqui...")
    result = run([str(python), "-c", OCR_CHECK], capture_output=True, text=True, errors="replace")
    words = result.stdout.split() if result.returncode == 0 else []
    if words[:1] != [OCR_SAMPLE]:
        found = f"leu {words[0]!r}" if words else last_line(result.stderr)
        hint = ""
        if "DLL load failed" in (result.stderr or ""):
            hint = (
                " Falta uma biblioteca do Windows: instale o Microsoft Visual C++"
                f" Redistributable ({VC_REDIST})."
            )
        raise SetupFailed(
            f"o OCR local não leu {OCR_SAMPLE} na imagem de teste ({found}).{hint} Rode de"
            f" novo; se repetir, apague {VENV_DIR} e rode mais uma vez."
        )


def last_line(text):
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:300] if lines else "sem mensagem"


def say(text):
    print(text, flush=True)


def warn(text):
    print(text, file=sys.stderr, flush=True)


if __name__ == "__main__":
    sys.exit(main())
