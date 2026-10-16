"""Sends reserved entries to a broker and looks up the results of their orders.

The coordinator controls every transition: the executor only calls its methods and the
broker. An entry is sent at most once per reservation, and a result is looked up only by
the order id linked to the pending reservation; looking it up never sends anything.
"""

from dataclasses import dataclass
from typing import Optional, Tuple

from pocketautonomo.broker import (
    ORDER_ACCEPTED,
    ORDER_REJECTED,
    OrderRequest,
    OrderStatus,
    SubmissionResponse,
)
from pocketautonomo.cycle import DEFINITIVE_RESULTS, REJECTED, UNKNOWN

# Outcomes of OrderExecutor.submit(); SubmissionReport describes each one.
NOT_STARTED = "NOT_STARTED"
ACCEPTANCE_RECORDED = "ACCEPTANCE_RECORDED"
REJECTION_RECORDED = "REJECTION_RECORDED"
UNCERTAIN = "UNCERTAIN"

# Outcomes of OrderExecutor.reconcile_pending(), described by ReconciliationReport.
NO_PENDING = "NO_PENDING"
NO_ORDER_ID = "NO_ORDER_ID"
PENDING = "PENDING"
RESULT_RECORDED = "RESULT_RECORDED"
RESULT_ALREADY_RECORDED = "RESULT_ALREADY_RECORDED"
RECORD_FAILED = "RECORD_FAILED"

# The OrderStatus state of an order that is still running. reconcile_pending() also
# recognizes UNKNOWN, for an order whose result the broker does not know, and the
# results in cycle.DEFINITIVE_RESULTS; any other state is unknown.
ORDER_OPEN = "OPEN"
_UNSETTLED_STATES = (ORDER_OPEN, UNKNOWN)
_KNOWN_ORDER_STATES = _UNSETTLED_STATES + DEFINITIVE_RESULTS


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


@dataclass(frozen=True)
class ReconciliationReport:
    """What one OrderExecutor.reconcile_pending() call did.

    outcome is one of:

    - NO_PENDING: there was no pending reservation, so nothing was looked up.
    - NO_ORDER_ID: the pending reservation has no linked broker order id, so nothing was
      looked up and it was left pending; error tells its submission state.
    - PENDING: the lookup gave no definitive result, and this call changed nothing.
      error is None when the broker reported the order as OPEN or its result as
      UNKNOWN; otherwise it describes the failed lookup, the invalid answer, the answer
      about another order or the unknown state.
    - RESULT_RECORDED: the broker reported a definitive result and record_result()
      applied it.
    - RESULT_ALREADY_RECORDED: the broker reported the definitive result that was
      already recorded for the reservation, so nothing changed.
    - RECORD_FAILED: the broker reported a definitive result, but record_result()
      raised, so this call recorded nothing; error tells why.

    reservation_id is the pending reservation read at the start, None when there was
    none, and broker_order_id is the order id linked to it, the only one looked up.
    result is the definitive result the broker reported, also when recording it failed,
    and None in every other case.
    """

    outcome: str
    reservation_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None


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

    reconcile_pending() looks up the result of the pending reservation's order, only by
    the order id linked to the reservation, and records a definitive result with
    record_result(). It never sends an order, it also works while the coordinator is
    paused, and the coordinator keeps applying its rules of pause and recovery.
    """

    def __init__(self, coordinator, broker):
        self._coordinator = coordinator
        self._broker = broker

    def submit(self, reservation_id, deadline=None):
        """Send the pending reservation reservation_id once; return a SubmissionReport.

        deadline, when given, goes with the order (OrderRequest.deadline): the broker
        rejects the order explicitly once it has passed, so a slow start of the submission
        never places it late.
        """
        try:
            reservation = self._coordinator.begin_submission(reservation_id)
        except Exception as error:  # refused or failed: nothing may be sent
            return SubmissionReport(NOT_STARTED, reservation_id, errors=(_describe(error),))
        entry = reservation.entry
        request = OrderRequest(
            reservation.reservation_id, entry.asset, entry.direction, entry.stake_cents, deadline
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

    def reconcile_pending(self):
        """Look up the result of the pending reservation's order; return a report.

        One status() snapshot gives the pending reservation and the order id linked to
        it. Only that order is looked up, with broker.get_order(), never an order found
        by its value, its time or anything else, and nothing is ever sent:

        - without a pending reservation the outcome is NO_PENDING, and without a linked
          order id it is NO_ORDER_ID; nothing is looked up in either case;
        - the answer must be an OrderStatus about exactly that order, in a known state.
          A failed lookup, an invalid answer, an answer about another order and an
          unknown state all give PENDING with the error, without changing the
          coordinator;
        - an OPEN order or an UNKNOWN result gives PENDING, and nothing is recorded;
        - a definitive result is recorded with record_result() for the reservation read
          at the start. True gives RESULT_RECORDED and False RESULT_ALREADY_RECORDED; an
          exception gives RECORD_FAILED, with the result received and the error.

        It also works while the coordinator is paused, for example after a restart, and
        the coordinator keeps applying its rules of pause and recovery. A closed
        coordinator raises CoordinatorError from status().
        """
        status = self._coordinator.status()  # one snapshot: the reservation and its link
        pending = status.pending
        if pending is None:
            return ReconciliationReport(NO_PENDING)
        reservation_id = pending.reservation_id
        order_id = status.broker_order_id
        if order_id is None:
            return ReconciliationReport(
                NO_ORDER_ID,
                reservation_id,
                error=(
                    f"reservation {reservation_id} is {status.submission_state} without a"
                    " linked broker order id, and its result is looked up only by that id"
                ),
            )
        try:
            answer = self._broker.get_order(order_id)
        except Exception as error:  # timeout, network error or anything unexpected
            return ReconciliationReport(
                PENDING,
                reservation_id,
                order_id,
                error=f"looking up order {order_id} failed: {_describe(error)}",
            )
        problem = _check_order_status(answer, order_id)
        if problem is not None:
            return ReconciliationReport(PENDING, reservation_id, order_id, error=problem)
        if answer.state in _UNSETTLED_STATES:  # still open, or its result is unknown
            return ReconciliationReport(PENDING, reservation_id, order_id)
        result = answer.state
        try:
            applied = self._coordinator.record_result(reservation_id, result)
        except Exception as error:  # the result is known, but it was not recorded
            return ReconciliationReport(
                RECORD_FAILED,
                reservation_id,
                order_id,
                result,
                f"recording the result {result} of order {order_id} failed:"
                f" {_describe(error)}",
            )
        outcome = RESULT_RECORDED if applied else RESULT_ALREADY_RECORDED
        return ReconciliationReport(outcome, reservation_id, order_id, result)

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


def _check_order_status(answer, order_id):
    """Why answer is not an OrderStatus of exactly order order_id in a known state.

    Returns None when it is one.
    """
    if not isinstance(answer, OrderStatus):
        return f"invalid broker answer for order {order_id}: {answer!r}"
    if not isinstance(answer.broker_order_id, str) or answer.broker_order_id != order_id:
        return (
            f"the broker answered about order {answer.broker_order_id!r} instead of"
            f" order {order_id!r}"
        )
    if not isinstance(answer.state, str) or answer.state not in _KNOWN_ORDER_STATES:
        return f"unknown state {answer.state!r} of order {order_id}"
    return None
