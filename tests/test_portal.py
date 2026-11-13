"""演示门户行为测试：延迟可见、重复拒绝、断线与管理接口。"""
import importlib
import time

import httpx
import pytest


@pytest.fixture
async def portal(tmp_path, monkeypatch):
    db_file = str(tmp_path / "portal.db")
    monkeypatch.setenv("PORTAL_DB", db_file)
    monkeypatch.setenv("PORTAL_VISIBILITY_DELAY_MS", "300")
    monkeypatch.setenv("PORTAL_SAVE_DELAY_MS", "0")
    monkeypatch.setenv("PORTAL_FAIL_MODE", "none")
    import app.portal.server as srv
    importlib.reload(srv)
    transport = httpx.ASGITransport(app=srv.portal)
    async with httpx.AsyncClient(transport=transport, base_url="http://p") as c:
        yield c, srv


async def _save(c, hole="ZK-1", top=0.0, bottom=5.0, litho="黏土"):
    return await c.post("/layers", data={
        "hole_no": hole, "top_depth": str(top),
        "bottom_depth": str(bottom), "lithology": litho,
    })


async def test_save_then_delayed_visibility(portal):
    c, _ = portal
    r = await _save(c)
    assert r.status_code == 200 and "save-success" in r.text
    # 立即查询不可见
    assert "query-empty" in (await c.get("/", params={"q": "ZK-1"})).text
    time.sleep(0.4)
    body = (await c.get("/", params={"q": "ZK-1"})).text
    assert "result-row" in body and "r-link" in body


async def test_duplicate_rejected(portal):
    c, _ = portal
    assert (await _save(c)).status_code == 200
    time.sleep(0.4)
    r = await _save(c, litho="其他岩性")
    assert r.status_code == 409 and "save-error" in r.text


async def test_invalid_depth_rejected(portal):
    c, _ = portal
    r = await _save(c, top=5.0, bottom=5.0)
    assert r.status_code == 400


async def test_disconnect_persists_but_breaks_body(portal):
    c, srv = portal
    await c.post("/admin/fail-mode", data={"mode": "disconnect"})
    # 真实 uvicorn 下为连接中断(ReadError)；ASGI 测试传输直接透传生成器异常
    with pytest.raises((httpx.ReadError, ConnectionResetError, httpx.RemoteProtocolError)):
        await _save(c, hole="ZK-9")
    # 数据已落库
    state = (await c.get("/admin/state")).json()
    assert any(x["hole_no"] == "ZK-9" for x in state["layers"])


async def test_admin_visibility_delay_runtime_change(portal):
    c, srv = portal
    await c.post("/admin/visibility-delay", data={"delay_ms": "1000"})
    assert srv.VIS_DELAY == pytest.approx(1.0)
    r = await _save(c, hole="ZK-7")
    assert r.status_code == 200
    assert "query-empty" in (await c.get("/", params={"q": "ZK-7"})).text
    time.sleep(1.1)
    assert "result-row" in (await c.get("/", params={"q": "ZK-7"})).text


async def test_conflict_mode_seeds_record(portal):
    c, srv = portal
    await c.post("/admin/reset")
    await c.post("/admin/fail-mode", data={"mode": "conflict"})
    state = (await c.get("/admin/state")).json()
    assert any(x["hole_no"] == "ZK-01" and x["lithology"] == "黏土"
               for x in state["layers"])
