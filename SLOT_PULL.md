# 双槽续拉

跟车员按槽点取整行原片。一个站库采一台设备的一次录制，调用依次进行，站库和设备库可以分别关闭重开。轨检原件、实收、正式测线和班单的口径见 `README.md`。跨卷片和封卷仍由默认录制处理。

## 调用约定

以 `RecordingDevice(path, recording_id, events, mode='by_slot')` 开启按槽录制。重开使用原计划和原模式。每槽只按该槽在计划中的相对顺序装片，两槽各可有一笔拉取在办。按槽模式的装片和实收由拉取流程发起。该模式不经 `next_event()` 发件，也不经带该设备的旧 `capture` 发起新实收。默认录制沿用原调用。

新增 `intake.capture.pull(db, device, shift_id, slot, pull_id)`。槽号为整数 0 或 1。拉取号是班内标识，固定槽与录制。已登记的号改槽，或带着另一录制重投，抛出 `ValueError` 且不推进。名册、原片、身份检查和班末终结规则沿用原流程。

| `status` | 含义 |
| --- | --- |
| `pending` | 本号在办，尚未实收或尚未证实原任次退出。设备失答时用原号续办。 |
| `busy` | 同槽有另一笔在办。本次未接单。 |
| `received` | 本号原件已实收且原任次退出已确认。 |
| `end` | 本槽已经没有待装片或在办原片，本号以结束办结。 |

结果有 `.status`。`received` 另给 `.event_id`、`.key`（`AttemptKey`）和 `.receipt`（既有 `CaptureReceipt`）。同号重投复领原终局。`device=None` 时，已办返回同一 `received` 或 `end`，已登记的在办返回 `pending`，未知拉取号抛出 `LookupError`。槽位后来复用时，已办结果仍按原任次复领。

实收提交须同时保全原片、实收和该次拉取的归属，提交后才可退出源片。实收 SQLite 写入失败沿原异常抛出，站库无本次实收，源片仍在，可按原号续办。退出回包丢失时按原任次待查，在原退出得到确认前不得以这笔拉取返回 `received`。站端原收到的正式原片仍可由既有 `publish` 和 `read` 办理测线。

## 样例与故障控制

`slot_pull_sample.py` 提供 `SHIFT`、`RECORDING`、`ROSTER` 和 `slot_events()`。计划依次为 0 槽 P0 的 [7]、0 槽 P1 的 [11]、1 槽 Q 的 [13]，每片是一条具名测线。可给 `slot_events(recording_id=...)` 传另一录制号构造错绑对照。`python3 -B slot_pull_sample.py` 运行默认录制对照，显示 P0 占槽时 Q 未公开，以及顺序取完后三条签发原值。

按槽设备支持可选 `lost_load_replies` 事件号集合，指定事件在首次实际装片提交后丢失该回包。原 `lost_unload_replies` 和 `query_available` 可控制退出失答及退出查询。`fail_custody_commit(db)` 可围住一次新原片实收调用，使站库事务在提交时因延迟外键约束失败，离开上下文后即可重试。

核验时让 P0 装片失答，在它仍在办时实收并签出 Q。重开两库，核对 P0 的实收提交失败保留源片，退出失答仍能沿原任次结清，随后采 P1 并用旧号领回 P0。核对在办同槽异号、同号异绑、读尽重投，以及断源复领的原读、出处和首执。

## 运行入口

沿用 `.pointwise-runtime.json` 的 `version=1` 和 `kind=cli`。`setup` 是准备命令的参数数组列表，`verify` 和 `demo` 各为一条命令的参数数组。让 `verify` 跑原完整回归和按槽核验，断言失败应以非零状态退出。让 `demo` 实际展示 Q 先签、P0 续拉和关源后按号领取。原 `python3 -B demo.py --rollover` 仍可独立查看跨卷对照。
