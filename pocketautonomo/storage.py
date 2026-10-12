import errno
import os
import pathlib
import sqlite3
from contextlib import contextmanager

from pocketautonomo.cycle import ABANDONED, UNKNOWN
from pocketautonomo.submission import (
    ACCEPTED,
    RESERVED,
    SUBMISSION_STATES,
    SUBMITTING,
    UNVERIFIED,
)

if os.name == "nt":
    import msvcrt
else:
    import fcntl

# Schema version kept in PRAGMA user_version. Databases written before versions were
# tracked have user_version 0.
SCHEMA_VERSION = 1

# The tables as version 0 created them. MIGRATIONS then bring every database, new or
# old, to SCHEMA_VERSION through the same statements.
BASE_TABLES = (
    """CREATE TABLE IF NOT EXISTS cycles (
        cycle_id INTEGER PRIMARY KEY,
        asset TEXT NOT NULL,
        direction TEXT NOT NULL,
        outcome TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS reservations (
        reservation_id TEXT PRIMARY KEY,
        cycle_id INTEGER NOT NULL REFERENCES cycles (cycle_id),
        attempt INTEGER NOT NULL,
        stake_cents INTEGER NOT NULL,
        result TEXT,
        UNIQUE (cycle_id, attempt)
    )""",
)

# MIGRATIONS[n] takes the schema from version n - 1 to n. They only add, so every saved
# row is kept, and they all run in one transaction. A released migration is never
# edited: a later change needs a new one.
MIGRATIONS = {
    1: (
        # A reservation saved before submissions were tracked may have been submitted,
        # so it becomes UNVERIFIED, a state that never allows a submission. It is also
        # the default for any insert that does not state a submission state.
        "ALTER TABLE reservations ADD COLUMN submission_state TEXT NOT NULL"
        " DEFAULT 'UNVERIFIED'",
        "ALTER TABLE reservations ADD COLUMN broker_order_id TEXT"
        " CHECK (broker_order_id IS NULL"
        " OR (typeof(broker_order_id) = 'text' AND trim(broker_order_id) <> ''))",
        # An order belongs to one reservation at most. NULLs never collide.
        "CREATE UNIQUE INDEX reservations_broker_order_id ON reservations (broker_order_id)",
    ),
}

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


def _check_reservation_id(reservation_id):
    if not isinstance(reservation_id, str):
        raise ValueError("reservation_id must be a string")


def _check_order_id(broker_order_id):
    if not isinstance(broker_order_id, str) or not broker_order_id.strip():
        raise ValueError("broker_order_id must be a non-empty string")


class SqliteStore:
    """Coordinator state in a SQLite file. Every save is committed before it returns.

    Opening takes the exclusive lock before touching the database, so only one store
    at a time, in any process, can load and change the state; connect_read_only()
    serves independent readers. Opening also migrates the schema to SCHEMA_VERSION.
    close() releases the database and the lock, and the store cannot be used
    afterwards. The methods take no lock of their own: the coordinator calls them
    under its lock.

    A reservation whose result is NULL or UNKNOWN is still pending. Its submission
    state is RESERVED until save_submission_start(), SUBMITTING until
    save_acceptance() and then ACCEPTED, together with the broker order id. A
    reservation saved before submissions were tracked is UNVERIFIED: it may have been
    submitted, so it is never submitted again.
    """

    def __init__(self, path):
        self._connection = None
        self._lock = _lock_exclusively(path)  # before the database is touched
        try:
            with _translated(f"opening {path}"):
                self._connection = sqlite3.connect(path, check_same_thread=False)
                # Both take effect only outside a transaction.
                self._connection.execute("PRAGMA foreign_keys = ON")
                self._connection.execute("PRAGMA synchronous = FULL")
            self._migrate()
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

    def load_submissions(self):
        """Return {reservation_id: (submission_state, broker_order_id)} for every reservation.

        broker_order_id is None until the reservation is ACCEPTED. A row with an unknown
        state, or whose order id does not match its state, raises StorageError.
        """
        connection = self._open_connection()
        with _translated("loading the submissions"):
            rows = connection.execute(
                "SELECT reservation_id, submission_state, broker_order_id"
                " FROM reservations ORDER BY cycle_id, attempt"
            ).fetchall()
        submissions = {}
        for reservation_id, state, broker_order_id in rows:
            accepted = state == ACCEPTED
            if state not in SUBMISSION_STATES or accepted != (broker_order_id is not None):
                raise StorageError(
                    f"inconsistent saved state: reservation {reservation_id} is {state}"
                    f" with order id {broker_order_id!r}"
                )
            submissions[reservation_id] = (state, broker_order_id)
        return submissions

    def save_cycle(self, cycle_id, asset, direction):
        with self._transaction("saving the cycle") as connection:
            connection.execute(
                "INSERT INTO cycles (cycle_id, asset, direction) VALUES (?, ?, ?)",
                (cycle_id, asset, direction),
            )

    def save_reservation(self, reservation):
        """Save a new reservation as RESERVED, without a broker order id."""
        entry = reservation.entry
        with self._transaction("saving the reservation") as connection:
            connection.execute(
                "INSERT INTO reservations"
                " (reservation_id, cycle_id, attempt, stake_cents, submission_state)"
                " VALUES (?, ?, ?, ?, ?)",
                (reservation.reservation_id, reservation.cycle_id, entry.attempt,
                 entry.stake_cents, RESERVED),
            )

    def save_submission_start(self, reservation_id):
        """Mark a RESERVED reservation without result as SUBMITTING, before it is sent.

        The change is committed before returning. Only RESERVED becomes SUBMITTING, so a
        reservation starts one submission at most: a second call, also after a
        restart, raises StorageError and changes nothing.
        """
        _check_reservation_id(reservation_id)
        with self._transaction("saving the submission start") as connection:
            state, _, result = self._submission_row(connection, reservation_id)
            if state == UNVERIFIED:
                raise StorageError(
                    f"reservation {reservation_id} is {UNVERIFIED}: it may already have"
                    " been submitted, so no submission can start"
                )
            if state != RESERVED:
                raise StorageError(
                    f"reservation {reservation_id} is {state}: a submission starts only"
                    f" once, from {RESERVED}"
                )
            if result is not None:
                raise StorageError(
                    f"reservation {reservation_id} already has the result {result}"
                )
            connection.execute(
                "UPDATE reservations SET submission_state = ? WHERE reservation_id = ?",
                (SUBMITTING, reservation_id),
            )

    def save_acceptance(self, reservation_id, broker_order_id):
        """Save that the broker accepted a reservation as the order broker_order_id.

        A pending reservation that is SUBMITTING or UNVERIFIED becomes ACCEPTED, and the
        order id is committed before True is returned. Repeating the same acceptance
        returns False and changes nothing. A different order id, an order id that
        belongs to another reservation, a reservation still RESERVED or one with a
        definitive result raises StorageError and changes nothing.
        """
        _check_reservation_id(reservation_id)
        _check_order_id(broker_order_id)
        with self._transaction("saving the acceptance") as connection:
            state, saved_order_id, result = self._submission_row(connection, reservation_id)
            if state == ACCEPTED:
                if saved_order_id == broker_order_id:
                    return False  # the same acceptance again
                raise StorageError(
                    f"reservation {reservation_id} was already accepted as order"
                    f" {saved_order_id}, not {broker_order_id}"
                )
            if state not in (SUBMITTING, UNVERIFIED):
                raise StorageError(
                    f"reservation {reservation_id} is {state}: only {SUBMITTING} or"
                    f" {UNVERIFIED} can become {ACCEPTED}"
                )
            if result not in (None, UNKNOWN):
                raise StorageError(
                    f"reservation {reservation_id} already has the result {result}"
                )
            owner = connection.execute(
                "SELECT reservation_id FROM reservations WHERE broker_order_id = ?",
                (broker_order_id,),
            ).fetchone()
            if owner is not None:
                raise StorageError(
                    f"order {broker_order_id} already belongs to reservation {owner[0]}"
                )
            connection.execute(
                "UPDATE reservations SET submission_state = ?, broker_order_id = ?"
                " WHERE reservation_id = ?",
                (ACCEPTED, broker_order_id, reservation_id),
            )
            return True

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

    def _migrate(self):
        """Bring the schema to SCHEMA_VERSION in one transaction, or change nothing."""
        with self._transaction("migrating the database") as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if not 0 <= version <= SCHEMA_VERSION:
                raise StorageError(
                    f"the database has schema version {version}, but this program"
                    f" supports versions 0 to {SCHEMA_VERSION}; it was left untouched"
                )
            if version == SCHEMA_VERSION:
                return
            if version == 0:
                for statement in BASE_TABLES:
                    connection.execute(statement)
            for target in range(version + 1, SCHEMA_VERSION + 1):
                for statement in MIGRATIONS[target]:
                    connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _submission_row(connection, reservation_id):
        row = connection.execute(
            "SELECT submission_state, broker_order_id, result FROM reservations"
            " WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            raise StorageError(f"reservation {reservation_id} does not exist")
        return row

    def _open_connection(self):
        if self._connection is None:
            raise StorageError("the store is closed")
        return self._connection

    @contextmanager
    def _transaction(self, action):
        """Run the block in one write transaction, committed when the block finishes.

        BEGIN IMMEDIATE takes the write lock first, so whatever the block reads to
        decide belongs to the same transaction. Any error, including one raised by the
        commit, rolls everything back.
        """
        connection = self._open_connection()
        with _translated(action):
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
