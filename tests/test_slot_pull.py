import sqlite3
import tempfile
import unittest
from pathlib import Path

from archive.publication import publish
from device.buffers import AttemptKey
from device.recording import RecordingDevice
from device.unload import DeviceUnavailable
from handover.issue import finish_shift_handover, issue
from intake.capture import capture, pull
from intake.custody import get_capture_receipt
from recording_sample import RECORDING as SAMPLE_RECORDING
from recording_sample import fragment, ordinary, rollover_events
from review.lookup import read
from shift.open_shift import connect_station, open_shift
from slot_pull_sample import RECORDING, ROSTER, SHIFT, fail_custody_commit, slot_events


class SlotPullTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.db = self.connect()
        open_shift(self.db, SHIFT, ROSTER)

    def connect(self):
        db = connect_station(self.root / 'station.sqlite')
        self.addCleanup(db.close)
        return db

    def device(self, events=None, **options):
        plan = slot_events() if events is None else events
        device = RecordingDevice(self.root / 'device.sqlite', RECORDING, plan, mode='by_slot', **options)
        self.addCleanup(device.close)
        return device

    def reopen(self, **options):
        self.db.close()
        self.db = self.connect()
        return self.device(**options)

    def test_load_and_unload_reply_loss_commit_failure_and_slot_reuse(self):
        plan = slot_events()
        p0, p1, q = plan
        faults = {'lost_load_replies': {'P0'}, 'lost_unload_replies': {'P0'}}
        device = self.device(plan, **faults)
        # P0 装片失答：本号在办，同槽异号未接单，同号改槽或改录制拒绝。
        self.assertEqual(pull(self.db, device, SHIFT, 0, 'pull-p0').status, 'pending')
        self.assertEqual(pull(self.db, device, SHIFT, 0, 'pull-other').status, 'busy')
        with self.assertRaises(ValueError):
            pull(self.db, device, SHIFT, 1, 'pull-p0')
        other = RecordingDevice(self.root / 'other.sqlite', 'other-run', slot_events('other-run'), mode='by_slot')
        self.addCleanup(other.close)
        with self.assertRaises(ValueError):
            pull(self.db, other, SHIFT, 0, 'pull-p0')
        self.assertEqual(pull(self.db, None, SHIFT, 0, 'pull-p0').status, 'pending')
        with self.assertRaises(LookupError):
            pull(self.db, None, SHIFT, 0, 'pull-unknown')
        # P0 仍在办时实收并签出 Q 的 13 mm。
        q_pull = pull(self.db, device, SHIFT, 1, 'pull-q')
        self.assertEqual((q_pull.status, q_pull.event_id, q_pull.key), ('received', 'Q', q.key))
        signed_q = publish(self.db, SHIFT, 'Q')
        self.assertEqual(list(read(self.db, signed_q.receipt.number).readings), [13])
        # 重开两库：实收提交失败保全源片，站库无本次实收，拉取仍在办。
        device.close()
        device = self.reopen(**faults)
        with fail_custody_commit(self.db):
            with self.assertRaises(sqlite3.IntegrityError) as failed:
                pull(self.db, device, SHIFT, 0, 'pull-p0')
        self.assertEqual(failed.exception.sqlite_errorname, 'SQLITE_CONSTRAINT_FOREIGNKEY')
        self.assertIsNone(get_capture_receipt(self.db, SHIFT, 'P0'))
        self.assertEqual(device.read_attempt(p0.key), p0)
        self.assertEqual(pull(self.db, None, SHIFT, 0, 'pull-p0').status, 'pending')
        # 按原号续办：实收提交后退出失答，按原任次待查，不得返回 received。
        self.assertEqual(pull(self.db, device, SHIFT, 0, 'pull-p0').status, 'pending')
        self.assertEqual(device.query_unload(p0.key).status, 'exited')
        self.assertEqual(get_capture_receipt(self.db, SHIFT, 'P0').exit_status, 'pending')
        # 站端原收到的正式原片仍可签发。
        signed_p0 = publish(self.db, SHIFT, 'P0')
        view = read(self.db, signed_p0.receipt.number)
        self.assertEqual(list(view.readings), [7])
        source, = view.provenance
        self.assertEqual(
            (source.recording_id, source.roll, source.slot, source.load, source.event_id, source.checksum),
            (RECORDING, 'roll-1', 0, 1, 'P0', p0.checksum),
        )
        # 退出查询不可用时仍在办，恢复后沿原任次结清。
        device.query_available = False
        self.assertEqual(pull(self.db, device, SHIFT, 0, 'pull-p0').status, 'pending')
        device.query_available = True
        settled = pull(self.db, device, SHIFT, 0, 'pull-p0')
        self.assertEqual((settled.status, settled.event_id, settled.key), ('received', 'P0', p0.key))
        self.assertEqual(settled.receipt.exit_status, 'exited')
        # 槽位复用给 P1 后，旧号仍领 P0 的原任次和原实收。
        p1_pull = pull(self.db, device, SHIFT, 0, 'pull-p1')
        self.assertEqual((p1_pull.status, p1_pull.event_id, p1_pull.key), ('received', 'P1', p1.key))
        again = pull(self.db, device, SHIFT, 0, 'pull-p0')
        self.assertEqual((again.status, again.event_id, again.key), ('received', 'P0', p0.key))
        self.assertEqual(again.receipt, settled.receipt)
        self.assertEqual(pull(self.db, device, SHIFT, 1, 'pull-q').receipt, q_pull.receipt)
        # 槽已读尽：本号以结束办结，同号可断源复领。
        self.assertEqual(pull(self.db, device, SHIFT, 0, 'pull-tail').status, 'end')
        self.assertEqual(pull(self.db, device, SHIFT, 0, 'pull-tail').status, 'end')
        self.assertEqual(pull(self.db, None, SHIFT, 0, 'pull-tail').status, 'end')
        # 断源复领的原读、出处和首执。
        device.close()
        offline = pull(self.db, None, SHIFT, 0, 'pull-p0')
        self.assertEqual((offline.status, offline.event_id, offline.key), ('received', 'P0', p0.key))
        self.assertEqual(offline.receipt, settled.receipt)
        self.assertEqual(publish(self.db, SHIFT, 'P0'), signed_p0)
        self.assertEqual(list(read(self.db, signed_p0.receipt.number).readings), [7])
        self.assertEqual(pull(self.db, None, SHIFT, 1, 'pull-q').status, 'received')

    def test_managed_recording_only_serves_pulls(self):
        plan = slot_events()
        device = self.device(plan)
        with self.assertRaises(DeviceUnavailable):
            device.next_event()
        with self.assertRaises(ValueError):
            capture(self.db, device, SHIFT, plan[0])
        self.assertEqual(device.position, 0)
        self.assertEqual(device.read_attempt(plan[0].key), None)
        ordered = RecordingDevice(self.root / 'ordered.sqlite', SAMPLE_RECORDING, (ordinary(),))
        self.addCleanup(ordered.close)
        with self.assertRaises(ValueError):
            pull(self.db, ordered, SHIFT, 0, 'pull-x')
        with self.assertRaises(ValueError):
            pull(self.db, device, SHIFT, 2, 'pull-x')
        with self.assertRaises(ValueError):
            pull(self.db, device, SHIFT, 0, ' bad ')
        # 重开须使用原模式，两种方向都拒绝。
        device.close()
        with self.assertRaisesRegex(ValueError, 'different mode'):
            RecordingDevice(self.root / 'device.sqlite', RECORDING, plan)
        ordered.close()
        with self.assertRaisesRegex(ValueError, 'different mode'):
            RecordingDevice(self.root / 'ordered.sqlite', SAMPLE_RECORDING, (ordinary(),), mode='by_slot')
        with self.assertRaisesRegex(ValueError, 'mode'):
            RecordingDevice(self.root / 'mode.sqlite', RECORDING, plan, mode='sideways')
        with self.assertRaisesRegex(ValueError, 'buffer attempts'):
            RecordingDevice(self.root / 'seal.sqlite', SAMPLE_RECORDING, rollover_events(), mode='by_slot')

    def test_bad_fragment_keeps_source_and_pull_in_flight(self):
        foreign_line = fragment(
            AttemptKey(RECORDING, 'roll-x', 0, 1), 'bad-line', 'outside', 'row-x', 'whole', 0, 1, [3],
            shift_id=SHIFT,
        )
        foreign_shift = fragment(
            AttemptKey(RECORDING, 'roll-x', 1, 1), 'bad-shift', 'P0', 'row-y', 'whole', 0, 1, [4],
            shift_id='other-shift',
        )
        device = self.device((foreign_line, foreign_shift))
        with self.assertRaises(ValueError):
            pull(self.db, device, SHIFT, 0, 'pull-bad-line')
        with self.assertRaises(ValueError):
            pull(self.db, device, SHIFT, 1, 'pull-bad-shift')
        self.assertEqual(device.read_attempt(foreign_line.key), foreign_line)
        self.assertEqual(device.read_attempt(foreign_shift.key), foreign_shift)
        self.assertIsNone(get_capture_receipt(self.db, SHIFT, 'bad-line'))
        self.assertIsNone(get_capture_receipt(self.db, SHIFT, 'bad-shift'))
        self.assertEqual(pull(self.db, None, SHIFT, 0, 'pull-bad-line').status, 'pending')

    def test_closure_rules_follow_capture(self):
        plan = slot_events()
        device = self.device(plan, lost_load_replies={'P0'})
        self.assertEqual(pull(self.db, device, SHIFT, 0, 'pull-p0').status, 'pending')
        self.assertEqual(pull(self.db, device, SHIFT, 1, 'pull-q').status, 'received')
        publish(self.db, SHIFT, 'Q')
        handover = issue(self.db, SHIFT, 'slot-night', ('Q',))
        finish_shift_handover(self.db, SHIFT, handover.number)
        with self.assertRaises(ValueError):
            pull(self.db, device, SHIFT, 0, 'pull-p0')
        with self.assertRaises(ValueError):
            pull(self.db, device, SHIFT, 0, 'pull-p1')
        with self.assertRaises(LookupError):
            pull(self.db, None, SHIFT, 0, 'pull-p1')
        self.assertEqual(pull(self.db, None, SHIFT, 0, 'pull-p0').status, 'pending')
        self.assertEqual(pull(self.db, device, SHIFT, 1, 'pull-q').status, 'received')
        self.assertEqual(pull(self.db, None, SHIFT, 1, 'pull-q').status, 'received')


if __name__ == '__main__':
    unittest.main()
