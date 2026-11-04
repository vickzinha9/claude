"""runtime/painel.json: what the launcher shows, rewritten after each visit and each reading of a cycle.

It is only a snapshot for the window of the launcher (app.py): the operation never reads it.
It is written to a temporary file and moved over the old one, so the launcher never reads a
half-written file; a failure to write it is shown once and the operation goes on.
"""

import json
import os
from datetime import datetime, timezone

from pocketautonomo.progression import stake_for_attempt


def _iso(moment):
    return None if moment is None else moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


class Panel:
    def __init__(self, path, operator, switch=None, warn=None):
        self.path = path
        self.operator = operator
        self.switch = switch
        self._warn = warn
        self._failed = False
        self.decision = None
        self.seen = None
        self.timings = {}

    def visit(self, decision, reading_ms=None, decision_ms=None):
        if decision is not None:
            self.decision = {
                "asset": decision.asset,
                "action": decision.action,
                "reason": decision.reason,
                "quote": getattr(decision.sample, "quote", None),
                "captured_at": _iso(getattr(decision.sample, "at", None)),
                "payout": getattr(decision.sample, "payout", None),
                "valid_until": _iso(decision.valid_until),
                "rule": decision.validation.rule.describe(self.operator.horizon)
                if decision.validation is not None and decision.validation.rule is not None else None,
            }
            self.timings.update(reading_ms=reading_ms, decision_ms=decision_ms)
        self.write()

    def reading(self, seen):
        self.seen = {
            "asset": seen.asset, "quote": seen.quote, "captured_at": _iso(seen.captured_at), "payout": seen.payout,
        }
        self.timings["reading_ms"] = seen.reading_ms
        self.write()

    def snapshot(self):
        operator = self.operator
        status = operator.coordinator.status()
        attempt = status.attempt if status.cycle_id is not None and status.is_active else 1
        armed, why = self.switch() if self.switch is not None else (False, "operação simulada")
        last = operator.last_event or {}
        return {
            "updated_at": _iso(datetime.now(timezone.utc)),
            "execution": operator.log.execution,
            "real": operator.real,
            "armed": armed,
            "armed_note": why,
            "paused": bool(self.switch is not None and self.switch.pause_path.exists()),
            "state": operator.state(),
            "cycle_id": status.cycle_id,
            "cycle_asset": status.asset,
            "cycle_direction": status.direction,
            "attempt": attempt,
            "stake_cents": stake_for_attempt(attempt),
            "decision": self.decision,
            "quote": self.seen,
            "timings": self.timings,
            "session": dict(operator.session),
            "stop_reason": operator.stop_reason or operator.collection_paused,
            "last_event": {key: (_iso(value) if isinstance(value, datetime) else value) for key, value in last.items()},
        }

    def write(self):
        try:
            data = json.dumps(self.snapshot(), ensure_ascii=False, default=str)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(data, encoding="utf-8")
            os.replace(temporary, self.path)
        except OSError as error:
            if not self._failed and self._warn is not None:
                self._warn(f"AVISO: o painel {self.path} não pôde ser gravado ({error}); a operação continua.")
            self._failed = True
