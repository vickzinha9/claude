import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing

from pocketautonomo.coordinator import Coordinator, CoordinatorError
from pocketautonomo.cycle import CALL, LOSS
from pocketautonomo.storage import (
    DatabaseInUseError,
    SqliteStore,
    StorageError,
    connect_read_only,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSET = "EURUSD_otc"
BALANCE = 100_000  # R$1.000,00 in cents

# Second instance, in its own process: opens the database and reports what it saw.
OPEN_AND_REPORT = """
import sys
from pocketautonomo.coordinator import Coordinator
from pocketautonomo.storage import DatabaseInUseError, SqliteStore
try:
    coordinator = Coordinator(SqliteStore(sys.argv[1]))
except DatabaseInUseError as error:
    print(error)
    sys.exit(3)
status = coordinator.status()
pending = status.pending.reservation_id if status.pending else None
print(status.is_paused, status.recovery_pending, status.attempt, pending)
coordinator.close()
"""

# First instance, in its own process: reserves an entry, prints its id and keeps the
# database open until its stdin is closed or the process is killed.
HOLD = """
import sys
from pocketautonomo.coordinator import Coordinator
from pocketautonomo.storage import SqliteStore
coordinator = Coordinator(SqliteStore(sys.argv[1]))
coordinator.resume()
coordinator.start_cycle("EURUSD_otc", "CALL")
print(coordinator.reserve_next_entry(100_000).reservation_id, flush=True)
sys.stdin.read()
coordinator.close()
"""


class ExclusivityTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = os.path.join(directory.name, "state.db")

    def open(self):
        coordinator = Coordinator(SqliteStore(self.path))
        self.addCleanup(coordinator.close)
        return coordinator

    def open_once_released(self):
        """Open after the OS drops a dead process's lock (Windows may take a moment)."""
        deadline = time.monotonic() + 10
        while True:
            try:
                return self.open()
            except DatabaseInUseError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)

    def digest(self):
        with open(self.path, "rb") as database:
            return hashlib.sha256(database.read()).hexdigest()

    def second_instance(self):
        return subprocess.run(
            [sys.executable, "-c", OPEN_AND_REPORT, self.path],
            cwd=ROOT, capture_output=True, text=True, timeout=60,
        )

    def start_holder(self):
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLD, self.path], cwd=ROOT, text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        watchdog = threading.Timer(60, holder.kill)
        watchdog.start()

        def stop():
            watchdog.cancel()
            if holder.poll() is None:
                holder.kill()
            holder.communicate(timeout=60)

        self.addCleanup(stop)
        return holder

    def test_second_process_is_refused_while_the_first_is_open(self):
        first = self.open()
        first.resume()
        first.start_cycle(ASSET, CALL)
        reservation = first.reserve_next_entry(BALANCE)
        first.begin_submission(reservation.reservation_id)
        saved = self.digest()

        refused = self.second_instance()
        self.assertEqual(refused.returncode, 3, refused.stderr)
        self.assertIn("another coordinator is already using", refused.stdout)
        self.assertEqual(self.digest(), saved)  # the refused instance changed nothing
        self.assertTrue(first.record_result(reservation.reservation_id, LOSS))

        first.close()
        opened = self.second_instance()
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertEqual(opened.stdout.split(), ["True", "True", "2", "None"])

    def test_lock_is_released_when_the_holding_process_is_killed(self):
        holder = self.start_holder()
        reservation_id = holder.stdout.readline().strip()
        if not reservation_id:
            self.fail("the holder process did not start: " + holder.communicate(timeout=60)[1])

        with self.assertRaisesRegex(DatabaseInUseError, "another coordinator is already using"):
            SqliteStore(self.path)
        with closing(connect_read_only(self.path)) as reader:  # independent reads still work
            rows = reader.execute("SELECT reservation_id, result FROM reservations").fetchall()
            self.assertEqual(rows, [(reservation_id, None)])
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute("DELETE FROM reservations")

        holder.kill()  # abrupt end: the holder never calls close()
        holder.wait(timeout=60)
        status = self.open_once_released().status()
        self.assertEqual(
            (status.is_paused, status.recovery_pending, status.pending.reservation_id),
            (True, True, reservation_id),
        )

    def test_closing_releases_the_database_and_refuses_later_operations(self):
        with self.open() as coordinator:
            coordinator.resume()
            coordinator.start_cycle(ASSET, CALL)
            reservation = coordinator.reserve_next_entry(BALANCE)
            with self.assertRaises(DatabaseInUseError):  # also refused within one process
                SqliteStore(self.path)
        coordinator.close()  # closing again is harmless
        operations = {
            "status": coordinator.status,
            "resume": coordinator.resume,
            "start_cycle": lambda: coordinator.start_cycle(ASSET, CALL),
            "reserve_next_entry": lambda: coordinator.reserve_next_entry(BALANCE),
            "record_result": lambda: coordinator.record_result(reservation.reservation_id, LOSS),
            "begin_submission": lambda: coordinator.begin_submission(reservation.reservation_id),
            "confirm_acceptance": lambda: coordinator.confirm_acceptance(
                reservation.reservation_id, "ORD-1"
            ),
            "close_not_submitted": lambda: coordinator.close_not_submitted(
                reservation.reservation_id
            ),
            "abandon_cycle": coordinator.abandon_cycle,
        }
        for name, operation in operations.items():
            with self.subTest(operation=name):
                with self.assertRaisesRegex(CoordinatorError, "closed"):
                    operation()
        store = SqliteStore(self.path)  # close() released the lock
        store.close()
        with self.assertRaisesRegex(StorageError, "closed"):
            store.load()


if __name__ == "__main__":
    unittest.main()
