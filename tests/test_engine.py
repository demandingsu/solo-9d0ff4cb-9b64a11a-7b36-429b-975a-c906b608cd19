"""引擎状态机测试：用假自动化替代 Playwright，覆盖全部关键业务规则。"""
import asyncio

import pytest

from app.worker import states
from app.worker.db import DB
from app.worker.engine import Engine


ROWS = [
    {"row_no": 1, "hole_no": "ZK-01", "top_depth": 0.0, "bottom_depth": 5.0,
     "lithology": "粉质黏土"},
    {"row_no": 2, "hole_no": "ZK-01", "top_depth": 5.0, "bottom_depth": 12.0,
     "lithology": "细砂"},
    {"row_no": 3, "hole_no": "ZK-02", "top_depth": 0.0, "bottom_depth": 3.5,
     "lithology": "淤泥质土"},
]


class FakeAutomation:
    """可编程的假门户：store 为门户当前可见记录。"""

    def __init__(self):
        self.store: list[dict] = []
        self.query_calls = 0
        self.submit_calls = 0
        self.fail_query_times = 0          # 前 N 次查询抛运行时错误
        self.disconnect_on_submit = False  # 提交后未确认（但已落库）
        self.slow_visibility = False       # 保存确认但复核暂不可见
        self._submitted_hidden: list[dict] = []

    async def query_layer(self, portal, hole_no, top, bottom):
        from app.worker.automation import AutomationError, QueryResult
        self.query_calls += 1
        if self.fail_query_times > 0:
            self.fail_query_times -= 1
            raise AutomationError("模拟浏览器超时")
        # 已落库但尚未到可见时间的记录（断线/延迟入库）查询不到
        visible = [r for r in self.store if r not in self._submitted_hidden]
        matched = [r for r in visible
                   if r["hole_no"] == hole_no
                   and abs(r["top_depth"] - top) < 1e-9
                   and abs(r["bottom_depth"] - bottom) < 1e-9]
        return QueryResult(found=len(matched), records=[dict(r) for r in matched])

    async def submit_layer(self, portal, hole_no, top, bottom, lithology):
        from app.worker.automation import SubmitResult
        self.submit_calls += 1
        rec = {"hole_no": hole_no, "top_depth": float(top),
               "bottom_depth": float(bottom), "lithology": lithology,
               "url": f"/layers/fake-{self.submit_calls}"}
        # 模拟真实门户：数据已落库
        self.store.append(rec)
        if self.disconnect_on_submit:
            # 断线只影响首次保存；之后恢复正常（模拟门户临时网络中断）
            self.disconnect_on_submit = False
            self._submitted_hidden.append(rec)
            return SubmitResult(confirmed=False, error="模拟保存后断线")
        if self.slow_visibility:
            # 仅首条记录延迟可见，之后恢复正常
            self.slow_visibility = False
            self._submitted_hidden.append(rec)
            return SubmitResult(confirmed=True, record_url=rec["url"])
        # 正常模式：立即可见（若此前有隐藏记录，此时也一并到可见时间）
        self._submitted_hidden.clear()
        return SubmitResult(confirmed=True, record_url=rec["url"])

    def reveal_all(self):
        self._submitted_hidden.clear()


@pytest.fixture
def engine(tmp_path):
    db = DB(str(tmp_path / "worker.db"))
    fake = FakeAutomation()
    eng = Engine(db, fake)
    yield eng, db, fake
    db.close()


def _make_task(db, portal="http://portal/"):
    task, created = db.create_or_get_task(
        "t1", portal, "hash1", "layers.csv", ROWS)
    assert created
    return task["id"]


async def test_new_rows_get_confirmed(engine):
    eng, db, fake = engine
    tid = _make_task(db)
    await eng.process_task(tid)
    task = db.get_task(tid)
    assert task["status"] == states.TASK_COMPLETED
    rows = db.list_rows(tid)
    assert all(r["status"] == states.ROW_CONFIRMED for r in rows)
    assert fake.submit_calls == 3
    # 每行先查后填
    assert fake.query_calls >= 3
    assert all(r["record_url"] for r in rows)


async def test_existing_match_is_skipped_no_submit(engine):
    eng, db, fake = engine
    fake.store.append({"hole_no": "ZK-01", "top_depth": 0.0, "bottom_depth": 5.0,
                       "lithology": "粉质黏土", "url": "/layers/x"})
    tid = _make_task(db)
    await eng.process_task(tid)
    rows = db.list_rows(tid)
    assert rows[0]["status"] == states.ROW_SKIPPED
    assert rows[0]["record_url"] == "http://portal/layers/x"
    assert fake.submit_calls == 2  # 首行跳过，其余两行填报


async def test_lithology_mismatch_blocks_and_later_rows_untouched(engine):
    eng, db, fake = engine
    fake.store.append({"hole_no": "ZK-01", "top_depth": 0.0, "bottom_depth": 5.0,
                       "lithology": "黏土", "url": "/layers/x"})
    tid = _make_task(db)
    await eng.process_task(tid)
    task = db.get_task(tid)
    assert task["status"] == states.TASK_WAITING
    rows = db.list_rows(tid)
    assert rows[0]["status"] == states.ROW_CONFLICT
    assert rows[1]["status"] == states.ROW_PENDING
    assert rows[2]["status"] == states.ROW_PENDING
    assert fake.submit_calls == 0


async def test_duplicate_records_conflict(engine):
    eng, db, fake = engine
    for i in range(2):
        fake.store.append({"hole_no": "ZK-01", "top_depth": 0.0, "bottom_depth": 5.0,
                           "lithology": "粉质黏土", "url": f"/layers/{i}"})
    tid = _make_task(db)
    await eng.process_task(tid)
    assert db.list_rows(tid)[0]["status"] == states.ROW_CONFLICT
    assert fake.submit_calls == 0


async def test_disconnect_marks_unconfirmed_and_never_auto_resubmits(engine):
    eng, db, fake = engine
    fake.disconnect_on_submit = True
    tid = _make_task(db)
    await eng.process_task(tid)
    rows = db.list_rows(tid)
    assert rows[0]["status"] == states.ROW_SUBMITTED_UNCONFIRMED
    assert rows[1]["status"] == states.ROW_PENDING  # 停在首行
    submitted_after_first = fake.submit_calls
    # 再次推进：只查不填，查不到也不得重提
    await eng.process_task(tid)
    assert fake.submit_calls == submitted_after_first
    assert db.list_rows(tid)[0]["status"] == states.ROW_SUBMITTED_UNCONFIRMED
    # 门户记录延迟可见后续查确认，并继续后续行
    fake.reveal_all()
    await eng.process_task(tid)
    task = db.get_task(tid)
    assert task["status"] == states.TASK_COMPLETED
    rows = db.list_rows(tid)
    assert rows[0]["status"] == states.ROW_CONFIRMED
    assert "续查" in rows[0]["detail"]
    # 首行从未二次提交
    assert fake.submit_calls == 3


async def test_slow_visibility_then_resume(engine):
    eng, db, fake = engine
    fake.slow_visibility = True
    tid = _make_task(db)
    await eng.process_task(tid)
    rows = db.list_rows(tid)
    # 保存页确认但查询不可见 → 未确认停行
    assert rows[0]["status"] == states.ROW_SUBMITTED_UNCONFIRMED
    fake.reveal_all()
    await eng.process_task(tid)
    assert db.get_task(tid)["status"] == states.TASK_COMPLETED


async def test_conflict_resume_passes_after_manual_fix(engine):
    eng, db, fake = engine
    fake.store.append({"hole_no": "ZK-01", "top_depth": 0.0, "bottom_depth": 5.0,
                       "lithology": "黏土", "url": "/layers/x"})
    tid = _make_task(db)
    await eng.process_task(tid)
    assert db.list_rows(tid)[0]["status"] == states.ROW_CONFLICT
    # 未修正前续查仍停行
    await eng.process_task(tid)
    assert db.list_rows(tid)[0]["status"] == states.ROW_CONFLICT
    # 人工核实修正门户数据
    fake.store[0]["lithology"] = "粉质黏土"
    await eng.process_task(tid)
    assert db.get_task(tid)["status"] == states.TASK_COMPLETED
    assert db.list_rows(tid)[0]["status"] == states.ROW_CONFIRMED


async def test_runtime_error_retries_then_recovers(engine, monkeypatch):
    eng, db, fake = engine
    monkeypatch.setattr("app.config.MAX_RUNTIME_ERRORS", 5)
    fake.fail_query_times = 2
    tid = _make_task(db)
    # 每次推进每行只尝试一次，失败停行；resume 驱动重试
    await eng.process_task(tid)
    assert db.list_rows(tid)[0]["attempts"] == 1
    await eng.process_task(tid)
    first = db.list_rows(tid)[0]
    assert first["attempts"] == 2 and first["status"] == states.ROW_ERROR
    # 第三次查询恢复，整任务完成
    await eng.process_task(tid)
    assert db.get_task(tid)["status"] == states.TASK_COMPLETED
    assert db.list_rows(tid)[0]["attempts"] == 2


async def test_runtime_error_exhausts_to_failed(engine, monkeypatch):
    eng, db, fake = engine
    monkeypatch.setattr("app.config.MAX_RUNTIME_ERRORS", 2)
    fake.fail_query_times = 99
    tid = _make_task(db)
    await eng.process_task(tid)   # 第 1 次 → ERROR 停行
    await eng.process_task(tid)   # 第 2 次 → 达到上限 → FAILED
    assert db.get_task(tid)["status"] == states.TASK_FAILED
    assert db.list_rows(tid)[0]["status"] == states.ROW_ERROR
    # 故障恢复后，resume 仍可继续（ERROR 允许重试）
    fake.fail_query_times = 0
    await eng.process_task(tid)
    assert db.get_task(tid)["status"] == states.TASK_COMPLETED


async def test_duplicate_file_same_portal_returns_same_task(engine):
    eng, db, fake = engine
    t1, created1 = db.create_or_get_task("id-a", "http://portal/", "h1", "a.csv", ROWS)
    t2, created2 = db.create_or_get_task("id-b", "http://portal/", "h1", "a.csv", ROWS)
    assert created1 and not created2
    assert t1["id"] == t2["id"]
    # 不同门户相同文件 → 独立任务
    t3, created3 = db.create_or_get_task("id-c", "http://other/", "h1", "a.csv", ROWS)
    assert created3 and t3["id"] == "id-c"


async def test_restart_resume_from_unconfirmed_without_resubmit(engine):
    """模拟重启：UNCONFIRMED 行在新引擎实例中仍只查不填。"""
    eng, db, fake = engine
    fake.disconnect_on_submit = True
    tid = _make_task(db)
    await eng.process_task(tid)
    assert db.list_rows(tid)[0]["status"] == states.ROW_SUBMITTED_UNCONFIRMED
    submits = fake.submit_calls

    # 新引擎实例（同一 DB 文件），模拟服务重启
    eng2 = Engine(db, fake)
    await eng2.process_task(tid)
    assert fake.submit_calls == submits  # 没有重提
    assert db.list_rows(tid)[0]["status"] == states.ROW_SUBMITTED_UNCONFIRMED
    fake.reveal_all()
    await eng2.process_task(tid)
    assert db.get_task(tid)["status"] == states.TASK_COMPLETED


async def test_confirmed_rows_never_refilled(engine):
    eng, db, fake = engine
    tid = _make_task(db)
    await eng.process_task(tid)
    total_submits = fake.submit_calls
    # 再来一轮（模拟重复触发）
    await eng.process_task(tid)
    assert fake.submit_calls == total_submits
    assert all(r["status"] in states.ROW_TERMINAL for r in db.list_rows(tid))
