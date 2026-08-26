"""
滑动验证码测试脚本。

用法:
    # 测试缺口检测（使用本地测试图片）
    python -m smu_badminton.test_slide_captcha --test-detect

    # 测试完整流程（需要有效 token）
    python -m smu_badminton.test_slide_captcha --test-full --token YOUR_TOKEN

    # 使用调试模式（保存图片）
    python -m smu_badminton.test_slide_captcha --test-full --token YOUR_TOKEN --debug
"""
import argparse
import base64
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

# 设置日志
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# 调试输出目录
DEBUG_DIR = Path(__file__).parent.parent.parent / "debug_captcha"


def ensure_debug_dir():
    """确保调试目录存在。"""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def save_debug_image(name: str, img_data: bytes, subdir: str = ""):
    """保存调试图片。"""
    ensure_debug_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{name}.png"
    filepath = DEBUG_DIR / subdir / filename if subdir else DEBUG_DIR / filename
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_bytes(img_data)
    logger.info(f"保存调试图片: {filepath}")
    return str(filepath)


def save_debug_json(name: str, data: dict, subdir: str = ""):
    """保存调试 JSON。"""
    ensure_debug_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{timestamp}_{name}.json"
    filepath = DEBUG_DIR / subdir / filename if subdir else DEBUG_DIR / filename
    filepath.parent.mkdir(parents=True, exist_ok=True)
    filepath.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"保存调试 JSON: {filepath}")
    return str(filepath)


def test_gap_detection_with_sample():
    """使用样本图片测试缺口检测。"""
    import cv2
    import numpy as np
    from smu_badminton.slide_captcha import solve_slide_captcha, decode_base64_image

    logger.info("=" * 60)
    logger.info("测试缺口检测算法（使用模拟图片）")
    logger.info("=" * 60)

    # 创建测试图片
    # 背景图：600x180，模拟真实验证码尺寸
    bg = np.zeros((180, 600, 3), dtype=np.uint8)

    # 添加一些随机噪点模拟真实背景
    np.random.seed(42)
    noise = np.random.randint(0, 50, (180, 600, 3), dtype=np.uint8)
    bg = cv2.add(bg, noise)

    # 在 x=350 处画一个缺口（白色矩形 + 边缘）
    gap_x = 350
    gap_y = 50
    gap_w = 60
    gap_h = 80

    # 缺口区域（带阴影效果）
    bg[gap_y:gap_y + gap_h, gap_x:gap_x + gap_w] = 80
    cv2.rectangle(bg, (gap_x, gap_y), (gap_x + gap_w, gap_y + gap_h), (200, 200, 200), 2)

    # 拼图小块
    tpl = np.zeros((180, 80, 3), dtype=np.uint8)
    tpl[gap_y:gap_y + gap_h, 10:10 + gap_w] = 150
    cv2.rectangle(tpl, (10, gap_y), (10 + gap_w, gap_y + gap_h), (200, 200, 200), 2)

    # 编码为 base64
    _, bg_buf = cv2.imencode(".png", bg)
    bg_b64 = base64.b64encode(bg_buf).decode()
    _, tpl_buf = cv2.imencode(".png", tpl)
    tpl_b64 = base64.b64encode(tpl_buf).decode()

    # 保存调试图片
    if DEBUG_DIR.exists():
        save_debug_image("test_bg.png", bg_buf)
        save_debug_image("test_tpl.png", tpl_buf)

    # 检测缺口
    result_x = solve_slide_captcha(bg_b64, tpl_b64, debug=True)

    logger.info(f"真实缺口位置: x={gap_x}")
    logger.info(f"检测结果: x={result_x}")

    if result_x is not None:
        error = abs(result_x - gap_x)
        logger.info(f"误差: {error} 像素")
        if error <= 10:
            logger.info("✅ 检测精度优秀")
        elif error <= 20:
            logger.info("⚠️ 检测精度良好")
        else:
            logger.warning("❌ 检测精度较差")
    else:
        logger.error("❌ 检测失败")

    return result_x, gap_x


def test_gap_detection_with_real_captcha(token: str):
    """使用真实验证码测试缺口检测。"""
    import cv2
    import numpy as np
    from smu_badminton.slide_captcha import solve_slide_captcha
    from smu_badminton.booking_api import gen_slide_captcha

    logger.info("=" * 60)
    logger.info("测试缺口检测算法（使用真实验证码）")
    logger.info("=" * 60)

    # 获取验证码
    logger.info("获取验证码...")
    captcha_data = gen_slide_captcha(token)
    if not captcha_data:
        logger.error("获取验证码失败")
        return None

    captcha_id = captcha_data.get("id", "")
    captcha_info = captcha_data.get("captcha", {})
    bg_image = captcha_info.get("backgroundImage", "")
    tpl_image = captcha_info.get("templateImage", "")

    bg_raw_width = captcha_info.get("backgroundImageWidth", 600)
    bg_raw_height = captcha_info.get("backgroundImageHeight", 360)
    tpl_raw_width = captcha_info.get("templateImageWidth", 110)
    tpl_raw_height = captcha_info.get("templateImageHeight", 360)

    logger.info(f"验证码 ID: {captcha_id[:30]}...")
    logger.info(f"背景图尺寸: {bg_raw_width}x{bg_raw_height}")
    logger.info(f"拼图尺寸: {tpl_raw_width}x{tpl_raw_height}")

    if not bg_image or not tpl_image:
        logger.error("验证码图片缺失")
        return None

    # 保存图片
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    subdir = f"real_{timestamp}"

    if bg_image.startswith("http"):
        import requests
        resp = requests.get(bg_image, timeout=10)
        if resp.status_code == 200:
            save_debug_image("bg_real.png", resp.content, subdir)
    else:
        b64_data = bg_image.split(",")[1] if "," in bg_image else bg_image
        save_debug_image("bg_real.png", base64.b64decode(b64_data), subdir)

    if tpl_image.startswith("http"):
        import requests
        resp = requests.get(tpl_image, timeout=10)
        if resp.status_code == 200:
            save_debug_image("tpl_real.png", resp.content, subdir)
    else:
        b64_data = tpl_image.split(",")[1] if "," in tpl_image else tpl_image
        save_debug_image("tpl_real.png", base64.b64decode(b64_data), subdir)

    # 检测缺口
    logger.info("开始缺口检测...")
    slide_x_raw = solve_slide_captcha(bg_image, tpl_image, debug=True)

    if slide_x_raw is None:
        logger.error("缺口检测失败")
        return None

    # 计算显示坐标
    bg_display_width = bg_raw_width // 2
    slide_x_display = int(slide_x_raw * bg_display_width / bg_raw_width)

    logger.info(f"缺口位置（原图）: x={slide_x_raw}")
    logger.info(f"缺口位置（显示）: x={slide_x_display}")

    # 保存检测信息
    save_debug_json("detection_result.json", {
        "captcha_id": captcha_id,
        "slide_x_raw": slide_x_raw,
        "slide_x_display": slide_x_display,
        "bg_raw_size": [bg_raw_width, bg_raw_height],
        "bg_display_size": [bg_display_width, bg_raw_height // 2],
    }, subdir)

    return {
        "captcha_id": captcha_id,
        "slide_x_raw": slide_x_raw,
        "slide_x_display": slide_x_display,
        "captcha_info": captcha_info,
    }


def test_full_flow(token: str, auto_verify: bool = True):
    """测试完整的滑动验证码流程。"""
    from smu_badminton.booking_api import (
        gen_slide_captcha,
        check_slide_captcha,
        solve_and_verify_slide_captcha,
    )

    logger.info("=" * 60)
    logger.info("测试完整滑动验证码流程")
    logger.info("=" * 60)

    # 方式 1: 使用封装好的完整流程
    logger.info("方式 1: 使用 solve_and_verify_slide_captcha...")
    result = solve_and_verify_slide_captcha(token)

    if result:
        captcha_id, captcha_code = result
        logger.info(f"✅ 验证成功!")
        logger.info(f"captcha_id: {captcha_id}")
        logger.info(f"captcha_code: {captcha_code}")
    else:
        logger.error("❌ 验证失败")

    return result


def test_trajectory_generation():
    """测试轨迹生成算法。"""
    from smu_badminton.booking_api import _generate_track_list

    logger.info("=" * 60)
    logger.info("测试轨迹生成算法")
    logger.info("=" * 60)

    # 测试不同距离
    test_distances = [50, 100, 150, 200, 250]

    for slide_x in test_distances:
        logger.info(f"\n--- 测试距离: {slide_x}px ---")
        track = _generate_track_list(slide_x, bg_width=300, bg_height=180)

        # 分析轨迹
        move_events = [e for e in track if e["type"] == "move"]
        down_event = [e for e in track if e["type"] == "down"][0]
        up_event = [e for e in track if e["type"] == "up"][0]

        total_time = up_event["t"] - down_event["t"]
        x_positions = [e["x"] for e in move_events]
        y_drifts = [e["y"] for e in move_events]

        # 计算最大 X 和回弹
        max_x = max(e["x"] for e in track)
        final_x = up_event["x"]
        bounce = max_x - final_x

        logger.info(f"轨迹点数: {len(track)}")
        logger.info(f"总耗时: {total_time}ms")
        logger.info(f"最大 X: {max_x}, 最终 X: {final_x}")
        logger.info(f"回弹: {bounce} 像素" if bounce > 0 else "无回弹")
        logger.info(f"Y 方向漂移: {min(y_drifts)} ~ {max(y_drifts)}")

        # 检查是否到达目标
        if abs(final_x - slide_x) <= 2:
            logger.info(f"✅ 正确到达目标位置")
        else:
            logger.warning(f"⚠️ 未正确到达目标: 期望 {slide_x}, 实际 {final_x}")

        # 检查回弹比例（真实数据约 35-50%）
        bounce_ratio = bounce / slide_x if slide_x > 0 else 0
        if 0.3 <= bounce_ratio <= 0.55:
            logger.info(f"✅ 回弹比例正常: {bounce_ratio:.1%}")
        elif bounce > 0:
            logger.warning(f"⚠️ 回弹比例异常: {bounce_ratio:.1%}")

        # 保存轨迹数据
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_debug_json(f"track_{slide_x}px.json", {
            "slide_x": slide_x,
            "total_time_ms": total_time,
            "max_x": max_x,
            "final_x": final_x,
            "bounce": bounce,
            "bounce_ratio": bounce_ratio,
            "track": track,
        }, "tracks")


def main():
    parser = argparse.ArgumentParser(description="滑动验证码测试工具")
    parser.add_argument("--test-detect", action="store_true", help="测试缺口检测（使用模拟图片）")
    parser.add_argument("--test-detect-real", action="store_true", help="测试缺口检测（使用真实验证码，需要 token）")
    parser.add_argument("--test-full", action="store_true", help="测试完整验证流程")
    parser.add_argument("--test-track", action="store_true", help="测试轨迹生成")
    parser.add_argument("--token", type=str, help="访问令牌")
    parser.add_argument("--debug", action="store_true", help="调试模式（保存图片）")

    args = parser.parse_args()

    if not any([args.test_detect, args.test_detect_real, args.test_full, args.test_track]):
        parser.print_help()
        print("\n示例用法:")
        print("  python -m smu_badminton.test_slide_captcha --test-detect")
        print("  python -m smu_badminton.test_slide_captcha --test-track")
        print("  python -m smu_badminton.test_slide_captcha --test-full --token YOUR_TOKEN")
        return

    if args.debug:
        logger.setLevel(logging.DEBUG)
        ensure_debug_dir()
        logger.info(f"调试输出目录: {DEBUG_DIR}")

    try:
        if args.test_detect:
            test_gap_detection_with_sample()

        if args.test_detect_real:
            if not args.token:
                logger.error("--test-detect-real 需要 --token 参数")
                return
            test_gap_detection_with_real_captcha(args.token)

        if args.test_track:
            test_trajectory_generation()

        if args.test_full:
            if not args.token:
                logger.error("--test-full 需要 --token 参数")
                return
            test_full_flow(args.token)

    except KeyboardInterrupt:
        logger.info("\n用户中断")
    except Exception as e:
        logger.exception(f"测试异常: {e}")


if __name__ == "__main__":
    main()
