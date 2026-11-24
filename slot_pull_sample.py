import json
import sqlite3
from contextlib import closing, contextmanager

from archive.publication import publish
from archive.staging import list_staged_fragments
from device.buffers import AttemptKey
from device.recording import RecordingDevice
from intake.capture import capture
from intake.custody import get_capture_receipt
from recording_sample import fragment
from review.lookup import read
from shift.open_shift import connect_station, open_shift

SHIFT = "slot-watch"
RECORDING = "slot-run"
ROSTER = ("P0", "P1", "Q")


def slot_events(recording_id=RECORDING):
    return tuple(
        fragment(AttemptKey(recording_id, "roll-1", slot, load), name, name,
                 name + "-row", "whole", 0, 1, [value], shift_id=SHIFT)
        for name, slot, load, value in (("P0", 0, 1, 7), ("P1", 0, 2, 11), ("Q", 1, 1, 13))
    )


@contextmanager
def fail_custody_commit(db):
    """Force an actual commit failure during a fresh fragment receipt."""
    if db.in_transaction or db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise ValueError("fixture needs an idle station connection with foreign keys enabled")
    db.execute("CREATE TEMP TABLE slot_fixture_parent (id INTEGER PRIMARY KEY)")
    db.execute("""CREATE TEMP TABLE slot_fixture_child (
        parent_id INTEGER REFERENCES slot_fixture_parent(id) DEFERRABLE INITIALLY DEFERRED
    )""")
    db.execute("""CREATE TEMP TRIGGER slot_fixture_fault AFTER INSERT ON main.fragment_custody
        BEGIN INSERT INTO slot_fixture_child VALUES (1); END""")
    try:
        yield
    finally:
        db.execute("DROP TRIGGER temp.slot_fixture_fault")
        db.execute("DROP TABLE temp.slot_fixture_child")
        db.execute("DROP TABLE temp.slot_fixture_parent")


def check_baseline():
    plan = slot_events()
    p0, _, q = plan
    with closing(connect_station(":memory:")) as db, closing(RecordingDevice(":memory:", RECORDING, plan)) as device:
        open_shift(db, SHIFT, ROSTER)
        device.next_event()  # Model the first reply being lost at the caller.
        blocked = device.next_event() is None
        position = device.position
        q_visible = device.read_attempt(q.key) is not None or device.replay_event(q.event_id) is not None
        if device.read_attempt(p0.key) != p0 or not blocked or position != 1 or q_visible:
            raise RuntimeError("the default recording did not stop at its occupied slot")
        with fail_custody_commit(db):
            try:
                capture(db, device, SHIFT, device.replay_event(p0.event_id))
            except sqlite3.IntegrityError as error:
                if error.sqlite_errorname != "SQLITE_CONSTRAINT_FOREIGNKEY":
                    raise
            else:
                raise RuntimeError("the custody transaction unexpectedly committed")
        p0_retained = device.read_attempt(p0.key) == p0
        if (db.in_transaction or get_capture_receipt(db, SHIFT, p0.event_id) is not None
                or list_staged_fragments(db, SHIFT, "P0") or not p0_retained):
            raise RuntimeError("commit failure did not preserve the original source and empty custody")
        observed = {}
        for index, original in enumerate(plan):
            event = device.replay_event(p0.event_id) if index == 0 else device.next_event()
            if event != original or capture(db, device, SHIFT, event).status != "exited":
                raise RuntimeError(f"default recording did not deliver {original.event_id} in order")
            signed = publish(db, SHIFT, original.event_id)
            result = read(db, signed.receipt.number)
            observed[original.event_id] = list(result.readings)
        if observed != {"P0": [7], "P1": [11], "Q": [13]}:
            raise RuntimeError(f"unexpected default publications: {observed}")
    print(json.dumps({"default_blocked_position": position, "q_visible_while_blocked": q_visible,
                      "failed_commit_left_p0_resident": p0_retained, "default_publications": observed}))


if __name__ == "__main__":
    check_baseline()
