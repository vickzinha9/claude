"""The launcher's window (pocketautonomo.app): its commands, its panel and its arm switch, without trading."""

import json
import tempfile
import unittest
from pathlib import Path

from pocketautonomo import app, arming


class CommandTest(unittest.TestCase):
    def test_each_mode_runs_operate_and_the_window_never_arms_by_itself(self):
        url = "https://pocketoption.com/pt/cabinet/quick-high-low/"
        analysis = app.command("Análise", "demo", url)
        simulation = app.command("Simulação", "demo", url)
        real = app.command("Execução real", "real", url)
        for args in (analysis, simulation, real):
            self.assertEqual(args[1:4], ["-m", "pocketautonomo.operate", "--continuo"])
            self.assertNotIn("--armar", args)
        self.assertIn("--analise", analysis)
        self.assertNotIn("--real", simulation)
        self.assertEqual(real[-3:], ["--real", "--conta", "real"])
        self.assertNotIn("--tendencia", real)
        trend = app.command("Tendência real", "real", url)
        self.assertNotIn("--armar", trend)
        self.assertEqual(trend[-3:], ["--real", "--conta", "real"])
        for part in ("--tendencia", "--so-elegiveis", "--limite-perda", "150", "--limite-ciclos-perdidos", "2"):
            self.assertIn(part, trend)
        self.assertIn("--so-elegiveis", analysis)
        self.assertNotIn("--so-elegiveis", real)  # the validated-rule mode keeps scanning every favorite

    def test_the_panel_lines(self):
        self.assertIn("Sem dados", app.describe_panel(None)[0])
        lines = app.describe_panel({
            "execution": "REAL (conta real)", "state": "PENDÊNCIA", "attempt": 2, "stake_cents": 1000,
            "decision": {"asset": "EUR/USD OTC", "action": "PUT", "reason": "regra validada", "rule": "tendência"},
            "quote": {"asset": "EUR/USD OTC", "quote": "1.11451", "captured_at": "2026-11-04T12:00:01.250+00:00"},
            "last_event": {"event": "ordem real aceita pela conta"}, "session": {"cycles": 1, "net_cents": -500},
        })
        text = "\n".join(lines)
        for part in ("PENDÊNCIA", "EUR/USD OTC", "1.11451", "12:00:01.250", "PUT", "tentativa 2 de 4, stake R$ 10", "-5.00"):
            self.assertIn(part, text)


class FakeRunner:
    def __init__(self):
        import queue

        self.lines = queue.Queue()
        self.started = []
        self.alive = False

    def start(self, args):
        self.started.append(args)
        self.alive = True

    def running(self):
        return self.alive

    def enter(self):
        pass

    def release(self):
        self.released = getattr(self, "released", 0) + 1

    def stop(self):
        self.alive = False


class WindowTest(unittest.TestCase):
    def test_the_window_shows_the_panel_and_starts_disarmed(self):
        try:
            import tkinter as tk

            root = tk.Tk()
        except Exception as error:  # noqa: BLE001 - no display (Linux CI)
            self.skipTest(f"sem tela para o Tk: {error}")
        self.addCleanup(root.destroy)
        root.withdraw()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        runtime = Path(directory.name)
        arming.arm(runtime, "real", "teste")  # left armed by something else: Iniciar disarms
        arming.set_pause(runtime, True)  # left paused by an earlier run: Iniciar lifts it
        (runtime / "painel.json").write_text(json.dumps({"state": "COLETANDO", "attempt": 1, "stake_cents": 500}))
        runner = FakeRunner()
        window = app.App(root, runtime, runner)
        window.mode.set("Execução real")
        window.account.set("real")
        window.start()
        self.assertEqual(runner.started[0][-3:], ["--real", "--conta", "real"])
        self.assertIsNone(arming.read_state(runtime / arming.ARM_FILE_NAME))
        self.assertFalse((runtime / arming.PAUSE_FILE_NAME).exists())
        window.refresh()
        self.assertIn("DESARMADA", window.arm_state.get())
        self.assertIn("COLETANDO", window.panel.get())
        window.stop()
        self.assertFalse(runner.alive)

    def test_the_pause_is_released_only_when_the_program_asks_and_the_user_confirms(self):
        from unittest import mock

        try:
            import tkinter as tk

            root = tk.Tk()
        except Exception as error:  # noqa: BLE001 - no display (Linux CI)
            self.skipTest(f"sem tela para o Tk: {error}")
        self.addCleanup(root.destroy)
        root.withdraw()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        runner = FakeRunner()
        window = app.App(root, Path(directory.name), runner)
        runner.alive = True
        with mock.patch("tkinter.messagebox.askyesno", return_value=True) as ask:
            window.release_pause()  # nothing asked yet
            ask.assert_not_called()
            self.assertEqual(getattr(runner, "released", 0), 0)
            # As in the real log: the prompt has no line break, so only the PAUSA block before it reaches the window.
            runner.lines.put("Com a tela de operações visível, pressione Enter aqui para começar a varredura (Ctrl+C cancela). PAUSA DO MODO CONTÍNUO: a operação contínua não recomeça sozinha.")
            runner.lines.put("  Último clique gravado: GBP/USD OTC, posição 5, com 8 elementos na lista; troca pendente.")
            window.root.update()
            window.root.after(0)
            window.refresh()
            ask.return_value = False
            window.release_pause()  # asked, but the user says no
            self.assertEqual(getattr(runner, "released", 0), 0)
            ask.return_value = True
            window.release_pause()
        self.assertEqual(runner.released, 1)
        with mock.patch("tkinter.messagebox.askyesno", return_value=True):
            window.release_pause()  # already answered: not twice
        self.assertEqual(runner.released, 1)

    def test_only_a_running_real_execution_can_be_armed_whatever_the_list_shows(self):
        from unittest import mock

        try:
            import tkinter as tk

            root = tk.Tk()
        except Exception as error:  # noqa: BLE001 - no display (Linux CI)
            self.skipTest(f"sem tela para o Tk: {error}")
        self.addCleanup(root.destroy)
        root.withdraw()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        runtime = Path(directory.name)
        runner = FakeRunner()
        window = app.App(root, runtime, runner)
        window.mode.set("Análise")
        window.start()
        window.mode.set("Execução real")  # changed in the list while the analysis runs, as in the log of 2026-11-04
        with mock.patch("tkinter.messagebox.showinfo") as info, mock.patch("tkinter.messagebox.askyesno", return_value=True) as ask:
            window.arm()
        info.assert_called_once()
        ask.assert_not_called()
        self.assertIsNone(arming.read_state(runtime / arming.ARM_FILE_NAME))
        window.stop()
        window.mode.set("Execução real")
        window.account.set("demo")
        window.start()
        window.account.set("real")  # the list again: the run is on the demo account
        with mock.patch("tkinter.messagebox.askyesno", return_value=True) as ask:
            window.arm()
        self.assertIn("DEMO", ask.call_args[0][1])
        self.assertEqual(arming.read_state(runtime / arming.ARM_FILE_NAME)["account"], "demo")


if __name__ == "__main__":
    unittest.main()


class CleanUrlTest(unittest.TestCase):
    def test_a_token_in_the_link_is_never_kept_or_passed_on(self):
        self.assertEqual(
            app.clean_url(" https://pocketoption.com/pt/cabinet/quick-high-low/?ssid=SEGREDO#SEGREDO "),
            "https://pocketoption.com/pt/cabinet/quick-high-low/",
        )
