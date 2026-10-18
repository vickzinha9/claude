PROGRESSION_CENTS = (500, 1000, 2000, 4000)


def stake_for_attempt(attempt):
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        raise ValueError("attempt must be an integer from 1 to 4")
    if not 1 <= attempt <= len(PROGRESSION_CENTS):
        raise ValueError("attempt must be an integer from 1 to 4")
    return PROGRESSION_CENTS[attempt - 1]
