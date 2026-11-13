"""补录状态机与后台工作循环。

逐行规则（按 孔号+层顶+层底 识别一层）：
1. 提交前先查：
   - 唯一记录且岩性相同 → 跳过（SKIPPED），记录门户链接
   - 不存在 → 才允许填表；保存确认 → CONFIRMED
   - 多条 / 唯一但岩性不符 → CONFLICT，停在该行，后续行不处理
2. 已开始提交却未确认（超时/断线/重启）→ UNCONFIRMED：
   恢复时只允许查询，查不到也不得自动重提，继续停行等待人工核实。
3. CONFLICT / UNCONFIRMED 行可经 /resume 续查：
   查询变为唯一且岩性一致即解除并继续后续行；否则继续停行。
4. 所有动作前先查，已确认的行绝不重复填报。
"""
from __future__ import annotations

import asyncio
from urllib.parse import urljoin

from .. import config
from .automation import AutomationError, BrowserAutomation
from .db import DB
from .states import (
    ROW_CONFIRMED, ROW_CONFLICT, ROW_ERROR, ROW_PENDING, ROW_SKIPPED,
    ROW_SUBMITTED_UNCONFIRMED, ROW_TERMINAL, TASK_COMPLETED, TASK_FAILED,
    TASK_RUNNING, TASK_WAITING,
)


class Engine:
    def __init__(self, db: DB, automation: BrowserAutomation | None = None):
        self.db = db
        self.automation = automation or BrowserAutomation()
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._wake = asyncio.Event()

    async def lock_for(self, task_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lk = self._locks.get(task_id)
            if lk is None:
                lk = asyncio.Lock()
                self._locks[task_id] = lk
            return lk

    def notify(self) -> None:
        self._wake.set()

    # ---------------- 主处理 ----------------
    async def process_task(self, task_id: str) -> None:
        """对任务做一次完整推进，遇到停行/未确认/完成即返回。"""
        async with await self.lock_for(task_id):
            task = self.db.get_task(task_id)
            if task is None or task["status"] == TASK_COMPLETED:
                return
            self.db.set_task_status(task_id, TASK_RUNNING)
            portal = task["portal_url"]

            # 每轮从 DB 重新读取：行状态可能在本轮内由 PENDING 变为 UNCONFIRMED，
            # 必须以最新状态为准，避免旧内存对象触发禁止的自动重提
            snapshot = self.db.list_rows(task_id)
            for snapshot_row in snapshot:
                row = self.db.get_row(task_id, snapshot_row["row_no"])
                state = await self._process_row(portal, task_id, row)
                if state == "block":
                    self.db.set_task_status(
                        task_id, TASK_WAITING,
                        f"停在第 {row['row_no']} 行（{row['status']}），待续查/核实",
                    )
                    return
                if state == "fail":
                    self.db.set_task_status(
                        task_id, TASK_FAILED,
                        f"第 {row['row_no']} 行连续运行时错误，等待 resume 重试",
                    )
                    return

            self.db.set_task_status(task_id, TASK_COMPLETED, "全部行已确认/跳过")

    async def _process_row(self, portal: str, task_id: str, row: dict) -> str:
        """返回 'ok' | 'block' | 'fail'。"""
        status = row["status"]
        if status in ROW_TERMINAL:
            return "ok"

        # UNCONFIRMED：只查不填；查不到也不得自动重提
        if status == ROW_SUBMITTED_UNCONFIRMED:
            return await self._recheck_only(portal, task_id, row)

        if status == ROW_CONFLICT:
            return await self._recheck_only(portal, task_id, row)

        # PENDING / ERROR：先查后填（ERROR 为点击保存前的故障，未实际提交，可重试）
        return await self._pending_flow(portal, task_id, row)

    async def _recheck_only(self, portal: str, task_id: str, row: dict) -> str:
        """续查路径：只做查询。命中且一致则放行，否则继续停行。"""
        try:
            qr = await self.automation.query_layer(
                portal, row["hole_no"], row["top_depth"], row["bottom_depth"])
        except AutomationError as exc:
            return self._runtime_error(task_id, row, f"续查失败：{exc}")

        if qr.found == 1 and qr.records[0]["lithology"] == row["lithology"]:
            rec = qr.records[0]
            self.db.update_row(
                task_id, row["row_no"], ROW_CONFIRMED,
                detail="续查确认：门户已存在唯一一致记录（未重填）",
                record_url=_abs(portal, rec.get("url")),
            )
            return "ok"

        if qr.found == 0:
            self.db.update_row(
                task_id, row["row_no"], ROW_SUBMITTED_UNCONFIRMED,
                detail="续查未查到记录；该行已开始过提交，禁止自动重提，待人工核实",
            )
        elif qr.found > 1:
            self.db.update_row(
                task_id, row["row_no"], ROW_CONFLICT,
                detail=f"续查发现 {qr.found} 条同层记录，待人工核实",
            )
        else:  # 唯一但岩性不符
            rec = qr.records[0]
            self.db.update_row(
                task_id, row["row_no"], ROW_CONFLICT,
                detail=f"续查岩性不符：门户={rec['lithology']!r}，CSV={row['lithology']!r}",
                record_url=_abs(portal, rec.get("url")),
            )
        return "block"

    async def _pending_flow(self, portal: str, task_id: str, row: dict) -> str:
        """PENDING/ERROR 行：先查，再按规则跳过/填报/停行。"""
        try:
            qr = await self.automation.query_layer(
                portal, row["hole_no"], row["top_depth"], row["bottom_depth"])
        except AutomationError as exc:
            return self._runtime_error(task_id, row, f"查询失败：{exc}")

        if qr.found == 1:
            rec = qr.records[0]
            if rec["lithology"] == row["lithology"]:
                self.db.update_row(
                    task_id, row["row_no"], ROW_SKIPPED,
                    detail="查询命中唯一记录且岩性相同，跳过填报",
                    record_url=_abs(portal, rec.get("url")),
                )
                return "ok"
            self.db.update_row(
                task_id, row["row_no"], ROW_CONFLICT,
                detail=f"岩性不符：门户={rec['lithology']!r}，CSV={row['lithology']!r}",
                record_url=_abs(portal, rec.get("url")),
            )
            return "block"

        if qr.found > 1:
            self.db.update_row(
                task_id, row["row_no"], ROW_CONFLICT,
                detail=f"门户存在 {qr.found} 条同层（同孔号/层顶/层底）记录，待人工核实",
            )
            return "block"

        # found == 0：不存在才填报
        # 提交期间保持 PENDING（易失过程不落“已提交”状态）：
        # 点击保存前崩溃 → 重启后仍是 PENDING，可安全重试；
        # 点击后无确认 → 才落 UNCONFIRMED，此后只查不填。
        try:
            sr = await self.automation.submit_layer(
                portal, row["hole_no"], row["top_depth"],
                row["bottom_depth"], row["lithology"],
            )
        except AutomationError as exc:
            # 提交动作在点击保存前失败：未实际提交，保持 PENDING 走重试计数
            return self._runtime_error(task_id, row, f"填表失败：{exc}",
                                       reset_to_pending=True)

        if not sr.confirmed:
            # 保存后断线/超时：可能已入库，只能停行等续查，绝不重提
            self.db.update_row(
                task_id, row["row_no"], ROW_SUBMITTED_UNCONFIRMED,
                detail=sr.error or "保存未确认",
                record_url=_abs(portal, sr.record_url) if sr.record_url else None,
            )
            return "block"

        # 保存成功页确认；再按延迟可见策略做有限次查询复核
        confirmed_url = _abs(portal, sr.record_url)
        ok = await self._post_save_verify(portal, row)
        if ok:
            self.db.update_row(
                task_id, row["row_no"], ROW_CONFIRMED,
                detail="网页填报成功并查询复核一致", record_url=confirmed_url,
            )
        else:
            # 保存页已确认但暂未在查询中可见（延迟入库）：按未确认停行，仅可续查
            self.db.update_row(
                task_id, row["row_no"], ROW_SUBMITTED_UNCONFIRMED,
                detail="保存成功页已确认，但查询暂不可见（延迟入库），仅可续查",
                record_url=confirmed_url,
            )
            return "block"
        return "ok"

    async def _post_save_verify(self, portal: str, row: dict) -> bool:
        for attempt in range(config.VISIBILITY_MAX_ATTEMPTS):
            await asyncio.sleep(config.VISIBILITY_WAIT_MS / 1000.0)
            try:
                qr = await self.automation.query_layer(
                    portal, row["hole_no"], row["top_depth"], row["bottom_depth"])
            except AutomationError:
                continue
            if qr.found == 1 and qr.records[0]["lithology"] == row["lithology"]:
                return True
        return False

    def _runtime_error(self, task_id: str, row: dict, detail: str,
                       reset_to_pending: bool = False) -> str:
        attempts = row["attempts"] + 1
        if reset_to_pending:
            # 点击前故障：未提交，回到 PENDING，累计尝试次数
            self.db.update_row(task_id, row["row_no"], ROW_PENDING,
                               detail=f"{detail}（第 {attempts} 次，未提交可重试）",
                               inc_attempt=True)
        else:
            self.db.update_row(task_id, row["row_no"], ROW_ERROR,
                               detail=f"{detail}（第 {attempts} 次）",
                               inc_attempt=True)
        if attempts >= config.MAX_RUNTIME_ERRORS:
            return "fail"
        return "block"

    # ---------------- 后台循环 ----------------
    async def run_forever(self) -> None:
        while True:
            did = False
            for task in self.db.list_tasks(limit=500):
                if task["status"] in (TASK_RUNNING, TASK_WAITING, TASK_FAILED):
                    continue  # 停行/失败任务只由 resume 或新文件请求驱动
                if task["status"] == TASK_COMPLETED:
                    continue
                did = True
                try:
                    await self.process_task(task["id"])
                except Exception as exc:  # 防御：单任务异常不拖垮循环
                    self.db.set_task_status(task["id"], TASK_FAILED, f"工作循环异常：{exc}")
            if not did:
                try:
                    await asyncio.wait_for(self._wake.wait(),
                                           timeout=config.WORKER_IDLE_SLEEP_S)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()


def _abs(portal: str, url: str | None) -> str | None:
    if not url:
        return None
    return urljoin(portal, url)
