"""
测试 captchaId / captchaCode 的有效期窗口（纯 Python，零副作用：checkCaptcha 不下单）。

为什么测的是 captchaId 会话期、而不是加密 captchaCode 本身？
  - captchaCode 是 checkCaptcha 成功后服务端签发的 36 字符 uuid, SPA 的 w() 会用
    AES-128-CBC 加密后提交(纯 Python 已复现, 见 booking_api.encrypt_captcha_code, 无需浏览器)。
  - captchaCode 绑定在 captchaId 会话上, 故 captchaId 会话 TTL 即 captchaCode 的有效期窗口,
    本测试直接测出【精确窗口】(非上界):
      * 若 gen->check 窗口只有几十秒  -> 预解(pre-solve)必死, 必须命中即解。
      * 若为分钟级 -> 预解值得做(窗口 > 预期提前量 10~30s 即可行)。

方法（每个延迟点独立、零副作用）：
  1. genCaptcha 取一个【全新】captchaId（每个延迟点只 check 一次, 排除重试耗尽干扰）。
  2. 生成一条【行为像真人但终点故意错位】的轨迹（ease-out + 微超调回弹 + y 漂移
     + 释放前 ~1.1s 停顿, 完全对标 HAR 真人轨迹）, 避免被风控 ban 误判成过期。
  3. sleep(delay)。
  4. POST checkCaptcha, 记录【完整原始响应】, 按 msg 关键词分类:
       ALIVE(success)  : code==200 且返回 captchaCode      -> 会话存活(且走运命中)
       ALIVE(non-success): 非成功但无"过期/失效"关键词        -> 会话仍存活(位置错)
       EXPIRED         : 含 过期/失效/超时/不存在/已使用 等  -> 会话已过期
  5. 升序延迟, 首次 EXPIRED 即停; 输出存活窗口区间。

用法:
  # 方式 A: 直接给 access_token（机器人/服务器里取一个）
  python debug_captcha/test_captcha_expiry.py --token ACCESS_TOKEN

  # 方式 B: 给账密, 脚本自动 CAS 登录拿 token（login_with_retry 会自动解登录图形码）
  python debug_captcha/test_captcha_expiry.py --username 2025xxxx --password xxxx

  # 快速冒烟（单次 delay=5s, 验证流程通不通; token 失效则在 gen 即停, ~2 秒）
  python debug_captcha/test_captcha_expiry.py --token X --once

  # 自定义延迟点（秒, 升序）
  python debug_captcha/test_captcha_expiry.py --token X --delays 5,15,30,60,120,300,600
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

import requests
from urllib.parse import quote

# 复用项目配置
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src"))
from smu_badminton.config import WF_ORIGIN, WF_CAPTCHA_URL, BADMINTON_TYPE_ID  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("captcha-expiry")

# HAR 中 Referer 的 typeName 是百分号编码的"羽毛球场地"(5 字), 必须编码否则 HTTP 头非 latin-1
BOOKING_REFERER = f"{WF_ORIGIN}/yy-sys/pc/resources/{BADMINTON_TYPE_ID}/list?typeName={quote('羽毛球场地')}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0",
    "Content-Type": "application/json;charset=UTF-8",
    "Accept": "application/json, text/plain, */*",
    "Origin": WF_ORIGIN,
    "Referer": BOOKING_REFERER,
}

# 显示坐标尺寸（HAR 中 checkCaptcha 用的 bgImageWidth/Height）
BG_W = 300
BG_H = 180

# 过期类关键词（命中即判 EXPIRED）
EXPIRED_KW = ("过期", "失效", "超时", "expire", "timeout", "不存在", "已使用", "已过期", "废弃")


# ---------------------------------------------------------------------------
# 真人轨迹生成：对标 HAR 真人轨迹（x:0→70, ~80 事件, ~1.6s, y 负向小漂移, 释放前 ~1.1s 停顿）
# 终点 = target_x（故意错位, 不是真缺口）, 但行为通过风控。
# ---------------------------------------------------------------------------
def gen_human_track(target_x: int, up_t_ms: int, seed: int) -> list:
    """生成对标 HAR 的 trackList（down/move/up），终点落在 target_x（故意错位）。

    时间模型（与 HAR 完全一致）:
      - t = performance.now() = 自 startTime 起的毫秒数。
      - up 事件 t == up_t_ms（HAR 不变量: stopTime = startTime + up_t 必须成立）。
      - 前段是用户"看图"的预停顿（down_t），最后 ~1.6s 才是 ease-out 拖拽 + 微超调回弹。
      - 释放前 ~0.3-0.8s 到达终点后停顿（HAR 真人停了 ~1.1s 才松手）。
    up_t_ms 由调用方按"gen->check 实际经过时长"传入, 故 startTime 锚定 gen 时刻、
    stopTime≈当前, 既不未来也不远古, 且不变量精确成立。
    """
    rng = random.Random(seed)
    drag_ms = rng.randint(1400, 1800)            # 实际拖拽 1.4~1.8s
    pre_pause = rng.randint(200, 800)            # 到达终点后、释放前的停顿
    down_t = int(up_t_ms) - drag_ms - pre_pause  # 预停顿(看图)
    if down_t < 200:                             # up_t 太小: 压缩拖拽保序
        down_t = 200
        drag_ms = max(int(up_t_ms) - down_t - 100, 400)
    arrive_t = int(up_t_ms) - pre_pause          # 到达终点时刻
    span = arrive_t - down_t                     # down->arrive 的拖拽时长
    n_moves = rng.randint(40, 60)
    overshoot = rng.randint(2, 4)                 # 微超调 2~4px（HAR 几乎无超调）
    peak = target_x + overshoot

    pts = []  # (x, y, t)
    for i in range(n_moves + 1):
        frac = i / n_moves
        e = 1 - (1 - frac) ** 3                  # ease-out cubic 主位移
        x = peak * e
        if frac > 0.85:                          # 末段回弹到 target
            rt = (frac - 0.85) / 0.15
            x = peak - (peak - target_x) * rt
        y = -rng.randint(0, 6)                    # y 负向小漂移（HAR 全程 y 在 -1~-6）
        t = down_t + int(frac * span)            # 不加抖动, 保证单调递增
        pts.append((int(round(x)), int(y), t))
    pts[-1] = (target_x, pts[-1][1], arrive_t)  # 终点精确

    track = [{"x": 0, "y": 0, "type": "down", "t": int(down_t)}]
    for x, yy, tt in pts:
        track.append({"x": x, "y": yy, "type": "move", "t": int(tt)})
    track.append({"x": target_x, "y": pts[-1][1], "type": "up", "t": int(up_t_ms)})
    return track


# ---------------------------------------------------------------------------
# HTTP: 直接调 genCaptcha / checkCaptcha（不复用 booking_api, 以注入自定义轨迹 + 看原始响应）
# ---------------------------------------------------------------------------
def http_gen(session: requests.Session, token: str) -> dict:
    # HAR: POST genCaptcha?token=...  body="{}" (content-length 2)
    url = f"{WF_CAPTCHA_URL}/genCaptcha?token={token}"
    r = session.post(url, headers=HEADERS, json={}, timeout=15)
    try:
        return r.json()
    except Exception:
        return {"_status": r.status_code, "_text": r.text[:300]}


def http_check(session: requests.Session, token: str, captcha_id: str,
               track: list, gen_ts: float) -> dict:
    # 时间模型对标 HAR: startTime=gen 时刻, up_t=track 末事件 t,
    # stopTime=startTime+up_t -> HAR 不变量 stop-start==up_t 精确成立, 且 stop≈当前。
    url = f"{WF_CAPTCHA_URL}/checkCaptcha?token={token}"
    up_t = int(track[-1]["t"])
    start = datetime.fromtimestamp(gen_ts, tz=timezone.utc)
    stop = start + timedelta(milliseconds=up_t)

    def _iso(dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

    body = {
        "id": captcha_id,
        "data": {
            "bgImageWidth": BG_W,
            "bgImageHeight": BG_H,
            "startTime": _iso(start),
            "stopTime": _iso(stop),
            "trackList": track,
        },
    }
    r = session.post(url, headers=HEADERS, json=body, timeout=15)
    try:
        return r.json()
    except Exception:
        return {"_status": r.status_code, "_text": r.text[:300]}


def extract_captcha_id(gen_resp: dict) -> Tuple[Optional[str], dict]:
    """从 genCaptcha 响应里取 captchaId 与 captcha 信息（兼容多种包裹）。"""
    top = gen_resp if isinstance(gen_resp, dict) else {}
    data = top.get("data") if isinstance(top.get("data"), dict) else {}
    cid = top.get("id") or data.get("id")
    cap = top.get("captcha") or data.get("captcha") or {}
    if not cid and isinstance(data, dict):
        cid = data.get("id") or data.get("captchaId")
    return cid, cap


def classify(resp: dict) -> Tuple[str, str]:
    """返回 (状态, 摘要)。状态: ALIVE(success)/ALIVE(non-success)/EXPIRED/ERROR。"""
    if not isinstance(resp, dict):
        return "ERROR", str(resp)[:200]
    code = resp.get("code")
    msg = str(resp.get("msg", ""))
    success = resp.get("success")
    data = resp.get("data") or {}
    ccode = ""
    if isinstance(data, dict):
        ccode = data.get("captchaCode", "")
    # 成功
    if (success is True) or (code in (200, "200") and ccode):
        return "ALIVE(success)", f"code={code} msg={msg!r} captchaCode={ccode[:16]}…"
    # 过期关键词
    low = msg.lower()
    for kw in EXPIRED_KW:
        if kw in msg or kw in low:
            return "EXPIRED", f"code={code} msg={msg!r}"
    # 非成功但无过期关键词 -> 视为存活（位置错/行为警告等, 会话未过期）
    return "ALIVE(non-success)", f"code={code} msg={msg!r} success={success}"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(token: str, delays: list, target_x: int = 70,
        solve: bool = True, debug: bool = False) -> list:
    session = requests.Session()
    results = []
    last_alive = None
    first_expired = None

    for i, d in enumerate(delays):
        logger.info("=" * 64)
        logger.info("延迟点 %d: delay=%ds  (gen 全新 captchaId)", i, d)

        gen_resp = http_gen(session, token)
        gen_ts = time.time()                       # 锚定 gen 时刻 -> startTime
        cid, cap = extract_captcha_id(gen_resp)
        if not cid:
            logger.error("genCaptcha 失败, 原始响应: %s", json.dumps(gen_resp, ensure_ascii=False)[:400])
            results.append({"delay": d, "status": "GEN_FAIL", "raw": gen_resp})
            # gen 失败可能是 token 失效; 继续无意义, 提前停
            if "token" in json.dumps(gen_resp, ensure_ascii=False).lower() or "401" in str(gen_resp):
                logger.error("疑似 token 失效, 终止。")
                break
            continue

        bg = cap.get("backgroundImage", "") if isinstance(cap, dict) else ""
        logger.info("captchaId=%s  bg=%s...", str(cid)[:40], (bg[:24] + "…") if bg else "(无)")

        # 真解滑块(默认): OpenCV 算缺口 X, 映射到显示坐标 -> 位置正确时
        # 存活=success(返回 captchaCode)、过期=过期消息, 信号无歧义。
        # 解失败则退回 target_x(错位), 仅得 ALIVE(non-success)/EXPIRED。
        if solve and isinstance(cap, dict):
            bg_img = cap.get("backgroundImage")
            tpl_img = cap.get("templateImage")
            raw_w = cap.get("backgroundImageWidth") or 600
            if bg_img and tpl_img:
                from smu_badminton.slide_captcha import solve_slide_captcha
                gap_raw = solve_slide_captcha(bg_img, tpl_img, debug=debug)
                if gap_raw is not None:
                    target_x = int(gap_raw * BG_W / raw_w)
                    logger.info("OpenCV 缺口: raw=%s -> disp_x=%s (raw_w=%s BG_W=%s)",
                                gap_raw, target_x, raw_w, BG_W)
                else:
                    logger.warning("OpenCV 解失败, 退回错位 target_x=%s", target_x)
            else:
                logger.warning("gen 响应缺 bg/tpl, 退回 target_x=%s", target_x)

        if d > 0:
            logger.info("sleep %ds ...", d)
            time.sleep(d)

        # 轨迹时间锚定 gen 时刻: up_t = gen->当前 实际经过时长(≥2s 容纳拖拽)。
        # 这样 startTime=gen, stopTime=gen+up_t≈当前, HAR 不变量 stop-start==up_t 精确成立。
        elapsed_ms = (time.time() - gen_ts) * 1000
        up_t = max(int(round(elapsed_ms)), 2000)
        track = gen_human_track(target_x, up_t_ms=up_t, seed=d * 13 + 7)
        logger.info("轨迹: %d 事件, 终点 x=%d (错位), up_t=%dms", len(track), target_x, up_t)

        t0 = time.time()
        resp = http_check(session, token, cid, track, gen_ts)
        dt_ms = (time.time() - t0) * 1000
        status, summary = classify(resp)
        logger.info("checkCaptcha -> %s  (%.0fms)  %s", status, dt_ms, summary)
        logger.info("原始响应: %s", json.dumps(resp, ensure_ascii=False)[:300])
        results.append({"delay": d, "status": status, "summary": summary,
                        "captchaId": cid, "raw": resp})

        if status == "EXPIRED":
            first_expired = d
            logger.warning("*** 首次过期于 delay=%ds, 停止后续延迟点 ***", d)
            break
        elif status.startswith("ALIVE"):
            last_alive = d
        # ERROR 继续下一个延迟点

    # 汇总
    logger.info("=" * 64)
    logger.info("汇总:")
    logger.info("  最后一个存活延迟: %ss", last_alive)
    logger.info("  首个过期延迟:     %ss", first_expired)
    if first_expired is not None and last_alive is not None:
        logger.info("  => captchaId 会话有效期窗口: (%ss, %ss]", last_alive, first_expired)
        logger.info("  => 预解可行性: 若窗口 > 预期提前量(10~30s) 则可行; 否则必须命中即解。")
    elif first_expired is None and last_alive is not None:
        logger.info("  => 在所测最大延迟 %ss 内未过期: 会话期 >= %ss（分钟级, 预解很可能可行）",
                    last_alive, last_alive)
        logger.info("  => 建议再用浏览器确认 code 自身窗口（发坏 slot 的 mutation）。")
    elif first_expired is not None and last_alive is None:
        logger.info("  => delay=0 即过期/不可用: token 或会话机制异常, 复核原始响应。")
    return results


def obtain_token(args) -> Optional[str]:
    if args.token:
        return args.token
    if args.username and args.password:
        from smu_badminton.cas_manager import get_token_cached
        from smu_badminton.config import CAS_LOGIN_URL, CAS_CAPTCHA_URL
        logger.info("自动 CAS 登录 (username=%s) ...", args.username)
        tokens = get_token_cached(CAS_LOGIN_URL, CAS_CAPTCHA_URL,
                                 args.username, args.password)
        if not tokens or not tokens.get("access_token"):
            logger.error("登录失败。")
            return None
        at = tokens["access_token"]
        logger.info("登录成功, access_token=%s…", str(at)[:12])
        return at
    return None


def main():
    ap = argparse.ArgumentParser(description="测试 captchaId/captchaCode 有效期窗口")
    ap.add_argument("--token", help="access_token（方式 A）")
    ap.add_argument("--username", help="学号（方式 B, 自动登录）")
    ap.add_argument("--password", help="密码（方式 B）")
    ap.add_argument("--delays", default="5,15,30,60,120,300,600",
                     help="延迟点(秒, 升序, 逗号分隔), 默认 5,15,30,60,120,300,600")
    ap.add_argument("--once", action="store_true", help="冒烟: 只测单次 delay=5s")
    ap.add_argument("--target-x", type=int, default=70,
                    help="错位轨迹终点(显示坐标), 默认 70; --no-solve 时使用, 或 solve 失败时回退")
    ap.add_argument("--no-solve", action="store_true",
                    help="禁用 OpenCV 真解, 用固定 target_x(错位) -> 仅 ALIVE(non-success)/EXPIRED 信号")
    ap.add_argument("--debug", action="store_true", help="保存 OpenCV 缺口标记图等调试输出")
    args = ap.parse_args()

    token = obtain_token(args)
    if not token:
        ap.error("必须提供 --token 或 (--username + --password)")

    delays = [5] if args.once else [int(x) for x in args.delays.split(",") if x.strip()]
    logger.info("开始测试, 延迟点=%s, target_x=%d, solve=%s", delays, args.target_x, not args.no_solve)
    results = run(token, delays, target_x=args.target_x,
                  solve=not args.no_solve, debug=args.debug)

    # 落盘完整结果
    out = __import__("pathlib").Path(__file__).parent / "captcha_expiry_result.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("完整结果已写入: %s", out)


if __name__ == "__main__":
    main()
