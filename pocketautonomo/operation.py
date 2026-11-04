"""Simulated operation: the analysis' CALL/PUT decisions start progression cycles on a simulator.

SIMULATED EXECUTION ONLY. Every entry goes to paper_broker.PaperBroker, through
OrderExecutor and the Coordinator with its SQLite store: nothing is sent to the Pocket
Option and no money moves. The results are simulated with the quotes observed on this
computer and never feed the analysis, which reads only quotes read on the screen: the
scan's CSV files and, written by operate.py, the quotes read during the cycles, with their
origin.

Operator applies the rules of the operation:

1. First entry: consider() starts a cycle only with a CALL or PUT decision of a rule
   validated on separate data, still within its validity (until 5 s after the capture of
   its quote, analysis.DECISION_WINDOW_SECONDS) and with a payout of 90% or more. AGUARDAR
   starts nothing. The decision is consumed together with the cycle, in one transaction of
   the coordinator's store, so it never starts a second cycle, also after a restart.
2. The cycle keeps its asset and direction to the end. While busy() is true, the caller
   suspends the navigation between favorites and feeds the quotes of that asset to
   observe(), or calls tick() when it has none.
3. Stakes of 500, 1000, 2000 and 4000 cents: four attempts at most. Only a LOSS confirmed by
   the simulator moves on; WIN, DRAW, CANCELLED and REJECTED end the cycle, and there is
   never a fifth entry. The payout is checked only for the first entry.
4. Each attempt has its own window. The first entry's is the decision's validity. Each
   gale's opens when the LOSS of the previous attempt is confirmed and lasts
   gale_window seconds (GALE_WINDOW_SECONDS by default); its entry needs a quote of the
   asset captured after the window opened and at most MAX_QUOTE_AGE_SECONDS old. A window
   that closes without an entry ends the cycle as ABANDONED, with the reason, and an
   entry reserved when its window had already closed is closed as NOT_SUBMITTED: a late
   order is never sent.
5. An uncertain submission (UNKNOWN) is never sent again. The simulator is asked for the
   order of the reservation, which it saves before accepting it: with an order, the
   acceptance is linked and its result followed; without one, the simulator records that
   none will ever be accepted, and the attempt ends as REJECTED, ending the cycle.
6. A restart: recover() closes a reservation that was never submitted, follows a
   submitted one through the simulator, and ends a restored open cycle without a pending
   entry as ABANDONED, because the window of its next entry does not survive the restart.
   Then the coordinator resumes.
7. A storage failure, or an answer that breaks these rules, stops the operation in PAUSA
   (OperationStopped); the saved state is kept for the next start.

The states (state()) are COLETANDO, AGUARDANDO DECISÃO, CICLO ATIVO, PENDÊNCIA and PAUSA.
Every event goes to the operation's CSV, with the times of the reading, of the decision and
of the simulated submission kept apart; the last one is the simulator's time and proves
nothing about the broker's latency.
"""

import csv
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from pocketautonomo import analysis
from pocketautonomo.broker import Broker, OrderRequest
from pocketautonomo.coordinator import ConsumedDecision, CoordinatorError, DecisionAlreadyUsedError
from pocketautonomo.cycle import (
    ABANDONED,
    CALL,
    CANCELLED,
    DRAW,
    LOSS,
    MAX_ATTEMPTS,
    PUT,
    REJECTED,
    UNKNOWN,
    WIN,
)
from pocketautonomo.executor import (
    ACCEPTANCE_RECORDED,
    NO_ORDER_ID,
    NO_PENDING,
    PENDING,
    REJECTION_RECORDED,
    UNCERTAIN,
    OrderExecutor,
)
from pocketautonomo.paper_broker import DEADLINE_PASSED, MAX_QUOTE_AGE_SECONDS, NEVER_ACCEPTED
from pocketautonomo.progression import stake_for_attempt
from pocketautonomo.storage import StorageError
from pocketautonomo.submission import RESERVED, UNVERIFIED

COLLECTING = "COLETANDO"
WAITING_DECISION = "AGUARDANDO DECISÃO"
ACTIVE_CYCLE = "CICLO ATIVO"
PENDING_ENTRY = "PENDÊNCIA"
PAUSED = "PAUSA"
STATES = (COLLECTING, WAITING_DECISION, ACTIVE_CYCLE, PENDING_ENTRY, PAUSED)

FICTITIOUS_BALANCE_CENTS = 10_000  # only lets the reservations through; it is not a balance
GALE_WINDOW_SECONDS = 5.0
MIN_GALE_WINDOW, MAX_GALE_WINDOW = 1.0, 60.0

# The value of the execution column: whether the line's orders went to a platform.
SIMULATED = "simulada"
DEMONSTRATION = "demonstração (dados artificiais)"
REAL = "REAL (conta real)"  # pocket_broker.PocketBroker: orders on the account logged in the browser
REAL_ON_DEMO = "REAL (conta demo)"  # the same orders, on the platform's demo account
REAL_EXECUTIONS = (REAL, REAL_ON_DEMO)
# The words of the simulated operation, as the real execution says them in its lines.
REAL_WORDS = (
    ("entrada simulada enviada", "ordem real aceita pela conta"),
    ("resultado simulado", "resultado da conta"),
    ("ao simulador", "à conta"),
    ("do simulador", "da conta"),
    ("pelo simulador", "pela conta"),
    ("no simulador", "na conta"),
    ("este simulador", "esta conta"),
    ("o simulador", "a conta"),
    ("ordem simulada", "ordem real"),
    ("entrada simulada", "ordem real"),
    ("simulador", "conta"),
)
SESSION_LIMIT = "limite da sessão"


@dataclass(frozen=True)
class SessionLimits:
    """When the real execution stops sending for the rest of the run.

    full_losses: cycles that ended with four confirmed LOSS (default 1: the first one ends the
    sending). loss_cents: the net loss of the run, from the account's own profits. cycles: the
    cycles started in the run (None: no limit).
    """

    full_losses: int = 1
    loss_cents: int = 7500
    cycles: Optional[int] = None
SIMULATOR_TIME_NOTE = "tempo do simulador; não comprova a latência da corretora"

# The value of the event column.
DECISION_REFUSED = "decisão recusada"
DEADLINE_REFUSED = "entrada recusada: prazo vencido"
CYCLE_STARTED = "ciclo iniciado"
ENTRY_SUBMITTED = "entrada simulada enviada"
ENTRY_REJECTED = "entrada recusada pelo simulador"
ENTRY_NOT_SENT = "entrada não enviada"
ENTRY_UNCERTAIN = "envio incerto (UNKNOWN)"
ORDER_FOUND = "ordem simulada achada pela reserva"
ORDER_ABSENT = "reserva sem ordem simulada"
RESULT = "resultado simulado"
WINDOW_OPENED = "janela do gale aberta"
WINDOW_EXPIRED = "janela vencida"
CYCLE_ENDED = "ciclo encerrado"
RECOVERY = "recuperação"
STOPPED = "pausa"
SCAN_PAUSED = "PAUSA da varredura"  # the scan's data stopped being usable: no new cycle until the end of the run

OPERATION_COLUMNS = (
    "at_utc",  # this computer's clock, in UTC, when the event happened
    "execution",  # simulada, or demonstração (dados artificiais): never a real order
    "state",  # the state right after the event
    "event",
    "cycle_id",
    "asset",
    "direction",
    "attempt",  # 1 to 4
    "stake_cents",
    "decision_id",  # the decision that started the cycle
    "reservation_id",
    "simulated_order_id",  # SIM-<uuid4>, from the simulator
    "window_opens_utc",  # the window of the attempt: for the first entry, the decision's validity
    "window_closes_utc",
    "quote",  # the quote of the event: of the decision, of the entry or of the settlement
    "quote_captured_at_utc",
    "quote_age_ms",  # from the capture of the quote to the event
    "payout_percent",  # as read; it is checked only for the first entry
    "reading_ms",  # the reading of the screen that gave the quote: asset, quote, payout, asset
    "decision_ms",  # the local analysis' decision; empty for a gale, which needs no decision
    "simulated_submit_ms",  # the submission to the SIMULATOR: it proves nothing about the broker
    "result",  # WIN, LOSS, DRAW, CANCELLED, REJECTED or UNKNOWN of the attempt
    "outcome",  # how the cycle ended, when the event ended it
    "reason",
    # The real execution only (pocket_broker); empty in the simulated operation.
    "real_order_id",  # the order's id at the platform
    "real_submit_ms",  # the whole submission: panel checked, click, platform's acceptance
    "accept_ms",  # from the click to the platform's acceptance
    "profit_cents",  # the account's net result of the order
    "balance_cents",  # the account's balance after the event
    "session_net_cents",  # the run's net result so far, from the account's profits
)


class OperationStopped(Exception):
    """The operation cannot go on safely; the message tells why. The saved state is kept."""


class TimedBroker(Broker):
    """The simulator as the executor sees it: each submit_order() call is timed alone.

    last_ms is the duration of the last call, in milliseconds, without the coordinator's
    own writes before and after it.
    """

    def __init__(self, broker, monotonic):
        self.broker = broker
        self.monotonic = monotonic
        self.last_ms = None

    def submit_order(self, request):
        self.last_ms = None
        start = self.monotonic()
        try:
            return self.broker.submit_order(request)
        finally:
            self.last_ms = round((self.monotonic() - start) * 1000, 1)

    def get_order(self, broker_order_id):
        return self.broker.get_order(broker_order_id)


@dataclass(frozen=True)
class Window:
    """Where an attempt can be submitted: from opens to closes, this computer's clock in UTC."""

    cycle_id: int
    attempt: int
    opens: datetime
    closes: datetime
    # A quote read after this moment may enter; None: after opens. With the account, the expiry of the lost
    # order: its LOSS arrives a moment later, and the platform may hide the price on the chart right then.
    fresh_after: Optional[datetime] = None


@dataclass(frozen=True)
class Seen:
    """A quote of an asset read on the screen, with the payout read with it, when known."""

    asset: str
    captured_at: datetime
    price: float
    quote: str
    payout: object = None
    reading_ms: object = None
    payout_note: object = None  # why the payout is missing, or how it was confirmed


@dataclass(frozen=True)
class Consideration:
    """What consider() did with one decision."""

    started: bool
    reason: str
    cycle_id: Optional[int] = None


def decision_identifier(decision, horizon):
    """The id of a CALL/PUT decision: its asset, direction, quote's capture, rule and horizon.

    The same decision presented again has the same id; a decision from another quote, rule
    or horizon has another one.
    """
    rule = decision.validation.rule
    moment = decision.sample.at.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    return (
        f"{analysis.asset_key(decision.asset)}:{decision.action}:{moment}:"
        f"{rule.kind}-{rule.lookback_factor}:{horizon:g}"
    )


def utc_iso(moment):
    return None if moment is None else moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def hour(moment):
    """The time of moment, in UTC, to the millisecond, as in 12:00:05.123 UTC."""
    moment = moment.astimezone(timezone.utc)
    return f"{moment:%H:%M:%S}.{moment.microsecond // 1000:03d} UTC"


def money(cents):
    return f"R$ {cents // 100},{cents % 100:02d}"


def age_ms(captured_at, now):
    return round((now - captured_at).total_seconds() * 1000, 1)


def number_text(value):
    """value with a decimal comma, as in 92 or 92,5."""
    return f"{value:g}".replace(".", ",")


def cell(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        return utc_iso(value)
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


class OperationLog:
    """The events of an operation: one CSV line each, flushed at once, and a terminal line.

    The CSV, a new file at path, is created with the first event.
    """

    def __init__(self, path, execution=SIMULATED, say=None):
        self.path = path
        self.execution = execution
        self._say = say if say is not None else (lambda text: print(text, flush=True))
        self._file = None
        self._writer = None
        self.rows = []  # every event written, as dicts

    def say(self, text):
        self._say(text)

    def write(self, row):
        row = dict(row, execution=self.execution)
        if self._file is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self.path, "x", newline="", encoding="utf-8")
            self._writer = csv.writer(self._file)
            self._writer.writerow(OPERATION_COLUMNS)
            self._say(f"CSV da operação: {self.path}")
        self._writer.writerow([cell(row.get(name)) for name in OPERATION_COLUMNS])
        self._file.flush()
        self.rows.append(row)
        self._say(event_line(row))

    def close(self):
        if self._file is not None:
            self._file.close()


def event_line(row):
    """The terminal line of one event."""
    label = {
        DEMONSTRATION: "DEMONSTRAÇÃO", REAL: "EXECUÇÃO REAL", REAL_ON_DEMO: "EXECUÇÃO REAL (CONTA DEMO)",
    }.get(row["execution"], "OPERAÇÃO SIMULADA")
    parts = [label, row["state"], row["event"]]
    if row.get("cycle_id") is not None:
        parts.append(f"ciclo {row['cycle_id']}")
    if row.get("asset"):
        parts.append(f"{row['asset']} {row.get('direction') or ''}".strip())
    if row.get("attempt") is not None:
        attempt = f"tentativa {row['attempt']} de {MAX_ATTEMPTS}"
        if row.get("stake_cents") is not None:
            attempt += f", {money(row['stake_cents'])}"
        parts.append(attempt)
    if row.get("simulated_order_id"):
        parts.append(f"ordem {row['simulated_order_id']}")
    if row.get("real_order_id"):
        parts.append(f"ordem da conta {row['real_order_id']}")
    if row.get("result"):
        parts.append(f"resultado {row['result']}")
    if row.get("profit_cents") is not None:
        profit = row["profit_cents"]
        parts.append(f"líquido {'-' if profit < 0 else '+'}{money(abs(profit))}")
    if row.get("balance_cents") is not None:
        parts.append(f"saldo {money(row['balance_cents'])}")
    if row.get("outcome"):
        parts.append(f"ciclo terminou com {row['outcome']}")
    if row.get("quote"):
        quote = f"cotação {row['quote']}"
        age = row.get("quote_age_ms")
        if age is not None:
            shown = f"{age:.0f} ms" if abs(age) < 10_000 else f"{number_text(round(age / 1000, 1))} s"
            quote += f" (capturada {shown} antes)"
        parts.append(quote)
    if row.get("payout_percent") is not None:
        parts.append(f"payout {number_text(row['payout_percent'])}%")
    times = [
        f"{name} {row[key]:.0f} ms"
        for key, name in (("reading_ms", "leitura"), ("decision_ms", "decisão"))
        if row.get(key) is not None
    ]
    if row.get("simulated_submit_ms") is not None:
        times.append(f"envio ao simulador {row['simulated_submit_ms']:.0f} ms ({SIMULATOR_TIME_NOTE})")
    if row.get("real_submit_ms") is not None:
        accept = row.get("accept_ms")
        times.append(
            f"envio à conta {row['real_submit_ms']:.0f} ms"
            + (f" (clique até o aceite {accept:.0f} ms)" if accept is not None else "")
        )
    if times:
        parts.append(", ".join(times))
    if row.get("reason"):
        parts.append(row["reason"])
    return " | ".join(parts)


class Operator:
    """Runs the simulated operation over a Coordinator with a store and a PaperBroker.

    The coordinator must come straight from its store, paused: recover() resolves what the
    last run left and resumes it. now is the UTC clock shared with the broker and with the
    quotes' capture times; monotonic measures the durations. pause_collection() puts the
    operation in PAUSA until the end of the run, when the scan's data stops being usable.
    """

    def __init__(
        self,
        coordinator,
        broker,
        log,
        *,
        now,
        monotonic=time.perf_counter,
        horizon=analysis.DEFAULT_HORIZON,
        gale_window=GALE_WINDOW_SECONDS,
        gate=None,
        limits=None,
        trend=False,
    ):
        self.coordinator = coordinator
        self.broker = broker
        self._timed = TimedBroker(broker, monotonic)
        self.executor = OrderExecutor(coordinator, self._timed)
        self.log = log
        self.now = now
        self.monotonic = monotonic
        self.horizon = horizon
        self.gale_window = gale_window
        # gate() is None, or why no cycle may start now (execution disarmed, pause asked in the launcher).
        self.gate = gate
        self.limits = limits  # SessionLimits of the run, or None
        self.trend = trend  # the trend mode: a decision of the chart's reading may start a cycle without validation
        self.real = getattr(log, "execution", None) in REAL_EXECUTIONS
        self._id_key = "real_order_id" if self.real else "simulated_order_id"
        self.session = {"full_losses": 0, "net_cents": 0, "cycles": 0}
        self.window = None  # the Window of the next gale, while it is open
        self.validated = {}  # asset key -> asset, for the assets whose last decision had a validated rule
        self.recovering = True  # until recover() resolved what the last run left
        self.stop_reason = None
        self.collection_paused = None  # why the scan is in PAUSA: no new cycle until the end of the run
        self.counts = {"cycles": 0, "entries": 0, "deadline": 0, "refused": 0, "windows": 0}
        self._latest = {}  # asset key -> the latest Seen
        self._shown = None  # the last state shown in the terminal
        self.last_event = None  # the last row written, for the launcher's panel

    # What the caller asks.

    def state(self):
        return self._state_of(self.coordinator.status())

    def busy(self):
        """Whether a cycle is open or an entry pending: the navigation stays suspended."""
        status = self.coordinator.status()
        return status.pending is not None or status.is_active

    def held_asset(self):
        """The asset of the open cycle, whose quotes the caller must feed to observe()."""
        return self.coordinator.status().asset

    def pause_collection(self, reason):
        """PAUSA until the end of the run: the scan says that its data can no longer be used.

        From now on, a CALL or PUT decision is refused, and the coordinator is paused as soon as
        no cycle is open, so nothing new starts. An entry already pending keeps what was recorded
        and is still settled by the simulator; nothing is cancelled or sent again. The pause is
        not saved: the next run starts from recover(), as always. Calling again keeps the first
        reason.

        The scan calls it after a visit, and a visit only runs with no cycle open (the navigation
        is suspended during a cycle), so in the scan it never finds a cycle open. It does not
        change the cycle rules: called during a cycle, it would let that cycle's gales go on.
        """
        self._check_running()
        first = self.collection_paused is None
        if first:
            self.collection_paused = reason
        if not self.busy() and not self.coordinator.status().is_paused:
            self._guard(self.coordinator.pause, "a pausa do coordenador falhou")
        if first:
            self._write(
                SCAN_PAUSED,
                {},
                reason=f"{reason}; nenhuma decisão inicia ciclo até o fim desta execução, e as pendências ficam como estão",
            )
            self._show_state()

    # Decisions and quotes.

    def consider(self, decision, reading_ms=None, decision_ms=None):
        """Start a cycle with decision when it allows a first entry; return a Consideration."""
        self._check_running()
        key = analysis.asset_key(decision.asset)
        validation = decision.validation
        if validation is not None:  # a visit without data says nothing about the rule
            if validation.validated:
                self.validated[key] = decision.asset
            else:
                self.validated.pop(key, None)
        if decision.action not in (CALL, PUT):
            self._show_state()
            return Consideration(False, f"{decision.action}: {decision.reason}")
        now = self.now()
        sample = decision.sample
        row = {"asset": decision.asset, "direction": decision.action, "reading_ms": reading_ms, "decision_ms": decision_ms}
        if sample is not None:
            row.update(
                quote=sample.quote, quote_captured_at_utc=sample.at, quote_age_ms=age_ms(sample.at, now),
                payout_percent=sample.payout, window_opens_utc=sample.at, window_closes_utc=decision.valid_until,
            )
        if self.collection_paused is not None:
            return self._refuse(row, f"varredura em PAUSA: {self.collection_paused}")
        blocked = self.gate() if self.gate is not None else None
        if blocked:
            return self._refuse(row, blocked)
        trend = self.trend and getattr(validation, "trend", False)
        if not (validation is not None and (validation.validated or trend) and validation.rule is not None):
            return self._refuse(row, "a decisão não vem de uma regra validada em dados separados")
        if sample is None or decision.valid_until is None:
            return self._refuse(row, "a decisão não tem a cotação e o prazo de entrada")
        identifier = decision_identifier(decision, self.horizon)
        row["decision_id"] = identifier
        used = self.coordinator.decision_cycle(identifier)
        if used is not None:
            return self._refuse(row, f"decisão já usada pelo ciclo {used}: uma decisão inicia um ciclo no máximo")
        status = self.coordinator.status()
        if self.recovering or not status.can_start_cycle or status.recovery_pending:
            return self._refuse(row, self._busy_reason(status))
        if now > decision.valid_until:
            self.counts["deadline"] += 1
            return self._refuse(
                row,
                f"a decisão valia até {hour(decision.valid_until)} e agora são {hour(now)}: nenhuma"
                " ordem atrasada é enviada",
                DEADLINE_REFUSED,
            )
        payout = sample.payout
        if payout is None or payout < analysis.ELIGIBLE_PAYOUT:
            shown = "indisponível" if payout is None else f"{number_text(payout)}%"
            return self._refuse(
                row, f"payout {shown}: a primeira entrada exige {number_text(analysis.ELIGIBLE_PAYOUT)}% ou mais"
            )
        if sample.at > now:
            return self._refuse(row, "a cotação da decisão tem horário depois de agora")
        rule = validation.rule.describe(self.horizon)
        consumed = ConsumedDecision(
            identifier, utc_iso(sample.at), utc_iso(decision.valid_until), utc_iso(now), float(payout), rule
        )
        try:
            cycle_id = self.coordinator.start_cycle(decision.asset, decision.action, consumed)
        except DecisionAlreadyUsedError as error:
            return self._refuse(row, f"decisão já usada: {error}")
        except StorageError as error:
            self._stop(f"a gravação do ciclo e da decisão falhou ({describe(error)})", row)
        except CoordinatorError as error:
            return self._refuse(row, f"o coordenador não iniciou o ciclo: {error}")
        self.counts["cycles"] += 1
        self.session["cycles"] += 1
        self._write(
            CYCLE_STARTED,
            row,
            cycle_id=cycle_id,
            reason=(
                f"decisão {decision.action} da regra {rule}, payout {number_text(payout)}% na primeira"
                f" entrada; o ciclo mantém {decision.asset} e {decision.action} até o fim"
            ),
        )
        seen = Seen(decision.asset, sample.at, sample.price, sample.quote, payout, reading_ms)
        self._feed(seen)  # the decision's quote is the entry quote of the simulator
        self._enter(Window(cycle_id, 1, sample.at, decision.valid_until), seen, decision_ms)
        self._show_state()
        return Consideration(True, CYCLE_STARTED, cycle_id)

    def observe(self, asset, captured_at, price, quote=None, payout=None, reading_ms=None):
        """Take a quote read on the screen during a cycle, then advance as tick() does."""
        self._check_running()
        self._feed(Seen(asset, captured_at, float(price), quote if quote is not None else f"{price:g}", payout, reading_ms))
        self.tick()

    def tick(self):
        """Advance with what is known now: results, uncertain submissions and gale windows."""
        self._check_running()
        status = self.coordinator.status()
        if status.pending is not None and status.submission_state != RESERVED:
            if status.broker_order_id is None:
                self._resolve_unlinked()
            else:
                self._reconcile()
        if self.recovering:
            self._finish_recovery()
        window = self.window
        if window is not None:
            status = self.coordinator.status()
            now = self.now()
            if status.pending is not None:
                pass  # its entry is pending already
            elif now > window.closes:
                self._expire(window, now)
            else:
                seen = self._latest.get(analysis.asset_key(status.asset))
                if (
                    seen is not None
                    and seen.captured_at > (window.fresh_after or window.opens)  # read after the lost order ended
                    and 0 <= (now - seen.captured_at).total_seconds() <= MAX_QUOTE_AGE_SECONDS
                ):
                    self.window = None
                    self._enter(window, seen)
        self._show_state()

    def recover(self):
        """Resolve what the last run left, then resume the coordinator; call it once, first."""
        self._check_running()
        status = self.coordinator.status()
        pending = status.pending
        if pending is not None:
            if status.submission_state == RESERVED:
                self._guard(
                    lambda: self.coordinator.close_not_submitted(pending.reservation_id),
                    "o encerramento da reserva que nunca foi enviada falhou",
                )
                self._write(
                    RECOVERY,
                    self._pending_row(pending),
                    reason=(
                        "a reserva de antes do reinício nunca foi enviada (RESERVED): encerrada"
                        " como NOT_SUBMITTED, e o ciclo, abandonado"
                    ),
                )
                self._cycle_ended()
            elif status.broker_order_id is None:
                self._resolve_unlinked()
            else:
                self._reconcile()
        self._finish_recovery()
        self._show_state()

    # Entries.

    def _enter(self, window, seen, decision_ms=None):
        """Reserve the attempt of window and submit it to the simulator, only within window."""
        reservation = self._guard(
            lambda: self.coordinator.reserve_next_entry(FICTITIOUS_BALANCE_CENTS),
            f"a reserva da tentativa {window.attempt} falhou",
        )
        if reservation is None or reservation.attempt != window.attempt:
            self._stop(f"o ciclo {window.cycle_id} não liberou a tentativa {window.attempt}")
        row = self._pending_row(
            reservation,
            window_opens_utc=window.opens,
            window_closes_utc=window.closes,
            quote=seen.quote,
            quote_captured_at_utc=seen.captured_at,
            payout_percent=seen.payout,
            reading_ms=seen.reading_ms,
            decision_ms=decision_ms,
        )
        now = self.now()
        if now > window.closes:
            self._guard(
                lambda: self.coordinator.close_not_submitted(reservation.reservation_id),
                "o encerramento da reserva com o prazo vencido falhou",
            )
            self.counts["deadline"] += 1
            self._write(
                DEADLINE_REFUSED,
                row,
                quote_age_ms=age_ms(seen.captured_at, now),
                reason=(
                    f"o prazo da tentativa {window.attempt} venceu às {hour(window.closes)}, entre a"
                    f" reserva e o envio (agora {hour(now)}): a reserva foi encerrada como"
                    " NOT_SUBMITTED, sem ordem atrasada, e o ciclo, abandonado"
                ),
            )
            self._cycle_ended()
            return
        # The deadline goes with the order: the simulator rejects it explicitly when the
        # start of the submission, saved first, takes it past the window.
        self._timed.last_ms = None
        report = self.executor.submit(reservation.reservation_id, window.closes)
        submit_key = "real_submit_ms" if self.real else "simulated_submit_ms"
        row.update({submit_key: self._timed.last_ms}, quote_age_ms=age_ms(seen.captured_at, now))
        if report.outcome == ACCEPTANCE_RECORDED:
            self.counts["entries"] += 1
            order = self._guard(lambda: self.broker.order(report.broker_order_id), "a leitura da ordem simulada falhou")
            if self.real:
                timings = getattr(self.broker, "last_ms", {}) or {}
                self._write(
                    ENTRY_SUBMITTED,
                    row,
                    real_order_id=order.order_id,
                    quote=order.entry_quote,
                    quote_captured_at_utc=order.entry_captured_at,
                    accept_ms=timings.get("accept_ms"),
                    payout_percent=order.payout_percent if order.payout_percent is not None else seen.payout,
                    balance_cents=order.balance_after_open_cents,
                    session_net_cents=self.session["net_cents"],
                    reason=(
                        f"aceita pela conta {self.log.execution}: {money(order.stake_cents)} em {order.asset}"
                        f" {order.direction}, 300 s, abertura {order.entry_quote} (cotação na tela"
                        f" {order.screen_entry_quote or 'indisponível'}); vence às {hour(order.expires_at)}"
                        + (f"; {order.note}" if order.note else "")
                    ),
                )
                self._check_broker()
            else:
                self._write(
                    ENTRY_SUBMITTED,
                    row,
                    simulated_order_id=order.order_id,
                    quote=order.entry_quote,
                    quote_captured_at_utc=order.entry_captured_at,
                    reason=(
                        f"vence às {hour(order.expires_at)} e é liquidada pela primeira cotação de"
                        f" {order.asset} lida a partir daí, até {hour(order.settle_by)}"
                    ),
                )
        elif report.outcome == REJECTION_RECORDED:
            reason = report.response.reason or "sem motivo"
            event = ENTRY_REJECTED
            if reason.startswith(DEADLINE_PASSED):  # the window closed during the submission
                event = DEADLINE_REFUSED
                self.counts["deadline"] += 1
            self._write(event, row, result=REJECTED, reason=f"o simulador recusou a entrada: {reason}")
            self._cycle_ended()
        elif report.outcome == UNCERTAIN:
            self.counts["entries"] += 1  # it may have been accepted: it is never sent again
            errors = "; ".join(report.errors)
            self._write(
                ENTRY_UNCERTAIN,
                row,
                result=UNKNOWN if self.coordinator.status().is_blocked else None,
                **{self._id_key: report.broker_order_id},
                reason=f"{errors}; nada é reenviado: a reserva fica pendente até a consulta ao simulador",
            )
            self._check_failure(f"a resposta do simulador não pôde ser gravada ({errors})")
            self._resolve_unlinked()
        else:  # NOT_STARTED: nothing was sent
            errors = "; ".join(report.errors)
            self._check_failure(f"o início do envio não pôde ser gravado ({errors})")
            self._guard(
                lambda: self.coordinator.close_not_submitted(reservation.reservation_id),
                "o encerramento da reserva que não foi enviada falhou",
            )
            self._write(
                ENTRY_NOT_SENT,
                row,
                reason=f"o envio não começou ({errors}): a reserva foi encerrada como NOT_SUBMITTED",
            )
            self._cycle_ended()

    def _resolve_unlinked(self):
        """A pending reservation sent without a linked order id: ask the simulator by reservation."""
        status = self.coordinator.status()
        pending = status.pending
        if pending is None or status.broker_order_id is not None or status.submission_state == RESERVED:
            return
        if status.submission_state == UNVERIFIED:
            self._stop(
                f"a reserva {pending.reservation_id} é {UNVERIFIED}, de antes do registro dos envios,"
                " e este simulador não responde por ela"
            )
        entry = pending.entry

        def lookup():
            return self._guard(
                lambda: self.broker.find_order_for(pending.reservation_id),
                "a consulta ao simulador pela reserva falhou",
            )

        found = lookup()
        if found is None:
            request = OrderRequest(pending.reservation_id, entry.asset, entry.direction, entry.stake_cents)
            absent = self._guard(lambda: self.broker.confirm_absent(request), "o registro da reserva sem ordem falhou")
            if not absent:  # an order appeared after the lookup: look it up again
                found = lookup()
                if found is None or found.order_id is None:
                    self._stop(
                        f"o simulador não confirmou nem negou a ordem da reserva {pending.reservation_id}"
                    )
        row = self._pending_row(pending)
        if found is not None and found.order_id is not None:
            self._guard(
                lambda: self.coordinator.confirm_acceptance(pending.reservation_id, found.order_id),
                "a gravação da ordem achada pela reserva falhou",
            )
            self._write(
                ORDER_FOUND,
                row,
                **{self._id_key: found.order_id},
                quote=found.entry_quote,
                reason=(
                    "a consulta ao simulador pela reserva achou a ordem simulada; o resultado dela"
                    f" é acompanhado, e nada foi reenviado (vence às {hour(found.expires_at)})"
                ),
            )
            return
        note = found.note if found is not None and found.note else NEVER_ACCEPTED
        self._guard(
            lambda: self.coordinator.record_result(pending.reservation_id, REJECTED),
            "a gravação da recusa confirmada pelo simulador falhou",
        )
        self._write(
            ORDER_ABSENT,
            row,
            result=REJECTED,
            reason=(
                f"o simulador confirmou que nenhuma ordem foi aceita para a reserva ({note}); a"
                " tentativa termina como REJECTED, sem reenvio"
            ),
        )
        self._cycle_ended()

    def _reconcile(self):
        """Look up the pending order and apply its result: the next gale's window, or the end."""
        pending = self.coordinator.status().pending
        report = self.executor.reconcile_pending()
        if report.outcome == NO_PENDING or (report.outcome == PENDING and report.error is None):
            return
        if report.outcome == NO_ORDER_ID:
            self._resolve_unlinked()
            return
        if report.outcome == PENDING or report.result is None:
            self._stop(f"a consulta da ordem simulada {report.broker_order_id} falhou: {report.error}")
        if report.error is not None:  # RECORD_FAILED
            self._check_failure(f"o resultado {report.result} da ordem {report.broker_order_id} não foi gravado")
            self._stop(f"o resultado {report.result} da ordem {report.broker_order_id} não foi gravado: {report.error}")
        order = self._guard(lambda: self.broker.order(report.broker_order_id), "a leitura da ordem simulada falhou")
        attempt = pending.attempt
        row = self._pending_row(
            pending,
            quote=order.exit_quote,
            quote_captured_at_utc=order.exit_captured_at,
            **{self._id_key: order.order_id},
        )
        if self.real:
            profit = getattr(order, "profit_cents", None)
            if profit is not None:
                self.session["net_cents"] += profit
            row.update(
                profit_cents=profit, balance_cents=getattr(order, "balance_after_close_cents", None),
                payout_percent=getattr(order, "payout_percent", None), session_net_cents=self.session["net_cents"],
            )
            reason = settlement_reason(order) + (f"; {order.note}" if order.note else "")
        else:
            reason = settlement_reason(order)
        self._write(RESULT, row, result=report.result, reason=reason)
        status = self.coordinator.status()
        if not status.is_active:
            self._cycle_ended()
            return
        if self.recovering:
            return  # a restored cycle releases no gale: _finish_recovery() abandons it
        self._prepare(stake_for_attempt(status.attempt))  # the gale's amount, before its window
        now = self.now()
        expiry = getattr(order, "expires_at", None) if self.real else None
        fresh_after = min(expiry, now) if isinstance(expiry, datetime) else None
        self.window = Window(status.cycle_id, status.attempt, now, now + timedelta(seconds=self.gale_window), fresh_after)
        since = f", lida depois do vencimento ({hour(fresh_after)})," if fresh_after is not None else ""
        self._write(
            WINDOW_OPENED,
            self._row(attempt=status.attempt, stake_cents=stake_for_attempt(status.attempt)),
            window_opens_utc=self.window.opens,
            window_closes_utc=self.window.closes,
            reason=(
                f"LOSS confirmada na tentativa {attempt}: a tentativa {status.attempt} precisa de uma"
                f" cotação nova de {status.asset}{since} até {hour(self.window.closes)}; sem nova decisão,"
                " mesmo ativo e mesma direção, sem exigir payout"
            ),
        )

    def _expire(self, window, now):
        self.window = None
        self.counts["windows"] += 1
        self._guard(self.coordinator.abandon_cycle, "o abandono do ciclo com a janela vencida falhou")
        status = self.coordinator.status()
        self._write(
            WINDOW_EXPIRED,
            self._row(attempt=window.attempt, stake_cents=stake_for_attempt(window.attempt)),
            window_opens_utc=window.opens,
            window_closes_utc=window.closes,
            reason=(
                f"janela da tentativa {window.attempt} vencida: nenhuma cotação nova de {status.asset}"
                f" entre {hour(window.opens)} e {hour(window.closes)} (agora {hour(now)}); o ciclo foi"
                " interrompido, sem ordem atrasada"
            ),
        )
        self._cycle_ended()

    def _cycle_ended(self):
        self.window = None
        status = self.coordinator.status()
        if status.is_active or status.cycle_id is None:
            return
        outcome = status.outcome
        if outcome == LOSS:
            detail = f"quarta LOSS, na tentativa {status.attempt}: sem quinta entrada"
        elif outcome == ABANDONED:
            detail = f"interrompido antes da tentativa {status.attempt}"
        else:
            detail = f"{outcome} na tentativa {status.attempt}"
        if outcome == LOSS:
            self.session["full_losses"] += 1
        limit = self._limit_reached()
        self._write(
            CYCLE_ENDED,
            self._row(attempt=status.attempt),
            outcome=outcome,
            session_net_cents=self.session["net_cents"] if self.real else None,
            reason=f"{detail}; " + (
                f"{SESSION_LIMIT}: {limit}" if limit else "a busca de oportunidades válidas recomeça"
            ),
        )
        if limit:
            self.pause_collection(f"{SESSION_LIMIT}: {limit}")
        else:
            self._prepare(stake_for_attempt(1))

    def _limit_reached(self):
        """Why the run's limits stop the sending now, or None."""
        limits = self.limits
        if limits is None:
            return None
        session = self.session
        if limits.full_losses is not None and session["full_losses"] >= limits.full_losses:
            return f"{session['full_losses']} ciclo(s) com quatro LOSS confirmadas nesta execução"
        if limits.loss_cents is not None and -session["net_cents"] >= limits.loss_cents:
            return f"perda líquida de {money(-session['net_cents'])} nesta execução, limite {money(limits.loss_cents)}"
        if limits.cycles is not None and session["cycles"] >= limits.cycles:
            return f"{session['cycles']} ciclos iniciados nesta execução, limite {limits.cycles}"
        return None

    def _prepare(self, stake_cents):
        """Let a real broker set the next order's time and amount ahead; nothing is sent."""
        prepare = getattr(self.broker, "prepare", None)
        if prepare is None or self.stop_reason is not None or self.collection_paused is not None:
            return
        problem = prepare(stake_cents)
        if problem:
            self.log.say(f"  AVISO: {problem}; a ordem confere tudo de novo antes do clique")

    def _check_broker(self):
        """A real order that broke an expectation stops every new order; its result is still followed."""
        fault = getattr(self.broker, "fault", None)
        if fault and self.collection_paused is None:
            self.pause_collection(f"execução real parada: {fault}")

    def _finish_recovery(self):
        if not self.recovering:
            return
        status = self.coordinator.status()
        if status.pending is not None:
            return  # the restored entry is still waiting for its simulated result
        if status.is_active:
            self._guard(self.coordinator.abandon_cycle, "o abandono do ciclo restaurado falhou")
            self._write(
                RECOVERY,
                self._row(attempt=status.attempt),
                reason=(
                    "ciclo restaurado aberto: a janela da próxima entrada não sobrevive ao reinício;"
                    " o ciclo foi abandonado, sem nova entrada"
                ),
            )
            self._cycle_ended()
        self._check_failure()
        self._guard(self.coordinator.resume, "a retomada do coordenador falhou")
        self.recovering = False

    # Helpers.

    def _feed(self, seen):
        self._guard(
            lambda: self.broker.observe(seen.asset, seen.captured_at, seen.price, seen.quote),
            "o simulador não aceitou a cotação",
        )
        self._latest[analysis.asset_key(seen.asset)] = seen

    def _refuse(self, row, reason, event=DECISION_REFUSED):
        self.counts["refused"] += 1
        self._write(event, row, reason=reason)
        self._show_state()
        return Consideration(False, reason)

    def _busy_reason(self, status):
        if self.recovering:
            return "a recuperação do estado gravado ainda não terminou"
        if status.pending is not None:
            return f"a tentativa {status.attempt} do ciclo {status.cycle_id} está pendente"
        if status.is_active:
            return f"o ciclo {status.cycle_id} ({status.asset} {status.direction}) está em andamento"
        if status.submission_failure is not None:
            return f"novos envios parados por uma falha de gravação ({status.submission_failure})"
        return "a operação está em pausa"

    def _row(self, **values):
        status = self.coordinator.status()
        row = {
            "cycle_id": status.cycle_id, "asset": status.asset, "direction": status.direction,
            "decision_id": status.decision_id,
        }
        row.update(values)
        return row

    def _pending_row(self, reservation, **values):
        entry = reservation.entry
        status = self.coordinator.status()
        row = {
            "cycle_id": reservation.cycle_id, "asset": entry.asset, "direction": entry.direction,
            "attempt": entry.attempt, "stake_cents": entry.stake_cents, "decision_id": status.decision_id,
            "reservation_id": reservation.reservation_id, self._id_key: status.broker_order_id,
        }
        row.update(values)
        return row

    def _write(self, event, row, **values):
        row = dict(row, **values)
        row.update(at_utc=self.now(), event=self._words(event), state=self.state())
        if row.get("reason"):
            row["reason"] = self._words(row["reason"])
        self.log.write(row)
        self.last_event = row

    def _words(self, text):
        """text as the real execution says it: the account instead of the simulator."""
        if not self.real or not text:
            return text
        for simulated, real in REAL_WORDS:
            text = text.replace(simulated, real)
        return text

    def _show_state(self):
        status = self.coordinator.status()
        state = self._state_of(status)
        if state != self._shown:
            self._shown = state
            self.log.say(self._words(f"ESTADO: {state} — {self._explain(state, status)}"))

    def _state_of(self, status):
        if self.stop_reason is not None:
            return PAUSED
        if status.pending is not None:
            return PENDING_ENTRY
        if status.is_paused or status.submission_failure is not None or self.collection_paused is not None:
            return PAUSED
        if status.is_active:
            return ACTIVE_CYCLE
        return WAITING_DECISION if self.validated else COLLECTING

    def _explain(self, state, status):
        if state == PENDING_ENTRY:
            entry = status.pending.entry
            text = (
                f"tentativa {entry.attempt} de {MAX_ATTEMPTS}, {money(entry.stake_cents)}, de {entry.asset}"
                f" {entry.direction}, no simulador: aguardando o resultado simulado"
            )
            if status.is_blocked:
                text += "; a resposta do envio ficou incerta (UNKNOWN), e nada é reenviado"
            return text + "; navegação entre favoritos suspensa"
        if state == PAUSED:
            if self.stop_reason is not None:
                if status.pending is not None:
                    return f"{self.stop_reason}; a tentativa {status.attempt} continua pendente, como foi gravada"
                return self.stop_reason
            if self.recovering:
                return "recuperação do estado gravado, antes de qualquer ciclo novo"
            if self.collection_paused is not None:
                return f"varredura em PAUSA: {self.collection_paused}; nenhuma decisão inicia ciclo até o fim desta execução"
            return self._busy_reason(status)
        if state == ACTIVE_CYCLE:
            window = self.window
            text = f"ciclo {status.cycle_id} em {status.asset} {status.direction}, tentativa {status.attempt}"
            if window is not None:
                text += f", com janela até {hour(window.closes)}"
            return text + "; navegação entre favoritos suspensa"
        if state == WAITING_DECISION:
            names = ", ".join(sorted(self.validated.values()))
            assets = "1 ativo" if len(self.validated) == 1 else f"{len(self.validated)} ativos"
            return (
                f"regra validada em {assets} ({names}); um ciclo só começa com uma decisão CALL/PUT"
                " atual, dentro do prazo e com payout de 90% ou mais"
            )
        return "sem regra validada, a análise responde AGUARDAR; a coleta de dados continua"

    def _guard(self, action, what):
        """Run action; a storage failure or a refusal of the coordinator stops the operation."""
        try:
            return action()
        except (StorageError, CoordinatorError, KeyError, ValueError) as error:
            self._stop(f"{what} ({describe(error)})")

    def _check_failure(self, what=None):
        failure = self.coordinator.status().submission_failure
        if failure is not None:
            self._stop(f"{what or 'uma gravação falhou'}; novos envios parados ({failure})")

    def _check_running(self):
        if self.stop_reason is not None:
            raise OperationStopped(self.stop_reason)

    def _stop(self, reason, row=None):
        """Pause the coordinator, write the reason and raise OperationStopped."""
        self.stop_reason = reason
        try:
            self.coordinator.pause()
        except CoordinatorError:
            pass  # closed: nothing can start anyway
        self._write(
            STOPPED,
            dict(row or {}),
            reason=f"{reason}; a operação parou, e o estado gravado fica para a próxima abertura",
        )
        self._show_state()
        raise OperationStopped(reason)


def settlement_reason(order):
    """Why the simulator settled order as it did."""
    if order.state == CANCELLED:
        return f"{order.note}: o simulador cancelou a ordem, sem inventar resultado"
    movement = {
        WIN: f"a favor de {order.direction}",
        LOSS: f"contra {order.direction}",
        DRAW: "igual à entrada (empate)",
    }[order.state]
    return (
        f"entrada {order.entry_quote}, liquidação {order.exit_quote} lida às {hour(order.exit_captured_at)}"
        f" (vencimento {hour(order.expires_at)}): cotação {movement}"
    )


def describe(error):
    return f"{type(error).__name__}: {error}"
