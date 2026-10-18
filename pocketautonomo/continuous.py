"""The continuous mode of the simulated operation: python -m pocketautonomo.operate --continuo.

SIMULATED EXECUTION ONLY, as in operate.py: no order goes to the Pocket Option and no money moves.

The continuous mode runs one operate.Execution after the other, until Ctrl+C or until you close
the browser window. Each run is the same as a run of operate.main(), with all of its protections:
the saved state of the coordinator and of the simulator reopened and recovered before any visit
(a pending entry is checked, never sent again), a decision that starts one cycle at most, stakes
of R$ 5, R$ 10, R$ 20 and R$ 40 with four attempts at most, and the PAUSA of the scan.
runtime/scan.lock is taken first and held from the first run to the last.

- The first run whose browser opens asks for the Enter after your login, as operate does, and,
  with --configure, for the favorites. Every later run opens the browser again and waits by
  itself for the trading screen (wait_for_screen): it only reads, never clicks, and only on
  pocketoption.com, until the favorites and the asset are read for SCREEN_READY_SECONDS in a row.
  On pocketoption.com that wait has no limit, as for a login you do later; a page outside it for
  SCREEN_OFF_SITE_SECONDS ends the run, so that a new browser loads it again.
- A new run starts by itself only after an end that a new browser may cure (EndOfRun and
  BrowserError): the browser failed, the favorites were missing for SCREEN_LOST_CYCLES cycles in a
  row (a session that ended, a page that broke), STALL_CYCLES cycles in a row had no complete
  reading, or the page did not load. It waits RESTART_SECONDS after a run with complete readings
  and, after each run without one, the next of FAILURE_WAITS; MAX_FAILURES runs in a row without a
  complete reading, or more than MAX_RESTARTS new runs within RESTART_WINDOW_SECONDS, stop it.
- Nothing else starts a run by itself. A switch that the asset field did not confirm puts the scan
  in PAUSA (scan.Pause): here, the run ends right after that visit, with no other click, and the
  PAUSA is kept in runtime/continuo-estado.json (ContinuousState). A page loaded again does not
  prove that its chart is the asset that its name shows, and the program has no source that tells,
  so no run starts while it is kept. A run that ended between a click and the confirmation of its
  switch, however it ended, leaves such a PAUSA too (ClickJournal); a click that failed with an
  error of the browser may have reached the page, so its switch has to be confirmed as any other.
  Only you release a PAUSA, typing RELEASE_WORD after the login of the first run of a new start,
  once you checked the screen.
- A safety stop of the scan (an unsafe favorites selector, a page outside pocketoption.com, a
  favorite that a click removed, a new tab), a stop of the operation (a write that failed, an
  answer outside the rules), a file that could not be written, a configuration problem and
  Ctrl+C end the continuous mode too.
- The last click stays in runtime/continuo-estado.json until a later list of the favorites shows
  that it removed none, so that the first list of a new run, also after a restart, checks it: the
  scan stops when that favorite is gone and the list shrank. Reviewing the favorites with
  --configure, or releasing a PAUSA after the list was read and shown to you, clears it.
- Everything the terminal shows also goes to runtime/continuo-<data>-<hora>-<sufixo>.log, each
  line with this computer's clock, in UTC (ContinuousLog), with the questions of the terminal but
  never what you type, and the traceback of an error that ends the program; a new file starts
  after MAX_LOG_BYTES, and nothing is deleted. On Windows, the system and the display are kept
  from sleeping while it runs (KeepAwake).

Exit codes: 0 when you closed the browser window with no PAUSA, no cycle open and a line with data
in that run, 1 for a PAUSA, a stop, too many failures or restarts, or a window closed with a cycle
open or without data, 2 for invalid arguments or configuration, and 130 after Ctrl+C.
"""

import ctypes
import json
import os
import sys
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from pocketautonomo import observe, operate, scan
from pocketautonomo.observe import (
    EXIT_CONFIG,
    EXIT_FAILED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    BrowserError,
    ConfigError,
    ObserverStopped,
    count,
    say,
    utc_text,
    warn,
)

STATE_FILE_NAME = scan.CONTINUOUS_STATE_FILE_NAME  # continuo-estado.json
LOG_PREFIX = "continuo-"
STATE_VERSION = 1
SCREEN_POLL_SECONDS = 5.0  # between two readings of the page while the continuous mode waits for the screen
SCREEN_READY_SECONDS = 30.0  # the screen ready for this long, in a row, before the scan starts again
SCREEN_REMINDER_SECONDS = 600.0  # the same reason of the wait is said again after this long
# The page outside pocketoption.com for this long, in a row, while waiting for the screen: the page
# did not load, and the run ends, so that a new browser loads it again. On pocketoption.com, the wait
# for your login has no limit.
SCREEN_OFF_SITE_SECONDS = 600.0
SCREEN_LOST_CYCLES = 2  # cycles in a row without favorites to visit: the run ends
STALL_CYCLES = 10  # cycles in a row without any complete reading: the run ends
RESTART_SECONDS = 15.0  # before a new run, after a run that had complete readings
FAILURE_WAITS = (30.0, 60.0, 120.0, 300.0)  # before a new run, after 1, 2, 3 and 4 runs in a row without one
MAX_FAILURES = 5  # runs in a row without any complete reading: the continuous mode stops
# New runs started by themselves within RESTART_WINDOW_SECONDS: one more stops the continuous mode,
# whatever the runs read, so that a browser that keeps failing is not opened again and again.
MAX_RESTARTS = 6
RESTART_WINDOW_SECONDS = 3600.0
MAX_LOG_BYTES = 20 * 1024 * 1024  # a log file this long goes on in a new one
LOG_RETRY_SECONDS = 60.0  # after a write of the log failed, the wait before a new file
RELEASE_WORD = "LIBERAR"
PENDING = "pendente"  # the click was made, or was about to be, and its switch was not confirmed yet
CONFIRMED = "confirmada"
WRITE_ATTEMPTS = 5  # Windows: another program may hold the file for a moment
WRITE_RETRY_SECONDS = 0.2
# SetThreadExecutionState, in kernel32: the system and the display stay on while it holds.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
AWAKE = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED

STATE_NOTE = (
    "Estado do modo contínuo da operação simulada (python -m pocketautonomo.operate --continuo; no Windows,"
    " continuo_windows.cmd), gravado pelo programa; não edite. pausa: a PAUSA que impede o modo contínuo de"
    " recomeçar sozinho, até você liberá-la digitando LIBERAR depois do login. ultimo_clique: o último favorito"
    " clicado, conferido na próxima leitura da lista de favoritos, também depois de reabrir. pausas_liberadas:"
    " as PAUSAS que você liberou."
)
INTRO = (
    "MODO CONTÍNUO: uma execução depois da outra, até Ctrl+C ou até você fechar a janela do navegador. Depois"
    " de uma falha do navegador, da sessão desconectada ou de ciclos sem leitura completa, uma execução nova"
    " abre o navegador de novo e espera sozinha a tela de operações, só lendo a página, sem clicar; a espera"
    f" entre execuções cresce, e {MAX_FAILURES} execuções seguidas sem leitura completa, ou mais de {MAX_RESTARTS}"
    f" execuções novas em {RESTART_WINDOW_SECONDS / 3600:g} hora, param o modo contínuo."
    " Ele nunca recomeça sozinho depois de uma PAUSA (troca de ativo não confirmada), de uma parada por"
    " segurança, de uma falha de gravação ou de Ctrl+C. Cada execução tem as mesmas regras da operação simulada:"
    " nenhuma ordem vai para a Pocket Option."
)
NO_PROOF = (
    "uma página carregada de novo não comprova que o gráfico é do ativo que o nome mostra, e o programa não tem"
    " uma fonte que diga a qual ativo o gráfico pertence"
)


class StateError(Exception):
    """runtime/continuo-estado.json cannot be read or is not what the program writes: exit code 1."""


class EndOfRun(Exception):
    """The continuous mode ends a run that a new browser may cure: no favorites, or no complete reading."""


class PauseStop(Exception):
    """The PAUSA started: the run ends right after the visit that started it."""


class PauseKept(Exception):
    """The PAUSA kept in the state was not released: nothing starts."""


@dataclass(frozen=True)
class Outcome:
    """How a run ended: stop ends the continuous mode with code; otherwise a new run starts."""

    stop: bool
    code: int = EXIT_FAILED
    progress: bool = False  # it had a complete reading: the runs in a row without one start again from 0
    why: str = ""


def run(args, ctx, open_session, read_line, *, sleep=None, keep_awake=None):
    """The continuous mode of operate.main(); its exit code. sleep and keep_awake exist for tests."""
    log = ContinuousLog(ctx.now)
    log.start()
    try:
        supervisor = Continuous(
            args, ctx, open_session, log.reading(read_line), time.sleep if sleep is None else sleep,
            KeepAwake() if keep_awake is None else keep_awake, log,
        )
        return supervisor.run()
    except BaseException:
        log.failure(traceback.format_exc())  # only in the log: the terminal gets it from Python, as always
        raise
    finally:
        log.stop()


class Continuous:
    """The runs of the continuous mode, one after the other, and what ends them."""

    def __init__(self, args, ctx, open_session, read_line, sleep, awake, log):
        self.args = args
        self.ctx = ctx
        self.open_session = open_session
        self.read_line = read_line
        self.sleep = sleep
        self.awake = awake
        self.log = log
        runtime_dir = ctx.runtime_dir
        self.profile_dir = runtime_dir / observe.PROFILE_DIR_NAME
        self.selectors_path = runtime_dir / observe.SELECTORS_FILE_NAME
        self.scan_path = runtime_dir / scan.SCAN_SELECTORS_FILE_NAME
        self.state_path = runtime_dir / STATE_FILE_NAME
        self.saved = None  # ContinuousState
        self.selectors = self.favorites_selector = self.url = None
        self.invalid = False  # the favorites file is invalid: --configure replaces it, keeping a copy
        self.runs = 0
        self.reviewed = False  # you confirmed the login, and saw the PAUSA kept, if any: later runs wait by themselves

    def run(self):
        args, ctx = self.args, self.ctx
        lock = None
        try:
            say(operate.INTRO)
            say(INTRO)
            # The lock first, held from the first run to the last: the state is read only by its holder.
            lock = scan.ScanLock(ctx.runtime_dir / scan.LOCK_FILE_NAME)
            self.saved = ContinuousState.load(self.state_path, ctx.now)  # an unreadable state stops here
            self.selectors = scan.load_reader_selectors(self.selectors_path)
            self.favorites_selector, self.invalid = scan.current_favorites_selector(self.scan_path, args.configure)
            if isinstance(self.selectors["price"], observe.QuoteOcrSource):
                ctx.engine.get()  # a missing OCR stops here, before the browser opens, and loads once for every run
                observe.announce_ocr(ctx)
            self.log.open(ctx.runtime_dir)
            self.awake.start()
            self.url = args.url if args.url is not None else observe.ask_url(self.read_line)
            scan.print_plan(self.url, args, self.profile_dir, self.selectors_path, self.scan_path)
            say(f"Estado do modo contínuo: {self.state_path}.")
            self.unfinished_click()
            if self.saved.pause is not None:
                warn(
                    f"PAUSA gravada pelo modo contínuo, desde {self.saved.pause.get('desde_utc') or '?'} (relógio"
                    f" local, UTC): {self.saved.pause['motivo']}. Ela aparece de novo depois do login, e nada é"
                    f" clicado antes de você liberá-la."
                )
            return self.loop()
        except ConfigError as error:
            warn(f"CONFIGURAÇÃO: {error}")
            return EXIT_CONFIG
        except (StateError, ObserverStopped, scan.ScanBusy, OSError) as error:
            warn(f"PARADA: {error}")
            return EXIT_FAILED
        except KeyboardInterrupt:
            say("Interrompido por Ctrl+C.")
            return EXIT_INTERRUPTED
        finally:
            self.awake.stop()
            if lock is not None:
                lock.release()
            self.closing_lines()

    def loop(self):
        failures = 0  # runs in a row without any complete reading
        restarts = []  # the moments, on ctx.monotonic, of the new runs started by themselves
        while True:
            if self.reviewed and self.saved.pause is not None:  # never: a PAUSA ends the continuous mode
                warn(f"PARADA: há uma PAUSA gravada em {self.state_path}; nenhuma execução começa sozinha.")
                return EXIT_FAILED
            self.runs += 1
            outcome = self.execute(self.runs)
            if outcome.stop:
                return outcome.code
            moment = self.ctx.monotonic()
            restarts = [start for start in restarts if moment - start < RESTART_WINDOW_SECONDS] + [moment]
            if len(restarts) > MAX_RESTARTS:
                warn(
                    f"PARADA: {len(restarts)} execuções terminaram e pediram uma nova em menos de"
                    f" {seconds_text(RESTART_WINDOW_SECONDS / 60)} min (a última: {outcome.why}). O modo contínuo parou,"
                    " para não abrir o navegador de novo e de novo; confira a janela do navegador, a sessão e a"
                    " conexão antes de abri-lo de novo."
                )
                return EXIT_FAILED
            if outcome.progress:
                failures = 0
                delay = RESTART_SECONDS
            else:
                failures += 1
                if failures >= MAX_FAILURES:
                    warn(
                        f"PARADA: {failures} execuções seguidas terminaram sem nenhuma leitura completa (a última:"
                        f" {outcome.why}). O modo contínuo parou, para não insistir; confira a janela do navegador,"
                        " a sessão e os seletores antes de abri-lo de novo."
                    )
                    return EXIT_FAILED
                delay = FAILURE_WAITS[min(failures, len(FAILURE_WAITS)) - 1]
            at = self.ctx.now() + timedelta(seconds=delay)
            streak = (
                ""
                if outcome.progress
                else f"; {failures} de {MAX_FAILURES} execuções seguidas sem leitura completa (a {MAX_FAILURES}ª para o"
                " modo contínuo)"
            )
            say(
                f"CONTÍNUO: execução {self.runs + 1} em {seconds_text(delay)} s, às {utc_text(at)} (relógio local,"
                f" UTC){streak}. O navegador fica fechado até lá; Ctrl+C encerra."
            )
            try:
                self.sleep(delay)
            except KeyboardInterrupt:
                say("Interrompido por Ctrl+C entre duas execuções.")
                return EXIT_INTERRUPTED

    def execute(self, number):
        """One run: the saved state, the browser, the screen, the visits; how it ended."""
        args, ctx = self.args, self.ctx
        say("")
        say(f"CONTÍNUO: execução {number}, às {utc_text(ctx.now())} (relógio local, UTC).")
        execution = operate.Execution(args, ctx)
        session = None
        interrupted = False
        outcome = None
        try:
            execution.open(rules=number == 1)
            session = self.open_session(self.profile_dir)
            observe.open_page(session, self.url)
            if not self.reviewed:  # the first run whose browser opened: you are there
                scan.wait_for_login(self.read_line, args.configure)
                if session.is_closed():
                    raise ObserverStopped("a janela do navegador foi fechada antes da confirmação do login")
                self.review_pause(session)
                if args.configure:
                    self.review_click(session)
                    self.favorites_selector = scan.configure_favorites(
                        session, self.scan_path, self.favorites_selector, self.invalid, self.selectors,
                        self.read_line, ctx,
                    )
                    self.invalid = False
                    if self.saved.last_click is not None:
                        self.saved.change(last_click=None)  # you checked the list that the selector finds now
                        say("Registro do último clique apagado: você conferiu a lista de favoritos.")
                self.reviewed = True
            else:
                wait_for_screen(session, self.selectors, self.favorites_selector, ctx)
            say(scan.HANDS_OFF)
            execution.visit(session, self.selectors, self.favorites_selector, lambda state: self.prepare(state, number))
            # The visits end only when the window closes: you closed it. finish() says 1 without any
            # line with data in this run, or with a cycle open.
            code = execution.finish()
            outcome = Outcome(True, code, why="a janela do navegador foi fechada")
            say("CONTÍNUO: a janela do navegador foi fechada; o modo contínuo terminou, sem abri-la de novo.")
        except scan.WindowClosed:  # while waiting for the screen
            say("A janela do navegador foi fechada durante a espera da tela de operações; o modo contínuo terminou.")
            execution.stopped()
            outcome = Outcome(True, EXIT_OK, why="a janela do navegador foi fechada")
        except PauseStop:
            self.after_pause(session, execution)
            execution.stopped()
            outcome = Outcome(True, EXIT_FAILED, why="PAUSA")
        except PauseKept as error:
            warn(f"PARADA: {error}.")
            execution.stopped()
            outcome = Outcome(True, EXIT_FAILED, why="PAUSA mantida")
        except EndOfRun as error:
            say(f"CONTÍNUO: a execução {number} termina aqui: {error}.")
            outcome = Outcome(False, progress=execution.progress.complete > 0, why=str(error))
            try:
                self.check_list(session, execution)
            except ObserverStopped as stop:  # the last click removed a favorite: a safety stop
                warn(f"PARADA: {stop}")
                self.kept_click_hint()
                outcome = Outcome(True, EXIT_FAILED, why=str(stop))
            execution.stopped()
        except BrowserError as error:
            warn(f"FALHA DO NAVEGADOR: {error}")
            execution.stopped()
            outcome = Outcome(False, progress=execution.progress.complete > 0, why=f"falha do navegador ({error})")
        except ConfigError as error:
            warn(f"CONFIGURAÇÃO: {error}")
            outcome = Outcome(True, EXIT_CONFIG, why="configuração")
        except operate.STOPS as error:
            warn(f"PARADA: {error}")
            execution.stopped()
            self.kept_click_hint()
            outcome = Outcome(True, EXIT_FAILED, why=str(error))
        except KeyboardInterrupt:
            interrupted = True
            say(f"Interrompido por Ctrl+C: {scan.written_summary(execution.progress)}.")
            execution.stopped()
            outcome = Outcome(True, EXIT_INTERRUPTED, why="Ctrl+C")
        finally:
            execution.close_files()
            observe.close_session(session, interrupted)
            execution.close_state()
            outcome = self.settle(outcome, number, execution)
        return outcome

    def prepare(self, state, number):
        """The continuous mode's part of the scan: the journal, the watch, the hooks and the last click."""
        state.journal = ClickJournal(self.saved, number, lambda: state.progress.cycles, self.ctx.now)
        state.after_cycle = Watch()
        state.pause_ends_run = True
        state.hooks = ContinuousHooks(state.hooks, state, self, number)
        record = self.saved.last_click
        if record is not None:
            state.last_clicked = (record["chave"], record["total"])
            say(
                f"Último clique gravado, de uma execução anterior: {record['ativo']}, com"
                f" {count(record['total'], 'elemento', 'elementos')} na lista. A primeira leitura da lista confere"
                " se ele continua nela; se ele tiver sumido e a lista diminuído, a varredura para, sem clicar."
            )

    def record_pause(self, pause, visit, number, csv_name):
        """Keep the PAUSA that the scan started, before anything else."""
        self.saved.change(
            pause={
                "ativo": pause.asset,
                "ciclo": pause.cycle,
                "execucao": number,
                "csv": csv_name,
                "desde_utc": utc_text(visit.recorded_at or self.ctx.now()),
                "motivo": pause.cause,
            }
        )

    def after_pause(self, session, execution):
        """The PAUSA ended the run: say so, and read the favorites list once more, only reading."""
        warn(
            f"MODO CONTÍNUO: a PAUSA ficou gravada em {self.state_path}, e a operação contínua parou e não recomeça"
            f" sozinha: {NO_PROOF}. Para recomeçar, abra o modo contínuo de novo e, depois do login, confira a tela"
            f" e digite {RELEASE_WORD}."
        )
        state = execution.state
        if session is None or session.is_closed() or state is None or state.last_clicked is None:
            return
        outside = scan.platform_problem(session)
        if outside is not None:
            warn(f"  A lista de favoritos não foi lida depois da PAUSA: {outside}; o último clique fica gravado.")
            return
        try:
            scan.checked_favorites(session, state)  # reads the list, again after a moment if needed; no click
        except ObserverStopped as error:
            warn(f"  Na leitura da lista depois da PAUSA: {error}. O último clique fica gravado.")
        except BrowserError as error:
            warn(f"  A lista de favoritos não pôde ser lida depois da PAUSA ({error}); o último clique fica gravado.")
        else:
            if state.last_clicked is None:  # the list was read, and the click removed no favorite
                say("  A lista de favoritos, lida de novo depois da PAUSA, mostra que o clique não tirou nenhum favorito dela.")
            else:
                say("  A lista de favoritos não pôde ser lida depois da PAUSA; o último clique fica gravado.")

    def check_list(self, session, execution):
        """Before a run that a new one follows ends: the list read once more for the last click, only reading.

        It is what the next visit of the same run would check first. With the favorite still there,
        or the list not shorter, the click is settled and forgotten; gone with a shorter list, the
        scan's ObserverStopped stops the continuous mode. A list that cannot be read now keeps it.
        """
        state = execution.state
        if session is None or session.is_closed() or state is None or state.last_clicked is None:
            return
        if scan.platform_problem(session) is not None:
            return
        try:
            scan.checked_favorites(session, state)
        except BrowserError:
            return

    def settle(self, outcome, number, execution):
        """outcome, or a stop with a PAUSA kept when the run ended before the switch of its last click was confirmed."""
        record = self.saved.last_click
        if record is None or record.get("estado") != PENDING or self.saved.pause is not None:
            return outcome
        reason = (
            f"a execução {number} terminou depois do clique em {record['ativo']} (ciclo {record.get('ciclo')}),"
            " antes de a troca ser confirmada: esse clique pode ter mudado o nome do ativo e o gráfico depois, e"
            f" {NO_PROOF}"
        )
        csv_path = execution.progress.csv_path
        pause = {
            "ativo": record["ativo"],
            "ciclo": record.get("ciclo"),
            "execucao": number,
            "csv": csv_path.name if csv_path is not None else None,
            "desde_utc": utc_text(self.ctx.now()),
            "motivo": reason,
        }
        try:
            self.saved.change(pause=pause)
        except OSError as error:
            warn(f"AVISO: a PAUSA não pôde ser gravada em {self.state_path} ({error}); o clique pendente continua lá.")
        warn(f"PAUSA: {reason}. O modo contínuo parou e não recomeça sozinho; depois do login, confira a tela e digite {RELEASE_WORD}.")
        if outcome is None or not outcome.stop:
            return Outcome(True, EXIT_FAILED, why="troca pendente")
        if outcome.code == EXIT_OK:
            return Outcome(True, EXIT_FAILED, why="troca pendente")
        return outcome

    def unfinished_click(self):
        """A click left pending by a start that ended without settling it, as a power cut would: a PAUSA."""
        record = self.saved.last_click
        if record is None or record.get("estado") != PENDING or self.saved.pause is not None:
            return
        self.saved.change(
            pause={
                "ativo": record["ativo"],
                "ciclo": record.get("ciclo"),
                "execucao": record.get("execucao"),
                "csv": None,
                "desde_utc": record.get("em_utc"),
                "motivo": (
                    f"o modo contínuo anterior terminou sem gravar o fim da execução {record.get('execucao')}, depois"
                    f" do clique em {record['ativo']} (ciclo {record.get('ciclo')}) e antes de a troca ser confirmada:"
                    f" esse clique pode ter mudado o nome do ativo e o gráfico depois, e {NO_PROOF}"
                ),
            }
        )

    def review_pause(self, session):
        """After the first login: release the PAUSA kept in the state only when you type RELEASE_WORD."""
        pause = self.saved.pause
        if pause is None:
            return
        warn("PAUSA DO MODO CONTÍNUO: a operação contínua não recomeça sozinha.")
        say(
            f"  Gravada às {pause.get('desde_utc') or '?'} (relógio local, UTC), na execução {pause.get('execucao') or '?'}"
            f" de um modo contínuo anterior{csv_text(pause.get('csv'))}: {pause['motivo']}."
        )
        compared = self.describe_click(session)
        say(
            "  O programa não verificou, e não consegue verificar, a qual ativo o gráfico pertence. Antes de"
            " liberar, confira na janela do navegador que o gráfico é o do ativo mostrado e que a lista de"
            f" favoritos é a que você quer. Se a troca para {pause['ativo']} continuar sem confirmação, a PAUSA"
            " volta na primeira visita dele; tirá-lo dos favoritos evita isso."
        )
        answer = observe.ask(
            self.read_line,
            f"Digite {RELEASE_WORD} para liberar a PAUSA e começar, ou Enter para sair sem mudar nada: ",
            "antes da liberação da PAUSA",
        )
        if answer.strip().upper() != RELEASE_WORD:
            raise PauseKept(f"a PAUSA continua gravada em {self.state_path}; nada foi clicado nem alterado")
        released = dict(pause, liberada_utc=utc_text(self.ctx.now()), ultimo_clique=self.saved.last_click)
        # The last click goes only when it was compared above with the list of now, which you saw;
        # otherwise the first reading of the list in the scan still checks it, and stops if it is gone.
        # Its switch, pending or not, is what you just released: only the check of the list stays.
        last_click = None if compared or self.saved.last_click is None else dict(self.saved.last_click, estado=CONFIRMED)
        self.saved.change(released=self.saved.released + [released], pause=None, last_click=last_click)
        say(
            "PAUSA liberada por você, depois de conferir a tela; o programa não verificou a identidade do gráfico."
            " A varredura começa."
        )

    def review_click(self, session):
        """Before --configure lists the favorites: the last click kept, and what accepting the list does."""
        if self.saved.last_click is None:
            return
        self.describe_click(session)
        say("  Ao aceitar a lista abaixo, o registro desse clique é apagado: é você quem confere a lista.")

    def describe_click(self, session):
        """The last click kept, and whether the favorite is still in the list now; only reads.

        True when there is no click kept, or when the list was read and compared with it, so that
        what you saw covers it; False when the list could not be read.
        """
        record = self.saved.last_click
        if record is None:
            say("  Nenhum clique gravado à espera da conferência da lista.")
            return True
        say(
            f"  Último clique gravado: {record['ativo']}, posição {record.get('posicao') or '?'}, com"
            f" {count(record['total'], 'elemento', 'elementos')} na lista, às {record.get('em_utc') or '?'} (relógio"
            f" local, UTC); troca {record.get('estado')}."
        )
        problem = scan.platform_problem(session)
        if problem is None and not self.favorites_selector:
            problem = "ainda não há seletor dos favoritos"
        if problem is not None:
            say(f"  A lista de favoritos não foi lida: {problem}. O clique continua gravado e é conferido na primeira leitura da lista.")
            return False
        favorites = scan.list_favorites(session, self.favorites_selector)
        if favorites.problem is not None:
            say(
                f"  A lista de favoritos não pôde ser lida agora ({favorites.problem}). O clique continua gravado e é"
                " conferido na primeira leitura da lista."
            )
            return False
        names = ", ".join(item.name for item in favorites.usable)
        if record["chave"] in favorites.keys:
            say(f"  {record['ativo']} continua na lista, que tem agora {count(favorites.total, 'elemento', 'elementos')}: {names}.")
            return True
        warn(
            f"  ATENÇÃO: {record['ativo']} não está mais na lista de favoritos: eram"
            f" {count(record['total'], 'elemento', 'elementos')} no clique, e agora são {favorites.total} ({names}). Se"
            " não foi você quem o tirou dos favoritos, o clique pode tê-lo tirado: confira a lista na tela."
        )
        return True

    def kept_click_hint(self):
        """After a stop: the last click kept for the next start, and how to clear it."""
        record = self.saved.last_click if self.saved is not None else None
        if record is None or record.get("estado") == PENDING:
            return
        say(
            f"O último clique, em {record['ativo']}, continua gravado em {self.state_path}: a próxima abertura confere"
            " a lista de novo. Se foi você quem mudou a lista de favoritos, abra o modo contínuo com --configure"
            " (no Windows, continuo_windows.cmd --configure) e confirme a lista; a confirmação apaga esse registro."
        )

    def closing_lines(self):
        if self.runs:
            say(f"MODO CONTÍNUO encerrado: {count(self.runs, 'execução', 'execuções')}.")
        if self.log.paths:
            say(f"Log do modo contínuo: {', '.join(str(path) for path in self.log.paths)}")


def csv_text(name):
    return f", CSV {name}" if name else ""


class ContinuousHooks:
    """operate's hooks of the visits, and the end of the run when the PAUSA starts.

    The PAUSA starts at the end of a visit, which runs only with no cycle open, and operate's
    after_visit sees it first: it refuses every decision and records the PAUSA of the operation.
    """

    def __init__(self, inner, scanning, supervisor, number):
        self.inner = inner
        self.scanning = scanning  # the scan.Scan
        self.supervisor = supervisor
        self.number = number

    def before_visit(self, session, favorite):
        self.inner.before_visit(session, favorite)

    def after_visit(self, session, visit):
        self.inner.after_visit(session, visit)
        pause = self.scanning.progress.paused
        if pause is not None:
            path = self.scanning.progress.csv_path
            self.supervisor.record_pause(pause, visit, self.number, path.name if path is not None else None)
            raise PauseStop()


class Watch:
    """Scan.after_cycle: the end of a run whose favorites are gone, or whose cycles have no complete reading."""

    def __init__(self):
        self.lost = 0  # cycles in a row without favorites to visit
        self.stalled = 0  # cycles in a row without any complete reading

    def __call__(self, cycle, favorites, complete):
        self.lost = self.lost + 1 if favorites.problem is not None else 0
        self.stalled = 0 if complete else self.stalled + 1
        if self.lost >= SCREEN_LOST_CYCLES:
            return EndOfRun(
                f"{count(self.lost, 'ciclo', 'ciclos seguidos')} sem favoritos para visitar ({favorites.problem}): a"
                " sessão pode ter terminado, ou a página, quebrado. A execução seguinte abre o navegador de novo e"
                " espera a tela de operações, sem clicar"
            )
        if self.stalled >= STALL_CYCLES:
            return EndOfRun(
                f"{count(self.stalled, 'ciclo', 'ciclos seguidos')} sem nenhuma leitura completa. A execução seguinte"
                " abre o navegador de novo e espera a tela de operações, sem clicar"
            )
        return None


class ClickJournal:
    """Scan.journal: the last click of the scan, kept in the state file as soon as it changes.

    clicking() keeps the favorite about to be clicked, as pendente, before the click: a run that
    ends between the click and the confirmation of its switch, even abruptly, leaves it so, and that
    becomes a PAUSA (Continuous.settle and Continuous.unfinished_click). confirmed() marks the
    switch confirmed by the asset field; checked(), called once a later list showed that the click
    removed no favorite, clears it; not_clicked() undoes clicking() when the element was refused
    before any click.
    """

    def __init__(self, state, number, cycle_of, now):
        self.saved = state
        self.number = number
        self.cycle_of = cycle_of
        self.now = now
        self._before = None

    def clicking(self, target, total):
        self._before = self.saved.last_click
        record = {
            "chave": target.key,
            "ativo": target.name,
            "posicao": target.position,
            "total": total,
            "estado": PENDING,
            "execucao": self.number,
            "ciclo": self.cycle_of(),
            "em_utc": utc_text(self.now()),
        }
        self.saved.change(last_click=record)  # a write that fails stops the run before the click

    def not_clicked(self):
        self.saved.change(last_click=self._before)

    def confirmed(self):
        record = self.saved.last_click
        if record is not None and record.get("estado") == PENDING:
            self.saved.change(last_click=dict(record, estado=CONFIRMED))

    def checked(self):
        if self.saved.last_click is not None:
            self.saved.change(last_click=None)


class ContinuousState:
    """runtime/continuo-estado.json: the PAUSA kept, the last click and the PAUSAS released.

    It is written whole to a temporary file, flushed to the disk and put in place with
    os.replace, so a cut leaves the last complete version. A file that cannot be read, or that
    is not what the program writes, stops the continuous mode before the browser opens: it may
    hold a PAUSA. The program never deletes nor replaces such a file.
    """

    KEYS = {"versao", "pausa", "ultimo_clique", "pausas_liberadas", "gravado_utc"}

    def __init__(self, path, now):
        self.path = path
        self.now = now
        self.pause = None
        self.last_click = None
        self.released = []

    @classmethod
    def load(cls, path, now):
        state = cls(path, now)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return state
        except OSError as error:
            raise StateError(f"não foi possível ler {path} ({error}); {cls.kept(path)}") from error
        try:
            data = json.loads(text)
        except ValueError as error:
            raise StateError(f"{path} não é um JSON válido ({error}); {cls.kept(path)}") from error
        problem = cls.problem(data)
        if problem is not None:
            raise StateError(f"{path} não é o estado que o programa grava ({problem}); {cls.kept(path)}")
        state.pause = data.get("pausa")
        state.last_click = data.get("ultimo_clique")
        state.released = list(data.get("pausas_liberadas") or [])
        return state

    @staticmethod
    def kept(path):
        return (
            "o modo contínuo não começa, porque o arquivo pode guardar uma PAUSA, e o programa não o apaga nem o"
            f" substitui. Confira-o; para começar sem ele, mude o nome de {path.name}"
        )

    @staticmethod
    def problem(data):
        if not isinstance(data, dict):
            return "precisa ser um objeto JSON"
        unknown = sorted(key for key in data if key not in ContinuousState.KEYS and not key.startswith("_"))
        if unknown:
            return f"chave desconhecida: {', '.join(unknown)}"
        if data.get("versao") != STATE_VERSION:
            return f"versão {data.get('versao')!r}, e o programa conhece só a {STATE_VERSION}"
        pause = data.get("pausa")
        if pause is not None and not (
            isinstance(pause, dict) and all(isinstance(pause.get(key), str) and pause.get(key) for key in ("ativo", "motivo"))
        ):
            return "a pausa não tem ativo e motivo"
        click = data.get("ultimo_clique")
        if click is not None:
            if not isinstance(click, dict):
                return "o último clique precisa ser um objeto"
            if not (isinstance(click.get("chave"), str) and click["chave"] and isinstance(click.get("ativo"), str)):
                return "o último clique não tem a chave e o ativo do favorito"
            total = click.get("total")
            if not (isinstance(total, int) and not isinstance(total, bool) and total >= 1):
                return "o último clique não tem o total de elementos da lista"
            if click.get("estado") not in (PENDING, CONFIRMED):
                return f"o estado {click.get('estado')!r} do último clique é desconhecido"
        released = data.get("pausas_liberadas")
        if released is not None and not (isinstance(released, list) and all(isinstance(item, dict) for item in released)):
            return "pausas_liberadas precisa ser uma lista de objetos"
        return None

    def change(self, **values):
        """Set pause, last_click or released and save; a write that fails leaves all of them as before."""
        before = {name: getattr(self, name) for name in values}
        for name, value in values.items():
            setattr(self, name, value)
        try:
            self.save()
        except OSError:
            for name, value in before.items():
                setattr(self, name, value)
            raise

    def save(self):
        data = {
            "_leia_me": STATE_NOTE,
            "versao": STATE_VERSION,
            "pausa": self.pause,
            "ultimo_clique": self.last_click,
            "pausas_liberadas": self.released,
            "gravado_utc": utc_text(self.now()),
        }
        write_json(self.path, data)


def write_json(path, data):
    """Write data to path through a temporary file flushed to the disk; a few tries on Windows' locks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    for attempt in range(1, WRITE_ATTEMPTS + 1):
        try:
            with open(temporary, "w", encoding="utf-8") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            replace_durably(temporary, path)
            return
        except PermissionError:  # an antivirus or an editor holding the file for a moment
            if attempt == WRITE_ATTEMPTS:
                raise
            time.sleep(WRITE_RETRY_SECONDS)


MOVEFILE_REPLACE_EXISTING = 0x1
MOVEFILE_WRITE_THROUGH = 0x8


def replace_durably(source, target):
    """Put source in the place of target, as os.replace does, and wait until the disk has the change.

    On Windows, MoveFileExW with MOVEFILE_WRITE_THROUGH returns only once the move is on the
    disk; elsewhere, the folder is flushed to the disk after os.replace.
    """
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move = kernel32.MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move.restype = ctypes.c_int
        if not move(str(source), str(target), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    os.replace(source, target)
    folder = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


def wait_for_screen(session, selectors, favorites_selector, ctx):
    """Wait, only reading the page, until the trading screen has been ready for SCREEN_READY_SECONDS.

    Ready: the page on pocketoption.com, the favorites selector finding usable favorites and the
    asset read. Nothing is clicked and nothing is read outside pocketoption.com, as during a login
    on another site. A favorites selector that became unsafe stops it, as it stops the scan. A
    closed window ends it with scan.WindowClosed.
    """
    say(
        f"AGUARDANDO A SESSÃO: o programa só lê a página, a cada {seconds_text(SCREEN_POLL_SECONDS)} s, sem clicar,"
        f" até a tela de operações e os favoritos aparecerem por {seconds_text(SCREEN_READY_SECONDS)} s seguidos."
        " Se a página pedir login, faça-o você mesmo na janela do navegador; depois, não use mais a janela."
    )
    ready_since = shown = shown_at = off_site_since = None
    while True:
        if session.is_closed():
            raise scan.WindowClosed()
        problem, off_site = screen_problem(session, selectors, favorites_selector, ctx)
        moment = ctx.monotonic()
        if not off_site:
            off_site_since = None
        elif off_site_since is None:
            off_site_since = moment
        elif moment - off_site_since >= SCREEN_OFF_SITE_SECONDS:
            raise EndOfRun(
                f"a página ficou fora de {observe.ALLOWED_DOMAIN} por {seconds_text(SCREEN_OFF_SITE_SECONDS / 60)} min"
                f" ({problem}): ela pode não ter carregado. A execução seguinte abre o navegador de novo"
            )
        if problem is None:
            if ready_since is None:
                ready_since = moment
                say(
                    "  Tela de operações e favoritos visíveis: a varredura começa em"
                    f" {seconds_text(SCREEN_READY_SECONDS)} s, se continuarem assim. Não use a janela do navegador."
                )
            elif moment - ready_since >= SCREEN_READY_SECONDS:
                say("Sessão pronta: a tela de operações e os favoritos continuaram visíveis.")
                return
            shown = None
        else:
            if ready_since is not None:
                ready_since = None
            if problem != shown or moment - shown_at >= SCREEN_REMINDER_SECONDS:
                say(f"  Sessão indisponível, às {utc_text(ctx.now())} (relógio local, UTC): {problem}. Esperando, sem clicar.")
                shown, shown_at = problem, moment
        session.wait(SCREEN_POLL_SECONDS)


def screen_problem(session, selectors, favorites_selector, ctx):
    """(why the trading screen is not ready now, or None; whether the page is outside pocketoption.com).

    It only reads, and only on pocketoption.com: the address is checked before the readings and
    again after them, and readings of a page that left it in between do not count.
    """
    url = session.current_url()
    if observe.url_problem(url) is not None:
        return f"a página atual ({observe.display_url(url)}) não está em {observe.ALLOWED_DOMAIN}", True
    favorites = scan.list_favorites(session, favorites_selector)
    asset = observe.read_field(session, "asset", selectors["asset"], ctx) if favorites.problem is None else None
    url = session.current_url()
    if observe.url_problem(url) is not None:
        return f"a página atual ({observe.display_url(url)}) não está em {observe.ALLOWED_DOMAIN}", True
    if favorites.unsafe:
        raise ObserverStopped(
            f"o seletor dos favoritos deixou de servir: {favorites.problem}. O modo contínuo parou, sem clicar;"
            " confira o seletor com --configure"
        )
    if favorites.problem is not None:
        return f"os favoritos não aparecem ({favorites.problem})", False
    if not asset.available:
        return f"o ativo selecionado não pôde ser lido ({asset.reason})", False
    return None, False


class KeepAwake:
    """Keeps Windows and its display from sleeping for lack of use while the continuous mode runs.

    SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED) at start()
    and SetThreadExecutionState(ES_CONTINUOUS) at stop(), from the thread that runs everything. It
    changes no setting of the system, and the state ends with the program, however it ends. It does
    not stop a lock of the screen by you, the power button, the lid of a notebook, nor an update
    that restarts the computer. On other systems it does nothing. set_state exists for tests.
    """

    def __init__(self, set_state=None):
        self._set_state = set_state if set_state is not None else execution_state_function()
        self.active = False

    def start(self):
        if self._set_state is None:
            say("Prevenção da suspensão: só no Windows; neste sistema, nada foi alterado.")
            return False
        if not self._set_state(AWAKE):
            warn(
                "AVISO: o Windows recusou a prevenção da suspensão (SetThreadExecutionState); o modo contínuo"
                " continua, mas o computador pode suspender ou desligar a tela pelas configurações de energia."
            )
            return False
        self.active = True
        say(
            "Prevenção da suspensão ligada: enquanto o modo contínuo rodar, o Windows não suspende nem desliga a"
            " tela por falta de uso. Ela não impede que você bloqueie a tela, o botão de energia, a tampa do"
            " notebook nem uma atualização que reinicie o computador."
        )
        return True

    def stop(self):
        if not self.active:
            return
        self.active = False
        self._set_state(ES_CONTINUOUS)
        say("Prevenção da suspensão desligada: valem de novo as configurações de energia do Windows.")


def execution_state_function():
    """kernel32's SetThreadExecutionState, or None outside Windows."""
    if os.name != "nt":
        return None
    try:
        function = ctypes.windll.kernel32.SetThreadExecutionState
    except (AttributeError, OSError):
        return None
    function.argtypes = [ctypes.c_uint]
    function.restype = ctypes.c_uint
    return function


class ContinuousLog:
    """Everything printed to the terminal, also in runtime/continuo-<data>-<hora>-<sufixo>.log.

    start() puts a tee on sys.stdout and sys.stderr: the terminal gets every text as before, and
    each line also goes to the log, after this computer's clock, in UTC, with | for the output and !
    for the warnings. reading() gives the questions of the terminal to the log, with ?, because on
    a console input() writes them past sys.stdout; what you type is never logged, as the link of
    the trading screen. failure() puts the traceback of an error that ends the program in the log.
    The lines before open() wait in memory, so that a start refused before runtime/ is used writes
    nothing. A file longer than MAX_LOG_BYTES goes on in a new one, and none is deleted. A write
    that fails is said once in the terminal; the log goes on in a new file from LOG_RETRY_SECONDS
    later, and the continuous mode never stops for it.
    """

    OUT, ERR, ASK = "|", "!", "?"

    def __init__(self, now):
        self.now = now
        self.paths = []
        self.failed = False
        self._dir = None
        self._file = None
        self._pending = []
        self._partial = {self.OUT: "", self.ERR: "", self.ASK: ""}
        self._streams = None
        self._retry_at = None

    def start(self):
        self._streams = (sys.stdout, sys.stderr)
        sys.stdout = _Tee(self, sys.stdout, self.OUT)
        sys.stderr = _Tee(self, sys.stderr, self.ERR)

    def stop(self):
        for kind in (self.OUT, self.ERR):
            self._end(kind)
        if self._streams is not None:
            sys.stdout, sys.stderr = self._streams
            self._streams = None
        self._close()

    def open(self, runtime_dir):
        """Start the file in runtime_dir, with the lines so far."""
        self._dir = runtime_dir
        self._new_file()
        if self._file is not None:
            say(f"Log do modo contínuo: {self.paths[-1]} (cada linha com o relógio local, em UTC).")

    def take(self, kind, text):
        joined = self._partial[kind] + text
        *lines, self._partial[kind] = joined.split("\n")
        for line in lines:
            self._line(kind, line)

    def reading(self, read_line):
        """read_line, with each question in the log; the answer is not logged."""

        def read(prompt=""):
            self._end(self.OUT)
            self._line(self.ASK, prompt.rstrip())
            try:
                return read_line(prompt)
            finally:
                self._end(self.OUT)  # a question written through sys.stdout, without the end of its line

        return read

    def failure(self, text):
        for line in text.rstrip("\n").split("\n"):
            self._line(self.ERR, line)

    def _end(self, kind):
        if self._partial[kind]:
            self._line(kind, self._partial[kind])
            self._partial[kind] = ""

    def _line(self, kind, text):
        entry = f"{utc_text(self.now())} {kind} {text}\n"
        if self._dir is None:
            self._pending.append(entry)
            return
        if self._file is None:
            if self._retry_at is None or self.now() < self._retry_at:
                return
            self._new_file()
            if self._file is None:
                return
        try:
            self._file.write(entry)
            self._file.flush()
            if self._file.tell() >= MAX_LOG_BYTES:
                self._new_file(previous=self.paths[-1])
        except OSError as error:
            self._failure(error)

    def _new_file(self, previous=None):
        self._close()
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            while True:
                path = self._dir / f"{LOG_PREFIX}{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}.log"
                if not path.exists():
                    break
            self._file = open(path, "x", encoding="utf-8")
            self.paths.append(path)
            if previous is not None:
                self._file.write(f"{utc_text(self.now())} | (continuação de {previous})\n")
            pending, self._pending = self._pending, []
            self._file.writelines(pending)
            self._file.flush()
            self._retry_at = None
        except OSError as error:
            self._failure(error)

    def _failure(self, error):
        self._close()
        self._retry_at = self.now() + timedelta(seconds=LOG_RETRY_SECONDS)
        if not self.failed and self._streams is not None:
            self._streams[1].write(
                f"AVISO: o log do modo contínuo não pôde ser gravado ({error}); o terminal continua com tudo, e um"
                f" arquivo novo é tentado a cada {seconds_text(LOG_RETRY_SECONDS)} s.\n"
            )
            self._streams[1].flush()
        self.failed = True

    def _close(self):
        file, self._file = self._file, None
        if file is not None:
            try:
                file.close()
            except OSError:
                pass


class _Tee:
    """A text stream that writes to stream and gives each text to the log too."""

    def __init__(self, log, stream, kind):
        self._log = log
        self._stream = stream
        self._kind = kind

    def write(self, text):
        written = self._stream.write(text)
        self._log.take(self._kind, text)
        return written

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


def seconds_text(value):
    return scan.seconds_text(value)
