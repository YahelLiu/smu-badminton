"""
滑块验证码缺口识别模块。

主方法（可靠）: alpha 边缘匹配
  - 用模板 alpha 通道精确定位拼图块 bbox（findNonZero(boundingRect)），
    裁出块，对块边缘与背景边缘做 matchTemplate，gap_x = max_loc[0]（无 offset）。
  - 试 3 种边缘变体（整块边缘 / alpha 轮廓 / alpha 掩膜内边缘），取最高置信度。
退化方法（fallback，主方法失败或置信度过低时）: 4 种旧算法加权投票。
  - 已修复 += tpl_contour[0] 重复计数 bug（max_loc[0] 即缺口左缘，不应再加块内偏移）。

返回值: 缺口 X 坐标（背景图【原始】像素坐标，如 600 宽）。调用方按显示宽度映射。
"""
import base64
import logging
from typing import Optional, Tuple, List
import cv2
import numpy as np
import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 图片加载
# ---------------------------------------------------------------------------
def _load_image(image_data: str, unchanged: bool = False) -> Optional[np.ndarray]:
    """加载图片，支持 base64、data URI 和 URL 格式。

    unchanged=True 保留 alpha 通道（IMREAD_UNCHANGED），用于带透明度的拼图小块。
    """
    if image_data.startswith("http://") or image_data.startswith("https://"):
        return _download_image(image_data, unchanged=unchanged)
    return decode_base64_image(image_data, unchanged=unchanged)


def _download_image(url: str, unchanged: bool = False) -> Optional[np.ndarray]:
    """下载图片并解码为 OpenCV 格式。"""
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            logger.error("下载图片失败: status=%d, url=%s", resp.status_code, url[:80])
            return None
        img_array = np.frombuffer(resp.content, dtype=np.uint8)
        flags = cv2.IMREAD_UNCHANGED if unchanged else cv2.IMREAD_COLOR
        img = cv2.imdecode(img_array, flags)
        if img is None:
            logger.error("图片解码失败: url=%s", url[:80])
        return img
    except Exception as e:
        logger.error("下载图片异常: %s, url=%s", e, url[:80])
        return None


def decode_base64_image(b64_str: str, unchanged: bool = False) -> Optional[np.ndarray]:
    """解码 base64 图片字符串（支持 data URI 格式）。

    unchanged=True 时用 IMREAD_UNCHANGED 保留 alpha 通道。
    """
    try:
        if "," in b64_str:
            b64_str = b64_str.split(",", 1)[1]
        img_bytes = base64.b64decode(b64_str)
        img_array = np.frombuffer(img_bytes, dtype=np.uint8)
        flags = cv2.IMREAD_UNCHANGED if unchanged else cv2.IMREAD_COLOR
        return cv2.imdecode(img_array, flags)
    except Exception as e:
        logger.error("解码 base64 图片失败: %s", e)
        return None


# ---------------------------------------------------------------------------
# 公开接口
# ---------------------------------------------------------------------------
def solve_slide_captcha(
    background_b64: str,
    template_b64: str,
    debug: bool = False
) -> Optional[int]:
    """识别滑块验证码缺口 X 坐标（背景图原始像素坐标）。

    Args:
        background_b64: 背景图 base64（JPEG，无 alpha -> BGR）
        template_b64: 拼图小块 base64（PNG，含 alpha -> BGRA）
        debug: 是否保存调试图片 / 打印各方法结果
    Returns:
        缺口 X 坐标（像素），失败返回 None
    """
    bg_img = _load_image(background_b64)                       # BGR
    tpl_img = _load_image(template_b64, unchanged=True)        # BGRA（保留 alpha）

    if bg_img is None or tpl_img is None:
        logger.error("图片解码失败: bg=%s, tpl=%s",
                     bg_img is not None, tpl_img is not None)
        return None

    return _find_gap_position(bg_img, tpl_img, debug=debug)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _find_gap_position(
    bg_img: np.ndarray,
    tpl_img: np.ndarray,
    debug: bool = False
) -> Optional[int]:
    """主方法（alpha 边缘匹配）优先；置信度过低或失败时退回 4 方法投票。"""
    try:
        primary_x, primary_conf = _solve_alpha_edge(bg_img, tpl_img)
        if debug and primary_x is not None:
            logger.info(f"alpha 边缘匹配(主): x={primary_x}, conf={primary_conf:.3f}")

        # 主方法置信度不足 -> 退回旧 4 方法投票（诊断 + 兜底）
        if primary_x is None or primary_conf < 0.25:
            tpl_bgr = (tpl_img[:, :, :3]
                       if (tpl_img.ndim == 3 and tpl_img.shape[2] == 4)
                       else tpl_img)
            legacy: List[Tuple[int, float, str]] = []
            for fn, name in ((_method_edge_template, "edge_template"),
                             (_method_color_difference, "color_diff"),
                             (_method_gray_template, "gray_template"),
                             (_method_contour, "contour")):
                try:
                    x, c = fn(bg_img, tpl_bgr)
                except Exception as e:
                    logger.debug("%s 失败: %s", name, e)
                    x, c = None, 0.0
                if x is not None:
                    legacy.append((x, c, name))
                    if debug:
                        logger.info(f"{name}: x={x}, conf={c:.3f}")

            if legacy:
                tw = sum(c * c for _, c, _ in legacy)
                legacy_x = (int(sum(x * c * c for x, c, _ in legacy) / tw)
                            if tw > 0
                            else int(sum(x for x, _, _ in legacy) / len(legacy)))
                if debug:
                    logger.info(f"legacy 投票: x={legacy_x}")
                # 主方法完全失败才用 legacy；置信度低时仍信主方法（更可靠）
                if primary_x is None:
                    primary_x = legacy_x
                    primary_conf = -1.0

        if primary_x is None:
            logger.error("所有检测方法都失败")
            return None

        final_x = primary_x
        if debug:
            logger.info(f"最终结果: x={final_x}")
            _save_debug_marker(bg_img, final_x)

        return final_x

    except Exception as e:
        logger.error("缺口识别失败: %s", e)
        return None


def _save_debug_marker(bg_img: np.ndarray, gap_x: int) -> None:
    """在背景图上画红线标记缺口 X 并落盘。"""
    try:
        import os
        from datetime import datetime
        debug_dir = os.path.join(os.path.dirname(__file__), "..", "..", "debug_captcha")
        os.makedirs(debug_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        marked = bg_img.copy()
        cv2.line(marked, (gap_x, 0), (gap_x, marked.shape[0]), (0, 0, 255), 2)
        cv2.putText(marked, f"x={gap_x}", (gap_x + 5, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        path = os.path.join(debug_dir, f"gap_{ts}.png")
        cv2.imwrite(path, marked)
        logger.info(f"调试图片已保存: {path}")
    except Exception as e:
        logger.warning(f"保存调试图片失败: {e}")


# ---------------------------------------------------------------------------
# 主方法: alpha 边缘匹配
# ---------------------------------------------------------------------------
def _solve_alpha_edge(
    bg_img: np.ndarray,
    tpl_img: np.ndarray
) -> Tuple[Optional[int], float]:
    """用 alpha 通道精确定位拼图块，边缘 matchTemplate 找缺口。

    gap_x = max_loc[0]（裁出的块左缘 = 缺口左缘，【不加】块内偏移）。
    返回 (gap_x, 置信度)。
    """
    try:
        # 取 alpha 与 BGR
        if tpl_img.ndim == 3 and tpl_img.shape[2] == 4:
            alpha = tpl_img[:, :, 3]
            bgr = tpl_img[:, :, :3]
        else:
            alpha = None
            bgr = tpl_img

        # 拼图块 bbox
        if alpha is not None:
            pts = cv2.findNonZero(alpha)
        else:
            g = cv2.cvtColor(tpl_img, cv2.COLOR_BGR2GRAY)
            _, binf = cv2.threshold(g, 10, 255, cv2.THRESH_BINARY)
            pts = cv2.findNonZero(binf)
        if pts is None:
            return None, 0.0
        x, y, w, h = cv2.boundingRect(pts)
        if w < 10 or h < 10:
            return None, 0.0

        piece_bgr = bgr[y:y + h, x:x + w]
        piece_alpha = (alpha[y:y + h, x:x + w]
                       if alpha is not None
                       else np.full((h, w), 255, np.uint8))

        # 背景边缘
        bg_gray = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)
        bg_edge = cv2.Canny(cv2.GaussianBlur(bg_gray, (3, 3), 0), 50, 150)

        # 块灰度边缘
        piece_gray = cv2.cvtColor(piece_bgr, cv2.COLOR_BGR2GRAY)
        piece_edge = cv2.Canny(cv2.GaussianBlur(piece_gray, (3, 3), 0), 50, 150)

        # 三种边缘变体，取最高 matchTemplate 置信度
        variants: List[Tuple[str, np.ndarray]] = [("full", piece_edge)]
        if alpha is not None:
            sil = cv2.Canny(cv2.GaussianBlur(piece_alpha, (3, 3), 0), 50, 150)
            variants.append(("alpha", sil))
        mask = (piece_alpha > 0).astype(np.uint8) * 255
        variants.append(("masked",
                         cv2.bitwise_and(piece_edge, piece_edge, mask=mask)))

        best_x, best_conf, best_name = None, -1.0, None
        bh, bw = bg_edge.shape[:2]
        for name, edge in variants:
            if edge.size == 0 or cv2.countNonZero(edge) < 8:
                continue
            eh, ew = edge.shape[:2]
            if eh > bh or ew > bw:
                continue
            res = cv2.matchTemplate(bg_edge, edge, cv2.TM_CCOEFF_NORMED)
            _, mv, _, ml = cv2.minMaxLoc(res)
            if mv > best_conf:
                best_conf = float(mv)
                best_x = int(ml[0])
                best_name = name

        if best_x is None:
            return None, 0.0
        logger.debug("alpha_edge 命中变体=%s x=%s conf=%.3f", best_name, best_x, best_conf)
        return best_x, best_conf

    except Exception as e:
        logger.debug("alpha 边缘匹配失败: %s", e)
        return None, 0.0


# ---------------------------------------------------------------------------
# 退化方法（fallback）: 以下 4 种旧算法，仅在主方法置信度不足时兜底
# 已修复 += tpl_contour[0] 重复计数 bug（max_loc[0] 即缺口左缘）。
# ---------------------------------------------------------------------------
def _method_edge_template(bg_img: np.ndarray, tpl_img: np.ndarray) -> Tuple[Optional[int], float]:
    """方法 1: Canny 边缘 + 模板匹配。"""
    try:
        bg_gray = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)
        tpl_gray = cv2.cvtColor(tpl_img, cv2.COLOR_BGR2GRAY)
        bg_edge = cv2.Canny(cv2.GaussianBlur(bg_gray, (3, 3), 0), 50, 150)
        tpl_edge = cv2.Canny(cv2.GaussianBlur(tpl_gray, (3, 3), 0), 50, 150)

        tpl_contour = _get_template_contour(tpl_edge)
        if tpl_contour is not None:
            x, y, w, h = tpl_contour
            tpl_edge_cropped = tpl_edge[y:y + h, x:x + w]
        else:
            tpl_edge_cropped = tpl_edge

        if tpl_edge_cropped.size == 0:
            return None, 0.0

        result = cv2.matchTemplate(bg_edge, tpl_edge_cropped, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        return max_loc[0], max_val  # 不加 tpl_contour[0]（已修复）
    except Exception as e:
        logger.debug("边缘模板匹配失败: %s", e)
        return None, 0.0


def _method_color_difference(bg_img: np.ndarray, tpl_img: np.ndarray) -> Tuple[Optional[int], float]:
    """方法 2: 颜色直方图相似度滑动搜索。"""
    try:
        tpl_gray = cv2.cvtColor(tpl_img, cv2.COLOR_BGR2GRAY)
        _, tpl_mask = cv2.threshold(tpl_gray, 10, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(tpl_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None, 0.0
        max_contour = max(contours, key=cv2.contourArea)
        x, y, w, h = cv2.boundingRect(max_contour)

        tpl_valid = tpl_img[y:y + h, x:x + w]
        tpl_hsv = cv2.cvtColor(tpl_valid, cv2.COLOR_BGR2HSV)
        tpl_h_hist = cv2.calcHist([tpl_hsv], [0], None, [180], [0, 180])
        tpl_s_hist = cv2.calcHist([tpl_hsv], [1], None, [256], [0, 256])
        tpl_v_hist = cv2.calcHist([tpl_hsv], [2], None, [256], [0, 256])
        cv2.normalize(tpl_h_hist, tpl_h_hist)
        cv2.normalize(tpl_s_hist, tpl_s_hist)
        cv2.normalize(tpl_v_hist, tpl_v_hist)

        bg_hsv = cv2.cvtColor(bg_img, cv2.COLOR_BGR2HSV)
        step = 10
        best_x, best_score = 0, 0.0
        for offset_x in range(0, bg_img.shape[1] - w, step):
            bg_region = bg_hsv[y:y + h, offset_x:offset_x + w]
            if bg_region.shape[0] != h or bg_region.shape[1] != w:
                continue
            bg_h_hist = cv2.calcHist([bg_region], [0], None, [180], [0, 180])
            bg_s_hist = cv2.calcHist([bg_region], [1], None, [256], [0, 256])
            bg_v_hist = cv2.calcHist([bg_region], [2], None, [256], [0, 256])
            cv2.normalize(bg_h_hist, bg_h_hist)
            cv2.normalize(bg_s_hist, bg_s_hist)
            cv2.normalize(bg_v_hist, bg_v_hist)
            score = (cv2.compareHist(tpl_h_hist, bg_h_hist, cv2.HISTCMP_CORREL)
                     + cv2.compareHist(tpl_s_hist, bg_s_hist, cv2.HISTCMP_CORREL)
                     + cv2.compareHist(tpl_v_hist, bg_v_hist, cv2.HISTCMP_CORREL)) / 3
            if score > best_score:
                best_score = score
                best_x = offset_x
        return best_x, best_score
    except Exception as e:
        logger.debug("颜色差异检测失败: %s", e)
        return None, 0.0


def _method_gray_template(bg_img: np.ndarray, tpl_img: np.ndarray) -> Tuple[Optional[int], float]:
    """方法 3: 多尺度灰度模板匹配。"""
    try:
        bg_gray = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)
        tpl_gray = cv2.cvtColor(tpl_img, cv2.COLOR_BGR2GRAY)
        tpl_contour = _get_template_contour(tpl_gray)
        if tpl_contour is not None:
            x, y, w, h = tpl_contour
            tpl_cropped = tpl_gray[y:y + h, x:x + w]
        else:
            tpl_cropped = tpl_gray
        if tpl_cropped.size == 0:
            return None, 0.0

        best_x, best_conf = 0, 0.0
        for scale in [0.9, 1.0, 1.1]:
            new_w = int(tpl_cropped.shape[1] * scale)
            new_h = int(tpl_cropped.shape[0] * scale)
            if new_w <= 0 or new_h <= 0:
                continue
            if new_w > bg_gray.shape[1] or new_h > bg_gray.shape[0]:
                continue
            tpl_scaled = cv2.resize(tpl_cropped, (new_w, new_h))
            result = cv2.matchTemplate(bg_gray, tpl_scaled, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            if max_val > best_conf:
                best_conf = max_val
                best_x = max_loc[0]  # 不加 tpl_contour[0]（已修复）
        return best_x, best_conf
    except Exception as e:
        logger.debug("灰度模板匹配失败: %s", e)
        return None, 0.0


def _method_contour(bg_img: np.ndarray, tpl_img: np.ndarray) -> Tuple[Optional[int], float]:
    """方法 4: 背景轮廓检测法。"""
    try:
        bg_gray = cv2.cvtColor(bg_img, cv2.COLOR_BGR2GRAY)
        bg_blur = cv2.GaussianBlur(bg_gray, (5, 5), 0)
        bg_thresh = cv2.adaptiveThreshold(
            bg_blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV, 11, 2)
        contours, _ = cv2.findContours(bg_thresh, cv2.RETR_TREE, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None, 0.0
        candidates = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < 500 or area > 10000:
                continue
            x, y, w, h = cv2.boundingRect(contour)
            aspect_ratio = float(w) / h if h > 0 else 0
            if 0.3 < aspect_ratio < 3:
                perimeter = cv2.arcLength(contour, True)
                if perimeter > 0:
                    compactness = 4 * np.pi * area / (perimeter ** 2)
                    candidates.append((x, y, w, h, area, compactness))
        if not candidates:
            return None, 0.0
        candidates.sort(key=lambda c: c[5], reverse=True)
        best = candidates[0]
        conf = min(best[5], 1.0) * min(best[4] / 3000, 1.0)
        return best[0], conf
    except Exception as e:
        logger.debug("轮廓检测失败: %s", e)
        return None, 0.0


def _get_template_contour(edge_img: np.ndarray) -> Optional[Tuple[int, int, int, int]]:
    """获取拼图小块的有效轮廓区域（去除透明/空白区域）。"""
    try:
        points = cv2.findNonZero(edge_img)
        if points is None:
            return None
        x, y, w, h = cv2.boundingRect(points)
        if w < 10 or h < 10:
            return None
        return x, y, w, h
    except Exception:
        return None
