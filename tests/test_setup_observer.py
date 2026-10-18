"""Tests of the collector's preparation, pocketautonomo.setup_observer, and of the launchers.

FakeRun stands in for subprocess.run, so no virtual environment is created, nothing is
installed and no browser opens here. The .cmd launchers are checked as text everywhere.
On Windows, PreparerOnWindowsTest also runs preparar_windows.cmd with cmd.exe, in a copy of
the project whose setup_observer is a fake, with a fake py first on the PATH: it chooses a
Python and installs nothing. ScanLauncherOnWindowsTest runs varrer_windows.cmd with cmd.exe
in a copy of the project, without .venv and then with a .venv without pip, only for --help
and for a refused link: no browser opens. OperationLauncherOnWindowsTest does the same with
operar_windows.cmd, and also runs its --demonstracao, which needs no browser.
ContinuousLauncherOnWindowsTest runs continuo_windows.cmd for --help and refused arguments.
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pocketautonomo import setup_observer as setup

ROOT = Path(__file__).resolve().parent.parent
PY312 = setup.Interpreter((3, 12, 10), 64, "C:/Python312/python.exe")
STEPS = ["pip", "ocr-pip", "chromium", "playwright", "launch", "ocr"]


def kind_of(command):
    """Which step a command of setup_observer is."""
    if "-c" in command:
        code = command[command.index("-c") + 1]
        return {
            setup.INTERPRETER_CHECK: "interpreter",
            setup.PLAYWRIGHT_CHECK: "playwright",
            setup.CHROMIUM_CHECK: "launch",
            setup.OCR_CHECK: "ocr",
        }[code]
    if command[1:3] == ["-m", "pip"]:
        return "ocr-pip" if setup.OCR_REQUIREMENTS[0] in command else "pip"
    if command[1:4] == ["-m", "playwright", "install"]:
        return "chromium"
    raise AssertionError(f"comando inesperado: {command}")


class FakeRun:
    """Stands in for subprocess.run: answers each command by its kind and records it.

    An answer is (returncode, stdout, stderr), or an exception to raise.
    """

    ANSWERS = {
        "interpreter": (0, "3.12.10 64\n", ""),
        "pip": (0, "", ""),
        "chromium": (0, "", ""),
        "playwright": (0, f"{setup.PLAYWRIGHT_VERSION}\n", ""),
        "launch": (0, "", ""),
        "ocr-pip": (0, "", ""),
        "ocr": (0, "1.26543 0.999\n", ""),
    }

    def __init__(self, **answers):
        self.answers = {**self.ANSWERS, **answers}
        self.commands = []

    @property
    def kinds(self):
        return [kind_of(command) for command in self.commands]

    def __call__(self, command, **options):
        self.commands.append(command)
        answer = self.answers[kind_of(command)]
        if isinstance(answer, BaseException):
            raise answer
        returncode, stdout, stderr = answer
        captured = options.get("capture_output", False)
        return subprocess.CompletedProcess(command, returncode, stdout if captured else None, stderr if captured else None)


class SetupObserverTest(unittest.TestCase):
    """Each test works in its own project folder, with a runtime/ that must stay as it was."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.project = Path(directory.name)
        self.venv = self.project / ".venv"
        patcher = mock.patch.object(setup, "VENV_DIR", self.venv)
        patcher.start()
        self.addCleanup(patcher.stop)
        profile = self.project / "runtime" / "browser-profile"
        profile.mkdir(parents=True)
        (profile / "sessao.txt").write_text("perfil existente", encoding="utf-8")
        (self.project / "runtime" / "observe-selectors.json").write_text('{"asset": "#a"}', encoding="utf-8")
        self.runtime_before = self.runtime_files()

    def runtime_files(self):
        runtime = self.project / "runtime"
        return {str(path.relative_to(runtime)): path.read_bytes() for path in runtime.rglob("*") if path.is_file()}

    def make_venv(self, windows=False, with_python=True):
        python = setup.venv_python(self.venv, windows)
        python.parent.mkdir(parents=True)
        if with_python:
            python.write_text("", encoding="utf-8")
        return python

    def run_setup(self, run, interpreter=PY312, windows=False, create_error=None):
        self.created = []

        def create_venv(path):
            self.created.append(path)
            if create_error is not None:
                raise create_error
            self.make_venv(windows)

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = setup.main([], run=run, create_venv=create_venv, interpreter=interpreter, windows=windows)
        self.assertEqual(self.runtime_files(), self.runtime_before)  # runtime/ untouched
        return code, out.getvalue(), err.getvalue()

    def expected_commands(self, python):
        python = str(python)
        return [
            [python, "-m", "pip", "install", "--disable-pip-version-check", "playwright==1.64.0"],
            [python, "-m", "pip", "install", "--disable-pip-version-check", "rapidocr==3.10.0", "onnxruntime==1.31.0"],
            [python, "-m", "playwright", "install", "chromium"],
            [python, "-c", setup.PLAYWRIGHT_CHECK],
            [python, "-c", setup.CHROMIUM_CHECK],
            [python, "-c", setup.OCR_CHECK],
        ]

    def test_creates_the_venv_and_installs_the_pinned_playwright_and_chromium(self):
        for windows in (False, True):
            with self.subTest(windows=windows):
                run = FakeRun()
                code, out, err = self.run_setup(run, windows=windows)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(self.created, [self.venv])
                python = setup.absolute(setup.venv_python(self.venv, windows))
                self.assertEqual(run.commands, self.expected_commands(python))
                self.assertIn(("Scripts" if windows else "bin"), str(python))
                self.assertIn(f"Criando {self.venv} com o Python 3.12.10 de 64 bits.", out)
                self.assertIn("Pronto:", out)
                self.assertIn("Próximo passo: observar_windows.cmd", out)
                shutil.rmtree(self.venv)

    def test_reuses_a_working_venv(self):
        python = self.make_venv()
        run = FakeRun(interpreter=(0, "3.13.1 64\n", ""))
        code, out, err = self.run_setup(run)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.created, [])
        self.assertEqual(run.commands[0], [str(python), "-c", setup.INTERPRETER_CHECK])
        self.assertEqual(run.commands[1:], self.expected_commands(setup.absolute(python)))
        self.assertIn(f"Reutilizando {self.venv}, com o Python 3.13.1 de 64 bits.", out)

    def test_each_failing_step_stops_with_a_clear_message(self):
        old = setup.Interpreter((3, 10, 11), 64, "C:/Python310/python.exe")
        bits32 = setup.Interpreter((3, 12, 10), 32, "C:/Python312-32/python.exe")
        traceback = "Traceback (most recent call last):\nplaywright._impl._errors.Error: BrowserType.launch: Executable doesn't exist\n"
        # (case, interpreter, windows, existing .venv, FakeRun answers, message, steps run)
        cases = (
            ("old Python", old, False, None, {}, "exige Python 3.11 ou mais novo", []),
            ("32-bit Python on Windows", bits32, True, None, {}, "só tem pacote pronto de 64 bits", []),
            (".venv without Python", PY312, False, "empty", {}, "já existe, mas não tem", []),
            (".venv whose Python fails", PY312, False, "python", {"interpreter": (1, "", "erro")}, "o Python dela não funciona", ["interpreter"]),
            (".venv whose Python does not start", PY312, False, "python", {"interpreter": OSError("sem Python")}, "o Python dela não funciona", ["interpreter"]),
            (".venv with Python 3.10", PY312, False, "python", {"interpreter": (0, "3.10.11 64\n", "")}, "usa o Python 3.10.11 de 64 bits, e o coletor exige Python 3.11", ["interpreter"]),
            ("pip fails", PY312, False, None, {"pip": (1, "", "")}, "a instalação de playwright==1.64.0 falhou (código 1)", ["pip"]),
            ("pip does not start", PY312, False, None, {"pip": OSError("sem pip")}, "a instalação de playwright==1.64.0 falhou (sem pip)", ["pip"]),
            ("OCR pip fails", PY312, False, None, {"ocr-pip": (1, "", "")}, "a instalação do OCR local falhou (código 1)", ["pip", "ocr-pip"]),
            ("Chromium download fails", PY312, False, None, {"chromium": (1, "", "")}, "a instalação do Chromium do Playwright falhou (código 1)", ["pip", "ocr-pip", "chromium"]),
            ("another Playwright", PY312, False, None, {"playwright": (0, "1.63.0\n", "")}, "não importa o Playwright 1.64.0 (encontrado: 1.63.0", ["pip", "ocr-pip", "chromium", "playwright"]),
            ("Chromium does not start", PY312, False, None, {"launch": (1, "", traceback)}, "o Chromium do Playwright não abriu (playwright._impl._errors.Error: BrowserType.launch: Executable doesn't exist)", ["pip", "ocr-pip", "chromium", "playwright", "launch"]),
            ("OCR misreads", PY312, False, None, {"ocr": (0, "1.26548 0.91\n", "")}, "o OCR local não leu 1.26543 na imagem de teste (leu '1.26548')", STEPS),
            ("OCR DLL missing", PY312, False, None, {"ocr": (1, "", "ImportError: DLL load failed while importing onnxruntime_pybind11_state\n")}, "Microsoft Visual C++ Redistributable", STEPS),
        )
        for case, interpreter, windows, existing, answers, message, steps in cases:
            with self.subTest(case=case):
                if existing is not None:
                    self.make_venv(windows, with_python=existing == "python")
                run = FakeRun(**answers)
                code, out, err = self.run_setup(run, interpreter=interpreter, windows=windows)
                self.assertEqual(code, 1)
                self.assertIn("FALHA: ", err)
                self.assertIn(message, err)
                self.assertEqual(run.kinds, steps)  # no step after the failing one
                if existing is not None:
                    self.assertTrue(self.venv.is_dir())  # an existing .venv is never deleted
                    self.assertIn("não foi apagada", err)
                if self.venv.exists():
                    shutil.rmtree(self.venv)
        code, out, err = self.run_setup(FakeRun(), create_error=OSError("disco cheio"))
        self.assertEqual(code, 1)
        self.assertIn(f"FALHA: não foi possível criar {self.venv} (OSError: disco cheio).", err)


class LaunchersTest(unittest.TestCase):
    """The .cmd files as text; the Windows browser job of the CI runs them with cmd.exe."""

    def commands(self, name):
        """The lines of the launcher that cmd.exe runs, without its rem comments."""
        data = (ROOT / name).read_bytes()
        self.assertEqual(data.replace(b"\r\n", b"").count(b"\n"), 0, f"{name} needs CRLF line endings")
        text = data.decode("ascii")  # ASCII only: no accent depends on the console code page
        lines = [line.strip() for line in text.split("\r\n")]
        return [line for line in lines if line and not line.lower().startswith("rem")]

    def test_launchers_enter_their_folder_and_never_touch_runtime(self):
        for name in (
            "preparar_windows.cmd", "observar_windows.cmd", "varrer_windows.cmd", "analisar_windows.cmd",
            "diagnosticar_windows.cmd", "operar_windows.cmd", "continuo_windows.cmd",
        ):
            with self.subTest(name=name):
                commands = self.commands(name)
                self.assertEqual(commands[0], "@echo off")
                self.assertIn('pushd "%~dp0" || goto :sem_pasta', commands)
                self.assertEqual(commands[-1], "exit /b %CODIGO%")
                self.assertIn("if not defined POCKETAUTONOMO_SEM_PAUSA pause", commands)
                for line in commands:
                    words = line.lower().split()
                    self.assertFalse({"del", "erase", "rd", "rmdir", "move", "ren", "rename"} & set(words), line)
                    self.assertNotIn("runtime", line.lower(), line)
        prepare = self.commands("preparar_windows.cmd")
        for candidate in ("py -3.12", "py -3", "python"):
            self.assertIn(f"call :experimentar {candidate}", " ".join(prepare))
        self.assertIn("call %PYTHON_CMD% -m pocketautonomo.setup_observer", prepare)
        self.assertIn("sys.version_info >= (3, 11) and sys.maxsize > 2**32", " ".join(prepare))
        # A candidate is accepted only with exit code exactly 0: "if not errorlevel 1" also
        # accepted negative codes, such as the -1610612730 of py without Python 3.12.
        self.assertIn('if "%ERRORLEVEL%"=="0" set "PYTHON_CMD=%*"', prepare)
        self.assertNotIn("errorlevel 1", " ".join(prepare).lower())
        [probe] = [line for line in prepare if line.startswith("call %* -c ")]
        self.assertTrue(probe.endswith("<nul >nul 2>&1"), probe)  # no prompt can wait for input
        observe = self.commands("observar_windows.cmd")
        self.assertIn('".venv\\Scripts\\python.exe" -m pocketautonomo.observe --configure --count 3 %*', observe)
        scan = self.commands("varrer_windows.cmd")
        self.assertIn('".venv\\Scripts\\python.exe" -m pocketautonomo.scan --configure %*', scan)
        self.assertIn('set "CODIGO=%ERRORLEVEL%"', scan)
        analyze = self.commands("analisar_windows.cmd")
        self.assertIn('".venv\\Scripts\\python.exe" -m pocketautonomo.analysis %*', analyze)
        self.assertIn('set "CODIGO=%ERRORLEVEL%"', analyze)
        diagnose = self.commands("diagnosticar_windows.cmd")
        self.assertIn('".venv\\Scripts\\python.exe" -m pocketautonomo.capture_check %*', diagnose)
        self.assertIn('set "CODIGO=%ERRORLEVEL%"', diagnose)
        operate = self.commands("operar_windows.cmd")
        self.assertIn('".venv\\Scripts\\python.exe" -m pocketautonomo.operate --configure %*', operate)
        self.assertIn('set "CODIGO=%ERRORLEVEL%"', operate)
        continuous = self.commands("continuo_windows.cmd")
        self.assertIn('".venv\\Scripts\\python.exe" -m pocketautonomo.operate --continuo %*', continuous)
        self.assertIn('set "CODIGO=%ERRORLEVEL%"', continuous)



# The exit code that py, from the Python install manager, gave on the user's Windows for
# "py -3.12" without Python 3.12 (0xA0000006 as an unsigned number).
PY_WITHOUT_VERSION = -1610612730

FAKE_SETUP = """\
import sys
print("SETUP FALSO: %d.%d de %d bits" % (sys.version_info[0], sys.version_info[1], 64 if sys.maxsize > 2**32 else 32))
"""

# py.cmd runs this script with the real Python. The versions in FAKE_PY_SEM fail with the
# code of py without the requested Python; the others run the rest of the command line.
FAKE_PY = """\
import os, subprocess, sys
version = sys.argv[1] if len(sys.argv) > 1 else ""
if version in os.environ.get("FAKE_PY_SEM", "").split(","):
    sys.exit(%d)
if version in ("-3.12", "-3"):
    sys.exit(subprocess.call([sys.executable] + sys.argv[2:]))
sys.exit(103)
""" % PY_WITHOUT_VERSION


@unittest.skipUnless(os.name == "nt", "cmd.exe só existe no Windows")
class PreparerOnWindowsTest(unittest.TestCase):
    """preparar_windows.cmd with cmd.exe: which Python it chooses, with a fake py.

    The project is a copy holding only the launcher and a fake setup_observer, which prints
    the Python that ran it and installs nothing.
    """

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        self.project = base / "projeto"
        (self.project / "pocketautonomo").mkdir(parents=True)
        shutil.copy(ROOT / "preparar_windows.cmd", self.project)
        (self.project / "pocketautonomo" / "setup_observer.py").write_text(FAKE_SETUP, encoding="ascii")
        self.fake_bin = base / "py-falso"
        self.fake_bin.mkdir()
        (self.fake_bin / "fake_py.py").write_text(FAKE_PY, encoding="ascii")
        (self.fake_bin / "py.cmd").write_bytes(
            f'@"{sys.executable}" "%~dp0fake_py.py" %*\r\n'.encode("ascii")
        )
        self.elsewhere = base  # the launcher must enter its own folder from here

    def run_preparer(self, missing, python_on_path):
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        path = [str(self.fake_bin)]
        if python_on_path:
            path.append(str(Path(sys.executable).parent))
        path += [os.path.join(system_root, "System32"), system_root]
        hidden = ("PATH", "PYTHONPATH", "PYTHONHOME")
        env = {key: value for key, value in os.environ.items() if key.upper() not in hidden}
        env.update(PATH=os.pathsep.join(path), FAKE_PY_SEM=",".join(missing), POCKETAUTONOMO_SEM_PAUSA="1")
        result = subprocess.run(
            ["cmd", "/d", "/c", "call", str(self.project / "preparar_windows.cmd")],
            cwd=self.elsewhere,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="ascii",
            errors="replace",
            timeout=120,
        )  # text mode: the CRLF of cmd.exe becomes \n
        return result.returncode, result.stdout + result.stderr

    def test_a_negative_code_is_a_failure_and_the_next_candidate_is_tried(self):
        code, out = self.run_preparer(["-3.12"], python_on_path=False)
        self.assertEqual(code, 0, out)
        self.assertIn(f"py -3.12: nao serve (codigo {PY_WITHOUT_VERSION}).", out)
        self.assertNotIn("Python escolhido: py -3.12", out)
        self.assertIn("Python escolhido: py -3\n", out)
        self.assertIn("SETUP FALSO: ", out)  # it ran, from the project folder
        self.assertIn("tudo pronto", out)

    def test_python_is_the_last_candidate(self):
        code, out = self.run_preparer(["-3.12", "-3"], python_on_path=True)
        self.assertEqual(code, 0, out)
        self.assertIn(f"py -3.12: nao serve (codigo {PY_WITHOUT_VERSION}).", out)
        self.assertIn(f"py -3: nao serve (codigo {PY_WITHOUT_VERSION}).", out)
        self.assertIn("Python escolhido: python\n", out)
        self.assertIn("SETUP FALSO: ", out)

    def test_without_any_candidate_nothing_runs(self):
        code, out = self.run_preparer(["-3.12", "-3"], python_on_path=False)
        self.assertEqual(code, 1, out)
        self.assertIn("python: nao serve (codigo ", out)  # not on the PATH
        self.assertIn("[ERRO] Nenhum Python 3.11 ou mais novo, de 64 bits, foi encontrado.", out)
        self.assertNotIn("Python escolhido", out)
        self.assertNotIn("SETUP FALSO", out)
        self.assertIn("preparar_windows.cmd falhou com codigo 1.", out)

    def test_py_312_is_still_the_first_choice(self):
        code, out = self.run_preparer([], python_on_path=True)
        self.assertEqual(code, 0, out)
        self.assertIn("Python escolhido: py -3.12\n", out)
        self.assertNotIn("nao serve", out)


@unittest.skipUnless(os.name == "nt", "cmd.exe só existe no Windows")
class ScanLauncherOnWindowsTest(unittest.TestCase):
    """varrer_windows.cmd with cmd.exe, in a copy of the project: no browser opens here."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.project = self.base / "projeto"
        self.project.mkdir()
        shutil.copy(ROOT / "varrer_windows.cmd", self.project)
        shutil.copytree(ROOT / "pocketautonomo", self.project / "pocketautonomo", ignore=shutil.ignore_patterns("__pycache__"))

    def run_launcher(self, *args):
        env = {key: value for key, value in os.environ.items() if key.upper() not in ("PYTHONPATH", "PYTHONHOME")}
        env["POCKETAUTONOMO_SEM_PAUSA"] = "1"
        result = subprocess.run(
            ["cmd", "/d", "/c", "call", str(self.project / "varrer_windows.cmd"), *args],
            cwd=self.base,  # the launcher must enter its own folder from here
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="ascii",
            errors="replace",
            timeout=120,
        )
        return result.returncode, result.stdout + result.stderr

    def test_the_launcher_runs_the_scan_from_its_own_folder(self):
        code, out = self.run_launcher("--help")
        self.assertEqual(code, 1, out)
        self.assertIn("[ERRO] Nao encontrei .venv\\Scripts\\python.exe nesta pasta.", out)
        self.assertIn("varrer_windows.cmd terminou com codigo 1.", out)
        venv = subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(self.project / ".venv")],
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(venv.returncode, 0, venv.stderr)
        code, out = self.run_launcher("--help")
        self.assertEqual(code, 0, out)
        self.assertIn("usage: python -m pocketautonomo.scan", out)
        self.assertIn("--cycles", out)
        self.assertIn("varrer_windows.cmd terminou com codigo 0.", out)
        code, out = self.run_launcher("--url", "http://example.org/")
        self.assertEqual(code, 2, out)
        self.assertIn("varrer_windows.cmd terminou com codigo 2.", out)
        self.assertFalse((self.project / "runtime").exists())


@unittest.skipUnless(os.name == "nt", "cmd.exe só existe no Windows")
class AnalysisLauncherOnWindowsTest(unittest.TestCase):
    """analisar_windows.cmd with cmd.exe, in a copy of the project: it only reads runtime\\."""

    def test_the_launcher_shows_the_analysis_from_its_own_folder(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        project = base / "projeto"
        project.mkdir()
        shutil.copy(ROOT / "analisar_windows.cmd", project)
        shutil.copytree(ROOT / "pocketautonomo", project / "pocketautonomo", ignore=shutil.ignore_patterns("__pycache__"))
        env = {key: value for key, value in os.environ.items() if key.upper() not in ("PYTHONPATH", "PYTHONHOME")}
        env["POCKETAUTONOMO_SEM_PAUSA"] = "1"

        def run(*args):
            result = subprocess.run(
                ["cmd", "/d", "/c", "call", str(project / "analisar_windows.cmd"), *args],
                cwd=base, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                encoding="utf-8", errors="replace", timeout=120,
            )
            return result.returncode, result.stdout + result.stderr

        code, out = run()
        self.assertEqual(code, 1, out)
        self.assertIn("[ERRO] Nao encontrei .venv\\Scripts\\python.exe nesta pasta.", out)
        venv = subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(project / ".venv")],
            capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(venv.returncode, 0, venv.stderr)
        env["PYTHONIOENCODING"] = "utf-8"
        code, out = run()
        self.assertEqual(code, 0, out)
        self.assertIn("AGUARDAR: sem cotações utilizáveis.", out)
        self.assertIn("analisar_windows.cmd terminou com codigo 0.", out)
        code, out = run("--horizon", "5")
        self.assertEqual(code, 2, out)
        self.assertIn("analisar_windows.cmd terminou com codigo 2.", out)
        self.assertFalse((project / "runtime").exists())  # nothing was written


@unittest.skipUnless(os.name == "nt", "cmd.exe só existe no Windows")
class OperationLauncherOnWindowsTest(unittest.TestCase):
    """operar_windows.cmd with cmd.exe, in a copy of the project: only --help, a refused
    argument and --demonstracao, which needs no browser."""

    def test_the_launcher_runs_the_demonstration_from_its_own_folder(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        project = base / "projeto"
        project.mkdir()
        shutil.copy(ROOT / "operar_windows.cmd", project)
        shutil.copytree(ROOT / "pocketautonomo", project / "pocketautonomo", ignore=shutil.ignore_patterns("__pycache__"))
        env = {key: value for key, value in os.environ.items() if key.upper() not in ("PYTHONPATH", "PYTHONHOME")}
        env.update(POCKETAUTONOMO_SEM_PAUSA="1", PYTHONIOENCODING="utf-8")

        def run(*args):
            result = subprocess.run(
                ["cmd", "/d", "/c", "call", str(project / "operar_windows.cmd"), *args],
                cwd=base, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                encoding="utf-8", errors="replace", timeout=300,
            )
            return result.returncode, result.stdout + result.stderr

        code, out = run("--demonstracao")
        self.assertEqual(code, 1, out)
        self.assertIn("[ERRO] Nao encontrei .venv\\Scripts\\python.exe nesta pasta.", out)
        venv = subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(project / ".venv")],
            capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(venv.returncode, 0, venv.stderr)
        code, out = run("--help")
        self.assertEqual(code, 0, out)
        self.assertIn("usage: python -m pocketautonomo.operate", out)
        code, out = run("--gale-window", "0")
        self.assertEqual(code, 2, out)
        self.assertFalse((project / "runtime").exists())
        code, out = run("--demonstracao")
        self.assertEqual(code, 0, out)
        self.assertIn("Demonstração concluída: as 8 cenas passaram nas conferências", out)
        self.assertIn("operar_windows.cmd terminou com codigo 0.", out)
        created = sorted(path.name for path in (project / "runtime").iterdir())
        self.assertTrue(created and all(name.startswith("demonstracao-") for name in created), created)


@unittest.skipUnless(os.name == "nt", "cmd.exe só existe no Windows")
class ContinuousLauncherOnWindowsTest(unittest.TestCase):
    """continuo_windows.cmd with cmd.exe, in a copy of the project: only --help and refused arguments,
    which open no browser."""

    def test_the_launcher_runs_the_continuous_mode_from_its_own_folder(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        project = base / "projeto"
        project.mkdir()
        shutil.copy(ROOT / "continuo_windows.cmd", project)
        shutil.copytree(ROOT / "pocketautonomo", project / "pocketautonomo", ignore=shutil.ignore_patterns("__pycache__"))
        env = {key: value for key, value in os.environ.items() if key.upper() not in ("PYTHONPATH", "PYTHONHOME")}
        env.update(POCKETAUTONOMO_SEM_PAUSA="1", PYTHONIOENCODING="utf-8")

        def run(*args):
            result = subprocess.run(
                ["cmd", "/d", "/c", "call", str(project / "continuo_windows.cmd"), *args],
                cwd=base, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                encoding="utf-8", errors="replace", timeout=300,
            )
            return result.returncode, result.stdout + result.stderr

        code, out = run("--help")
        self.assertEqual(code, 1, out)
        self.assertIn("[ERRO] Nao encontrei .venv\\Scripts\\python.exe nesta pasta.", out)
        venv = subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", str(project / ".venv")],
            capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(venv.returncode, 0, venv.stderr)
        code, out = run("--help")
        self.assertEqual(code, 0, out)
        self.assertIn("usage: python -m pocketautonomo.operate", out)
        self.assertIn("--continuo", out)
        code, out = run("--cycles", "2")
        self.assertEqual(code, 2, out)
        self.assertIn("--continuo roda até Ctrl+C e não aceita --cycles", out)
        self.assertIn("continuo_windows.cmd terminou com codigo 2.", out)
        code, out = run("--url", "http://example.org/")
        self.assertEqual(code, 2, out)
        self.assertFalse((project / "runtime").exists())  # nothing was written


if __name__ == "__main__":
    unittest.main()
