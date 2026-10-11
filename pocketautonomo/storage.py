import sqlite3
from contextlib import contextmanager

from pocketautonomo.cycle import UNKNOWN

SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA synchronous = FULL;
CREATE TABLE IF NOT EXISTS cycles (
    cycle_id INTEGER PRIMARY KEY,
    asset TEXT NOT NULL,
    direction TEXT NOT NULL,
    outcome TEXT
);
CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    cycle_id INTEGER NOT NULL REFERENCES cycles (cycle_id),
    attempt INTEGER NOT NULL,
    stake_cents INTEGER NOT NULL,
    result TEXT,
    UNIQUE (cycle_id, attempt)
);
"""


class StorageError(Exception):
    """Saving or loading failed, or the saved state is inconsistent."""


@contextmanager
def _translated(action):
    try:
        yield
    except sqlite3.Error as error:
        raise StorageError(f"{action} failed: {error}") from error


class SqliteStore:
    """Coordinator state in a SQLite file. Every save is committed before it returns.

    The methods take no lock of their own: the coordinator calls them under its lock.
    A reservation whose result is NULL or UNKNOWN is still pending.
    """

    def __init__(self, path):
        with _translated(f"opening {path}"):
            self._connection = sqlite3.connect(path, check_same_thread=False)
            try:
                self._connection.executescript(SCHEMA)
            except sqlite3.Error:
                self._connection.close()
                raise

    def load(self):
        """Return (cycles, reservations) as tuples, in the order they happened."""
        with _translated("loading the saved state"):
            cycles = self._connection.execute(
                "SELECT cycle_id, asset, direction, outcome FROM cycles ORDER BY cycle_id"
            ).fetchall()
            reservations = self._connection.execute(
                "SELECT reservation_id, cycle_id, attempt, stake_cents, result"
                " FROM reservations ORDER BY cycle_id, attempt"
            ).fetchall()
        return cycles, reservations

    def save_cycle(self, cycle_id, asset, direction):
        with self._transaction("saving the cycle"):
            self._connection.execute(
                "INSERT INTO cycles (cycle_id, asset, direction) VALUES (?, ?, ?)",
                (cycle_id, asset, direction),
            )

    def save_reservation(self, reservation):
        entry = reservation.entry
        with self._transaction("saving the reservation"):
            self._connection.execute(
                "INSERT INTO reservations (reservation_id, cycle_id, attempt, stake_cents)"
                " VALUES (?, ?, ?, ?)",
                (reservation.reservation_id, reservation.cycle_id, entry.attempt,
                 entry.stake_cents),
            )

    def save_result(self, reservation, result, outcome):
        """Save a result of a pending reservation and, if the cycle ended, its outcome."""
        with self._transaction("saving the result"):
            updated = self._connection.execute(
                "UPDATE reservations SET result = ?"
                " WHERE reservation_id = ? AND (result IS NULL OR result = ?)",
                (result, reservation.reservation_id, UNKNOWN),
            ).rowcount
            if updated != 1:
                raise StorageError(f"reservation {reservation.reservation_id} is not pending")
            if outcome is not None:
                self._connection.execute(
                    "UPDATE cycles SET outcome = ? WHERE cycle_id = ?",
                    (outcome, reservation.cycle_id),
                )

    def close(self):
        self._connection.close()

    @contextmanager
    def _transaction(self, action):
        with _translated(action), self._connection:  # commit, or roll back on any error
            yield
