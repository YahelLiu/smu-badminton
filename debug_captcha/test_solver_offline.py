"""离线验证求解器逻辑（无需 token / 无网络）。

加载 debug_captcha/inspect_bg.png(BGR) 与 inspect_tpl.png(BGRA, 含 alpha),
直接调 _find_gap_position 数组接口, 保存红线标记图。然后人眼看红线是否落在缺口左缘。

也可批量: 传一个目录, 对里面所有 bg/tpl 对跑一遍。
"""
import sys
import logging
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from smu_badminton.slide_captcha import _find_gap_position, _solve_alpha_edge  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("offline")


def load_pair(bg_path: Path, tpl_path: Path):
    bg = cv2.imread(str(bg_path), cv2.IMREAD_COLOR)
    tpl = cv2.imread(str(tpl_path), cv2.IMREAD_UNCHANGED)
    return bg, tpl


def mark(bg: np.ndarray, gap_x: int, out: Path):
    m = bg.copy()
    cv2.line(m, (gap_x, 0), (gap_x, m.shape[0]), (0, 0, 255), 2)
    cv2.putText(m, f"x={gap_x}", (gap_x + 5, 30),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
    cv2.imwrite(str(out), m)
    logger.info("标记图已保存: %s", out)


def main():
    d = Path(__file__).resolve().parent
    bg = d / "inspect_bg.png"
    tpl = d / "inspect_tpl.png"
    if not bg.exists() or not tpl.exists():
        logger.error("缺 inspect_bg.png / inspect_tpl.png, 先跑 inspect_imgs.py 生成")
        sys.exit(1)

    bg_img, tpl_img = load_pair(bg, tpl)
    logger.info("bg shape=%s tpl shape=%s", bg_img.shape, tpl_img.shape)

    # 主方法单独看
    px, pc = _solve_alpha_edge(bg_img, tpl_img)
    logger.info("alpha_edge(主): x=%s conf=%.3f", px, pc)

    # 主流程(含 fallback)
    gap = _find_gap_position(bg_img, tpl_img, debug=True)
    logger.info("最终 gap_x(raw)=%s  -> disp=%s (raw_w=%s BG_W=300)",
                gap, None if gap is None else int(gap * 300 / bg_img.shape[1]),
                bg_img.shape[1])
    if gap is not None:
        mark(bg_img, gap, d / "offline_gap_marked.png")


if __name__ == "__main__":
    main()
