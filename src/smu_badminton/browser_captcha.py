"""
基于 Playwright 的验证码混合方案 (v1: 驱动真实页面 + 拦截 mutation 取加密 captchaCode)。

思路:
  - Python 已完成 CAS 登录拿到 access_token + id_token, 已选定场次.
  - 浏览器加载真实 WF 预约页面 (注入 token 或用持久化 session), 触发滑块验证码.
  - 用已有的 OpenCV 缺口识别 (slide_captcha.solve_slide_captcha) 算出缺口 X.
  - 在浏览器里模拟真人拖拽滑块 -> SDK 原生完成加密 -> SPA 发 saveAppointment mutation.
  - 拦截该 mutation 请求, 取出 captchaId + 加密 captchaCode, 然后 **中止该请求**.
  - 返回给 Python, 由 Python 用这两个值发真实下单 (复用现有 booking_api.make_appointment).

  v1 先以 "驱动真实页面" 稳健形态落地; 是否能预解/最小 harness 放 v2.

UI 选择器: SPA 选择器未知, 提供 --probe 模式先 dump DOM 结构, 再据此精修 --book 的选择器。
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

from .config import (
    WF_ORIGIN,
    WF_API_URL,
    BADMINTON_TYPE_ID,
)

logger = logging.getLogger(__name__)

# 持久化用户数据目录: 保存登录 session, 避免每次重新 CAS 登录
USER_DATA_DIR = Path(__file__).resolve().parent.parent.parent / ".playwright_profile"
DEBUG_DIR = Path(__file__).resolve().parent.parent.parent / "debug_captcha"


# ---------------------------------------------------------------------------
# 选择器常量 (首次 headed 跑完 --probe 后, 据实际 DOM 精修这些)
# ---------------------------------------------------------------------------
SEL = {
    # 日期选择 / 时段列表 / 预约按钮 等留空, 待 probe 后填
    "book_btn_text": ["预约", "立即预约", "我要预约", "确定预约"],
    "captcha_handle": ".slider-btn, .slider_btn, [class*='slider'] [class*='btn'], [class*='handle']",
    "captcha_bg_img": "img[class*='bg'], img[class*='background'], [class*='captcha'] img[class*='bg']",
    "captcha_tpl_img": "img[class*='template'], img[class*='tpl']",
    "captcha_container": "[class*='captcha'], [class*='slider'], [class*='verify']",
}


async def _ensure_dirs():
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def _save_debug(name: str, data: bytes):
    _ensure_dirs()
    ts = time.strftime("%Y%m%d_%H%M%S")
    p = DEBUG_DIR / f"{ts}_{name}"
    p.write_bytes(data)
    logger.info("debug -> %s", p)
    return p


# ---------------------------------------------------------------------------
# 真人拖拽: 起点拖动 distance 像素, ease-out + 轻微 y 抖动 + 微超调回弹
# ---------------------------------------------------------------------------
def _human_path(distance: int, steps: int = 40, y_jitter: float = 2.0):
    """生成归一化路径点 (dx_ratio, dy)。dx 单调到 1.0 附近, 末尾微超调后回弹。"""
    pts = []
    overshoot = min(5, max(2, int(distance * 0.04)))  # 3~5px 超调
    target = distance
    peak = target + overshoot
    for i in range(steps + 1):
        t = i / steps
        # ease-out cubic 主位移
        e = 1 - (1 - t) ** 3
        x = peak * e
        # 最后 15% 回弹到 target
        if t > 0.85:
            rt = (t - 0.85) / 0.15
            x = peak - (peak - target) * rt
        # y 抖动: 小幅正弦 + 随机(用 hash 替代 random, 避免不稳定)
        y = y_jitter * ( (i % 3 - 1) ) * 0.6
        pts.append((int(round(x)), y))
    # 确保终点精确
    pts[-1] = (target, 0.0)
    return pts


async def human_drag(page, start_x: float, start_y: float, distance: int, duration_ms: int = 1200):
    """在 (start_x,start_y) 处按下, 按 _human_path 拖动 distance 像素, 再抬起。"""
    await page.mouse.move(start_x, start_y)
    await asyncio.sleep(0.12)
    await page.mouse.down()
    pts = _human_path(distance)
    step_ms = max(8, duration_ms // len(pts))
    for dx, dy in pts:
        await page.mouse.move(start_x + dx, start_y + dy, steps=1)
        await asyncio.sleep(step_ms / 1000.0)
    await asyncio.sleep(0.08)
    await page.mouse.up()


# ---------------------------------------------------------------------------
# 滑块求解: 从验证码 DOM 提取背景/模板图, 调 OpenCV 算 X, 映射到显示坐标
# ---------------------------------------------------------------------------
async def solve_slider_in_page(page, debug: bool = True) -> Optional[int]:
    """
    返回滑块需拖动的显示像素距离。失败返回 None。
    """
    # 等验证码容器出现
    try:
        await page.wait_for_selector(SEL["captcha_container"], timeout=8000)
    except Exception:
        logger.error("未找到验证码容器, 选择器=%s", SEL["captcha_container"])
        return None

    # 提取背景图 (可能是 <img src> 或 data URI; 模板同理)
    async def _get_img_data(sel: str) -> Optional[str]:
        try:
            el = await page.query_selector(sel)
            if el is None:
                return None
            src = await el.get_attribute("src")
            if src and src.startswith("data:"):
                return src
            if src and src.startswith("http"):
                return src
            # 有时是 canvas; 尝试 toDataURL
            tag = await el.evaluate("e => e.tagName.toLowerCase()")
            if tag == "canvas":
                return await el.evaluate("e => e.toDataURL('image/png')")
            return src
        except Exception as e:
            logger.debug("取图失败 sel=%s: %s", sel, e)
            return None

    bg = await _get_img_data(SEL["captcha_bg_img"])
    tpl = await _get_img_data(SEL["captcha_tpl_img"])
    if not bg or not tpl:
        # 退化: dump 容器 HTML 帮助精修
        try:
            html = await page.inner_html(SEL["captcha_container"])
            _save_debug("captcha_container.html", html.encode("utf-8"))
        except Exception:
            pass
        logger.error("取不到背景/模板图, 已 dump 容器 HTML")
        return None

    if debug:
        for name, data in (("bg", bg), ("tpl", tpl)):
            try:
                raw = base64.b64decode(data.split(",", 1)[1]) if data.startswith("data:") else b""
                if raw:
                    _save_debug(f"{name}.png", raw)
            except Exception:
                pass

    # OpenCV 算缺口 X (原图坐标)
    from .slide_captcha import solve_slide_captcha
    gap_x_raw = solve_slide_captcha(bg, tpl, debug=debug)
    if gap_x_raw is None:
        logger.error("OpenCV 缺口识别失败")
        return None

    # 映射到显示坐标: 取背景图元素的显示宽度
    try:
        bg_el = await page.query_selector(SEL["captcha_bg_img"])
        box = await bg_el.bounding_box()
        disp_w = box["width"] if box else None
    except Exception:
        disp_w = None

    # 原图宽度: 若是 data URI 无法直接知, 默认按 600 (HAR 中常见). 有 disp_w 时按比例缩放。
    raw_w = 600
    if disp_w:
        gap_x_disp = int(gap_x_raw * disp_w / raw_w)
    else:
        gap_x_disp = gap_x_raw
    logger.info("缺口: raw=%s disp=%s (disp_w=%s)", gap_x_raw, gap_x_disp, disp_w)
    return gap_x_disp


# ---------------------------------------------------------------------------
# 拦截 mutation: 取出 captchaId + 加密 captchaCode, 中止请求
# ---------------------------------------------------------------------------
def _install_mutation_harvester(context, sink: dict):
    """拦截 saveAppointmentInformationAll 请求, 把 captchaId/captchaCode 存入 sink, 并 abort。"""
    async def on_request(request):
        try:
            url = request.url
            if "graphql" not in url:
                return
            body = request.post_data
            if not body or "saveAppointmentInformationAll" not in body:
                return
            try:
                payload = json.loads(body)
            except Exception:
                return
            variables = payload.get("variables", {})
            cid = variables.get("captchaId")
            ccode = variables.get("captchaCode")
            if cid and ccode:
                sink["captchaId"] = cid
                sink["captchaCode"] = ccode
                sink["raw"] = body
                logger.info("已捕获 captchaId=%s captchaCode=%s (len=%d)",
                            cid, ccode[:16] + "…", len(ccode))
        except Exception as e:
            logger.debug("harvester 异常: %s", e)

    context.on("request", lambda req: asyncio.create_task(on_request(req)))


# ---------------------------------------------------------------------------
# probe 模式: dump DOM 结构, 帮助精修选择器
# ---------------------------------------------------------------------------
async def probe_mode(headed: bool = True):
    from playwright.async_api import async_playwright
    await _ensure_dirs()
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(USER_DATA_DIR),
            headless=not headed,
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        url = f"{WF_ORIGIN}/yy-sys/pc/resources/{BADMINTON_TYPE_ID}/list"
        logger.info("probe 打开: %s", url)
        await page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(3)
        _save_debug("probe_page.png", await page.screenshot(full_page=True))
        _save_debug("probe_url.txt", page.url.encode())
        # 列出所有按钮文案 + 可见输入
        btns = await page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('button, [role=button], a, .el-button, .ant-btn').forEach(e => {
                const t = (e.innerText||'').trim();
                if (t && t.length < 20) out.push({tag: e.tagName, text: t, cls: e.className.toString().slice(0,60)});
            });
            return out.slice(0, 80);
        }""")
        _save_debug("probe_buttons.json", json.dumps(btns, ensure_ascii=False, indent=2).encode())
        logger.info("页面标题: %s", await page.title())
        logger.info("按钮/链接文案: %s", json.dumps([b['text'] for b in btns], ensure_ascii=False))
        logger.info("已 dump: probe_page.png / probe_buttons.json / probe_url.txt 到 debug_captcha/")
        logger.info("若未登录(看到登录页), 请在浏览器里手动完成 CAS 登录, 然后重跑 --probe。")
        input("按回车关闭浏览器…") if headed else None
        await ctx.close()


# ---------------------------------------------------------------------------
# book 模式: v1 骨架, 选择器待 probe 后精修
# ---------------------------------------------------------------------------
async def book_mode(date: str, start: str, end: str, resource_id: str, headed: bool = True):
    from playwright.async_api import async_playwright
    await _ensure_dirs()
    sink: dict = {}
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            str(USER_DATA_DIR),
            headless=not headed,
            viewport={"width": 1366, "height": 900},
        )
        _install_mutation_harvester(ctx, sink)

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        url = f"{WF_ORIGIN}/yy-sys/pc/resources/{BADMINTON_TYPE_ID}/list"
        logger.info("打开资源页: %s", url)
        await page.goto(url, wait_until="domcontentloaded")
        await asyncio.sleep(2)

        # TODO(选择器精修): 选日期 -> 选时段 -> 点预约, 触发验证码
        # 这里先 dump 当前页面, 供下一步精修
        _save_debug("book_step1.png", await page.screenshot())
        logger.warning("v1 选择器未精修: 请先跑 --probe 拿到 DOM, 再补全日期/时段/预约选择器。")

        # 触发验证码后(占位): 调用滑块求解 + 拖拽
        # gap_disp = await solve_slider_in_page(page)
        # handle = await page.query_selector(SEL["captcha_handle"])
        # box = await handle.bounding_box()
        # await human_drag(page, box['x']+box['width']/2, box['y']+box['height']/2, gap_disp)

        # 等待 mutation 被捕获
        # try:
        #     await page.wait_for_function("window.__captured", timeout=15000)
        # except Exception:
        #     pass

        await ctx.close()

    return sink


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="Playwright 验证码混合方案")
    ap.add_argument("--probe", action="store_true", help="探测 SPA DOM, dump 选择器线索")
    ap.add_argument("--book", action="store_true", help="执行预约流程(需先 probe 精修选择器)")
    ap.add_argument("--date", default="2026-08-26")
    ap.add_argument("--start", default="20:00")
    ap.add_argument("--end", default="21:00")
    ap.add_argument("--resource-id", default=BADMINTON_TYPE_ID)
    ap.add_argument("--headless", action="store_true", help="无头(默认有头便于调试)")
    args = ap.parse_args()

    if args.probe:
        asyncio.run(probe_mode(headed=not args.headless))
    elif args.book:
        r = asyncio.run(book_mode(args.date, args.start, args.end, args.resource_id,
                                  headed=not args.headless))
        logger.info("结果: %s", json.dumps(r, ensure_ascii=False))
    else:
        ap.print_help()
        print("\n先跑: python -m smu_badminton.browser_captcha --probe")


if __name__ == "__main__":
    main()
