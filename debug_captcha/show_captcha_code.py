"""零副作用确认 captchaCode 真实形态: gen -> solve -> check, 打印完整 captchaCode 及长度。

只调 genCaptcha / checkCaptcha, 不发 booking mutation, 不占每日预约配额。
用途: 坐实 captchaCode 的真实形态(已证实 = 36 字符 uuid, 不是 HAR 配置的 captchaCodeLength=4)。
  - len(captchaCode) >= 16 -> 走 SPA w() 的 AES-128-CBC 分支,
    key=captcha_id[:16] iv=captcha_id[1:17], captcha_id 已知 -> 纯 Python 可逐字节复现
    (见 booking_api.encrypt_captcha_code; 实测 36 字符 uuid 必走此分支)。

用法: python debug_captcha/show_captcha_code.py --token ACCESS_TOKEN [--n 3]
"""
import sys
import time
import json
import argparse
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))   # src
sys.path.insert(0, str(Path(__file__).resolve().parent))                  # debug_captcha
import requests  # noqa: E402
from smu_badminton.slide_captcha import solve_slide_captcha  # noqa: E402
from test_captcha_expiry import (  # noqa: E402
    http_gen, http_check, extract_captcha_id, gen_human_track, BG_W,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("show-code")


def main():
    ap = argparse.ArgumentParser(description="打印真实 captchaCode 形态(零副作用)")
    ap.add_argument("--token", required=True, help="access_token")
    ap.add_argument("--n", type=int, default=1, help="重复次数")
    args = ap.parse_args()

    s = requests.Session()
    for i in range(args.n):
        g = http_gen(s, args.token)
        gen_ts = time.time()
        cid, cap = extract_captcha_id(g)
        if not cid:
            logger.error("[%d] gen 失败: %s", i, json.dumps(g, ensure_ascii=False)[:200])
            continue
        bg = cap.get("backgroundImage", "") if isinstance(cap, dict) else ""
        tpl = cap.get("templateImage", "") if isinstance(cap, dict) else ""
        raw_w = (cap.get("backgroundImageWidth") or 600) if isinstance(cap, dict) else 600

        gap = solve_slide_captcha(bg, tpl, debug=False)
        if gap is None:
            logger.error("[%d] solve 失败", i)
            continue
        target_x = int(gap * BG_W / raw_w)

        elapsed = (time.time() - gen_ts) * 1000
        up_t = max(int(round(elapsed)), 2000)
        track = gen_human_track(target_x, up_t_ms=up_t, seed=i * 7 + 1)

        resp = http_check(s, args.token, cid, track, gen_ts)
        code = resp.get("code")
        success = resp.get("success")
        data = resp.get("data") or {}
        ccode = data.get("captchaCode", "") if isinstance(data, dict) else ""

        if (success is True) or (code in (200, "200") and ccode):
            logger.info("[%d] 成功! captchaCode=%r  len=%d  type=%s",
                        i, ccode, len(str(ccode)), type(ccode).__name__)
            logger.info("    captchaId=%s", cid)
            logger.info("    完整 data=%s", json.dumps(data, ensure_ascii=False)[:400])
            logger.info("    => %s",
                        "原样直传(<16, 无需加密)" if len(str(ccode)) < 16
                        else ">=16, 走 AES-CBC 分支(key/iv 来自 captcha_id, 纯 Python 可复现)")
        else:
            logger.info("[%d] 未成功: code=%s msg=%r success=%s",
                        i, code, resp.get("msg"), success)
        time.sleep(1.5)


if __name__ == "__main__":
    main()
