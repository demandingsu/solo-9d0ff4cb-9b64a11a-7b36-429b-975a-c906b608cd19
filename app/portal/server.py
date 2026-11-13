"""无账号本地演示门户：仅提供网页查询与逐层填表。

特性：
- 同一页面上半部分查询表单、下半部分录入表单，纯 HTML 表单提交。
- 保存后延迟可见（PORTAL_VISIBILITY_DELAY_MS），演示“延迟入库”。
- 故障模式 PORTAL_FAIL_MODE：
    none       正常
    disconnect 保存已落库后断开连接（演示保存后断线）
    conflict   预置同层异岩性记录，制造查询冲突
    slow       保存响应极慢，配合 API 超时制造“已提交未确认”
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

DB_PATH = os.environ.get("PORTAL_DB", "/data/portal.db")
# 运行时可调（admin 接口），初始值来自环境变量
VIS_DELAY = int(os.environ.get("PORTAL_VISIBILITY_DELAY_MS", "2500")) / 1000.0
SAVE_DELAY = int(os.environ.get("PORTAL_SAVE_DELAY_MS", "300")) / 1000.0
SLOW_EXTRA = int(os.environ.get("SLOW_EXTRA_MS", "15000")) / 1000.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS layers (
    id TEXT PRIMARY KEY,
    hole_no TEXT NOT NULL,
    top_depth REAL NOT NULL,
    bottom_depth REAL NOT NULL,
    lithology TEXT NOT NULL,
    created_at REAL NOT NULL,
    visible_at REAL NOT NULL
);
"""


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def fail_mode() -> str:
    return os.environ.get("PORTAL_FAIL_MODE", "none").strip().lower()


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = _db()
    # conflict 模式预置：ZK-01 的 0-5m 已有“黏土”，CSV 若填“粉质黏土”即冲突
    if fail_mode() == "conflict":
        exists = conn.execute(
            "SELECT 1 FROM layers WHERE hole_no=? AND top_depth=0 AND bottom_depth=5",
            ("ZK-01",),
        ).fetchone()
        if not exists:
            now = time.time()
            conn.execute(
                "INSERT INTO layers VALUES (?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, "ZK-01", 0.0, 5.0, "黏土", now, now),
            )
            conn.commit()
    conn.close()
    yield


portal = FastAPI(title="本地钻孔资料门户（演示）", lifespan=lifespan)
app = portal  # uvicorn app.portal.server:app


PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>钻孔分层资料库（演示门户）</title>
<style>
body{font-family:sans-serif;max-width:980px;margin:24px auto;padding:0 16px;color:#222}
section{border:1px solid #ccc;border-radius:8px;padding:16px;margin-bottom:20px}
h2{margin-top:0} input{padding:6px;margin:4px 6px 4px 0}
table{border-collapse:collapse;width:100%;margin-top:10px}
th,td{border:1px solid #bbb;padding:6px 8px;text-align:left}
.msg-ok{background:#e6f6e6;border:1px solid #6abf69;padding:10px;border-radius:6px}
.msg-err{background:#fdeaea;border:1px solid #d66;padding:10px;border-radius:6px}
small{color:#666}
</style></head><body>
<h1>钻孔分层资料库 <small>无账号演示门户 · 故障模式：__MODE__</small></h1>

<section>
<h2>分层查询</h2>
<form method="get" action="/">
  <label>孔号 <input name="q" data-testid="query-hole" value="__Q__"></label>
  <button data-testid="query-submit" type="submit">查询</button>
</form>
__QUERY_BLOCK__
</section>

<section>
<h2>逐层填报</h2>
<small>保存后约 __DELAY__ 秒才可在查询中可见（模拟延迟入库）。</small>
<form method="post" action="/layers">
  <label>孔号 <input name="hole_no" data-testid="form-hole" required></label><br>
  <label>层顶深度 <input name="top_depth" data-testid="form-top" required></label>
  <label>层底深度 <input name="bottom_depth" data-testid="form-bottom" required></label><br>
  <label>岩性 <input name="lithology" data-testid="form-lithology" required></label><br>
  <button type="submit" data-testid="form-save">保存分层</button>
</form>
</section>
</body></html>"""


def _render(query: str | None) -> str:
    block = ""
    if query is not None:
        conn = _db()
        now = time.time()
        rows = conn.execute(
            "SELECT * FROM layers WHERE hole_no=? AND visible_at<=? ORDER BY top_depth",
            (query, now),
        ).fetchall()
        conn.close()
        if not rows:
            block = ('<div data-testid="query-empty">未查询到该孔号的可见记录'
                     "（延迟入库的记录可能尚未显示）</div>")
        else:
            trs = []
            for r in rows:
                trs.append(
                    '<tr data-testid="result-row">'
                    f'<td>{r["hole_no"]}</td>'
                    f'<td data-testid="r-top">{r["top_depth"]}</td>'
                    f'<td data-testid="r-bottom">{r["bottom_depth"]}</td>'
                    f'<td data-testid="r-lithology">{r["lithology"]}</td>'
                    f'<td><a data-testid="r-link" href="/layers/{r["id"]}">查看记录</a></td>'
                    "</tr>"
                )
            block = (
                '<div data-testid="query-result"><table><thead><tr>'
                "<th>孔号</th><th>层顶</th><th>层底</th><th>岩性</th><th>链接</th>"
                "</tr></thead><tbody>" + "".join(trs) + "</tbody></table></div>"
            )
    return (PAGE.replace("__QUERY_BLOCK__", block)
                .replace("__Q__", query or "")
                .replace("__MODE__", fail_mode())
                .replace("__DELAY__", str(VIS_DELAY)))


@portal.get("/", response_class=HTMLResponse)
async def index(q: str | None = None):
    return HTMLResponse(_render(q.strip() if q else None))


@portal.get("/layers/{layer_id}", response_class=HTMLResponse)
async def detail(layer_id: str):
    conn = _db()
    r = conn.execute("SELECT * FROM layers WHERE id=?", (layer_id,)).fetchone()
    conn.close()
    if r is None:
        return HTMLResponse("<h1>404</h1><p>记录不存在或尚不可见。</p>", status_code=404)
    now = time.time()
    if r["visible_at"] > now:
        remain = int(r["visible_at"] - now) + 1
        return HTMLResponse(
            f"<h1>记录延迟可见</h1><p>该记录已保存，约 {remain} 秒后可查询。</p>",
            status_code=404,
        )
    return HTMLResponse(
        "<h1>分层记录</h1><table border=1 cellpadding=8>"
        f"<tr><th>孔号</th><td>{r['hole_no']}</td></tr>"
        f"<tr><th>层顶深度</th><td>{r['top_depth']}</td></tr>"
        f"<tr><th>层底深度</th><td>{r['bottom_depth']}</td></tr>"
        f"<tr><th>岩性</th><td>{r['lithology']}</td></tr></table>"
        f'<p><a href="/?q={r["hole_no"]}">返回查询</a></p>'
    )


def _broken_stream():
    """先吐出部分 body，再中断生成器：模拟保存后连接重置（仅影响本请求）。"""
    yield "<h1>保存中".encode("utf-8")
    raise ConnectionResetError("模拟保存后网络中断（数据已落库）")


@portal.post("/layers")
async def create_layer(
    request: Request,
    hole_no: str = Form(...), top_depth: float = Form(...),
    bottom_depth: float = Form(...), lithology: str = Form(...),
):
    if bottom_depth <= top_depth:
        return HTMLResponse(
            '<div data-testid="save-error">层底深度必须大于层顶深度</div>',
            status_code=400,
        )

    conn = _db()
    dup = conn.execute(
        "SELECT id FROM layers WHERE hole_no=? AND top_depth=? AND bottom_depth=?",
        (hole_no, top_depth, bottom_depth),
    ).fetchone()
    if dup:
        conn.close()
        return HTMLResponse(
            '<div data-testid="save-error">该分层（孔号/层顶/层底）已存在，拒绝重复保存</div>',
            status_code=409,
        )

    layer_id = uuid.uuid4().hex
    now = time.time()
    conn.execute(
        "INSERT INTO layers VALUES (?,?,?,?,?,?,?)",
        (layer_id, hole_no, top_depth, bottom_depth, lithology,
         now, now + VIS_DELAY),
    )
    conn.commit()
    conn.close()

    mode = fail_mode()
    if mode == "slow":
        time.sleep(SAVE_DELAY + SLOW_EXTRA)  # 触发 API 端提交超时
    else:
        time.sleep(SAVE_DELAY)

    if mode == "disconnect":
        # 数据已落库，但响应中断：API 只能标记“未确认”，靠续查发现
        return StreamingResponse(_broken_stream(), status_code=200,
                                 media_type="text/html")

    return HTMLResponse(
        '<div data-testid="save-success" class="msg-ok">保存成功，分层已入库'
        f"（{VIS_DELAY}s 后可查询）</div>"
        f'<a data-testid="record-link" href="/layers/{layer_id}">门户记录链接</a>'
    )


# ---------------- 演示管理接口（非补录通道，仅用于切换故障场景/重置） ----------------
@portal.post("/admin/fail-mode")
async def set_fail_mode(mode: str = Form(...)):
    mode = mode.strip().lower()
    if mode not in ("none", "disconnect", "conflict", "slow"):
        return JSONResponse({"error": "unknown mode"}, status_code=400)
    os.environ["PORTAL_FAIL_MODE"] = mode
    if mode == "conflict":
        conn = _db()
        exists = conn.execute(
            "SELECT 1 FROM layers WHERE hole_no=? AND top_depth=0 AND bottom_depth=5",
            ("ZK-01",),
        ).fetchone()
        if not exists:
            now = time.time()
            conn.execute(
                "INSERT INTO layers VALUES (?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, "ZK-01", 0.0, 5.0, "黏土", now, now),
            )
            conn.commit()
        conn.close()
    return {"fail_mode": mode}


@portal.post("/admin/reset")
async def reset():
    conn = _db()
    conn.execute("DELETE FROM layers")
    conn.commit()
    conn.close()
    return {"reset": True}


@portal.post("/admin/visibility-delay")
async def set_visibility_delay(delay_ms: int = Form(...)):
    """运行时调节保存→可查询的延迟，用于确定性演示延迟入库停行/续查。"""
    global VIS_DELAY
    VIS_DELAY = max(0, delay_ms) / 1000.0
    return {"visibility_delay_s": VIS_DELAY}


@portal.get("/admin/state")
async def state():
    conn = _db()
    rows = [dict(r) for r in conn.execute(
        "SELECT id,hole_no,top_depth,bottom_depth,lithology,visible_at FROM layers"
    ).fetchall()]
    conn.close()
    return {"fail_mode": fail_mode(), "count": len(rows), "layers": rows,
            "now": time.time()}
