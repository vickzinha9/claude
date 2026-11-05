RESERVED = "RESERVED"
SUBMITTING = "SUBMITTING"
ACCEPTED = "ACCEPTED"
UNVERIFIED = "UNVERIFIED"
# Closes a reservation whose submission never started. It is not a bet result, so it
# never belongs to the results of cycle.py.
NOT_SUBMITTED = "NOT_SUBMITTED"

SUBMISSION_STATES = (RESERVED, SUBMITTING, ACCEPTED, UNVERIFIED, NOT_SUBMITTED)
