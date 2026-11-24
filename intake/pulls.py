"""按槽续拉的站端登记。"""
import sqlite3
from dataclasses import dataclass

from device.buffers import AttemptKey, BufferAttempt

PULL_SCHEMA = '''
CREATE TABLE IF NOT EXISTS slot_pulls (
    shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
    pull_id TEXT NOT NULL,
    slot INTEGER NOT NULL CHECK (slot IN (0, 1)),
    recording_id TEXT NOT NULL,
    event_id TEXT,
    attempt_key TEXT,
    status TEXT NOT NULL CHECK (status IN ('in_flight', 'received', 'end')),
    PRIMARY KEY (shift_id, pull_id),
    FOREIGN KEY (shift_id, event_id) REFERENCES fragment_custody(shift_id, event_id)
);
'''


@dataclass(frozen=True)
class SlotPull:
    shift_id: str
    pull_id: str
    slot: int
    recording_id: str
    event_id: str | None
    key: AttemptKey | None
    status: str


def _slot_pull(row) -> SlotPull:
    return SlotPull(
        row['shift_id'], row['pull_id'], row['slot'], row['recording_id'], row['event_id'],
        None if row['attempt_key'] is None else AttemptKey.decode(row['attempt_key']), row['status'],
    )


def get_pull(db: sqlite3.Connection, shift_id: str, pull_id: str) -> SlotPull | None:
    row = db.execute(
        'SELECT * FROM slot_pulls WHERE shift_id = ? AND pull_id = ?', (shift_id, pull_id)
    ).fetchone()
    return None if row is None else _slot_pull(row)


def find_in_flight_pull(db: sqlite3.Connection, shift_id: str, slot: int) -> SlotPull | None:
    row = db.execute(
        "SELECT * FROM slot_pulls WHERE shift_id = ? AND slot = ? AND status = 'in_flight'",
        (shift_id, slot),
    ).fetchone()
    return None if row is None else _slot_pull(row)


def register_pull(db: sqlite3.Connection, shift_id: str, pull_id: str, slot: int, recording_id: str) -> None:
    if not db.in_transaction:
        raise RuntimeError('pull registration requires a station transaction')
    db.execute(
        "INSERT INTO slot_pulls VALUES (?, ?, ?, ?, NULL, NULL, 'in_flight')",
        (shift_id, pull_id, slot, recording_id),
    )


def attribute_pull(db: sqlite3.Connection, shift_id: str, pull_id: str, attempt: BufferAttempt) -> None:
    if not db.in_transaction:
        raise RuntimeError('pull attribution requires the custody transaction')
    result = db.execute(
        '''UPDATE slot_pulls SET event_id = ?, attempt_key = ?
           WHERE shift_id = ? AND pull_id = ? AND status = 'in_flight' ''',
        (attempt.event_id, attempt.key.encode(), shift_id, pull_id),
    )
    if result.rowcount != 1:
        raise ValueError('pull is no longer in flight')


def finish_pull(db: sqlite3.Connection, shift_id: str, pull_id: str, status: str) -> None:
    if status not in ('received', 'end'):
        raise ValueError('invalid pull finish status')
    if not db.in_transaction:
        raise RuntimeError('pull settlement requires a station transaction')
    result = db.execute(
        "UPDATE slot_pulls SET status = ? WHERE shift_id = ? AND pull_id = ? AND status = 'in_flight'",
        (status, shift_id, pull_id),
    )
    if result.rowcount != 1:
        raise ValueError('pull is no longer in flight')
