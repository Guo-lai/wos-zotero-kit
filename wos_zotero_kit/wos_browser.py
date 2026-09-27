"""
Web of Science 浏览器自动化工具

设计目标：
- 监督式自动化：检索、导出和下载自动完成
- 遇到登录 / 机构 SSO / 人机验证时暂停，等待用户在浏览器中手动处理
- 可选使用 CloakBrowser 降低自动化误触发，但不自动处理验证挑战
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from playwright.async_api import (
    BrowserContext,
    Download,
    Error as PlaywrightError,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from . import human_input

# 本模块运行在 MCP stdio server 里：stdout 是 JSON-RPC 通道，诊断信息只能走 logging(stderr)。
logger = logging.getLogger(__name__)


ADVANCED_SEARCH_URL = "https://www.webofscience.com/wos/woscc/advanced-search"
DEFAULT_WAIT_MS = 30_000
DOWNLOAD_TIMEOUT_MS = 120_000
DOWNLOAD_POLL_INTERVAL_MS = 1_000

# 提交检索前的预热时长（秒）。页面导航会清空 telemetry 采集窗口，
# 紧接着操作等于在一段全零特征上做最高风险的动作。
DEFAULT_WARMUP_SECONDS = 6.0
MAX_WARMUP_SECONDS = 60.0

# 导出对话框各步之间的基准等待。原来是固定 1 秒，节拍太规整。
EXPORT_STEP_BASE_MS = 1_000.0

_RNG = random.Random()
WOS_BROWSER_BACKEND_ENV = "WOS_BROWSER_BACKEND"
SUPPORTED_BROWSER_BACKENDS = {"auto", "playwright", "cloakbrowser"}

LOGIN_URL_HINTS = (
    "access.clarivate.com/login",
    "/login?app=wos",
)
LOGIN_TEXT_HINTS = (
    "sign in to your profile",
    "institutional sign in",
    "go to institution",
    "email address",
    "password",
)
CAPTCHA_TEXT_HINTS = (
    "verify you are human",
    "human verification",
    "captcha",
    "security check",
    "press and hold",
    "challenge",
)
RESULT_COUNT_PATTERNS = (
    re.compile(r"([\d,]+)\s+results?\b", re.IGNORECASE),
    re.compile(r"\bresults?\s*\(?([\d,]+)\)?", re.IGNORECASE),
)
# 可以执行 Export 的页面路径; 也用于 skip_search 模式判断用户是否已手动搜出结果。
RESULT_PAGE_MARKERS = (
    "/wos/woscc/summary",
    "/wos/woscc/full-record",
    "/wos/woscc/citation-report",
)


class WosAutomationError(RuntimeError):
    """Raised when the WoS automation cannot finish safely."""


@dataclass
class WosExportResult:
    query: str
    database: str
    result_count: int | None
    exported_files: list[str]
    manual_checkpoint_triggered: bool = False
    warnings: list[str] = field(default_factory=list)
    browser_backend: str = "playwright"


@dataclass
class WosOpenResult:
    database: str
    status: str
    url: str
    title: str
    verification_state: str | None = None
    message: str = ""
    browser_backend: str = "playwright"
    warnings: list[str] = field(default_factory=list)


SEARCH_ONLY_STATUSES = frozenset({"ready_to_export", "typed_awaiting_manual_submit", "blocked"})


@dataclass
class WosSearchOnlyResult:
    resolved_query: str
    result_count: int | None
    url: str
    status: str
    verification_state: str | None
    message: str
    browser_backend: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class _LaunchedContext:
    context: BrowserContext
    browser_backend: str
    warnings: list[str] = field(default_factory=list)
    playwright: Playwright | None = None


@dataclass
class _ActiveWosSession:
    playwright: Playwright | None
    context: BrowserContext
    page: Page
    download_dir: Path
    profile_dir: Path
    database: str
    manual_checkpoint_triggered: bool = False
    browser_backend: str = "playwright"
    browser_warnings: list[str] = field(default_factory=list)


_active_session: _ActiveWosSession | None = None


def _requested_browser_backend() -> str:
    backend = os.environ.get(WOS_BROWSER_BACKEND_ENV, "auto").strip().lower() or "auto"
    if backend not in SUPPORTED_BROWSER_BACKENDS:
        allowed = ", ".join(sorted(SUPPORTED_BROWSER_BACKENDS))
        raise WosAutomationError(
            f"{WOS_BROWSER_BACKEND_ENV}={backend!r} 不受支持；可选值: {allowed}。"
        )
    return backend


def _import_cloak_launch_persistent_context_async() -> Callable[..., Any]:
    from cloakbrowser import launch_persistent_context_async

    return launch_persistent_context_async


def _format_backend_error(exc: Exception) -> str:
    text = str(exc).strip()
    if not text:
        return exc.__class__.__name__
    return text.splitlines()[0]


def _split_keyword_text(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;\n]+", text) if part.strip()]


def _format_topic_term(term: str) -> str:
    term = term.strip()
    if term.startswith('"') and term.endswith('"') and len(term) >= 2:
        return term
    if re.search(r"\s", term):
        escaped = term.replace('"', r'\"')
        return f'"{escaped}"'
    return term


def build_wos_topic_query(keywords: str | Iterable[str]) -> str:
    """Build a WoS Advanced Search topic query from user-provided keywords."""
    if isinstance(keywords, str):
        raw_terms = _split_keyword_text(keywords)
    else:
        raw_terms = []
        for keyword in keywords:
            if keyword is None:
                continue
            raw_terms.extend(_split_keyword_text(str(keyword)))

    terms = [_format_topic_term(term) for term in raw_terms if term.strip()]
    if not terms:
        raise WosAutomationError("keywords 不能为空；请提供 query 或至少一个关键词。")
    return f"TS=({' OR '.join(terms)})"


def normalize_download_filename(download_path: str | Path) -> Path:
    """
    WoS 常把 RIS 结果下载成 .txt；这里统一规范成 .ris。
    该函数保持目录不变，仅必要时改后缀，便于单元验证。
    """
    path = Path(download_path)
    if path.suffix.lower() == ".ris":
        return path
    return path.with_suffix(".ris")


def _is_main_export_button_text(text: str) -> bool:
    normalized = " ".join(text.split()).lower()
    return normalized.startswith("export") and "refine" not in normalized


def _is_finished_ris_download(path: str | Path) -> bool:
    path = Path(path)
    if path.suffix.lower() == ".crdownload":
        return False
    return path.suffix.lower() in {".ris", ".txt"}


def _recent_ris_downloads(download_dir: Path, since_mtime: float) -> list[Path]:
    if not download_dir.exists():
        return []
    candidates = []
    for path in download_dir.iterdir():
        if not path.is_file() or not _is_finished_ris_download(path):
            continue
        try:
            if path.stat().st_mtime >= since_mtime:
                candidates.append(path)
        except OSError:
            continue
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)


def detect_verification_state_from_text(url: str, body_text: str) -> str | None:
    lower_url = url.lower()
    lower_text = body_text.lower()
    if any(hint in lower_url for hint in LOGIN_URL_HINTS) or any(
        hint in lower_text for hint in LOGIN_TEXT_HINTS
    ):
        return "login"
    if any(hint in lower_text for hint in CAPTCHA_TEXT_HINTS):
        return "captcha"
    return None


def parse_result_count(text: str) -> int | None:
    for pattern in RESULT_COUNT_PATTERNS:
        match = pattern.search(text)
        if match:
            return int(match.group(1).replace(",", ""))
    return None


async def _body_text(page: Page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=5_000)
    except PlaywrightError:
        return ""


async def _all_frames_text(page: Page) -> str:
    """收集主页 + 所有 iframe 的可见文本, 用于检测 CAPTCHA / 登录覆盖层 (常在 iframe 中)。"""
    chunks: list[str] = []
    try:
        chunks.append(await page.locator("body").inner_text(timeout=2_000))
    except PlaywrightError:
        pass
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            chunks.append(await frame.locator("body").inner_text(timeout=1_500))
        except PlaywrightError:
            continue
    return "\n".join(chunks)


async def _detect_verification_state(page: Page) -> str | None:
    # 先用 body 文本快速判断, 命中则直接返回; 未命中再扫所有 iframe (CAPTCHA widget 常在 iframe 内)。
    state = detect_verification_state_from_text(page.url, await _body_text(page))
    if state is not None:
        return state
    return detect_verification_state_from_text(page.url, await _all_frames_text(page))


async def _launch_playwright_persistent_context(
    playwright: Playwright, profile_dir: Path, download_dir: Path
) -> BrowserContext:
    profile_dir.mkdir(parents=True, exist_ok=True)
    download_dir.mkdir(parents=True, exist_ok=True)

    last_error: Exception | None = None
    for channel in ("msedge", "chrome", None):
        try:
            return await playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,
                accept_downloads=True,
                channel=channel,
                downloads_path=str(download_dir),
                viewport={"width": 1440, "height": 960},
            )
        except PlaywrightError as exc:
            last_error = exc
            continue
    raise WosAutomationError(f"无法启动浏览器上下文: {last_error}") from last_error


async def _launch_playwright_context(profile_dir: Path, download_dir: Path) -> _LaunchedContext:
    playwright = await async_playwright().start()
    try:
        context = await _launch_playwright_persistent_context(playwright, profile_dir, download_dir)
    except Exception:
        await playwright.stop()
        raise
    return _LaunchedContext(
        context=context,
        browser_backend="playwright",
        playwright=playwright,
    )


async def _launch_cloak_context(profile_dir: Path, download_dir: Path) -> _LaunchedContext:
    profile_dir.mkdir(parents=True, exist_ok=True)
    download_dir.mkdir(parents=True, exist_ok=True)

    launch_persistent_context_async = _import_cloak_launch_persistent_context_async()
    context = await launch_persistent_context_async(
        str(profile_dir),
        headless=False,
        accept_downloads=True,
        downloads_path=str(download_dir),
        viewport={"width": 1440, "height": 960},
    )
    return _LaunchedContext(
        context=context,
        browser_backend="cloakbrowser",
    )


async def _launch_context(profile_dir: Path, download_dir: Path) -> _LaunchedContext:
    backend = _requested_browser_backend()

    if backend in {"auto", "cloakbrowser"}:
        try:
            launched = await _launch_cloak_context(profile_dir, download_dir)
            if backend == "auto":
                launched.warnings.append("WOS_BROWSER_BACKEND=auto 已使用 CloakBrowser。")
            return launched
        except ModuleNotFoundError as exc:
            if backend == "cloakbrowser":
                raise WosAutomationError(
                    "WOS_BROWSER_BACKEND=cloakbrowser 但未安装 cloakbrowser；"
                    "请先 pip install cloakbrowser，或设 WOS_BROWSER_BACKEND=playwright。"
                ) from exc
            warning = "CloakBrowser 未安装，已回退到 Playwright。"
        except Exception as exc:
            if backend == "cloakbrowser":
                raise WosAutomationError(
                    "WOS_BROWSER_BACKEND=cloakbrowser 启动失败: "
                    f"{_format_backend_error(exc)}"
                ) from exc
            warning = (
                "CloakBrowser 启动失败，已回退到 Playwright: "
                f"{_format_backend_error(exc)}"
            )

        launched = await _launch_playwright_context(profile_dir, download_dir)
        launched.warnings.append(warning)
        return launched

    return await _launch_playwright_context(profile_dir, download_dir)


async def _close_launched_context(context: BrowserContext, playwright: Playwright | None) -> None:
    try:
        await context.close()
    finally:
        if playwright is not None:
            await playwright.stop()


async def _close_active_session() -> None:
    global _active_session
    session = _active_session
    _active_session = None
    if session is None:
        return
    await _close_launched_context(session.context, session.playwright)


async def close_active_session() -> bool:
    """关闭活跃 WoS 会话。幂等：没有会话时返回 False，不报错。"""
    if _active_session is None:
        return False
    await _close_active_session()
    return True


async def _start_active_session(
    download_dir: Path,
    profile_dir: Path,
    database: str,
) -> _ActiveWosSession:
    global _active_session
    await _close_active_session()
    try:
        launched = await _launch_context(profile_dir, download_dir)
        context = launched.context
        page = context.pages[0] if context.pages else await context.new_page()
    except Exception:
        raise

    _active_session = _ActiveWosSession(
        playwright=launched.playwright,
        context=context,
        page=page,
        download_dir=download_dir,
        profile_dir=profile_dir,
        database=database,
        browser_backend=launched.browser_backend,
        browser_warnings=list(launched.warnings),
    )
    return _active_session


async def _safe_title(page: Page) -> str:
    try:
        return await page.title()
    except PlaywrightError:
        return ""


async def _wait_for_any_visible(page: Page, selectors: Iterable[str], timeout_ms: int = 5_000) -> Locator | None:
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=timeout_ms)
            return locator
        except PlaywrightTimeoutError:
            continue
        except PlaywrightError:
            continue
    return None


async def _click_first(page: Page, selectors: Iterable[str], timeout_ms: int = 5_000) -> bool:
    locator = await _wait_for_any_visible(page, selectors, timeout_ms=timeout_ms)
    if locator is None:
        return False
    try:
        await human_input.human_click(page, locator, _RNG)
        return True
    except PlaywrightError:
        return False


async def _click_main_export_button(page: Page, timeout_ms: int = 10_000) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    last_error: Exception | None = None
    while asyncio.get_running_loop().time() < deadline:
        buttons = page.get_by_role("button").filter(has_text=re.compile(r"\bExport\b", re.IGNORECASE))
        try:
            count = await buttons.count()
        except PlaywrightError as exc:
            last_error = exc
            count = 0
        for index in reversed(range(count)):
            button = buttons.nth(index)
            try:
                text = await button.inner_text(timeout=1_000)
            except PlaywrightError as exc:
                last_error = exc
                continue
            if not _is_main_export_button_text(text):
                continue
            try:
                await human_input.human_click(page, button, _RNG)
                return True
            except PlaywrightError as exc:
                last_error = exc
                continue
        await asyncio.sleep(0.25)

    # Fallback for older markup, still avoid selectors that explicitly say Refine.
    fallback = [
        "button:has-text('Export'):not(:has-text('Refine'))",
        "[role='button']:has-text('Export'):not(:has-text('Refine'))",
        "a:has-text('Export'):not(:has-text('Refine'))",
    ]
    if await _click_first(page, fallback, timeout_ms=1_000):
        return True
    if last_error is not None:
        return False
    return False


async def _checkpoint_status(page: Page) -> tuple[str, str | None, str]:
    state = await _detect_verification_state(page)
    if state == "login":
        return (
            "login_required",
            state,
            "WoS 需要登录或机构认证。请在打开的浏览器中手动完成登录，然后调用 resume 工具继续。",
        )
    if state == "captcha":
        return (
            "captcha_required",
            state,
            "WoS 需要人机验证。请手动完成验证，然后调用 resume 工具继续。",
        )

    locator = await _wait_for_any_visible(
        page,
        ("textarea", "[contenteditable='true']", "div[role='textbox']", "input[type='text']"),
        timeout_ms=2_000,
    )
    if locator is not None:
        return ("ready", None, "已进入可用的 WoS Advanced Search 页面，可以调用 resume 工具继续检索。")

    return (
        "unknown",
        None,
        "未检测到登录/验证页，但也未可靠找到高级检索输入框；可能需要现场校准 WoS 页面结构。",
    )


async def _manual_checkpoint(page: Page, timeout_minutes: int) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_minutes * 60
    triggered = False

    while asyncio.get_running_loop().time() < deadline:
        state = await _detect_verification_state(page)
        if state is None:
            return triggered

        if not triggered:
            triggered = True
            try:
                await page.bring_to_front()
            except PlaywrightError:
                pass

        await asyncio.sleep(2)

    raise WosAutomationError(
        f"等待手动完成 WoS 登录/验证超时（>{timeout_minutes} 分钟）"
    )


async def _ensure_query_builder_tab(page: Page) -> bool:
    """在 Advanced Search 页切换到 QUERY BUILDER tab.

    WoS 2025/2026 Advanced Search 默认 tab 是 FIELDED SEARCH (把 TS=(...) 当字面词搜),
    QUERY BUILDER 才接受裸 TS=/PY= 语法。屏幕实测 tab 文本是 "QUERY BUILDER" (大写)。

    返回是否成功切换。选择器随 WoS 改版漂移是预期内的常见情况，本函数不因此抛错——
    调用方负责在拿到 False 时把这个信号对外暴露（比如写进 warnings），而不是假装切换成功了。
    """
    query_builder_selectors = [
        "[role='tab']:has-text('QUERY BUILDER')",
        "[role='tab']:has-text('Query Builder')",
        "button:has-text('QUERY BUILDER')",
        "button:has-text('Query Builder')",
        "a:has-text('QUERY BUILDER')",
        "a:has-text('Query Builder')",
    ]
    switched = await _click_first(page, query_builder_selectors, timeout_ms=5_000)
    if switched:
        await page.wait_for_timeout(800)
    return switched


async def _dismiss_overlays(page: Page) -> None:
    """关掉常见遮挡: cookie banner / Edge translate popup / Edge boost banner / "Got it" hints。"""
    dismiss_selectors = [
        "button:has-text('Accept all cookies')",
        "button:has-text('Reject all')",
        "button:has-text('I agree')",
        "button[aria-label='Close translation popup']",
        "button[aria-label*='Dismiss' i]",
        "button:has-text('Got it')",
        "button:has-text('No thanks')",
        "button:has-text('Maybe later')",
        "button[aria-label='Close']",
    ]
    for selector in dismiss_selectors:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=500):
                await locator.click(timeout=1_500)
                await page.wait_for_timeout(300)
        except PlaywrightError:
            continue
    try:
        await page.keyboard.press("Escape")
    except PlaywrightError:
        pass


async def _warmup_page(page: Page, warmup_seconds: float) -> None:
    """在高风险动作之前填满 telemetry 采集窗口。"""
    if warmup_seconds <= 0:
        return
    try:
        viewport = getattr(page, "viewport_size", None) or {"width": 1440, "height": 960}
        actions = human_input.plan_warmup(warmup_seconds, viewport, _RNG)
        await human_input.run_warmup(page, actions)
    except PlaywrightError:
        # 预热失败不该阻断主流程，最坏情况回到原来的风险水平。
        pass


def _export_step_pause_ms(rng: random.Random | None = None) -> float:
    """导出对话框步间的抖动等待毫秒数。"""
    return human_input.jittered_pause_ms(EXPORT_STEP_BASE_MS, rng or _RNG)


async def _goto_query_builder(page: Page) -> bool:
    """打开 WoS Advanced Search 页, 关闭遮挡弹窗, 切换到 QUERY BUILDER tab。

    返回值原样透传 `_ensure_query_builder_tab` 的切换结果。
    """
    await page.goto(ADVANCED_SEARCH_URL, wait_until="domcontentloaded", timeout=DEFAULT_WAIT_MS)
    await page.wait_for_timeout(1_500)
    await _dismiss_overlays(page)
    return await _ensure_query_builder_tab(page)


async def _open_advanced_search(page: Page, timeout_minutes: int) -> bool:
    manual_triggered = False
    await _goto_query_builder(page)

    state = await _detect_verification_state(page)
    if state is not None:
        manual_triggered = await _manual_checkpoint(page, timeout_minutes)
        await _goto_query_builder(page)

    return manual_triggered


async def _set_query(page: Page, query: str) -> None:
    # 优先匹配 Advanced Search 主 query textarea, 避免误匹配 history 页日期输入或 sidebar refine 框。
    candidates = [
        "textarea[name='advancedSearchInputArea']",
        "textarea[data-ta-id='AdvancedSearchInput']",
        "#advancedSearchInputArea",
        "textarea[aria-label*='advanced search' i]",
        "textarea[placeholder*='Example' i]",
        "textarea[placeholder*='TS=' i]",
        "main textarea",
        "textarea",
        "[contenteditable='true']",
        "div[role='textbox']",
    ]
    locator = await _wait_for_any_visible(page, candidates, timeout_ms=10_000)
    if locator is None:
        raise WosAutomationError("未找到 WoS 高级检索输入框，请确认已进入 Advanced Search 页面。")

    tag_name = (await locator.evaluate("el => el.tagName")).lower()
    if tag_name in {"textarea", "input"}:
        await human_input.human_type(page, locator, query, _RNG)
    else:
        await human_input.human_click(page, locator, _RNG)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        for char, delay_ms in zip(query, human_input.plan_keystroke_delays(query, _RNG)):
            await page.keyboard.type(char)
            await asyncio.sleep(delay_ms / 1000)


async def _submit_search(page: Page) -> None:
    search_selectors = [
        "button.mat-mdc-unelevated-button:has-text('Search')",
        "button[type='submit']:has-text('Search')",
        "button:has-text('Search'):not(:has-text('history')):not(:has-text('Smart'))",
        "[role='button']:has-text('Search'):not(:has-text('history'))",
        "input[type='submit'][value*='Search' i]",
    ]
    locator = await _wait_for_any_visible(page, search_selectors, timeout_ms=8_000)
    if locator is not None:
        try:
            await human_input.human_click(page, locator, _RNG)
            return
        except PlaywrightError:
            pass

    # 兜底: keyboard 回车提交. 之后 _read_result_count 会通过 URL 守卫验证是否到达 summary 页。
    try:
        await page.keyboard.press("Control+Enter")
    except PlaywrightError:
        pass


async def _read_result_count(page: Page) -> int | None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=DEFAULT_WAIT_MS)
    except PlaywrightTimeoutError:
        pass
    await page.wait_for_timeout(2_000)

    text = await _body_text(page)
    return parse_result_count(text)


async def _fill_input_with_keyboard_fallback(page: Page, locator: Locator, value: str) -> bool:
    try:
        await human_input.human_type(page, locator, value, _RNG)
        return True
    except PlaywrightError:
        pass
    try:
        await locator.click(timeout=2_000)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.type(value)
        return True
    except PlaywrightError:
        pass
    try:
        await locator.evaluate(
            """(el, value) => {
                const proto = Object.getPrototypeOf(el);
                const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
                if (descriptor && descriptor.set) descriptor.set.call(el, value);
                else el.value = value;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            value,
        )
        return True
    except PlaywrightError:
        return False


async def _set_export_limit_if_present(container: Locator | Page, max_records: int) -> None:
    """在弹窗内尝试设置导出记录范围 (1 ~ max_records)。

    严格限制在 container 内查找，防止误点背景页面的 Publication Years / 历史检索输入框。
    WoS 2026 版弹窗的两个输入框没有 From/To 标签（09-27 截图），最后按“弹窗里恰好两个文本框”兜底。
    """
    page = container.page if isinstance(container, Locator) else container
    range_radios = [
        container.locator("input[type='radio'][value*='range' i]"),
        container.locator("mat-radio-button:has-text('Records from')"),
        container.locator("label:has-text('Records from')"),
        container.locator("[role='radio']:has-text('Records from')"),
        container.get_by_text(re.compile(r"^\s*Records from", re.IGNORECASE)),
    ]
    for locator in range_radios:
        locator = _first_visible(locator)
        try:
            if await locator.is_visible(timeout=800):
                try:
                    await locator.check(timeout=1_500)
                    break
                except PlaywrightError:
                    await locator.click(timeout=1_500)
                    break
        except PlaywrightError:
            continue

    first_inputs = [
        container.locator("input[placeholder*='From' i]"),
        container.locator("input[aria-label*='from' i]"),
    ]
    second_inputs = [
        container.locator("input[placeholder^='To' i]"),
        container.locator("input[aria-label^='to' i]"),
    ]
    try:
        all_inputs = container.locator(
            "input:not([type='radio']):not([type='checkbox']):not([type='hidden'])"
        ).filter(visible=True)
        if await all_inputs.count() == 2:
            first_inputs.append(all_inputs.nth(0))
            second_inputs.append(all_inputs.nth(1))
    except PlaywrightError:
        pass

    for candidates, value in ((first_inputs, "1"), (second_inputs, str(max_records))):
        for locator in candidates:
            locator = _first_visible(locator)
            try:
                if await locator.is_visible(timeout=500):
                    if await _fill_input_with_keyboard_fallback(page, locator, value):
                        break
            except PlaywrightError:
                continue


async def _choose_ris_format(page: Page) -> None:
    """在主工具栏 Export 展开的下拉菜单中选择 RIS 格式。"""
    ris_selectors = [
        ".cdk-overlay-container [role='menuitem']:has-text('RIS')",
        ".cdk-overlay-container button:has-text('RIS')",
        "[role='menuitem']:has-text('RIS')",
        "button[role='menuitem']:has-text('RIS')",
        ".mat-mdc-menu-item:has-text('RIS')",
        "button:has-text('RIS')",
        "label:has-text('RIS')",
    ]
    if not await _click_first(page, ris_selectors, timeout_ms=8_000):
        raise WosAutomationError("未找到 RIS 导出选项，请检查 WoS 导出菜单是否已展开。")


async def _choose_full_record_if_present(container: Locator | Page) -> bool:
    """在弹窗内尝试将 Record Content 切换为 Full Record。

    严格限制在 container 内查找，失败时主动关闭下拉，绝不点击背景。
    WoS 2026 版下拉默认显示 "Author, Title, Source"，不一定是 mat-select。
    """
    page = container.page if isinstance(container, Locator) else container
    triggers = [
        container.locator("mat-select"),
        container.locator("[role='combobox']"),
        container.locator("[aria-haspopup]:has-text('Author, Title')"),
        container.locator("button:has-text('Author, Title')"),
        container.get_by_text(re.compile(r"^\s*Author, Title, Source", re.IGNORECASE)),
    ]
    options = [
        page.locator("[role='option']").filter(has_text=re.compile(r"^\s*Full Record\s*$", re.IGNORECASE)),
        page.locator("mat-option").filter(has_text=re.compile(r"^\s*Full Record\s*$", re.IGNORECASE)),
        page.get_by_text(re.compile(r"^\s*Full Record\s*$", re.IGNORECASE)),
    ]
    for trigger in triggers:
        trigger = _first_visible(trigger)
        try:
            if not await trigger.is_visible(timeout=800):
                continue
            await human_input.human_click(page, trigger, _RNG)
        except PlaywrightError:
            continue
        for option in options:
            option = _first_visible(option)
            try:
                if await option.is_visible(timeout=1_500):
                    await human_input.human_click(page, option, _RNG)
                    return True
            except PlaywrightError:
                continue
        try:
            await page.keyboard.press("Escape")
        except PlaywrightError:
            pass

    full_record_selectors = [
        "label:has-text('Full Record and Cited References')",
        "label:has-text('Full Record')",
        "[role='radio']:has-text('Full Record')",
    ]
    for sel in full_record_selectors:
        loc = _first_visible(container.locator(sel))
        try:
            if await loc.is_visible(timeout=500):
                await human_input.human_click(page, loc, _RNG)
                return True
        except PlaywrightError:
            continue
    return False


def _first_visible(locator: Locator) -> Locator:
    """取第一个可见匹配。裸 .first 会拿到 DOM 里更靠前的隐藏节点（如残留的空 role=dialog）。"""
    return locator.filter(visible=True).first


def _is_result_page(url: str) -> bool:
    return any(marker in (url or "") for marker in RESULT_PAGE_MARKERS)


async def _wait_for_result_page(page: Page, timeout_seconds: float = 25.0) -> bool:
    """等待页面从 advanced-search 导航到 summary 结果页或触发人机验证。"""
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if _is_result_page(page.url or ""):
            return True
        state = await _detect_verification_state(page)
        if state is not None:
            return False
        await asyncio.sleep(0.5)
    return _is_result_page(page.url or "")


async def _wait_for_export_dialog(page: Page, timeout_ms: int = 10_000) -> Locator | None:
    """等待导出模态对话框出现并返回其 Locator。"""
    dialog_selectors = [
        "mat-dialog-container",
        ".mat-mdc-dialog-container",
        "[role='dialog']",
        ".cdk-overlay-pane:has(button:has-text('Export'))",
        ".cdk-overlay-pane:has([role='radio'])",
        "app-export-dialog",
        "app-export-out-dialog",
    ]
    # 按弹窗文字锚定：标题 "Export Records to ..." + Cancel 按钮同在的最内层元素。
    # 09-27 实测弹窗已显示但上面的结构选择器一个没中，所以文字锚点不能省。
    text_anchored = page.locator("div").filter(
        has_text=re.compile(r"Export Records to", re.IGNORECASE)
    ).filter(has=page.locator("button", has_text=re.compile(r"^\s*Cancel\s*$", re.IGNORECASE)))
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        try:
            anchored = text_anchored.filter(visible=True).last
            if await anchored.is_visible(timeout=300):
                return anchored
        except PlaywrightError:
            pass
        for sel in dialog_selectors:
            loc = _first_visible(page.locator(sel))
            try:
                if await loc.is_visible(timeout=300):
                    return loc
            except PlaywrightError:
                continue
        await asyncio.sleep(0.3)
    return None


async def _click_dialog_export_button(container: Locator | Page, timeout_ms: int = 10_000) -> bool:
    """点击导出模态框内部的确认导出按钮 (B1 模态框精准匹配加固版).

    目标确认按钮位于弹窗内，绝不点击背景页面主工具栏的 Export 按钮。
    """
    page = container.page if isinstance(container, Locator) else container
    confirm_selectors = [
        "button.mat-primary:has-text('Export')",
        "button.mat-mdc-raised-button:has-text('Export')",
        "button[color='primary']:has-text('Export')",
        "button:has-text('Export')",
        "button:has-text('Save')",
        "mat-dialog-actions button:not(:has-text('Cancel'))",
        ".mat-mdc-dialog-actions button:not(:has-text('Cancel'))",
        "[mat-dialog-actions] button:not(:has-text('Cancel'))",
        "button[type='submit']",
    ]
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        for sel in confirm_selectors:
            loc = _first_visible(container.locator(sel))
            try:
                if await loc.is_visible(timeout=400):
                    logger.info("WoS 导出弹窗确认按钮命中: %s", sel)
                    # 表单防抖或校验等待
                    try:
                        if await loc.is_disabled():
                            await asyncio.sleep(0.8)
                    except PlaywrightError:
                        pass
                    try:
                        await loc.click(timeout=1_500)
                        return True
                    except PlaywrightError:
                        try:
                            await human_input.human_click(page, loc, _RNG)
                            return True
                        except PlaywrightError:
                            continue
            except PlaywrightError:
                continue

        # 兜底：若在 container 没扫到，在 CDK overlay 容器中寻找最后一个 Export 按钮
        try:
            overlay_btns = page.locator(".cdk-overlay-container button:has-text('Export')")
            cnt = await overlay_btns.count()
            if cnt > 0:
                last_btn = overlay_btns.last
                if await last_btn.is_visible(timeout=300):
                    logger.info("WoS 导出确认按钮走 CDK overlay 兜底")
                    try:
                        await last_btn.click(timeout=1_500)
                        return True
                    except PlaywrightError:
                        try:
                            await human_input.human_click(page, last_btn, _RNG)
                            return True
                        except PlaywrightError:
                            pass
        except PlaywrightError:
            pass

        await asyncio.sleep(0.3)

    return False


async def _screenshot_or_none(page: Page, path: Path) -> Path | None:
    try:
        await page.screenshot(path=str(path), full_page=False)
        return path
    except Exception:
        return None


async def _export_ris(page: Page, download_dir: Path, max_records: int) -> list[str]:
    # URL 守卫: 仅在结果页/记录页执行 Export, 避免在 history / advanced-search 页误点 disabled Export 按钮。
    current_url = page.url or ""
    if not _is_result_page(current_url):
        raise WosAutomationError(
            f"当前页面不是结果页, 无法导出 RIS (URL: {current_url}). "
            "可能 search 未真正提交或 query 语法被 WoS 拒绝, 请检查浏览器中实际状态。"
        )
    if not await _click_main_export_button(page, timeout_ms=10_000):
        raise WosAutomationError("未找到 Export 按钮，请确认检索结果页已正确打开。")

    await page.wait_for_timeout(_export_step_pause_ms())
    await _choose_ris_format(page)

    # 关键加固 1: 等待导出模态框出现
    dialog = await _wait_for_export_dialog(page, timeout_ms=10_000)
    if dialog is None:
        # 找不到弹窗就停：退回整页查找会先命中背景工具栏的 Export，B1 复发。
        shot_path = await _screenshot_or_none(page, download_dir / f"export-dialog-missing-{int(time.time())}.png")
        raise WosAutomationError(
            f"选择 RIS 后未出现导出对话框，已停止以免误点背景 Export。现场截图: {shot_path or 'none'}"
        )
    target_container: Locator = dialog

    await page.wait_for_timeout(_export_step_pause_ms())
    # 关键加固 2: 所有选项与输入均限制在弹窗内执行，绝不点击背景
    await _choose_full_record_if_present(target_container)
    await _set_export_limit_if_present(target_container, max_records)
    await page.wait_for_timeout(_export_step_pause_ms())

    started_after = time.time() - 2
    try:
        async with page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as download_info:
            if not await _click_dialog_export_button(target_container, timeout_ms=10_000):
                # 捕获失败现场
                shot_path = await _screenshot_or_none(page, download_dir / f"export-btn-missing-{int(time.time())}.png")
                raise WosAutomationError(
                    f"未找到导出对话框内的最终确认按钮。现场截图已存至: {shot_path or 'none'}"
                )
        download = await download_info.value
        return [str(await _save_download(download, download_dir))]
    except PlaywrightTimeoutError:
        candidates = await _poll_for_saved_ris(download_dir, started_after, timeout_ms=10_000)
        if candidates:
            return [str(path) for path in candidates[:1]]

        # B2 修复: 捕获超时时记录现场截图与人机验证状态, 避免盲目归因
        shot_path = await _screenshot_or_none(page, download_dir / f"export-timeout-debug-{int(time.time())}.png")
        v_state = await _detect_verification_state(page)
        curr_url = page.url or "unknown"
        raise WosAutomationError(
            f"等待 WoS RIS 下载超时 ({DOWNLOAD_TIMEOUT_MS // 1000}s). "
            f"当前 URL: {curr_url}; 人机验证状态: {v_state}; 现场截图: {shot_path or 'none'}."
        )


async def _poll_for_saved_ris(
    download_dir: Path, since_mtime: float, timeout_ms: int = DOWNLOAD_TIMEOUT_MS
) -> list[Path]:
    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
    while asyncio.get_running_loop().time() < deadline:
        candidates = _recent_ris_downloads(download_dir, since_mtime)
        if candidates:
            normalized = []
            for candidate in candidates:
                target = normalize_download_filename(candidate)
                if target != candidate:
                    if target.exists():
                        target.unlink()
                    candidate.rename(target)
                normalized.append(target)
            return normalized
        await asyncio.sleep(DOWNLOAD_POLL_INTERVAL_MS / 1000)
    return []


async def _save_download(download: Download, download_dir: Path) -> Path:
    suggested_name = download.suggested_filename or "savedrecs.ris"
    target = download_dir / suggested_name
    target.parent.mkdir(parents=True, exist_ok=True)

    await download.save_as(str(target))
    normalized = normalize_download_filename(target)
    if normalized != target:
        if normalized.exists():
            normalized.unlink()
        target.rename(normalized)
        target = normalized
    return target


async def search_and_export_wos(
    query: str,
    download_dir: str,
    max_records: int = 1000,
    database: str = "Web of Science Core Collection",
    wait_for_manual_checkpoint_minutes: int = 10,
    profile_dir: str | None = None,
) -> WosExportResult:
    """
    监督式 WoS 检索 + RIS 导出。

    说明：
    - 仅支持单条检索式
    - 若命中登录 / 验证页面，会等待用户手动完成
    """
    download_path = Path(download_dir)
    profile_path = Path(profile_dir) if profile_dir else download_path.parent / ".wos-browser-profile"
    warnings: list[str] = []

    if max_records <= 0:
        raise WosAutomationError("max_records 必须大于 0。")

    launched: _LaunchedContext | None = None
    try:
        launched = await _launch_context(profile_path, download_path)
        warnings.extend(launched.warnings)
        page = launched.context.pages[0] if launched.context.pages else await launched.context.new_page()
        manual_triggered = await _open_advanced_search(page, wait_for_manual_checkpoint_minutes)
        await _set_query(page, query)
        await _submit_search(page)
        await _wait_for_result_page(page, timeout_seconds=25.0)

        state = await _detect_verification_state(page)
        if state is not None:
            manual_triggered = await _manual_checkpoint(page, wait_for_manual_checkpoint_minutes) or manual_triggered

        result_count = await _read_result_count(page)
        if result_count is None:
            warnings.append("未能可靠解析 WoS 结果总数。")
        elif result_count > max_records:
            warnings.append(
                f"WoS 结果共 {result_count} 条，v1 仅导出前 {max_records} 条；请收窄检索式或拆分多条检索。"
            )

        exported_files = await _export_ris(page, download_path, min(max_records, result_count or max_records))
        return WosExportResult(
            query=query,
            database=database,
            result_count=result_count,
            exported_files=exported_files,
            manual_checkpoint_triggered=manual_triggered,
            warnings=warnings,
            browser_backend=launched.browser_backend,
        )
    finally:
        if launched is not None:
            await _close_launched_context(launched.context, launched.playwright)


async def open_wos_for_manual_handoff(
    download_dir: str,
    database: str = "Web of Science Core Collection",
    profile_dir: str | None = None,
) -> WosOpenResult:
    """
    打开 WoS Advanced Search 并保持浏览器会话，供用户手动完成登录/验证。

    该入口不提交检索式、不导出、不导入 Zotero；它只建立可恢复的浏览器状态。
    """
    download_path = Path(download_dir)
    profile_path = Path(profile_dir) if profile_dir else download_path.parent / ".wos-browser-profile"
    session = await _start_active_session(download_path, profile_path, database)
    page = session.page

    await _goto_query_builder(page)
    status, verification_state, message = await _checkpoint_status(page)
    if verification_state is not None:
        session.manual_checkpoint_triggered = True
        try:
            await page.bring_to_front()
        except PlaywrightError:
            pass

    return WosOpenResult(
        database=database,
        status=status,
        url=page.url,
        title=await _safe_title(page),
        verification_state=verification_state,
        message=message,
        browser_backend=session.browser_backend,
        warnings=list(session.browser_warnings),
    )


async def search_only_on_active_session(
    query: str,
    *,
    type_only: bool = False,
    warmup_seconds: float = DEFAULT_WARMUP_SECONDS,
) -> WosSearchOnlyResult:
    """只提交检索，停在结果页，会话保留。

    这是人机交替四拍里的第②拍：工具搜，人扫种子文献判断，之后再由
    wos_resume_search_and_import(skip_search=true) 导出。
    """
    session = _active_session
    if session is None:
        raise WosAutomationError(
            "没有活跃的 WoS 会话。请先调用 wos_open_and_wait 打开浏览器并完成登录/验证。"
        )

    warmup_seconds = min(max(float(warmup_seconds), 0.0), MAX_WARMUP_SECONDS)
    page = session.page
    warnings = list(session.browser_warnings)

    status, verification_state, message = await _checkpoint_status(page)
    if verification_state is not None:
        try:
            await page.bring_to_front()
        except PlaywrightError:
            pass
        return WosSearchOnlyResult(
            resolved_query=query,
            result_count=None,
            url=page.url or "",
            status="blocked",
            verification_state=verification_state,
            message=message,
            browser_backend=session.browser_backend,
            warnings=warnings,
        )

    if "/wos/woscc/advanced-search" not in (page.url or ""):
        await _goto_query_builder(page)
    await _dismiss_overlays(page)
    # 提交前最后一次切换，页面实际状态以它为准；不与前面 _goto_query_builder 的结果相与，
    # 否则一次瞬时失败即便随后自愈也会被错误地累计成警告。
    query_builder_ready = await _ensure_query_builder_tab(page)
    if not query_builder_ready:
        warnings.append(
            "未能确认已切换到 QUERY BUILDER tab，检索式可能被提交到了 FIELDED SEARCH——"
            "此时 TS=(...) 会被当作字面文本检索，而不是字段检索语法。请在信任本次导入结果前，"
            "核对返回的结果数量，并检查浏览器里当前的实际页面状态。"
        )

    # 预热：页面导航清空了 telemetry 采集窗口，先填上再动手。
    await _warmup_page(page, warmup_seconds)
    await _set_query(page, query)

    if type_only:
        try:
            await page.bring_to_front()
        except PlaywrightError:
            pass
        return WosSearchOnlyResult(
            resolved_query=query,
            result_count=None,
            url=page.url or "",
            status="typed_awaiting_manual_submit",
            verification_state=None,
            message=(
                "检索式已敲进输入框，工具未提交。请在浏览器里手动点 Search，"
                "看到结果列表后调用 wos_resume_search_and_import(skip_search=true) 导出。"
            ),
            browser_backend=session.browser_backend,
            warnings=warnings,
        )

    await _submit_search(page)
    await _wait_for_result_page(page, timeout_seconds=25.0)

    state = await _detect_verification_state(page)
    if state is not None:
        session.manual_checkpoint_triggered = True
        try:
            await page.bring_to_front()
        except PlaywrightError:
            pass
        return WosSearchOnlyResult(
            resolved_query=query,
            result_count=None,
            url=page.url or "",
            status="blocked",
            verification_state=state,
            message=(
                f"提交检索后 WoS 触发 {state} 拦截。请在浏览器中手动完成验证与检索，"
                "然后调用 wos_resume_search_and_import(skip_search=true) 直接导出；"
                "或改用 wos_search_only(type_only=true) 让工具只填检索式、由你手动点 Search。"
            ),
            browser_backend=session.browser_backend,
            warnings=warnings,
        )

    result_count = await _read_result_count(page)
    if result_count is None:
        warnings.append("未能可靠解析 WoS 结果总数。")

    return WosSearchOnlyResult(
        resolved_query=query,
        result_count=result_count,
        url=page.url or "",
        status="ready_to_export",
        verification_state=None,
        message="检索已提交，结果页已就绪。请扫一遍种子文献确认关键词是否合适。",
        browser_backend=session.browser_backend,
        warnings=warnings,
    )


async def resume_wos_search_and_export(
    query: str,
    download_dir: str,
    max_records: int = 1000,
    database: str = "Web of Science Core Collection",
    profile_dir: str | None = None,
    skip_search: bool = False,
    close_session: bool = False,
) -> WosExportResult:
    """
    从 `open_wos_for_manual_handoff` 保留的会话继续执行检索 + RIS 导出。

    如果 MCP server 重启导致会话丢失，会重新打开持久化 profile，但仍要求页面已不在登录/验证状态。

    skip_search=True 时不再重新提交检索，直接从用户已经手动搜出来的结果页导出 RIS。
    用于 WoS 反爬拦截自动检索的场景：人工搜，工具只负责导出、去重和入库。
    """
    global _active_session
    download_path = Path(download_dir)
    profile_path = Path(profile_dir) if profile_dir else download_path.parent / ".wos-browser-profile"
    warnings: list[str] = []

    if max_records <= 0:
        raise WosAutomationError("max_records 必须大于 0。")

    session = _active_session
    if session is None:
        if skip_search:
            raise WosAutomationError(
                "skip_search=True 需要一个已经打开、且已手动搜出结果的 WoS 会话。"
                "当前没有活跃会话，请先调用 wos_open_and_wait，在浏览器里手动完成检索后再试。"
            )
        session = await _start_active_session(download_path, profile_path, database)
        await _goto_query_builder(session.page)
        warnings.append("未找到已打开的 WoS 会话，已用持久化 profile 重新打开 Advanced Search。")
    warnings.extend(session.browser_warnings)

    page = session.page
    status, verification_state, message = await _checkpoint_status(page)
    if verification_state is not None:
        try:
            await page.bring_to_front()
        except PlaywrightError:
            pass
        raise WosAutomationError(message)
    if not skip_search and status != "ready":
        await _goto_query_builder(page)
        status, verification_state, message = await _checkpoint_status(page)
        if verification_state is not None or status != "ready":
            raise WosAutomationError(message)

    completed = False
    try:
        if skip_search:
            # 人工已经搜完, 这里绝不重新提交检索, 否则又会触发 WoS 反爬拦截。
            current_url = page.url or ""
            if not _is_result_page(current_url):
                try:
                    await page.bring_to_front()
                except PlaywrightError:
                    pass
                raise WosAutomationError(
                    f"skip_search=True 需要浏览器已停在 WoS 结果页, 当前 URL: {current_url or '(空)'}。"
                    "请先在浏览器里手动完成检索, 看到结果列表后再调用本工具。"
                )
            await _dismiss_overlays(page)
            warnings.append("skip_search=True：跳过自动检索，直接从当前结果页导出。")
        else:
            # 如果还停在 history / non-advanced 页, 强制回到 Advanced Search 入口。
            if "/wos/woscc/advanced-search" not in (page.url or ""):
                await _goto_query_builder(page)
            await _dismiss_overlays(page)
            # 即便已在 advanced-search, 也要确保是 QUERY BUILDER tab 而不是 FIELDED SEARCH。
            # 这是提交前最后一次切换, 页面实际状态以它为准; 前面 _goto_query_builder 里那次
            # 若瞬时失败但这次成功, 说明已经自愈, 不该报警。
            query_builder_ready = await _ensure_query_builder_tab(page)
            if not query_builder_ready:
                warnings.append(
                    "未能确认已切换到 QUERY BUILDER tab，检索式可能被提交到了 FIELDED SEARCH——"
                    "此时 TS=(...) 会被当作字面文本检索，而不是字段检索语法。请在信任本次导入结果前，"
                    "核对返回的结果数量，并检查浏览器里当前的实际页面状态。"
                )
            # 页面导航会重置行为采集窗口，先把缓冲区填满，再执行提交检索这个最高风险动作。
            await _warmup_page(page, DEFAULT_WARMUP_SECONDS)
            await _set_query(page, query)
            await _submit_search(page)
            await _wait_for_result_page(page, timeout_seconds=25.0)

            state = await _detect_verification_state(page)
            if state is not None:
                session.manual_checkpoint_triggered = True
                try:
                    await page.bring_to_front()
                except PlaywrightError:
                    pass
                raise WosAutomationError(
                    f"检索提交后 WoS 触发 {state} 拦截 (常见: 短时间多次自动检索导致). "
                    "请在浏览器中手动完成检索, 然后用 skip_search=true 重新调用 "
                    "wos_resume_search_and_import 直接导出结果页。"
                )

        result_count = await _read_result_count(page)
        if result_count is None:
            warnings.append("未能可靠解析 WoS 结果总数。")
        elif result_count > max_records:
            warnings.append(
                f"WoS 结果共 {result_count} 条，v1 仅导出前 {max_records} 条；请收窄检索式或拆分多条检索。"
            )

        exported_files = await _export_ris(page, download_path, min(max_records, result_count or max_records))
        completed = True
        return WosExportResult(
            query=query,
            database=database,
            result_count=result_count,
            exported_files=exported_files,
            manual_checkpoint_triggered=session.manual_checkpoint_triggered,
            warnings=warnings,
            browser_backend=session.browser_backend,
        )
    finally:
        # 默认保留会话：用户"检索次数不定"，每导一组就销毁会话意味着
        # 每轮都要重新登录并重赌一次人机验证。
        if completed and close_session:
            await _close_active_session()
