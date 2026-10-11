from dataclasses import dataclass

from pocketautonomo.progression import PROGRESSION_CENTS, stake_for_attempt

CALL = "CALL"
PUT = "PUT"
WIN = "WIN"
LOSS = "LOSS"
DRAW = "DRAW"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"
UNKNOWN = "UNKNOWN"

DIRECTIONS = (CALL, PUT)
# CANCELLED and REJECTED must be confirmed by the broker. A timeout or a network
# error proves neither: report it as UNKNOWN so the attempt stays pending.
DEFINITIVE_RESULTS = (WIN, LOSS, DRAW, CANCELLED, REJECTED)
RESULTS = DEFINITIVE_RESULTS + (UNKNOWN,)
MAX_ATTEMPTS = len(PROGRESSION_CENTS)


@dataclass(frozen=True)
class Entry:
    asset: str
    direction: str
    attempt: int
    stake_cents: int


class Cycle:
    """In-memory state of one progression cycle for a single asset and direction."""

    def __init__(self, asset, direction):
        if not isinstance(asset, str) or not asset.strip():
            raise ValueError("asset must be a non-empty string")
        if direction not in DIRECTIONS:
            raise ValueError("direction must be CALL or PUT")
        self._asset = asset
        self._direction = direction
        self._attempt = 1
        self._outcome = None
        self._blocked = False
        self._results = {}

    @property
    def asset(self):
        return self._asset

    @property
    def direction(self):
        return self._direction

    @property
    def attempt(self):
        return self._attempt

    @property
    def outcome(self):
        """None while the cycle is active; afterwards, the result that ended it."""
        return self._outcome

    @property
    def is_active(self):
        return self._outcome is None

    @property
    def is_blocked(self):
        """True while waiting for a definitive result after an UNKNOWN one."""
        return self._blocked

    def next_stake(self):
        """Stake in cents for the next entry, or None if no entry is allowed.

        Querying never advances the attempt.
        """
        if not self.is_active or self._blocked:
            return None
        return stake_for_attempt(self._attempt)

    def next_entry(self, balance_cents):
        """Next entry if one is allowed and the balance covers its stake, else None."""
        if isinstance(balance_cents, bool) or not isinstance(balance_cents, int):
            raise ValueError("balance_cents must be an integer")
        stake = self.next_stake()
        if stake is None or balance_cents < stake:
            return None
        return Entry(self._asset, self._direction, self._attempt, stake)

    def record_result(self, attempt, result):
        """Record the result of an attempt. Returns True if the state changed."""
        stake_for_attempt(attempt)  # raises ValueError unless attempt is 1 to 4
        if result not in RESULTS:
            raise ValueError("result must be one of " + ", ".join(RESULTS))

        recorded = self._results.get(attempt)
        if recorded is not None:
            if result in (recorded, UNKNOWN):
                return False  # duplicate or late result: never advances again
            raise ValueError(
                f"conflicting result for attempt {attempt}: {recorded} then {result}"
            )
        if attempt != self._attempt:
            raise ValueError(
                f"attempt {attempt} is not the current attempt {self._attempt}"
            )

        if result == UNKNOWN:
            changed = not self._blocked
            self._blocked = True
            return changed

        self._results[attempt] = result
        self._blocked = False
        if result == LOSS and attempt < MAX_ATTEMPTS:
            self._attempt += 1  # only a LOSS moves on to the next gale
        else:
            self._outcome = result
        return True
