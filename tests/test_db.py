from app.worker import states
from app.worker.db import DB

ROWS = [{"row_no": 1, "hole_no": "H", "top_depth": 0.0,
         "bottom_depth": 1.0, "lithology": "泥岩"}]


def test_task_and_row_persistence(tmp_path):
    path = str(tmp_path / "a.db")
    db = DB(path)
    task, created = db.create_or_get_task("t1", "http://p/", "sha", "f.csv", ROWS)
    assert created
    db.update_row("t1", 1, states.ROW_CONFIRMED, detail="ok",
                  record_url="http://p/layers/1")
    db.set_task_status("t1", states.TASK_COMPLETED, "done")
    db.close()

    # 重开连接（模拟重启），进度仍在
    db2 = DB(path)
    t = db2.get_task("t1")
    assert t["status"] == states.TASK_COMPLETED
    r = db2.get_row("t1", 1)
    assert r["status"] == states.ROW_CONFIRMED and r["record_url"].endswith("1")
    assert db2.row_counts("t1") == {"CONFIRMED": 1}
    # 重复任务返回既有记录
    t2, created2 = db2.create_or_get_task("t2", "http://p/", "sha", "f.csv", ROWS)
    assert not created2 and t2["id"] == "t1"
    db2.close()


def test_attempt_counter(tmp_path):
    db = DB(str(tmp_path / "b.db"))
    db.create_or_get_task("t1", "http://p/", "sha", "f.csv", ROWS)
    for _ in range(3):
        db.update_row("t1", 1, states.ROW_ERROR, detail="x", inc_attempt=True)
    assert db.get_row("t1", 1)["attempts"] == 3
    db.close()


def test_recover_stale_running(tmp_path):
    db = DB(str(tmp_path / "c.db"))
    db.create_or_get_task("t1", "http://p/", "sha", "f.csv", ROWS)
    db.set_task_status("t1", states.TASK_RUNNING)
    # 行进度（如未确认）必须保留
    db.update_row("t1", 1, states.ROW_SUBMITTED_UNCONFIRMED, detail="中断前已提交")
    n = db.recover_stale_running()
    assert n == 1
    t = db.get_task("t1")
    assert t["status"] == states.TASK_QUEUED and "重新排队" in (t["note"] or "")
    assert db.get_row("t1", 1)["status"] == states.ROW_SUBMITTED_UNCONFIRMED
    # 幂等：再次恢复无 RUNNING 可处理
    assert db.recover_stale_running() == 0
    db.close()
