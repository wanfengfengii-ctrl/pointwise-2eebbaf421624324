import sqlite3
from dataclasses import dataclass

from archive.staging import stage_fragment
from device.buffers import AttemptKey, BufferAttempt
from device.recording import BY_SLOT, RecordingDevice
from device.seal import SealRecord
from device.unload import DeviceUnavailable
from intake.custody import (
    CaptureReceipt, get_capture_receipt, load_received_seal, store_fragment, store_received_seal,
)
from intake.fragments import validate_fragment
from intake.pulls import (
    SlotPull, attribute_pull, find_in_flight_pull, finish_pull, get_pull, register_pull,
)
from intake.resume import resume
from shift.close_shift import get_closure
from shift.lines import require_identifier, require_line
from shift.open_shift import get_shift, station_write


@dataclass(frozen=True)
class CaptureResult:
    status: str
    receipt: CaptureReceipt | None = None
    seal: SealRecord | None = None


def _require_intake_open(db, shift_id, already_received):
    if not already_received and get_closure(db, shift_id) is not None:
        raise ValueError('shift recording has already been closed')


def capture(
    db: sqlite3.Connection, device: RecordingDevice | None, shift_id: str, event: BufferAttempt | SealRecord
) -> CaptureResult:
    if device is not None and device.mode == BY_SLOT:
        raise ValueError('a by-slot recording is managed by slot pulls')
    if isinstance(event, SealRecord):
        if event.shift_id != shift_id or (device is not None and event.recording_id != device.recording_id):
            raise ValueError('seal belongs to another shift or recording')
        with station_write(db):
            require_line(get_shift(db, shift_id).roster, event.line_id)
            existing = load_received_seal(db, shift_id, event.event_id)
            _require_intake_open(db, shift_id, existing is not None)
            stored = store_received_seal(db, event)
        return CaptureResult('seal_received', seal=stored)

    fragment = validate_fragment(event)
    if fragment.shift_id != shift_id or (device is not None and event.key.recording_id != device.recording_id):
        raise ValueError('fragment belongs to another shift or recording')
    with station_write(db):
        require_line(get_shift(db, shift_id).roster, fragment.line_id)
        previous = get_capture_receipt(db, shift_id, event.event_id)
        _require_intake_open(db, shift_id, previous is not None)
        receipt = store_fragment(db, fragment)
        stage_fragment(db, receipt.shift_id, receipt.event_id)
    result = resume(db, shift_id, receipt.event_id, device)
    return CaptureResult(result.status, receipt=result.receipt)


@dataclass(frozen=True)
class PullResult:
    status: str
    event_id: str | None = None
    key: AttemptKey | None = None
    receipt: CaptureReceipt | None = None


def _received_result(db: sqlite3.Connection, shift_id: str, registration: SlotPull) -> PullResult:
    receipt = get_capture_receipt(db, shift_id, registration.event_id)
    return PullResult('received', registration.event_id, registration.key, receipt)


def _continue_pull(
    db: sqlite3.Connection, device: RecordingDevice, shift_id: str, registration: SlotPull
) -> PullResult:
    if registration.event_id is None:
        try:
            attempt = device.load_slot(registration.slot)
        except DeviceUnavailable:
            return PullResult('pending')
        if attempt is None:
            with station_write(db):
                finish_pull(db, shift_id, registration.pull_id, 'end')
            return PullResult('end')
        fragment = validate_fragment(attempt)
        if fragment.shift_id != shift_id:
            raise ValueError('fragment belongs to another shift or recording')
        with station_write(db):
            require_line(get_shift(db, shift_id).roster, fragment.line_id)
            previous = get_capture_receipt(db, shift_id, attempt.event_id)
            _require_intake_open(db, shift_id, previous is not None)
            receipt = store_fragment(db, fragment)
            stage_fragment(db, receipt.shift_id, receipt.event_id)
            attribute_pull(db, shift_id, registration.pull_id, attempt)
        registration = get_pull(db, shift_id, registration.pull_id)
    outcome = resume(db, shift_id, registration.event_id, device)
    if outcome.status == 'exited':
        with station_write(db):
            finish_pull(db, shift_id, registration.pull_id, 'received')
        return _received_result(db, shift_id, get_pull(db, shift_id, registration.pull_id))
    return PullResult('pending')


def pull(
    db: sqlite3.Connection, device: RecordingDevice | None, shift_id: str, slot: int, pull_id: str
) -> PullResult:
    if type(slot) is not int or slot not in (0, 1):
        raise ValueError('invalid device slot')
    require_identifier(pull_id, 'pull identifier')
    if device is not None and device.mode != BY_SLOT:
        raise ValueError('slot pulls require a by-slot recording')
    get_shift(db, shift_id)
    registration = get_pull(db, shift_id, pull_id)
    if registration is not None:
        if registration.slot != slot:
            raise ValueError('pull identifier already bound to another slot')
        if device is not None and registration.recording_id != device.recording_id:
            raise ValueError('pull identifier already bound to another recording')
        if registration.status == 'end':
            return PullResult('end')
        if registration.status == 'received':
            return _received_result(db, shift_id, registration)
        if device is None:
            return PullResult('pending')
        return _continue_pull(db, device, shift_id, registration)
    if device is None:
        raise LookupError(f'unknown pull: {pull_id}')
    _require_intake_open(db, shift_id, False)
    if find_in_flight_pull(db, shift_id, slot) is not None:
        return PullResult('busy')
    with station_write(db):
        register_pull(db, shift_id, pull_id, slot, device.recording_id)
    return _continue_pull(db, device, shift_id, get_pull(db, shift_id, pull_id))
