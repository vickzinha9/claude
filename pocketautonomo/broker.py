"""Minimal contract between the order executor and a broker.

OrderExecutor sends one OrderRequest per reservation through Broker.submit_order() and
expects a SubmissionResponse with an explicit answer. No real broker adapter exists in
this package yet.

Broker.get_order() returns the OrderStatus of one order. Nothing in this package calls it
yet.
"""

import abc
from dataclasses import dataclass
from typing import Optional

# Explicit answers of a broker to an order.
ORDER_ACCEPTED = "ACCEPTED"
ORDER_REJECTED = "REJECTED"


@dataclass(frozen=True)
class OrderRequest:
    """One order with the exact data of a reservation; stake_cents is in cents."""

    reservation_id: str
    asset: str
    direction: str
    stake_cents: int


@dataclass(frozen=True)
class SubmissionResponse:
    """The explicit answer of a broker to one OrderRequest.

    status is ORDER_ACCEPTED, with the broker_order_id the broker gave to the order, or
    ORDER_REJECTED, without an order id, when the broker explicitly confirmed that it
    did not accept the order. reason is free text for logs. The executor checks every
    response and treats anything else as an invalid, uncertain answer.
    """

    status: str
    broker_order_id: Optional[str] = None
    reason: Optional[str] = None

    @classmethod
    def accepted(cls, broker_order_id):
        return cls(ORDER_ACCEPTED, broker_order_id)

    @classmethod
    def rejected(cls, reason=None):
        return cls(ORDER_REJECTED, None, reason)


@dataclass(frozen=True)
class OrderStatus:
    """What a broker reports about one order: its id and its current state.

    broker_order_id identifies the order the answer is about. state is the order's state
    as the broker reports it, for example still open or settled with a result. Whoever
    uses an OrderStatus must check that it is about the order it asked for and that its
    state is one it understands.
    """

    broker_order_id: str
    state: str


class Broker(abc.ABC):
    """Places orders on a broker."""

    @abc.abstractmethod
    def submit_order(self, request):
        """Send request once and return the broker's explicit answer.

        Return SubmissionResponse.accepted(order_id) when the broker accepted the order,
        and SubmissionResponse.rejected() only when it explicitly confirmed that the
        order was not accepted. A timeout, a network error or an unclear answer do not
        prove that no order was placed: raise an exception instead, and never send the
        order again, so that the reservation stays pending as UNKNOWN.
        """

    def get_order(self, broker_order_id):
        """Return the OrderStatus of the order broker_order_id; never place an order.

        Look up only the order with exactly this id, never another one found by its
        value, its time or anything else. A timeout, a network error or an unclear
        answer should raise an exception. This default is not abstract: it raises
        NotImplementedError, so a broker that only places orders can still be
        instantiated.
        """
        raise NotImplementedError(f"{type(self).__name__} cannot look up orders")
