"""The platform's clock against this computer's: how many seconds to add to this computer's clock.

NO ORDERS. Beside the forming candle the chart writes its period and the time left in it, "M1"
over "00:26" (candles.chart_countdown): the platform's own clock, which starts and ends the
candles. On 2026-11-04 this computer's clock was 8 s ahead of it: in the first 8 seconds of each
minute the forming candle was still the previous minute's, the program numbered it as the new
one, and every candle of such a reading went to the history one minute late. The clock the page
shows at the top of the chart is drawn in the chart too, under its labels, and is not read.

from_countdown turns one countdown, read in a capture between two moments of this computer's
clock, into the interval where the offset must be: the countdown shows whole seconds, and the
capture takes a moment. ClockOffset keeps the intersection of those intervals; one that does not
fit it at all means a clock was changed, such as this computer's synchronized, and the interval
starts again from it. Nothing here reads the page.
"""

from dataclasses import dataclass

NOTICE_SECONDS = 1.0  # this computer this far from the platform, or more, is said in the reading's status


def from_countdown(before, after, period, remaining, estimate=0.0):
    """(low, high) of the platform's clock minus this computer's, from a countdown of remaining seconds.

    before and after are this computer's clock (UTC datetimes) just before and just after the capture.
    The forming candle's start is the multiple of period nearest to where the countdown and the offset
    estimated so far put it: this holds while this computer is less than half a period off.
    """
    local = before.timestamp()
    start = round((local + estimate + remaining - period) / period) * period
    shown = start + period - remaining  # the platform's clock at the capture, in whole seconds
    return shown - 1.0 - after.timestamp(), shown + 1.0 - local


@dataclass
class ClockOffset:
    """Where the platform's clock minus this computer's lies, in seconds: [low, high]."""

    low: float = None
    high: float = None
    readings: int = 0
    restarts: int = 0

    @property
    def known(self):
        return self.low is not None

    def update(self, low, high):
        """Take one interval of the offset; a restart when it does not overlap the one kept."""
        self.readings += 1
        if self.low is None or low > self.high or high < self.low:
            if self.low is not None:
                self.restarts += 1
            self.low, self.high = low, high
        else:
            self.low, self.high = max(self.low, low), min(self.high, high)

    def estimate(self):
        """(seconds to add to this computer's clock, how far off that may be), or (None, None)."""
        if self.low is None:
            return None, None
        return (self.low + self.high) / 2, (self.high - self.low) / 2


def describe(offset):
    """This computer's clock against the platform's, in words, for an offset of the platform minus this computer."""
    amount = f"{abs(offset):.1f}".replace(".", ",")
    return f"este computador está {amount} s {'adiantado' if offset < 0 else 'atrasado'}"
