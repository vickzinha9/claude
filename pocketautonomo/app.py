"""The Windows launcher: one window for the analysis, the simulation and the real execution.

    .venv\\Scripts\\pythonw -m pocketautonomo.app      (pocketautonomo_windows.cmd)

The window does not trade by itself. It starts python -m pocketautonomo.operate --continuo in the
chosen mode, as a separate process, and shows what that process writes: its lines, and
runtime/painel.json (asset, quote, time, the analysis' CALL/PUT/AGUARDAR and why, the state, the
gale's level and stake, the last order and the run's net). The browser opens with the program's
own profile: you log in there once, and click "Já fiz login".

- Análise: reads and decides; no entry at all (operate --analise).
- Simulação: the gales on the simulator only (operate).
- Execução real: orders on the account in the browser (operate --real --conta real|demo). It starts
  DISARMED. Armar, after a confirmation, writes runtime/execucao-real-armada.json; from then on a
  valid CALL/PUT goes out without asking. Desarmar removes it: the next order is refused at once.
  Pausar stops new cycles; Parar disarms first and then ends the process.

Estatísticas shows python -m pocketautonomo.stats; Diagnóstico opens runtime/.
"""

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

from pocketautonomo import arming

MODES = ("Análise", "Simulação", "Execução real", "Tendência real")
REAL_MODES = ("Execução real", "Tendência real")
# The trend mode: the chart's reading opens the cycles, without a validated rule; R$ 150 or two cycles of four LOSS stop it.
TREND_ARGS = ["--tendencia", "--limite-perda", "150", "--limite-ciclos-perdidos", "2"]
DEFAULT_URL = "https://pocketoption.com/pt/cabinet/quick-high-low/"
SETTINGS_FILE_NAME = "launcher.json"
POLL_MS = 500


def clean_url(url):
    """The link without its query and fragment, where a session token may be: the profile keeps the login."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def command(mode, account, url):
    """The operate command of a mode: never --armar, the window arms explicitly."""
    base = [sys.executable, "-m", "pocketautonomo.operate", "--continuo", "--painel", "--url", url]
    if mode == "Análise":
        return base + ["--analise"]
    if mode == "Tendência real":
        return base + TREND_ARGS + ["--real", "--conta", account]
    if mode == "Execução real":
        return base + ["--real", "--conta", account]
    return base


def describe_panel(data):
    """The lines of the window's panel, from painel.json."""
    if not data:
        return ["Sem dados ainda: inicie um modo e faça o login no navegador."]
    decision = data.get("decision") or {}
    quote = data.get("quote") or {}
    last = data.get("last_event") or {}
    session = data.get("session") or {}
    stake = data.get("stake_cents")
    lines = [
        f"Execução: {data.get('execution')}   Estado: {data.get('state')}",
        f"Ativo: {quote.get('asset') or decision.get('asset') or '—'}   Cotação: {quote.get('quote') or decision.get('quote') or '—'}"
        f"   Horário (UTC): {(quote.get('captured_at') or decision.get('captured_at') or '—')[11:23]}",
        f"Análise: {decision.get('action') or '—'} — {decision.get('reason') or ''}",
        f"Regra: {decision.get('rule') or '—'}   Payout: {decision.get('payout') or quote.get('payout') or '—'}%",
        f"Gale: tentativa {data.get('attempt')} de 4, stake R$ {stake // 100 if stake else '—'}",
        f"Último evento: {last.get('event') or '—'} {last.get('result') or ''} {last.get('reason') or ''}"[:300],
        f"Sessão: {session.get('cycles', 0)} ciclos, líquido R$ {session.get('net_cents', 0) / 100:.2f}"
        + (f"   PARADA: {data['stop_reason']}" if data.get("stop_reason") else ""),
    ]
    return lines


class Runner:
    """The operate process: started, fed with Enter, read line by line, stopped."""

    def __init__(self, cwd):
        self.cwd = cwd
        self.process = None
        self.lines = queue.Queue()

    def start(self, args):
        environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.process = subprocess.Popen(
            args, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace", env=environment, creationflags=flags,
        )
        threading.Thread(target=self._read, args=(self.process,), daemon=True).start()

    def _read(self, process):
        for line in process.stdout:
            self.lines.put(line.rstrip("\n"))
        self.lines.put(f"[processo terminou com código {process.wait()}]")

    def running(self):
        return self.process is not None and self.process.poll() is None

    def enter(self):
        if self.running():
            try:
                self.process.stdin.write("\n")
                self.process.stdin.flush()
            except OSError:
                pass

    def stop(self):
        if self.running():
            self.process.terminate()


class App:
    def __init__(self, root, runtime_dir, runner):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.root = root
        self.runtime_dir = Path(runtime_dir)
        self.runner = runner
        self.settings_path = self.runtime_dir / SETTINGS_FILE_NAME
        settings = self._settings()
        root.title("PocketAutonomo")
        root.geometry("980x700")
        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Modo:").pack(side="left")
        self.mode = tk.StringVar(value=settings.get("mode", MODES[1]))
        ttk.Combobox(top, textvariable=self.mode, values=MODES, state="readonly", width=16).pack(side="left", padx=4)
        ttk.Label(top, text="Conta:").pack(side="left")
        self.account = tk.StringVar(value=settings.get("account", "demo"))
        ttk.Combobox(top, textvariable=self.account, values=arming.ACCOUNTS, state="readonly", width=6).pack(side="left", padx=4)
        ttk.Label(top, text="Link:").pack(side="left")
        self.url = tk.StringVar(value=settings.get("url", DEFAULT_URL))
        ttk.Entry(top, textvariable=self.url, width=52).pack(side="left", padx=4, fill="x", expand=True)
        buttons = ttk.Frame(root, padding=(8, 0))
        buttons.pack(fill="x")
        for text, action in (
            ("Iniciar", self.start), ("Já fiz login", self.runner.enter), ("Pausar/Retomar", self.toggle_pause),
            ("Parar", self.stop), ("Estatísticas", self.show_stats), ("Diagnóstico", self.diagnostic),
        ):
            ttk.Button(buttons, text=text, command=action).pack(side="left", padx=2, pady=4)
        arm = ttk.Frame(root, padding=(8, 0))
        arm.pack(fill="x")
        self.arm_button = tk.Button(arm, text="ARMAR execução real", command=self.arm, bg="#b71c1c", fg="white")
        self.arm_button.pack(side="left", padx=2)
        tk.Button(arm, text="DESARMAR", command=self.disarm, bg="#1b5e20", fg="white").pack(side="left", padx=2)
        self.arm_state = tk.StringVar()
        tk.Label(arm, textvariable=self.arm_state, font=("Segoe UI", 11, "bold")).pack(side="left", padx=10)
        self.panel = tk.StringVar()
        tk.Label(root, textvariable=self.panel, justify="left", anchor="w", font=("Consolas", 10)).pack(fill="x", padx=8, pady=6)
        self.log = tk.Text(root, height=20, font=("Consolas", 9))
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    # Actions.

    def start(self):
        if self.runner.running():
            self.write("Já existe uma execução rodando.")
            return
        url = clean_url(self.url.get())
        self.url.set(url)
        self._save(mode=self.mode.get(), account=self.account.get(), url=url)
        arming.disarm(self.runtime_dir)  # a new run always starts disarmed
        args = command(self.mode.get(), self.account.get(), url)
        self.write("Iniciando: " + " ".join(args[1:]))
        self.write("Faça o login no navegador que abrir e clique em 'Já fiz login'.")
        self.runner.start(args)
        # What runs now, not what the lists show: they can be changed while it runs.
        self.started = (self.mode.get(), self.account.get())

    def arm(self):
        from tkinter import messagebox

        mode, account = getattr(self, "started", (None, None))
        if mode not in REAL_MODES or not self.runner.running():
            messagebox.showinfo(
                "PocketAutonomo",
                "Só a execução que está rodando no modo 'Execução real' ou 'Tendência real' pode ser armada. Pare a atual e inicie um desses modos.",
            )
            return
        if not messagebox.askyesno(
            "Armar execução real",
            f"Armar a execução REAL na conta {account.upper()}?\n\nCada CALL/PUT válido envia uma ordem de"
            " R$ 5, 10, 20 ou 40 (nível do Gale), 300 s, sem nova confirmação, até você desarmar ou o"
            " limite da sessão parar o envio.",
        ):
            return
        arming.arm(self.runtime_dir, account, "painel")
        self.write(f"ARMADA na conta {account}.")

    def disarm(self):
        arming.disarm(self.runtime_dir)
        self.write("DESARMADA: nenhuma ordem nova é enviada.")

    def toggle_pause(self):
        paused = (self.runtime_dir / arming.PAUSE_FILE_NAME).exists()
        arming.set_pause(self.runtime_dir, not paused)
        self.write("Pausa retirada." if paused else "PAUSA: nenhum ciclo novo começa.")

    def stop(self):
        arming.disarm(self.runtime_dir)
        self.runner.stop()
        self.write("Parar: desarmada e execução encerrada; uma ordem pendente é conferida na próxima abertura.")

    def show_stats(self):
        from pocketautonomo import stats

        window = self.tk.Toplevel(self.root)
        window.title("Estatísticas")
        text = self.tk.Text(window, width=130, height=40, font=("Consolas", 9))
        text.pack(fill="both", expand=True)
        try:
            reports, readings, waits = stats.build(self.runtime_dir)
            text.insert("end", stats.text(reports, readings, waits))
        except Exception as error:  # noqa: BLE001 - shown in the window
            text.insert("end", f"As estatísticas não puderam ser lidas: {error}")

    def diagnostic(self):
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.write(f"Pasta de dados: {self.runtime_dir}")
        if hasattr(os, "startfile"):
            os.startfile(self.runtime_dir)  # noqa: S606 - the program's own folder

    def close(self):
        self.stop()
        self.root.destroy()

    # Updates.

    def refresh(self):
        while True:
            try:
                self.write(self.runner.lines.get_nowait())
            except queue.Empty:
                break
        state = arming.read_state(self.runtime_dir / arming.ARM_FILE_NAME)
        paused = (self.runtime_dir / arming.PAUSE_FILE_NAME).exists()
        self.arm_state.set(
            (f"ARMADA (conta {state.get('account')}) — ordens reais saem" if state else "DESARMADA — nenhuma ordem real")
            + ("   |   PAUSA" if paused else "")
        )
        self.panel.set("\n".join(describe_panel(self._panel())))
        self.root.after(POLL_MS, self.refresh)

    def write(self, line):
        self.log.insert("end", line + "\n")
        self.log.see("end")

    def _panel(self):
        try:
            return json.loads((self.runtime_dir / "painel.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _settings(self):
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, **values):
        try:
            self.runtime_dir.mkdir(parents=True, exist_ok=True)
            self.settings_path.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass


def main(argv=None):
    import tkinter as tk

    from pocketautonomo.observe import RUNTIME_DIR

    args = sys.argv[1:] if argv is None else argv
    root = tk.Tk()
    app = App(root, RUNTIME_DIR, Runner(Path(__file__).resolve().parent.parent))
    if "--smoke" in args:  # CI and tests: the window opens, refreshes and closes by itself
        root.after(1500, app.close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
