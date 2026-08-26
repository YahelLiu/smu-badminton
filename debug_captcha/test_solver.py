"""求解器可靠性验证: gen -> solve(alpha 主方法) -> check, N 次, 看成功率。

零副作用: 只调 genCaptcha / checkCaptcha, 不发 booking mutation。
成功 = checkCaptcha 返回 code 200 + captchaCode（位置对 + 会话活）。
"""
import sys
import json
import time
import argparse
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))   # src
sys.path.insert(0, str(Path(__file__).resolve().parent))                  # debug_captcha
import requests  # noqa: E402
from smu_badminton.slide_captcha import solve_slide_captcha  # noqa: E402
from test_captcha_expiry import (  # noqa: E402
    http_gen, http_check, extract_captcha_id, classify, gen_human_track, BG_W,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("solver-test")


def main():
    ap = argparse.ArgumentParser(description="求解器可靠性验证 gen->solve->check")
    ap.add_argument("--token", required=True)
    ap.add_argument("--n", type=int, default=5, help="重复次数")
    ap.add_argument("--debug", action="store_true", help="保存缺口标记图")
    args = ap.parse_args()

    s = requests.Session()
    ok = 0
    for i in range(args.n):
        g = http_gen(s, args.token)
        gen_ts = time.time()
        cid, cap = extract_captcha_id(g)
        if not cid:
            logger.info("[%d] gen 失败: %s", i, json.dumps(g, ensure_ascii=False)[:200])
            continue
        bg = cap.get("backgroundImage", "") if isinstance(cap, dict) else ""
        tpl = cap.get("templateImage", "") if isinstance(cap, dict) else ""
        raw_w = cap.get("backgroundImageWidth") or 600 if isinstance(cap, dict) else 600

        gap = solve_slide_captcha(bg, tpl, debug=args.debug)
        if gap is None:
            logger.info("[%d] solve 失败", i)
            continue
        target_x = int(gap * BG_W / raw_w)

        # 轨迹时间锚定 gen 时刻
        elapsed = (time.time() - gen_ts) * 1000
        up_t = max(int(round(elapsed)), 2000)
        track = gen_human_track(target_x, up_t_ms=up_t, seed=i * 7 + 1)

        resp = http_check(s, args.token, cid, track, gen_ts)
        status, summary = classify(resp)
        success = status.startswith("ALIVE(success)")
        ok += success
        logger.info("[%d] gap_raw=%s disp=%s -> %s | %s",
                    i, gap, target_x, status, summary)
        time.sleep(1.5)  # 避免过于频繁

    logger.info("=" * 40)
    logger.info("成功率: %d/%d", ok, args.n)


if __name__ == "__main__":
    main()
