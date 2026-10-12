"""Sends a reserved entry to a broker, at most once per reservation.

The coordinator controls every transition: the executor only calls its methods and the
broker. Results of accepted bets are not fetched here yet.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

from pocketautonomo.broker import (
    ORDER_ACCEPTED,
    ORDER_REJECTED,
    OrderRequest,
    SubmissionResponse,
)
from pocketautonomo.cycle import REJECTED, UNKNOWN

# Outcomes of OrderExecutor.submit(); SubmissionReport describes each one.
NOT_STARTED = "NOT_STARTED"
ACCEPTANCE_RECORDED = "ACCEPTANCE_RECORDED"
REJECTION_RECORDED = "REJECTION_RECORDED"
UNCERTAIN = "UNCERTAIN"


@dataclass(frozen=True)
class SubmissionReport:
    """What one OrderExecutor.submit() call did.

    outcome is one of:

    - NOT_STARTED: begin_submission() refused or failed, so the broker was not called
      and the reservation was left as it was.
    - ACCEPTANCE_RECORDED: the broker accepted the order and the coordinator recorded
      the acceptance, saving it first when it has a store. The bet stays pending until
      its result.
    - REJECTION_RECORDED: the broker explicitly rejected the order and the coordinator
      recorded REJECTED, which ended the cycle.
    - UNCERTAIN: the submission started, but the broker's answer is unknown or could not
      be recorded. The reservation stays pending and is never sent again.

    request is the order handed to the broker, or None when the broker was not called.
    response is the valid answer of the broker, recorded or not; it is None when the
    broker was not called, raised or returned an invalid answer. unknown_recorded tells
    whether this call recorded UNKNOWN, and errors describes every error met, in order.
    """

    outcome: str
    reservation_id: str
    request: Optional[OrderRequest] = None
    response: Optional[SubmissionResponse] = None
    unknown_recorded: bool = False
    errors: Tuple[str, ...] = ()

    @property
    def broker_order_id(self):
        """The order id of an acceptance received from the broker, recorded or not.

        When the outcome is UNCERTAIN, it is the id a later recovery needs to confirm
        the acceptance.
        """
        response = self.response
        if response is not None and response.status == ORDER_ACCEPTED:
            return response.broker_order_id
        return None


class OrderExecutor:
    """Sends the reservations of a coordinator to a broker.

    submit() calls begin_submission() first, so SUBMITTING is recorded, and saved when
    the coordinator has a store, before anything is sent. A reservation starts one
    submission at most, so repeated or simultaneous calls for it send at most one
    order. When the start is refused or fails, the broker is not called and the
    reservation is left as it was: it is never closed automatically.

    The order carries exactly the asset, direction, stake and id of the reservation.
    Then:

    - an acceptance with a valid order id is recorded with confirm_acceptance(); the
      bet stays pending until its result;
    - an explicit rejection is recorded as REJECTED;
    - a timeout, a network error, any other error raised by the broker or an invalid
      response leave the answer unknown: the reservation stays pending and UNKNOWN is
      recorded when possible. Nothing is sent again, recorded as LOSS or closed as
      NOT_SUBMITTED.

    When an explicit answer arrives but recording it fails, the report keeps the
    answer, including the order id actually received, and the error, and nothing else
    is recorded: the coordinator keeps the reservation pending and its existing stops.
    submit() returns a SubmissionReport instead of raising in all these cases.
    """

    def __init__(self, coordinator, broker):
        self._coordinator = coordinator
        self._broker = broker

    def submit(self, reservation_id):
        """Send the pending reservation reservation_id once; return a SubmissionReport."""
        try:
            reservation = self._coordinator.begin_submission(reservation_id)
        except Exception as error:  # refused or failed: nothing may be sent
            return SubmissionReport(NOT_STARTED, reservation_id, errors=(_describe(error),))
        entry = reservation.entry
        request = OrderRequest(
            reservation.reservation_id, entry.asset, entry.direction, entry.stake_cents
        )
        try:
            response = self._broker.submit_order(request)
        except Exception as error:  # timeout, network error or anything unexpected
            return self._uncertain(request, [_describe(error)])
        if not _is_explicit(response):
            return self._uncertain(request, [f"invalid broker response: {response!r}"])
        if response.status == ORDER_ACCEPTED:
            return self._record_acceptance(request, response)
        return self._record_rejection(request, response)

    def _record_acceptance(self, request, response):
        order_id = response.broker_order_id
        try:
            self._coordinator.confirm_acceptance(request.reservation_id, order_id)
        except Exception as error:  # the answer is known, but it was not recorded
            return SubmissionReport(
                UNCERTAIN,
                request.reservation_id,
                request,
                response,
                errors=(
                    f"recording the acceptance of order {order_id} failed:"
                    f" {_describe(error)}",
                ),
            )
        return SubmissionReport(ACCEPTANCE_RECORDED, request.reservation_id, request, response)

    def _record_rejection(self, request, response):
        try:
            self._coordinator.record_result(request.reservation_id, REJECTED)
        except Exception as error:  # the answer is known, but it was not recorded
            return SubmissionReport(
                UNCERTAIN,
                request.reservation_id,
                request,
                response,
                errors=(f"recording the explicit rejection failed: {_describe(error)}",),
            )
        return SubmissionReport(REJECTION_RECORDED, request.reservation_id, request, response)

    def _uncertain(self, request, errors):
        """The answer is unknown: keep the reservation pending, as UNKNOWN when possible."""
        unknown_recorded = False
        try:
            unknown_recorded = self._coordinator.record_result(request.reservation_id, UNKNOWN)
        except Exception as error:
            errors.append(f"recording UNKNOWN failed: {_describe(error)}")
        return SubmissionReport(
            UNCERTAIN,
            request.reservation_id,
            request,
            unknown_recorded=unknown_recorded,
            errors=tuple(errors),
        )


def _describe(error):
    return f"{type(error).__name__}: {error}"


def _is_explicit(response):
    """Whether response is a well-formed acceptance or explicit rejection."""
    if not isinstance(response, SubmissionResponse):
        return False
    if response.status == ORDER_ACCEPTED:
        order_id = response.broker_order_id
        return isinstance(order_id, str) and bool(order_id.strip())
    if response.status == ORDER_REJECTED:
        return response.broker_order_id is None
    return False
