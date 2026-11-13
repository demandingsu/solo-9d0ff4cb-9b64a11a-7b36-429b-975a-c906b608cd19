"""补录 API：接收门户地址与钻孔分层 CSV，驱动网页自动化补录。"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from . import config
from .worker import states
from .worker.automation import BrowserAutomation
from .worker.csvparse import CSVValidationError, file_sha256, parse_csv
from .worker.db import DB
from .worker.engine import Engine


class RowOut(BaseModel):
    row_no: int
    hole_no: str
    top_depth: float
    bottom_depth: float
    lithology: str
    status: str
    status_text: str
    detail: str | None
    record_url: str | None
    attempts: int
    updated_at: float


class TaskOut(BaseModel):
    id: str
    portal_url: str
    filename: str
    file_sha256: str
    status: str
    status_text: str
    note: str | None
    created_at: float
    updated_at: float
    row_counts: dict[str, int]
    total_rows: int
    rows: list[RowOut] | None = None


def _resolve_db_path() -> str:
    path = config.DB_PATH
    parent = os.path.dirname(os.path.abspath(path))
    if os.path.isdir(parent) and os.access(parent, os.W_OK):
        return path
    # 本地无 /data 写权限时回退到工作目录（容器卷挂载不受影响）
    fallback = os.path.join(os.path.dirname(os.path.dirname(__file__)), "worker.db")
    return fallback


db = DB(_resolve_db_path())
automation = BrowserAutomation()
engine = Engine(db, automation)
processing: set[str] = set()
proc_lock = asyncio.Lock()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # 崩溃恢复：残留 RUNNING 任务重新排队；UNCONFIRMED 行仍只查不填
    recovered = db.recover_stale_running()
    if recovered:
        print(f"[startup] 恢复 {recovered} 个中断任务为排队状态")
    worker = asyncio.create_task(engine.run_forever())
    try:
        yield
    finally:
        worker.cancel()
        with contextlib.suppress(Exception):
            await worker
        await automation.close()
        db.close()


app = FastAPI(
    title="钻孔分层网页补录 API",
    version="1.0.0",
    description="只经网页查询与逐层填表，将钻孔分层 CSV 补录到资料库。",
    lifespan=lifespan,
)


def _row_out(r: dict[str, Any]) -> RowOut:
    return RowOut(
        **{k: r[k] for k in (
            "row_no", "hole_no", "top_depth", "bottom_depth", "lithology",
            "status", "detail", "record_url", "attempts", "updated_at")},
        status_text=states.ROW_STATUS_TEXT.get(r["status"], r["status"]),
    )


def _task_out(task_id: str, include_rows: bool = False) -> TaskOut:
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    rows = db.list_rows(task_id)
    counts = db.row_counts(task_id)
    data = dict(task)
    data["status_text"] = states.TASK_STATUS_TEXT.get(task["status"], task["status"])
    data["row_counts"] = counts
    data["total_rows"] = len(rows)
    if include_rows:
        data["rows"] = [_row_out(r) for r in rows]
    return TaskOut(**data)


async def _spawn_processing(task_id: str) -> None:
    """同一任务的重复/并发请求只接续同一处理，不并发重入。"""
    async with proc_lock:
        if task_id in processing:
            return
        processing.add(task_id)

    async def runner():
        try:
            await engine.process_task(task_id)
        finally:
            async with proc_lock:
                processing.discard(task_id)

    asyncio.create_task(runner())
    engine.notify()


async def _wait_first_round(task_id: str, timeout_s: float = 20.0) -> None:
    """有界等待首轮处理结束，让首次响应携带尽量新的状态（不阻塞太久）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        async with proc_lock:
            busy = task_id in processing
        if not busy:
            return
        await asyncio.sleep(0.2)


@app.get("/health")
async def health():
    return {"status": "ok", "time": time.time()}


@app.post("/api/ingest", response_model=TaskOut, status_code=202)
async def ingest(
    portal_url: str = Form(..., description="门户地址（查询/填表页 URL）"),
    file: UploadFile = File(..., description="含 孔号,层顶深度,层底深度,岩性 的 CSV"),
):
    portal_url = portal_url.strip()
    if not (portal_url.startswith("http://") or portal_url.startswith("https://")):
        raise HTTPException(400, "portal_url 必须是 http(s) 地址")

    raw = await file.read()
    if not raw:
        raise HTTPException(400, "上传文件为空")
    try:
        rows = parse_csv(raw)
    except CSVValidationError as exc:
        raise HTTPException(400, f"CSV 校验失败：{exc}")

    digest = file_sha256(raw)
    task, created = db.create_or_get_task(
        uuid.uuid4().hex, portal_url, digest, file.filename or "upload.csv", rows)

    # 无论新建还是重复/并发请求，都接续同一个任务并驱动一次推进
    await _spawn_processing(task["id"])
    # 有界等待首轮结束，首次请求通常可直接看到进展（长任务到时即返回当前快照）
    await _wait_first_round(task["id"])
    return _task_out(task["id"], include_rows=True)


@app.get("/api/tasks", response_model=list[TaskOut])
async def list_tasks():
    return [_task_out(t["id"]) for t in db.list_tasks()]


@app.get("/api/tasks/{task_id}", response_model=TaskOut)
async def get_task(task_id: str, rows: bool = True):
    return _task_out(task_id, include_rows=rows)


@app.get("/api/tasks/{task_id}/rows", response_model=list[RowOut])
async def get_rows(task_id: str):
    if db.get_task(task_id) is None:
        raise HTTPException(404, "任务不存在")
    return [_row_out(r) for r in db.list_rows(task_id)]


@app.post("/api/tasks/{task_id}/resume", response_model=TaskOut)
async def resume(task_id: str):
    """续查/恢复：对停行（冲突、未确认、错误）重新只查或重试，并继续后续行。"""
    task = db.get_task(task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task["status"] == states.TASK_COMPLETED:
        return _task_out(task_id, include_rows=True)

    # 不持有任务锁：处理协程内部会自行加锁，这里只负责触发并等待本轮结束
    await _spawn_processing(task_id)
    await _wait_first_round(task_id, timeout_s=max(60, config.NAV_TIMEOUT_MS / 1000 + 30))
    return _task_out(task_id, include_rows=True)
