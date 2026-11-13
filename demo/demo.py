"""端到端演示脚本（docker compose --profile demo run demo）。

场景：
1. 正常补录 + 延迟入库：保存成功但查询延迟可见 → 未确认停行 → 续查确认
2. 保存后断线：门户已落库但响应中断 → 只能续查，禁止自动重提
3. 冲突：门户存在同层异岩性记录 → 停行待核实
4. 重复/并发请求：同门户同文件只接续同一任务，已确认行不重填
5. 重启恢复：任务进度持久化，重启 API 后仍可查询并续查
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "90"))
POLL_INTERVAL = 1.0


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)


async def wait_terminal(client: httpx.AsyncClient, task_id: str,
                        expect_waiting: bool = True) -> dict:
    while True:
        t = (await client.get(f"/api/tasks/{task_id}")).json()
        if t["status"] in ("COMPLETED", "FAILED") or (
            expect_waiting and t["status"] == "WAITING_VERIFICATION"
        ):
            return t
        await asyncio.sleep(POLL_INTERVAL)


async def resume_and_wait(client: httpx.AsyncClient, task_id: str) -> dict:
    """触发续查；若本轮仍停行则直接返回，否则轮询到终态。"""
    t = (await client.post(f"/api/tasks/{task_id}/resume")).json()
    if t["status"] == "WAITING_VERIFICATION":
        return t
    return await wait_terminal(client, task_id)


def show(task: dict) -> None:
    print(f"任务 {task['id'][:8]} 状态={task['status']}（{task['status_text']}）"
          f" 计数={task['row_counts']}")
    for r in task.get("rows", []):
        url = f" 链接={r['record_url']}" if r.get("record_url") else ""
        print(f"  行{r['row_no']} {r['hole_no']} {r['top_depth']}-{r['bottom_depth']}"
              f" {r['lithology']}: {r['status']}（{r['status_text']}）"
              f"{url}\n      ↳ {r['detail']}")


async def set_mode(portal: str, mode: str) -> None:
    async with httpx.AsyncClient(base_url=portal, timeout=10) as c:
        r = await c.post("/admin/fail-mode", data={"mode": mode})
        r.raise_for_status()
        print(f"门户故障模式 → {mode}")


async def reset_portal(portal: str) -> None:
    async with httpx.AsyncClient(base_url=portal, timeout=10) as c:
        await c.post("/admin/reset")
        await c.post("/admin/fail-mode", data={"mode": "none"})
        await c.post("/admin/visibility-delay", data={"delay_ms": "2500"})
    print("门户数据已重置：故障模式 → none，可见延迟 → 2.5s")


async def set_visibility_delay(portal: str, delay_ms: int) -> None:
    async with httpx.AsyncClient(base_url=portal, timeout=10) as c:
        r = await c.post("/admin/visibility-delay", data={"delay_ms": str(delay_ms)})
        r.raise_for_status()
    print(f"门户保存→可查询延迟 → {delay_ms}ms")


async def ingest(client: httpx.AsyncClient, portal_url: str,
                 csv_path: Path) -> dict:
    with csv_path.open("rb") as f:
        r = await client.post(
            "/api/ingest",
            data={"portal_url": portal_url},
            files={"file": (csv_path.name, f, "text/csv")},
        )
    r.raise_for_status()
    return r.json()


async def seed_rows_via_portal(portal: str, csv_path: Path) -> None:
    """通过门户网页表单预置数据（非补录通道，仅为制造“已存在”场景）。"""
    import csv as _csv
    with csv_path.open("r", encoding="utf-8-sig") as f:
        rows = list(_csv.DictReader(f))
    async with httpx.AsyncClient(base_url=portal, timeout=30) as pc:
        for r in rows:
            resp = await pc.post("/layers", data={
                "hole_no": r.get("孔号") or r["hole_no"],
                "top_depth": r.get("层顶深度") or r["top_depth"],
                "bottom_depth": r.get("层底深度") or r["bottom_depth"],
                "lithology": r.get("岩性") or r["lithology"],
            }, follow_redirects=True)
            resp.raise_for_status()
    # 等待预置记录度过门户延迟可见窗口，确保补录侧“先查”能命中
    await asyncio.sleep(3.5)
    print(f"已通过门户表单预置 {len(rows)} 条记录（{csv_path.name}）")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://api:8080")
    ap.add_argument("--portal", default="http://portal:8000")
    ap.add_argument("--csv", default=str(HERE / "layers.csv"))
    args = ap.parse_args()

    async with httpx.AsyncClient(base_url=args.api, timeout=HTTP_TIMEOUT) as client:
        # 等待两个服务就绪
        for name, base in (("API", args.api), ("门户", args.portal)):
            async with httpx.AsyncClient(timeout=5) as probe:
                for _ in range(60):
                    try:
                        await probe.get(base + ("/health" if name == "API" else "/"))
                        break
                    except httpx.HTTPError:
                        await asyncio.sleep(1)
                else:
                    print(f"{name} 未就绪", file=sys.stderr)
                    return 1
        print(f"API={args.api} 门户={args.portal}")
        csv_path = Path(args.csv)

        # ---------- 场景 1：正常补录 + 延迟入库 ----------
        banner("场景 1：延迟入库（保存成功但查询暂不可见 → 停行未确认 → 续查）")
        await reset_portal(args.portal)
        # 可见延迟 8s 超过补录端复核窗口（约 4.5s），首行必然停在“未确认”
        await set_visibility_delay(args.portal, 8000)
        t = await ingest(client, args.portal + "/", csv_path)
        t = await wait_terminal(client, t["id"])
        show(t)
        assert t["status"] == "WAITING_VERIFICATION"
        assert t["rows"][0]["status"] == "UNCONFIRMED"
        print("✓ 保存页已确认但查询暂不可见，按规则停行且不重提")
        # 恢复短延迟并等待已保存记录度过可见窗口，再续查
        await set_visibility_delay(args.portal, 2500)
        await asyncio.sleep(8.5)
        t = await resume_and_wait(client, t["id"])
        show(t)
        assert t["status"] == "COMPLETED", "场景 1 应全部完成"
        assert all(r["status"] in ("CONFIRMED", "SKIPPED") for r in t["rows"])

        # ---------- 场景 2：重复/并发请求接续同一任务 ----------
        banner("场景 2：同门户同文件重复与并发请求 → 接续同一任务，不重填")
        # 2a. 同文件并发三次：只返回同一任务，已确认行保持终态、零重复填报
        results = await asyncio.gather(*[
            ingest(client, args.portal + "/", csv_path) for _ in range(3)
        ])
        ids = {r["id"] for r in results}
        assert len(ids) == 1, f"应只有一个任务，实际：{ids}"
        t2 = (await client.get(f"/api/tasks/{ids.pop()}")).json()
        show(t2)
        assert t2["status"] == "COMPLETED"
        assert t2["row_counts"].get("CONFIRMED", 0) == t2["total_rows"]
        print("✓ 三个并发请求返回同一任务，已确认行未重复填报")

        # 2b. 全新文件但门户已有相同数据 → 先查后填，全部 SKIPPED
        en_csv = HERE / "layers_en_header.csv"
        await seed_rows_via_portal(args.portal, en_csv)
        t3 = await ingest(client, args.portal + "/", en_csv)
        t3 = await wait_terminal(client, t3["id"])
        show(t3)
        assert t3["row_counts"].get("SKIPPED", 0) == t3["total_rows"]
        print("✓ 门户已存在的唯一一致记录全部命中 SKIPPED，未产生任何新填报")

        # ---------- 场景 3：保存后断线 ----------
        banner("场景 3：保存后断线（数据可能已入库 → 未确认 → 仅续查）")
        await reset_portal(args.portal)
        await set_mode(args.portal, "disconnect")
        t = await ingest(client, args.portal + "/", csv_path)
        t = await wait_terminal(client, t["id"])
        show(t)
        unconf = [r for r in t["rows"] if r["status"] == "UNCONFIRMED"]
        assert unconf, "断线后首行应为 UNCONFIRMED"
        print("✓ 已开始提交但未确认，系统未自动重提，停行等待")
        await set_mode(args.portal, "none")
        # 等待断线保存的记录度过门户延迟可见窗口
        await asyncio.sleep(3)
        t = await resume_and_wait(client, t["id"])
        show(t)
        assert t["status"] == "COMPLETED", "续查后应全部确认完成"
        assert all(r["status"] in ("CONFIRMED", "SKIPPED") for r in t["rows"])
        print("✓ 续查发现门户已存在记录，确认后继续后续行，全程无重复填报")

        # ---------- 场景 4：内容冲突停行 ----------
        banner("场景 4：岩性冲突 → 停在该行待核实，后续行不动")
        await reset_portal(args.portal)
        await set_mode(args.portal, "conflict")
        t = await ingest(client, args.portal + "/", csv_path)
        t = await wait_terminal(client, t["id"])
        show(t)
        first = t["rows"][0]
        assert first["status"] == "CONFLICT" and first["row_no"] == 1
        assert all(r["status"] == "PENDING" for r in t["rows"][1:]), "后续行不得处理"
        print("✓ 冲突停行；直接续查不会放行（岩性仍不符）")
        again = (await client.post(f"/api/tasks/{t['id']}/resume")).json()
        assert again["rows"][0]["status"] == "CONFLICT"
        print("✓ 续查仍冲突，保持停行（需人工在门户核实后再续查）")
        # 人工核实并清空门户后恢复正常模式，续查将“查无记录→填报”并继续后续行
        async with httpx.AsyncClient(base_url=args.portal, timeout=10) as pc:
            await pc.post("/admin/reset")
            await pc.post("/admin/fail-mode", data={"mode": "none"})
        t = await resume_and_wait(client, t["id"])
        show(t)
        assert t["status"] == "COMPLETED"

        # ---------- 场景 5：进度查询与持久化提示 ----------
        banner("场景 5：进度查询（重启后同样有效，数据在 SQLite 卷中）")
        tasks = (await client.get("/api/tasks")).json()
        print(f"门户 {args.portal} 共 {len(tasks)} 个任务（按 门户+文件指纹 去重）")
        for item in tasks:
            print(f"  {item['id'][:8]} {item['filename']} {item['status']} "
                  f"{item['row_counts']}")
        print("\n重启验证命令：")
        print("  docker compose restart api   # 然后 GET /api/tasks/{id} 进度仍在")
        print("  docker compose run --rm api python -c \"import sqlite3;"
              "print(sqlite3.connect('/data/worker.db').execute("
              "'select status,count(*) from task_rows group by status').fetchall())\"")
        print("\n全部演示场景通过 ✅")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
