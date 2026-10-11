import errno
import os
import pathlib
import sqlite3
from contextlib import contextmanager

from pocketautonomo.cycle import ABANDONED, UNKNOWN

if os.name == "nt":
    import msvcrt
else:
    import fcntl

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

# errno values meaning that another open file already holds the lock.
_LOCK_HELD = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}


class StorageError(Exception):
    """Saving or loading failed, or the saved state is inconsistent."""


class DatabaseInUseError(StorageError):
    """Another coordinator, in this or in another process, has the database open."""


@contextmanager
def _translated(action):
    try:
        yield
    except sqlite3.Error as error:
        raise StorageError(f"{action} failed: {error}") from error


def _lock_exclusively(path):
    """Take the exclusive lock that guards the database at path; return its descriptor.

    The lock lives on the side file "<path>-lock": flock() on POSIX systems and
    msvcrt.locking() on Windows. The operating system drops it when the descriptor
    is closed or the process ends, even abruptly. The side file may stay on disk
    afterwards: only the lock protects the database, never the file's existence.
    """
    lock_path = os.fspath(path) + "-lock"
    try:
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o666)
    except OSError as error:
        raise StorageError(f"cannot open the lock file {lock_path}: {error}") from error
    try:
        if os.name == "nt":
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        os.close(descriptor)
        if error.errno in _LOCK_HELD:
            raise DatabaseInUseError(
                f"another coordinator is already using {path}; close it before opening"
                f" this database again (the exclusive lock on {lock_path} is held)"
            ) from error
        raise StorageError(f"cannot lock {lock_path}: {error}") from error
    return descriptor


def _unlock(descriptor):
    try:
        if os.name == "nt":
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(descriptor)  # closing also drops a flock() lock


def connect_read_only(path):
    """Open an independent, read-only connection to the database of a store.

    It does not take the coordinator lock, so it works while a coordinator is
    running, and SQLite refuses any write made through it.
    """
    uri = pathlib.Path(os.path.abspath(path)).as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


class SqliteStore:
    """Coordinator state in a SQLite file. Every save is committed before it returns.

    Opening takes the exclusive lock before touching the database, so only one store
    at a time, in any process, can load and change the state; connect_read_only()
    serves independent readers. close() releases the database and the lock, and the
    store cannot be used afterwards. The methods take no lock of their own: the
    coordinator calls them under its lock. A reservation whose result is NULL or
    UNKNOWN is still pending.
    """

    def __init__(self, path):
        self._connection = None
        self._lock = _lock_exclusively(path)  # before the database is touched
        try:
            with _translated(f"opening {path}"):
                self._connection = sqlite3.connect(path, check_same_thread=False)
                self._connection.executescript(SCHEMA)
        except BaseException:
            self.close()
            raise

    def load(self):
        """Return (cycles, reservations) as tuples, in the order they happened."""
        connection = self._open_connection()
        with _translated("loading the saved state"):
            cycles = connection.execute(
                "SELECT cycle_id, asset, direction, outcome FROM cycles ORDER BY cycle_id"
            ).fetchall()
            reservations = connection.execute(
                "SELECT reservation_id, cycle_id, attempt, stake_cents, result"
                " FROM reservations ORDER BY cycle_id, attempt"
            ).fetchall()
        return cycles, reservations

    def save_cycle(self, cycle_id, asset, direction):
        with self._transaction("saving the cycle") as connection:
            connection.execute(
                "INSERT INTO cycles (cycle_id, asset, direction) VALUES (?, ?, ?)",
                (cycle_id, asset, direction),
            )

    def save_reservation(self, reservation):
        entry = reservation.entry
        with self._transaction("saving the reservation") as connection:
            connection.execute(
                "INSERT INTO reservations (reservation_id, cycle_id, attempt, stake_cents)"
                " VALUES (?, ?, ?, ?)",
                (reservation.reservation_id, reservation.cycle_id, entry.attempt,
                 entry.stake_cents),
            )

    def save_result(self, reservation, result, outcome):
        """Save a result of a pending reservation and, if the cycle ended, its outcome."""
        with self._transaction("saving the result") as connection:
            updated = connection.execute(
                "UPDATE reservations SET result = ?"
                " WHERE reservation_id = ? AND (result IS NULL OR result = ?)",
                (result, reservation.reservation_id, UNKNOWN),
            ).rowcount
            if updated != 1:
                raise StorageError(f"reservation {reservation.reservation_id} is not pending")
            if outcome is not None:
                connection.execute(
                    "UPDATE cycles SET outcome = ? WHERE cycle_id = ?",
                    (outcome, reservation.cycle_id),
                )

    def save_abandonment(self, cycle_id):
        """Mark an open cycle without pending reservations as abandoned."""
        with self._transaction("saving the abandonment") as connection:
            updated = connection.execute(
                "UPDATE cycles SET outcome = ? WHERE cycle_id = ? AND outcome IS NULL"
                " AND NOT EXISTS (SELECT 1 FROM reservations WHERE cycle_id = ?"
                " AND (result IS NULL OR result = ?))",
                (ABANDONED, cycle_id, cycle_id, UNKNOWN),
            ).rowcount
            if updated != 1:
                raise StorageError(f"cycle {cycle_id} is not open without pending reservations")

    def close(self):
        """Close the database and release the lock. Closing again does nothing."""
        connection, self._connection = self._connection, None
        descriptor, self._lock = self._lock, None
        try:
            if connection is not None:
                connection.close()
        finally:
            if descriptor is not None:
                _unlock(descriptor)

    def _open_connection(self):
        if self._connection is None:
            raise StorageError("the store is closed")
        return self._connection

    @contextmanager
    def _transaction(self, action):
        connection = self._open_connection()
        with _translated(action), connection:  # commit, or roll back on any error
            yield connection
