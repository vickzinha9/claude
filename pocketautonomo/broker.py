"""Minimal contract between the order executor and a broker.

OrderExecutor sends one OrderRequest per reservation through Broker.submit_order() and
expects a SubmissionResponse with an explicit answer. No real broker adapter exists in
this package yet.
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
