"""Statistics of the orders, apart for the simulation and for the real execution: python -m pocketautonomo.stats.

Each sample comes from its own files in runtime/ and is never mixed with another:

- simulação: operacao-simulada.db and operacao-simulada-ordens.db (paper_broker);
- real (conta real): operacao-real.db and operacao-real-ordens.db (pocket_broker);
- real (conta demo): operacao-real-demo.db and operacao-real-demo-ordens.db.

For each: orders, win rate with its 95% Wilson interval, net result, by asset, by rule, by gale level,
cycles won and lost, the longest run of losses, the largest drawdown, the average payout, the
inconclusive orders and the refusals, and the line of every attempt (result, loss accumulated in the
cycle, recovery, net, payout). The real net is the account's own profit of each order; the simulated
one uses the payout of the decision that started the cycle. AGUARDAR reasons come from the scan's CSV
files; the latency of each step (mean, p95, p99) from the operation's and the timings' CSV files.

No rule is called profitable here: below MIN_SAMPLE settled orders the line says that the sample
is too small, and above it the interval says how much the rate can still be off.

It only reads the files, without their locks; an operation may be running.
"""

import argparse
import csv
import json
import math
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

MIN_SAMPLE = 200  # settled orders before a rate means anything (analysis.MIN_EVAL_TRADES)
Z95 = 1.96
SAMPLES = (
    ("simulação", "operacao-simulada.db", "operacao-simulada-ordens.db", "paper_orders", "operacao-2"),
    ("real (conta real)", "operacao-real.db", "operacao-real-ordens.db", "real_orders", "operacao-real-2"),
    ("real (conta demo)", "operacao-real-demo.db", "operacao-real-demo-ordens.db", "real_orders", "operacao-real-demo-"),
)
SETTLED = ("WIN", "LOSS", "DRAW")
STAGES = (
    ("reading_ms", "leitura da tela"),
    ("decision_ms", "decisão"),
    ("simulated_submit_ms", "envio ao simulador"),
    ("real_submit_ms", "envio à conta (painel + clique + aceite)"),
    ("accept_ms", "clique até o aceite da conta"),
)
TIMING_STAGES = (
    ("total_ms", "leitura da cotação (total)"),
    ("screenshot_ms", "captura"),
    ("decode_ms", "decodificação"),
    ("detection_ms", "detecção do marcador"),
    ("ocr_ms", "OCR"),
)


def wilson(wins, total, z=Z95):
    if total == 0:
        return None
    rate = wins / total
    centre = (rate + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return max(0.0, centre - half), min(1.0, centre + half)


def percentile(values, share):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * share
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def latency(values):
    values = [value for value in values if value is not None]
    if not values:
        return None
    return {
        "n": len(values), "media": round(sum(values) / len(values), 1), "p95": round(percentile(values, 0.95), 1),
        "p99": round(percentile(values, 0.99), 1), "max": round(max(values), 1),
    }


@dataclass
class Attempt:
    cycle_id: int
    attempt: int
    asset: str
    direction: str
    rule: str
    stake_cents: int
    state: str
    payout: float = None
    profit_cents: int = None
    at: str = ""
    loss_before_cents: int = 0  # the cycle's loss before this attempt
    cycle_net_cents: int = 0  # the cycle's net after it: the recovery when positive


@dataclass
class Group:
    orders: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    net_cents: int = 0

    def add(self, attempt):
        self.orders += 1
        self.wins += attempt.state == "WIN"
        self.losses += attempt.state == "LOSS"
        self.draws += attempt.state == "DRAW"
        self.net_cents += attempt.profit_cents or 0

    def line(self):
        decided = self.wins + self.losses
        interval = wilson(self.wins, decided)
        rate = f"{100 * self.wins / decided:.1f}%" if decided else "—"
        ci = f" (IC95% {100 * interval[0]:.1f}–{100 * interval[1]:.1f}%)" if interval else ""
        return (
            f"{self.orders} ordens, {self.wins} WIN / {self.losses} LOSS / {self.draws} DRAW, acerto {rate}{ci},"
            f" líquido {money(self.net_cents)}"
        )


@dataclass
class Report:
    name: str
    present: bool = False
    total: Group = field(default_factory=Group)
    by_asset: dict = field(default_factory=dict)
    by_rule: dict = field(default_factory=dict)
    by_level: dict = field(default_factory=dict)
    cycles: Counter = field(default_factory=Counter)
    longest_losses: int = 0
    drawdown_cents: int = 0
    payout_mean: float = None
    inconclusive: int = 0
    refused: int = 0
    attempts: list = field(default_factory=list)
    latency: dict = field(default_factory=dict)
    note: str = ""


def money(cents):
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}R$ {cents // 100},{cents % 100:02d}"


def _connect(path):
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _rows(path, sql):
    connection = _connect(path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def load(runtime_dir, name, database, orders_name, table, csv_prefix):
    """The Report of one sample, from its files only."""
    report = Report(name)
    state_path, orders_path = runtime_dir / database, runtime_dir / orders_name
    if not (state_path.exists() and orders_path.exists()):
        report.note = "sem arquivos desta amostra"
        report.latency = stage_latency(runtime_dir, csv_prefix)
        return report
    report.present = True
    reservations = {
        row[0]: row[1:] for row in _rows(
            state_path,
            "SELECT r.reservation_id, r.cycle_id, r.attempt, c.asset, c.direction, c.outcome, d.rule, d.payout"
            " FROM reservations r JOIN cycles c USING (cycle_id) LEFT JOIN decisions d USING (cycle_id)",
        )
    }
    for cycle_outcome, count in _rows(state_path, "SELECT outcome, count(*) FROM cycles GROUP BY outcome"):
        report.cycles[cycle_outcome or "aberto"] += count
    if table == "real_orders":
        orders = _rows(
            orders_path,
            "SELECT reservation_id, state, stake_cents, payout_percent, profit_cents, started_at FROM real_orders"
            " ORDER BY started_at",
        )
    else:
        orders = [
            (reservation, state, stake, None, None, submitted)
            for reservation, state, stake, submitted, order_id in _rows(
                orders_path,
                "SELECT reservation_id, state, stake_cents, submitted_at, order_id FROM paper_orders ORDER BY submitted_at",
            )
            if order_id is not None or state == "REJECTED"
        ]
    payouts, cycle_loss, cycle_net = [], defaultdict(int), defaultdict(int)
    streak = cumulative = peak = 0
    for reservation, state, stake, payout, profit, at in orders:
        known = reservations.get(reservation)
        if known is None:
            continue
        cycle_id, attempt, asset, direction, _outcome, rule, decision_payout = known
        if state == "REJECTED":
            report.refused += 1
            continue
        if state not in SETTLED:
            report.inconclusive += 1  # neither a win nor a loss: never counted as one
            continue
        payout = payout if payout is not None else decision_payout
        if profit is None:  # simulated: from the payout of the cycle's decision
            profit = round(stake * payout / 100) if state == "WIN" and payout is not None else (-stake if state == "LOSS" else 0)
        row = Attempt(cycle_id, attempt, asset, direction, rule or "—", stake, state, payout, profit, at)
        row.loss_before_cents = cycle_loss[cycle_id]
        cycle_net[cycle_id] += profit
        if profit < 0:
            cycle_loss[cycle_id] += -profit
        row.cycle_net_cents = cycle_net[cycle_id]
        report.attempts.append(row)
        if payout is not None:
            payouts.append(payout)
        for groups, key in ((report.by_asset, asset), (report.by_rule, row.rule), (report.by_level, attempt)):
            groups.setdefault(key, Group()).add(row)
        report.total.add(row)
        streak = streak + 1 if state == "LOSS" else 0
        report.longest_losses = max(report.longest_losses, streak)
        cumulative += profit
        peak = max(peak, cumulative)
        report.drawdown_cents = max(report.drawdown_cents, peak - cumulative)
    report.payout_mean = round(sum(payouts) / len(payouts), 2) if payouts else None
    report.latency = stage_latency(runtime_dir, csv_prefix)
    return report


def stage_latency(runtime_dir, csv_prefix):
    values = defaultdict(list)
    for path in sorted(runtime_dir.glob(f"{csv_prefix}*.csv")):
        for row in _csv(path):
            for key, _ in STAGES:
                number = _number(row.get(key))
                if number is not None:
                    values[key].append(number)
    return {key: latency(found) for key, found in values.items()}


def reading_latency(runtime_dir):
    values = defaultdict(list)
    for path in sorted(runtime_dir.glob("tempos-*.csv")):
        for row in _csv(path):
            for key, _ in TIMING_STAGES:
                number = _number(row.get(key))
                if number is not None:
                    values[key].append(number)
    return {key: latency(found) for key, found in values.items()}


def wait_reasons(runtime_dir, limit=8):
    reasons = Counter()
    for path in sorted(runtime_dir.glob("scan-*.csv")):
        for row in _csv(path):
            if row.get("analysis_decision") == "AGUARDAR":
                reason = (row.get("analysis_decision_note") or "").split(":")[0].strip() or "sem motivo"
                reasons[reason] += 1
    return reasons.most_common(limit)


def _csv(path):
    try:
        with open(path, newline="", encoding="utf-8") as file:
            yield from csv.DictReader(file)
    except (OSError, UnicodeDecodeError, csv.Error):
        return


def _number(text):
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def text(reports, readings, waits):
    lines = []
    for report in reports:
        lines.append(f"== {report.name.upper()} ==")
        if not report.present:
            lines.append(f"  {report.note}")
        else:
            total = report.total
            lines.append(f"  Total: {total.line()}")
            settled = total.wins + total.losses
            if settled < MIN_SAMPLE:
                lines.append(
                    f"  Amostra insuficiente: {settled} ordens decididas, menos de {MIN_SAMPLE}; nenhuma conclusão"
                    " sobre lucratividade."
                )
            lines.append(
                f"  Ciclos: {dict(report.cycles)}; maior sequência de LOSS: {report.longest_losses}; drawdown"
                f" {money(report.drawdown_cents)}; payout médio {report.payout_mean if report.payout_mean is not None else '—'}%;"
                f" inconclusivas {report.inconclusive}; recusadas/não enviadas {report.refused}"
            )
            for title, groups in (("Por nível de Gale", report.by_level), ("Por ativo", report.by_asset), ("Por regra", report.by_rule)):
                lines.append(f"  {title}:")
                for key in sorted(groups, key=str):
                    lines.append(f"    {key}: {groups[key].line()}")
            if report.attempts:
                lines.append("  Tentativas (ciclo/nível: resultado, perda acumulada antes, líquido, ciclo após, payout):")
                for row in report.attempts[-20:]:
                    lines.append(
                        f"    {row.cycle_id}/{row.attempt} {row.asset} {row.direction} {money(row.stake_cents)}: {row.state},"
                        f" perda antes {money(row.loss_before_cents)}, líquido {money(row.profit_cents)}, ciclo"
                        f" {money(row.cycle_net_cents)}, payout {row.payout if row.payout is not None else '—'}%"
                    )
        for key, label in STAGES:
            found = report.latency.get(key)
            if found:
                lines.append(f"  Latência {label}: média {found['media']} ms, p95 {found['p95']}, p99 {found['p99']} (n={found['n']})")
    lines.append("== LEITURA DA COTAÇÃO (todas as amostras) ==")
    for key, label in TIMING_STAGES:
        found = readings.get(key)
        if found:
            lines.append(f"  {label}: média {found['media']} ms, p95 {found['p95']}, p99 {found['p99']} (n={found['n']})")
    if waits:
        lines.append("== MOTIVOS DE AGUARDAR (varredura) ==")
        for reason, count in waits:
            lines.append(f"  {count}: {reason}")
    return "\n".join(lines)


def build(runtime_dir):
    reports = [load(runtime_dir, *sample) for sample in SAMPLES]
    return reports, reading_latency(runtime_dir), wait_reasons(runtime_dir)


def main(argv=None):
    from pocketautonomo.observe import RUNTIME_DIR

    parser = argparse.ArgumentParser(prog="python -m pocketautonomo.stats", description=__doc__.split("\n")[0])
    parser.add_argument("--runtime", type=Path, default=RUNTIME_DIR)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    reports, readings, waits = build(args.runtime)
    if args.json:
        data = {
            "samples": [dict(asdict(report), attempts=[asdict(row) for row in report.attempts]) for report in reports],
            "reading_latency": readings, "wait_reasons": waits,
        }
        print(json.dumps(data, ensure_ascii=False, default=str, indent=1))
    else:
        print(text(reports, readings, waits))
    return 0


if __name__ == "__main__":
    sys.exit(main())
