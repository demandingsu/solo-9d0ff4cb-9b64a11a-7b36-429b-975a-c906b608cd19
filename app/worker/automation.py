"""Playwright 网页自动化：只通过浏览器进行“查询”和“逐层填表”。

不直接调用门户后端接口，所有动作均为真实页面操作：
- 打开查询页 → 填孔号 → 提交表单 → 解析结果列表
- 打开录入页 → 填四个字段 → 点保存 → 解析成功页
保存可能延迟可见，也可能在保存后断线；本层只如实回报页面结果，
“已开始提交却未确认”的语义由引擎层保证不自动重提。
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from .. import config


class AutomationError(RuntimeError):
    """浏览器/页面层面的运行时故障（超时、断线、元素缺失等）。"""


@dataclass
class QueryResult:
    found: int = 0
    records: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def unique(self) -> bool:
        return self.found == 1


@dataclass
class SubmitResult:
    confirmed: bool                # 保存成功页是否明确返回
    record_url: str | None = None
    error: str | None = None


class BrowserAutomation:
    def __init__(self, headless: bool | None = None,
                 nav_timeout_ms: int | None = None,
                 submit_timeout_ms: int | None = None):
        self._headless = config.HEADLESS if headless is None else headless
        self._nav_timeout = config.NAV_TIMEOUT_MS if nav_timeout_ms is None else nav_timeout_ms
        self._submit_timeout = (
            config.SUBMIT_TIMEOUT_MS if submit_timeout_ms is None else submit_timeout_ms
        )
        self._lock = asyncio.Lock()
        self._pw = None
        self._browser = None
        self._context = None

    async def _ensure(self):
        if self._context is not None:
            return
        # 延迟导入：无浏览器环境（单元测试）不依赖 Playwright 安装
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self._headless)
        self._context = await self._browser.new_context()
        self._context.set_default_navigation_timeout(self._nav_timeout)
        self._context.set_default_timeout(self._nav_timeout)

    async def close(self) -> None:
        async with self._lock:
            for closer in (
                lambda: self._context and self._context.close(),
                lambda: self._browser and self._browser.close(),
                lambda: self._pw and self._pw.stop(),
            ):
                with contextlib.suppress(Exception):
                    await closer()
            self._context = None
            self._browser = None
            self._pw = None

    @contextlib.asynccontextmanager
    async def _page(self):
        async with self._lock:
            await self._ensure()
            page = await self._context.new_page()
            try:
                yield page
            finally:
                with contextlib.suppress(Exception):
                    await page.close()

    # ---------------- 查询 ----------------
    async def query_layer(self, portal_url: str, hole_no: str,
                          top: float, bottom: float) -> QueryResult:
        """按孔号查询门户记录，再按层顶/层底识别本层。"""
        try:
            async with self._page() as page:
                await page.goto(portal_url, wait_until="domcontentloaded")
                await page.fill('[data-testid="query-hole"]', hole_no)
                await page.click('[data-testid="query-submit"]')
                await page.wait_for_selector(
                    '[data-testid="query-result"], [data-testid="query-empty"]',
                    timeout=self._nav_timeout,
                )

                empty = await page.query_selector('[data-testid="query-empty"]')
                if empty is not None:
                    return QueryResult(found=0)

                rows = await page.query_selector_all('[data-testid="result-row"]')
                records: list[dict] = []
                for tr in rows:
                    top_el = await tr.query_selector('[data-testid="r-top"]')
                    bottom_el = await tr.query_selector('[data-testid="r-bottom"]')
                    litho_el = await tr.query_selector('[data-testid="r-lithology"]')
                    link_el = await tr.query_selector('[data-testid="r-link"]')
                    records.append({
                        "top_depth": float((await top_el.inner_text()).strip()),
                        "bottom_depth": float((await bottom_el.inner_text()).strip()),
                        "lithology": (await litho_el.inner_text()).strip(),
                        "url": await link_el.get_attribute("href"),
                    })

                matched = [
                    r for r in records
                    if abs(r["top_depth"] - float(top)) < 1e-9
                    and abs(r["bottom_depth"] - float(bottom)) < 1e-9
                ]
                return QueryResult(found=len(matched), records=matched)
        except asyncio.TimeoutError as exc:
            raise AutomationError(f"查询超时：{hole_no} {top}-{bottom}") from exc
        except AutomationError:
            raise
        except Exception as exc:  # 页面故障统一转换
            raise AutomationError(f"查询失败：{exc}") from exc

    # ---------------- 填表提交 ----------------
    async def submit_layer(self, portal_url: str, hole_no: str, top: float,
                           bottom: float, lithology: str) -> SubmitResult:
        """逐层填表并点保存。超时/断线时如实回报未确认，绝不自行重提。"""
        try:
            async with self._page() as page:
                # 点击保存之前的故障属于运行时错误（尚未提交，可安全重试）
                await page.goto(portal_url, wait_until="domcontentloaded")
                await page.fill('[data-testid="form-hole"]', hole_no)
                await page.fill('[data-testid="form-top"]', _fmt(top))
                await page.fill('[data-testid="form-bottom"]', _fmt(bottom))
                await page.fill('[data-testid="form-lithology"]', lithology)

                # 点击之后无法确定服务端是否已落库：任何中断都只能算“未确认”
                try:
                    async with asyncio.timeout(self._submit_timeout / 1000.0):
                        await page.click('[data-testid="form-save"]')
                        await page.wait_for_selector(
                            '[data-testid="save-success"], [data-testid="save-error"]',
                            timeout=self._submit_timeout,
                        )
                except Exception as exc:
                    return SubmitResult(
                        confirmed=False,
                        error=f"保存响应中断或超时（可能已入库，禁止自动重提）："
                              f"{type(exc).__name__}: {exc}",
                    )

                err = await page.query_selector('[data-testid="save-error"]')
                if err is not None:
                    return SubmitResult(
                        confirmed=False,
                        error=f"门户保存报错：{(await err.inner_text()).strip()}",
                    )

                link_el = await page.query_selector('[data-testid="record-link"]')
                url = await link_el.get_attribute("href") if link_el else None
                return SubmitResult(confirmed=True, record_url=url)
        except AutomationError:
            raise
        except Exception as exc:
            # 未进入保存流程的页面级故障（如打开录入页就失败）
            raise AutomationError(f"填表失败：{exc}") from exc


def _fmt(v: float) -> str:
    return ("%.10g" % v)
