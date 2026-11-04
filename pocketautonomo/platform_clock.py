"""The platform's clock against this computer's: how many seconds to add to this computer's clock.

NO ORDERS. The trading page shows the platform's time beside the chart, as text: HH:MM:SS
and its time zone, such as "17:44:51 UTC-3". Its candles start and end by that clock, not by
this computer's. On 2026-11-04 this computer's clock was 8 s ahead of it: in the first 8
seconds of each minute the forming candle was still the previous minute's, the program
numbered it as the new one, and every candle of such a reading went to the history one minute
late. Each later reading then contradicted them all.

ClockOffset takes each reading of that text with this computer's clock just before and just
after it, and keeps the interval where the offset must be: the text shows whole seconds, and
it may show a second late (DISPLAY_LAG). The readings narrow the interval; one that does not
fit it at all means a clock was changed, such as this computer's synchronized, and the interval
starts again from it. Nothing here reads the page: the session gives the text.
"""

import re
from dataclasses import dataclass
from datetime import timedelta

CLOCK_PATTERN = re.compile(
    r"^\s*(\d{1,2}):(\d{2}):(\d{2})\s*(?:UTC|GMT)\s*(?:([+-])\s*(\d{1,2})(?::?(\d{2}))?)?\s*$", re.IGNORECASE
)
DISPLAY_LAG = 1.0  # seconds: the text may still show the previous second this long after it ended
NOTICE_SECONDS = 1.0  # this computer this far from the platform, or more, is said in the reading's status


def parse(text):
    """(seconds of the day in UTC) of the clock's text, or None when it is not a time with its zone."""
    match = CLOCK_PATTERN.match(text or "")
    if match is None:
        return None
    hours, minutes, seconds = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if hours > 23 or minutes > 59 or seconds > 59:
        return None
    zone = 0
    if match.group(4):
        zone = int(match.group(5)) * 3600 + int(match.group(6) or 0) * 60
        if match.group(4) == "-":
            zone = -zone
    return (hours * 3600 + minutes * 60 + seconds - zone) % 86400


@dataclass
class ClockOffset:
    """Where the platform's clock minus this computer's lies, in seconds: [low, high]."""

    low: float = None
    high: float = None
    readings: int = 0
    unread: int = 0  # readings without a clock's text
    restarts: int = 0

    @property
    def known(self):
        return self.low is not None

    def update(self, before, after, text):
        """Take one reading of the clock's text, between before and after by this computer's clock (UTC datetimes)."""
        second = parse(text)
        if second is None:
            self.unread += 1
            return False
        middle = before + (after - before) / 2
        day = middle.replace(hour=0, minute=0, second=0, microsecond=0)
        shown = min(
            (day + timedelta(days=shift, seconds=second) for shift in (-1, 0, 1)), key=lambda moment: abs(moment - middle)
        )
        # At the reading, the platform's clock was in [shown, shown + 1 + DISPLAY_LAG), and the reading happened
        # between before and after.
        low = (shown - after).total_seconds()
        high = (shown - before).total_seconds() + 1 + DISPLAY_LAG
        self.readings += 1
        if self.low is None or low > self.high or high < self.low:
            if self.low is not None:
                self.restarts += 1
            self.low, self.high = low, high
        else:
            self.low, self.high = max(self.low, low), min(self.high, high)
        return True

    def estimate(self):
        """(seconds to add to this computer's clock, how far off that may be), or (None, None)."""
        if self.low is None:
            return None, None
        return (self.low + self.high) / 2, (self.high - self.low) / 2


def describe(offset):
    """This computer's clock against the platform's, in words, for an offset of the platform minus this computer."""
    amount = f"{abs(offset):.1f}".replace(".", ",")
    return f"este computador está {amount} s {'adiantado' if offset < 0 else 'atrasado'}"
