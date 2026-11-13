"""API 集成测试：HTTP 上传 → 引擎（假自动化）→ 逐行状态/续查/去重。"""
import asyncio

import httpx
import pytest

from app import main as web
from app.worker import states


class FakeAutomation:
    def __init__(self):
        self.store = []
        self.submits = 0

    async def query_layer(self, portal, hole_no, top, bottom):
        from app.worker.automation import QueryResult
        # 门户已入库记录（含本任务此前填报的）均可被查询到
        matched = [dict(r) for r in self.store
                   if r["hole_no"] == hole_no
                   and abs(r["top_depth"] - top) < 1e-9
                   and abs(r["bottom_depth"] - bottom) < 1e-9]
        return QueryResult(found=len(matched), records=matched)

    async def submit_layer(self, portal, hole_no, top, bottom, lithology):
        from app.worker.automation import SubmitResult
        self.submits += 1
        url = f"/layers/f{self.submits}"
        self.store.append({"hole_no": hole_no, "top_depth": float(top),
                           "bottom_depth": float(bottom),
                           "lithology": lithology, "url": url})
        return SubmitResult(confirmed=True, record_url=url)

CSV = ("孔号,层顶深度,层底深度,岩性\n"
       "ZK-01,0,5,粉质黏土\nZK-01,5,12,细砂\n").encode()


@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web.config, "VISIBILITY_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(web.config, "VISIBILITY_WAIT_MS", 0)
    fake = FakeAutomation()
    # 重建使用临时 DB 的引擎（模块导入时已按默认路径建库，这里整体替换）
    from app.worker.db import DB
    from app.worker.engine import Engine
    test_db = DB(str(tmp_path / "api.db"))
    web.db = test_db
    web.automation = fake
    web.engine = Engine(test_db, fake)

    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, fake
    test_db.close()


async def test_ingest_and_progress_and_links(client):
    c, fake = client
    r = await c.post(
        "/api/ingest",
        data={"portal_url": "http://portal/"},
        files={"file": ("layers.csv", CSV, "text/csv")},
    )
    assert r.status_code == 202
    task = r.json()
    assert task["total_rows"] == 2
    # 后台处理很快完成，轮询确认
    for _ in range(50):
        t = (await c.get(f"/api/tasks/{task['id']}")).json()
        if t["status"] == "COMPLETED":
            task = t
            break
        await asyncio.sleep(0.1)
    assert task["status"] == "COMPLETED"
    rows = (await c.get(f"/api/tasks/{task['id']}/rows")).json()
    assert all(r["status"] == "CONFIRMED" for r in rows)
    assert all(r["record_url"] == f"http://portal/layers/f{i+1}" for i, r in enumerate(rows))
    assert all(r["status_text"] for r in rows)


async def test_duplicate_requests_same_task_and_skips(client):
    c, fake = client
    async def post():
        return await c.post(
            "/api/ingest",
            data={"portal_url": "http://portal/"},
            files={"file": ("layers.csv", CSV, "text/csv")},
        )
    r1 = await post()
    assert r1.status_code == 202
    for _ in range(50):
        t = (await c.get(f"/api/tasks/{r1.json()['id']}")).json()
        if t["status"] == "COMPLETED":
            break
        await asyncio.sleep(0.1)

    r2, r3 = await asyncio.gather(post(), post())
    ids = {r1.json()["id"], r2.json()["id"], r3.json()["id"]}
    assert len(ids) == 1
    t = (await c.get(f"/api/tasks/{ids.pop()}")).json()
    # 已确认行保持终态、绝不重填；门户没有新增提交
    assert t["row_counts"].get("CONFIRMED") == 2
    assert t["status"] == "COMPLETED"
    assert fake.submits == 2


async def test_bad_portal_url_rejected(client):
    c, _ = client
    r = await c.post(
        "/api/ingest",
        data={"portal_url": "ftp://x"},
        files={"file": ("a.csv", CSV, "text/csv")},
    )
    assert r.status_code == 400


async def test_bad_csv_rejected(client):
    c, _ = client
    r = await c.post(
        "/api/ingest",
        data={"portal_url": "http://portal/"},
        files={"file": ("a.csv", b"a,b,c\n1,2,3\n", "text/csv")},
    )
    assert r.status_code == 400


async def test_resume_endpoint_on_completed(client):
    c, _ = client
    r = await c.post(
        "/api/ingest",
        data={"portal_url": "http://portal/"},
        files={"file": ("layers.csv", CSV, "text/csv")},
    )
    tid = r.json()["id"]
    for _ in range(50):
        t = (await c.get(f"/api/tasks/{tid}")).json()
        if t["status"] == "COMPLETED":
            break
        await asyncio.sleep(0.1)
    r = await c.post(f"/api/tasks/{tid}/resume")
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED"
