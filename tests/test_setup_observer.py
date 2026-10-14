"""Tests of the collector's preparation, pocketautonomo.setup_observer, and of the launchers.

FakeRun stands in for subprocess.run, so no virtual environment is created, nothing is
installed and no browser opens here. The two .cmd launchers are checked as text: cmd.exe
runs them only in the Windows browser job of the CI.
"""

import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pocketautonomo import setup_observer as setup

ROOT = Path(__file__).resolve().parent.parent
PY312 = setup.Interpreter((3, 12, 10), 64, "C:/Python312/python.exe")


def kind_of(command):
    """Which step a command of setup_observer is."""
    if "-c" in command:
        code = command[command.index("-c") + 1]
        return {
            setup.INTERPRETER_CHECK: "interpreter",
            setup.PLAYWRIGHT_CHECK: "playwright",
            setup.CHROMIUM_CHECK: "launch",
        }[code]
    if command[1:3] == ["-m", "pip"]:
        return "pip"
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
            [python, "-m", "playwright", "install", "chromium"],
            [python, "-c", setup.PLAYWRIGHT_CHECK],
            [python, "-c", setup.CHROMIUM_CHECK],
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
        old = setup.Interpreter((3, 9, 25), 64, "C:/Python39/python.exe")
        bits32 = setup.Interpreter((3, 12, 10), 32, "C:/Python312-32/python.exe")
        traceback = "Traceback (most recent call last):\nplaywright._impl._errors.Error: BrowserType.launch: Executable doesn't exist\n"
        # (case, interpreter, windows, existing .venv, FakeRun answers, message, steps run)
        cases = (
            ("old Python", old, False, None, {}, "exige Python 3.10 ou mais novo", []),
            ("32-bit Python on Windows", bits32, True, None, {}, "só tem pacote pronto de 64 bits", []),
            (".venv without Python", PY312, False, "empty", {}, "já existe, mas não tem", []),
            (".venv whose Python fails", PY312, False, "python", {"interpreter": (1, "", "erro")}, "o Python dela não funciona", ["interpreter"]),
            (".venv whose Python does not start", PY312, False, "python", {"interpreter": OSError("sem Python")}, "o Python dela não funciona", ["interpreter"]),
            (".venv with Python 3.9", PY312, False, "python", {"interpreter": (0, "3.9.25 64\n", "")}, "usa o Python 3.9.25 de 64 bits, e o Playwright 1.64.0 exige", ["interpreter"]),
            ("pip fails", PY312, False, None, {"pip": (1, "", "")}, "a instalação de playwright==1.64.0 falhou (código 1)", ["pip"]),
            ("pip does not start", PY312, False, None, {"pip": OSError("sem pip")}, "a instalação de playwright==1.64.0 falhou (sem pip)", ["pip"]),
            ("Chromium download fails", PY312, False, None, {"chromium": (1, "", "")}, "a instalação do Chromium do Playwright falhou (código 1)", ["pip", "chromium"]),
            ("another Playwright", PY312, False, None, {"playwright": (0, "1.63.0\n", "")}, "não importa o Playwright 1.64.0 (encontrado: 1.63.0", ["pip", "chromium", "playwright"]),
            ("Chromium does not start", PY312, False, None, {"launch": (1, "", traceback)}, "o Chromium do Playwright não abriu (playwright._impl._errors.Error: BrowserType.launch: Executable doesn't exist)", ["pip", "chromium", "playwright", "launch"]),
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
        for name in ("preparar_windows.cmd", "observar_windows.cmd"):
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
        self.assertIn("%PYTHON_CMD% -m pocketautonomo.setup_observer", prepare)
        self.assertIn("sys.version_info >= (3, 10) and sys.maxsize > 2**32", " ".join(prepare))
        observe = self.commands("observar_windows.cmd")
        self.assertIn('".venv\\Scripts\\python.exe" -m pocketautonomo.observe --configure --count 3 %*', observe)


if __name__ == "__main__":
    unittest.main()
