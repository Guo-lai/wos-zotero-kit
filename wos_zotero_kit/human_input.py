"""人类化输入基元。

风控的行为层看的是事件轨迹：真实鼠标从 A 到 B 会产生几十上百个带加速度的
mousemove，真人打字每个字符产生 keydown/keypress/keyup 三个事件。而
Playwright 的 locator.click() / locator.fill() 走 CDP 直接派发落点事件和
value 赋值，轨迹是空的——那是一根 delta 函数，阈值检测器一抓一个准。

本模块把这些 delta 摊成一段轨迹。分两层：
- plan_* 是纯函数，注入 random.Random，可单测
- human_* 是异步驱动，只做 Playwright 胶水，不单测（需要真浏览器）
"""

from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass

# 按键间隔取对数正态：真人按键间隔右偏——大量落在 60-120ms，偶发长停顿。
# 绝不用均匀分布：uniform jitter 的方差结构本身就是机器信号。
KEYSTROKE_MEDIAN_MS = 95.0
KEYSTROKE_SIGMA = 0.55
KEYSTROKE_MIN_MS = 25.0
KEYSTROKE_MAX_MS = 600.0

# 空格与标点后允许更长停顿（真人在这里换气/斟酌）。
PUNCTUATION_PAUSE_CHARS = " ,;:()[]=\"'"
PUNCTUATION_PAUSE_MULTIPLIER = 1.8

# 落点偏移：中心 ± 22% 尺寸的高斯抖动，边缘留 2px 余量。
CLICK_SPREAD_RATIO = 0.22
CLICK_EDGE_MARGIN_PX = 2.0

# 预热：每个动作的停顿区间，以及滚动幅度。
WARMUP_PAUSE_MIN_MS = 180.0
WARMUP_PAUSE_MAX_MS = 900.0
WARMUP_SCROLL_MIN_PX = 120
WARMUP_SCROLL_MAX_PX = 420
WARMUP_SCROLL_PROBABILITY = 0.28

_DEFAULT_RNG = random.Random()


def default_rng() -> random.Random:
    """模块级共享 RNG。调用方需要可复现时自己传 random.Random(seed)。"""
    return _DEFAULT_RNG


@dataclass(frozen=True)
class ClickPoint:
    x: float
    y: float


@dataclass(frozen=True)
class WarmupAction:
    kind: str  # "move" | "scroll"
    x: float = 0.0
    y: float = 0.0
    scroll_dy: int = 0
    pause_ms: float = 0.0


def plan_keystroke_delays(text: str, rng: random.Random) -> list[float]:
    """返回每个字符打完之后的停顿毫秒数，长度与 text 相同。"""
    delays: list[float] = []
    mu = math.log(KEYSTROKE_MEDIAN_MS)
    for char in text:
        delay = rng.lognormvariate(mu, KEYSTROKE_SIGMA)
        if char in PUNCTUATION_PAUSE_CHARS:
            delay *= PUNCTUATION_PAUSE_MULTIPLIER
        delays.append(min(max(delay, KEYSTROKE_MIN_MS), KEYSTROKE_MAX_MS))
    return delays


def plan_click_point(box: dict, rng: random.Random) -> ClickPoint:
    """在 bounding box 内取一个偏离正中心的落点。

    真手不会像素级命中中心；永远点中心是最容易被统计出来的模式之一。
    """
    left = float(box["x"])
    top = float(box["y"])
    width = float(box["width"])
    height = float(box["height"])

    center_x = left + width / 2
    center_y = top + height / 2
    offset_x = rng.gauss(0.0, width * CLICK_SPREAD_RATIO)
    offset_y = rng.gauss(0.0, height * CLICK_SPREAD_RATIO)

    margin_x = min(CLICK_EDGE_MARGIN_PX, width / 2)
    margin_y = min(CLICK_EDGE_MARGIN_PX, height / 2)
    x = min(max(center_x + offset_x, left + margin_x), left + width - margin_x)
    y = min(max(center_y + offset_y, top + margin_y), top + height - margin_y)
    return ClickPoint(x, y)


def plan_approach_path(
    start: ClickPoint, end: ClickPoint, rng: random.Random, segments: int = 3
) -> list[ClickPoint]:
    """从 start 走到 end 的折线中间点（含终点，不含起点）。

    每个中间点在直线上加一段垂直方向的抖动，避免走出一条完美直线——
    完美直线和瞬移一样不像人手。
    """
    segments = max(1, segments)
    dx = end.x - start.x
    dy = end.y - start.y
    distance = math.hypot(dx, dy)
    jitter_scale = min(distance * 0.12, 24.0)

    points: list[ClickPoint] = []
    for index in range(1, segments + 1):
        ratio = index / segments
        x = start.x + dx * ratio
        y = start.y + dy * ratio
        if index < segments and distance > 0:
            # 垂直于行进方向的偏移
            normal_x = -dy / distance
            normal_y = dx / distance
            magnitude = rng.gauss(0.0, jitter_scale)
            x += normal_x * magnitude
            y += normal_y * magnitude
        points.append(ClickPoint(x, y))
    return points


def plan_warmup(
    warmup_seconds: float, viewport: dict, rng: random.Random
) -> list[WarmupAction]:
    """生成一段总时长约 warmup_seconds 的鼠标游走 + 滚动序列。

    目的不是"看起来像在读"，而是让下一次上报的 telemetry 缓冲区里有东西——
    页面导航会清空采集窗口，紧接着操作就是在一段全零特征上做高风险动作。

    滚动成对生成（滚下去必滚回来），页面最终停在原位。
    """
    if warmup_seconds <= 0:
        return []

    width = float(viewport.get("width") or 1440)
    height = float(viewport.get("height") or 960)
    budget_ms = warmup_seconds * 1000
    actions: list[WarmupAction] = []
    spent_ms = 0.0
    pending_scroll_back = 0

    while spent_ms < budget_ms:
        remaining = budget_ms - spent_ms
        pause = min(rng.uniform(WARMUP_PAUSE_MIN_MS, WARMUP_PAUSE_MAX_MS), remaining)

        if pending_scroll_back:
            actions.append(WarmupAction(kind="scroll", scroll_dy=pending_scroll_back, pause_ms=pause))
            pending_scroll_back = 0
        elif rng.random() < WARMUP_SCROLL_PROBABILITY:
            amount = rng.randint(WARMUP_SCROLL_MIN_PX, WARMUP_SCROLL_MAX_PX)
            actions.append(WarmupAction(kind="scroll", scroll_dy=amount, pause_ms=pause))
            pending_scroll_back = -amount
        else:
            actions.append(
                WarmupAction(
                    kind="move",
                    x=rng.uniform(0.0, width),
                    y=rng.uniform(0.0, height),
                    pause_ms=pause,
                )
            )
        spent_ms += pause

    if pending_scroll_back:
        # 预算用完但还欠一次回滚：补上，宁可略超预算也不留下净位移。
        actions.append(WarmupAction(kind="scroll", scroll_dy=pending_scroll_back, pause_ms=0.0))
    return actions


def jittered_pause_ms(base_ms: float, rng: random.Random, spread: float = 0.45) -> float:
    """把固定的 wait_for_timeout 换成抖动等待，打散节拍。"""
    return rng.uniform(base_ms * (1.0 - spread), base_ms * (1.0 + spread))


# --- 异步驱动层：消费上面的规划结果，只做 Playwright 胶水 ---

# 起手点相对目标的随机偏移范围，模拟鼠标本来就在别处。
APPROACH_ORIGIN_DX = 180.0
APPROACH_ORIGIN_DY = 140.0
MOVE_STEP_MIN_S = 0.012
MOVE_STEP_MAX_S = 0.045
PRESS_HOLD_MIN_S = 0.045
PRESS_HOLD_MAX_S = 0.130


async def human_click(page, locator, rng: random.Random | None = None) -> None:
    """带轨迹的点击：先把鼠标沿折线移过去，落点随机偏移，再 down/up。"""
    rng = rng or default_rng()
    box = await locator.bounding_box()
    if not box:
        # 元素拿不到 box（未渲染/被裁剪）时退回原生点击，宁可少一层拟真也不能失败。
        await locator.click()
        return

    target = plan_click_point(box, rng)
    origin = ClickPoint(
        target.x + rng.uniform(-APPROACH_ORIGIN_DX, APPROACH_ORIGIN_DX),
        target.y + rng.uniform(-APPROACH_ORIGIN_DY, APPROACH_ORIGIN_DY),
    )
    for point in plan_approach_path(origin, target, rng):
        await page.mouse.move(point.x, point.y)
        await asyncio.sleep(rng.uniform(MOVE_STEP_MIN_S, MOVE_STEP_MAX_S))

    await page.mouse.down()
    await asyncio.sleep(rng.uniform(PRESS_HOLD_MIN_S, PRESS_HOLD_MAX_S))
    await page.mouse.up()


async def human_type(page, locator, text: str, rng: random.Random | None = None) -> None:
    """逐字符输入：先点进去清空，再按对数正态间隔一个一个敲。"""
    rng = rng or default_rng()
    await human_click(page, locator, rng)
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")

    for char, delay_ms in zip(text, plan_keystroke_delays(text, rng)):
        await page.keyboard.type(char)
        await asyncio.sleep(delay_ms / 1000)


async def run_warmup(page, actions) -> None:
    """回放预热计划，填满 telemetry 采集窗口。"""
    for action in actions:
        if action.kind == "move":
            await page.mouse.move(action.x, action.y)
        elif action.kind == "scroll":
            await page.mouse.wheel(0, action.scroll_dy)
        if action.pause_ms:
            await asyncio.sleep(action.pause_ms / 1000)
