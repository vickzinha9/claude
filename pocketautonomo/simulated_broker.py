"""A broker simulated in memory, for local runs and tests; it never reaches a real broker.

SimulatedBroker accepts every order with a new id SIM-<uuid4>, keeps it OPEN and settles
it only when settle_order() is called. Its results are given explicitly by whoever drives
the simulation, such as a test script: they come from no market data, predict nothing
and say nothing about how a real broker would settle an order. It never rejects an order
and never times out.

Its orders, their results and the requests it received live only in memory, inside the
SimulatedBroker object: nothing is saved, and all of it is lost when the object goes away
or the process ends. A coordinator reopened from its database still links its
reservations to their SIM-... ids, but a new SimulatedBroker does not know them: looking
one up raises KeyError, which OrderExecutor.reconcile_pending() reports as a failed
lookup, so the reservation stays pending.
"""

import threading
import uuid

from pocketautonomo.broker import Broker, OrderStatus, SubmissionResponse
from pocketautonomo.cycle import LOSS, WIN
from pocketautonomo.executor import ORDER_OPEN  # the open state reconcile_pending() knows

# Every simulated order id is this prefix followed by a uuid4.
SIMULATED_ORDER_PREFIX = "SIM-"
# The only results settle_order() accepts.
SETTLEMENT_RESULTS = (WIN, LOSS)


class SimulatedBroker(Broker):
    """A Broker whose orders and results are simulated in memory.

    submit_order() accepts every request as a new OPEN order with a SIM-<uuid4> id.
    get_order() reports the stored state of exactly the id it is given, and
    settle_order() moves an OPEN order to WIN or LOSS once. A lock guards the stored
    orders and requests, so the answers stay consistent when several threads share the
    object.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._states = {}  # broker_order_id -> ORDER_OPEN, WIN or LOSS
        self._requests = []  # every request received, in the order it arrived

    @property
    def requests(self):
        """The requests received so far, in the order they arrived, as a tuple."""
        with self._lock:
            return tuple(self._requests)

    def submit_order(self, request):
        """Keep request as a new OPEN order and return SubmissionResponse.accepted(id)."""
        order_id = f"{SIMULATED_ORDER_PREFIX}{uuid.uuid4()}"
        with self._lock:
            self._requests.append(request)
            self._states[order_id] = ORDER_OPEN
        return SubmissionResponse.accepted(order_id)

    def get_order(self, broker_order_id):
        """Return the OrderStatus of exactly broker_order_id; KeyError when it is unknown."""
        with self._lock:
            state = self._state(broker_order_id)
        return OrderStatus(broker_order_id, state)

    def settle_order(self, broker_order_id, result):
        """Settle the order broker_order_id as WIN or LOSS.

        The first settlement moves the order from OPEN to result and returns True.
        Settling it again with the same result returns False and changes nothing. Any
        other result raises ValueError, and so does a different result for an order that
        is already settled, which keeps its first result. An unknown id raises KeyError.
        """
        if result not in SETTLEMENT_RESULTS:
            raise ValueError(
                f"a simulated order settles only as {WIN} or {LOSS}, not {result!r}"
            )
        with self._lock:
            state = self._state(broker_order_id)
            if state == result:
                return False  # the same settlement again
            if state != ORDER_OPEN:
                raise ValueError(
                    f"simulated order {broker_order_id} is already settled as {state};"
                    f" it cannot become {result}"
                )
            self._states[broker_order_id] = result
            return True

    def _state(self, broker_order_id):
        """The stored state of broker_order_id; call it with the lock held."""
        try:
            return self._states[broker_order_id]
        except KeyError:
            raise KeyError(f"unknown simulated order id {broker_order_id!r}") from None
