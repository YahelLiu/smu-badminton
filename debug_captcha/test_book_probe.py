"""端到端验证: 服务端是否接受我们 AES 加密后的 captchaCode（"坏 slot 探针"）。

目的
====
离线已用 openssl 逐字节证明 booking_api.encrypt_captcha_code 复现了 SPA 的 w()
(标准 AES-128-CBC, 见 debug_captcha/test_encrypt.py)。本脚本回答最后一个问题:
**真实服务端在 saveAppointmentInformationAll 里, 到底接不接受我们加密后的 captchaCode?**

方法(坏 slot, 不真下单)
=======================
自动取一个真实 court + 时段(只读 query), 并用【该时段所属的日期】发
saveAppointmentInformationAll mutation(消除过去日期 + 未来 time_id 错配,
  否则服务端按 (time_id, date) 查不到时段 -> 抛「系统异常」, 淹没验证码信号)。
优先选 available_number==0 的【满档】court: 满档必然被业务层拒 -> 不会生成预约、零配额风险;
无满档时回退到有空档的 court(若 captcha 被接受且 slot 可约, 【可能真下单】, 见下方安全节)。
也可 --bookdate 2099-12-31, 但须配 --resource-id/--time-id 且与该日期一致, 否则报「系统异常」。
我们只关心: 服务端在拒绝之前, 是先校验验证码(captchaId+captchaCode), 还是先校验日期/资源。

  - A 发(默认): captchaCode 走 encrypt_captcha_code (加密, 与 SPA 一致)。
  - --control 再发一发 B: captchaCode 原样直传(明文, 不加密), 用一个【全新】验证码会话。
    (验证码单次有效, 故 A/B 各自 gen->solve->check 取自己的 captchaId+captchaCode。)

判读
====
  A=非验证码错误, B=验证码错误        => ✓✓ 服务端强制加密, 且我们的加密被接受(明文被拒)。
  A=验证码错误                        => ✗ 加密可能有问题 / 验证码会话异常, 排查。
  A=非验证码错误(无 --control)         => ✓ 加密大概率被接受; 加 --control 可坐实"服务端是否强制加密"。
  A=成功                              => ⚠ 过去日期竟成功 -> 可能已生成预约/烧配额! 立即去系统核对并取消。
  A/B 都非验证码错误                   => ? 服务端先校验日期/资源, 没到验证码层; 信号不足,
                                          建议用真实可约日期 + 满档/坏 slot 再测, 或检查 resource_id。

安全 / 配额风险(重要!)
======================
- 默认是【dry-run】: 只跑 gen->solve->check->加密 并打印, 不发 mutation(checkCaptcha 零副作用)。
- 加 --fire 才真正发 saveAppointmentInformationAll。--control 隐含 --fire(再发一发 B, 风险翻倍)。
- 默认用满档 slot + 其所属日期 -> 业务层必拒, 不生成预约; 但若服务端把【任意 mutation 尝试】都计入
  每日 1 次配额, 即便失败也可能烧配额。无满档而回退到可约 slot 时, 若 captcha 被接受【可能真下单】。
  **配额风险完全由你掌控, 你自己挑时间跑; 真下单了请立即去系统取消。**
- 只自动化本人合法预约。绝不碰 .env/env 找凭据(确认无); token 经 --token 或 --username/--password 给。

用法
====
  # 1) dry-run: 验证 captcha+加密 pipeline 活着(零 mutation 风险)
  python debug_captcha/test_book_probe.py --token ACCESS_TOKEN
  python debug_captcha/test_book_probe.py --username 2025xxxx --password xxxx

  # 2) 实发一发 A(加密, 过去日期):
  python debug_captcha/test_book_probe.py --token ACCESS_TOKEN --fire

  # 3) A/B 对照(再发一发 B 明文), 坐实服务端是否强制加密:
  python debug_captcha/test_book_probe.py --token ACCESS_TOKEN --control

  # 自带真实 resource_id/time_id(从浏览器/真实预约抓的), 跳过自动取资源:
  python debug_captcha/test_book_probe.py --token X --fire --resource-id <rid> --time-id <tid>
  # 想用远期不可约日期而非过去日期(更不可能生成预约):
  python debug_captcha/test_book_probe.py --token X --fire --bookdate 2099-12-31
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Tuple

import requests

# 复用项目源码 + 同目录的 test_captcha_expiry 辅助
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent / "src"))   # src/
sys.path.insert(0, str(_HERE))                   # debug_captcha/

from smu_badminton.booking_api import (  # noqa: E402
    make_appointment,
    encrypt_captcha_code,
    build_headers,
    _graphql_url,
    BADMINTON_TYPE_ID,
)
from smu_badminton.slide_captcha import solve_slide_captcha  # noqa: E402
from test_captcha_expiry import (  # noqa: E402
    http_gen,
    http_check,
    extract_captcha_id,
    gen_human_track,
    BG_W,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("book-probe")

_BEIJING_TZ = timezone(timedelta(hours=8))

# 响应里命中即判"验证码相关错误"的关键词(只扫响应, mutation 的 selection set 不含
# captchaId/captchaCode 字段名, 故整串扫不会误伤字段名)。
CAPTCHA_KW = (
    "验证码", "校验码", "captcha", "滑块", "sliding",
    "验证失败", "图形验证", "人机", "verify fail",
)

# make_appointment 自己产生的错误 code(非服务端业务错误)
_MAKE_APPT_LOCAL_FAIL = {"REQUEST_FAILED", "INVALID_RESPONSE", "USER_INFO_UNAVAILABLE"}

# findResourcesAllByAccount 查询(只读 query, 零副作用; 镜像 booking_api.fetch_resource_time_id)
_LIST_QUERY = (
    "query findResourcesAllByAccount($first: Int, $offset: Int, $typeId: String, "
    "$typeName: String, $resourceName: String, $bookDate: String, $bookStartTime: String, "
    "$bookEndTime: String, $item_name: [String], $is_cyclicity: String, "
    "$cyclicity_start_date: String, $cyclicity_end_date: String, $cyclicity_start_time: String, "
    "$cyclicity_end_time: String, $cyclicity_strategy: String, $cyclicity_weekList: [String], "
    "$cyclicity_dayList: [String], $order_by: String, $cur_language: String, "
    "$filter: ResourcesFilterMap) { findResourcesAllByAccount(first: $first, offset: $offset, "
    "typeId: $typeId, typeName: $typeName, resourceName: $resourceName, bookDate: $bookDate, "
    "bookStartTime: $bookStartTime, bookEndTime: $bookEndTime, item_name: $item_name, "
    "is_cyclicity: $is_cyclicity, cyclicity_start_date: $cyclicity_start_date, "
    "cyclicity_end_date: $cyclicity_end_date, cyclicity_start_time: $cyclicity_start_time, "
    "cyclicity_end_time: $cyclicity_end_time, cyclicity_strategy: $cyclicity_strategy, "
    "cyclicity_weekList: $cyclicity_weekList, cyclicity_dayList: $cyclicity_dayList, "
    "order_by: $order_by, cur_language: $cur_language, filter: $filter) "
    "{ id resources_name open_captcha_verify capacity available_number "
    "resourcesTimeSlot { id kssj jssj } } }"
)


# ============= 取 token =============
def obtain_tokens(args) -> Tuple[Optional[str], Optional[str]]:
    """返回 (access_token, id_token)。--token 直给; --username/--password 自动 CAS 登录。"""
    if args.token:
        return args.token, (args.id_token or "")
    if args.username and args.password:
        from smu_badminton.cas_manager import get_token_cached
        from smu_badminton.config import CAS_LOGIN_URL, CAS_CAPTCHA_URL
        logger.info("自动 CAS 登录 (username=%s) ...", args.username)
        tokens = get_token_cached(CAS_LOGIN_URL, CAS_CAPTCHA_URL,
                                  args.username, args.password)
        if not tokens or not tokens.get("access_token"):
            logger.error("登录失败。")
            return None, None
        at = tokens["access_token"]
        it = tokens.get("id_token", "")
        logger.info("登录成功, access_token=%s…  id_token=%s",
                    str(at)[:12], "有" if it else "无")
        return at, it
    return None, None


# ============= 取一个真实 resource_id + time_id(只读, 零副作用) =============
def _list_resources(session: requests.Session, token: str, id_token: str,
                    bookdate: str) -> list:
    """POST findResourcesAllByAccount(只读 query), 返回资源列表; 失败返回 []。"""
    payload = {
        "operationName": "findResourcesAllByAccount",
        "variables": {
            "typeId": BADMINTON_TYPE_ID,
            "bookDate": bookdate,
            "bookStartTime": "",
            "bookEndTime": "",
            "item_name": [],
            "resourceName": "",
            "account": "",
            "cur_language": "zh",
            "order_by": "",
            "filter": {
                "campus_code": {"eq": ""},
                "building_code": {"eq": ""},
                "floor_code": {"eq": ""},
                "need_approve": {"eq": None},
            },
        },
        "query": _LIST_QUERY,
    }
    try:
        resp = session.post(_graphql_url(id_token), headers=build_headers(token),
                            json=payload, timeout=15)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data.get("data", {}).get("findResourcesAllByAccount") or []
    except Exception as e:  # noqa: BLE001
        logger.warning("list_resources 异常(%s): %s", bookdate, e)
        return []


def pick_resource(token: str, id_token: str,
                  want_kssj: str, want_jssj: str
                  ) -> Optional[Tuple[str, str, str, str, str, bool]]:
    """从今天+1..+7(北京)逐个查, 取一个 court + 时段。

    只读 query, 零副作用。返回 (resource_id, time_id, kssj, jssj, date, is_full);
    全无则 None。

    优先选 available_number==0 的【满档】court: 日期真实且 time_id 与该日期一致
    (消除过去日期 + 未来 time_id 的错配), 同时满档必然被业务层拒 -> 不会生成预约,
    零配额风险。无满档时回退到首个有空档的 court(若 captcha 被接受且 slot 可约,
    可能真下单, 见 main() 警告)。
    """
    session = requests.Session()
    today = datetime.now(_BEIJING_TZ).date()

    def _choose_slot(slots):
        if want_kssj and want_jssj:
            for s in slots:
                if s.get("kssj") == want_kssj and s.get("jssj") == want_jssj:
                    return s
        return slots[0]

    for off in range(1, 8):
        d = (today + timedelta(days=off)).strftime("%Y-%m-%d")
        resources = _list_resources(session, token, id_token, d)
        if not resources:
            continue
        # 第一遍: 找满档(available_number==0)的 court -> 安全(不会下单)
        for r in resources:
            rid = r.get("id")
            slots = r.get("resourcesTimeSlot") or []
            if not rid or not slots:
                continue
            avail = r.get("available_number")
            if avail not in (0, "0"):
                continue
            chosen = _choose_slot(slots)
            tid = chosen.get("id")
            if tid:
                logger.info("取到【满档】资源(零下单风险): date=%s rid=%s tid=%s "
                            "kssj=%s jssj=%s available=%s",
                            d, str(rid)[:16], str(tid)[:16],
                            chosen.get("kssj"), chosen.get("jssj"), avail)
                return (rid, tid, chosen.get("kssj", ""), chosen.get("jssj", ""),
                        d, True)
        # 第二遍: 回退到任意有空档的 court(可能真下单!)
        for r in resources:
            rid = r.get("id")
            slots = r.get("resourcesTimeSlot") or []
            if not rid or not slots:
                continue
            chosen = _choose_slot(slots)
            tid = chosen.get("id")
            if tid:
                logger.warning("无满档资源, 回退到有空档 court(可能真下单!): "
                               "date=%s rid=%s tid=%s kssj=%s jssj=%s available=%s",
                               d, str(rid)[:16], str(tid)[:16],
                               chosen.get("kssj"), chosen.get("jssj"),
                               r.get("available_number"))
                return (rid, tid, chosen.get("kssj", ""), chosen.get("jssj", ""),
                        d, False)
    return None


# ============= 验证码 cycle: gen -> solve -> check, 取 (captchaId, raw captchaCode) =============
def obtain_captcha(token: str, max_tries: int = 3) -> Optional[Tuple[str, str]]:
    """跑一个全新验证码会话, 返回 (captchaId, raw captchaCode uuid)。

    checkCaptcha 零副作用(不下单)。最多重试 max_tries 次, 每次用全新 captchaId。
    """
    session = requests.Session()
    for i in range(max_tries):
        gen_resp = http_gen(session, token)
        gen_ts = time.time()
        cid, cap = extract_captcha_id(gen_resp)
        if not cid:
            logger.warning("[captcha %d] gen 失败: %s", i,
                           json.dumps(gen_resp, ensure_ascii=False)[:200])
            if "401" in str(gen_resp) or "token" in str(gen_resp).lower():
                logger.error("疑似 token 失效, 终止。")
                return None
            time.sleep(1.0)
            continue

        bg = cap.get("backgroundImage", "") if isinstance(cap, dict) else ""
        tpl = cap.get("templateImage", "") if isinstance(cap, dict) else ""
        raw_w = (cap.get("backgroundImageWidth") or 600) if isinstance(cap, dict) else 600
        gap = solve_slide_captcha(bg, tpl, debug=False) if bg and tpl else None
        if gap is None:
            logger.warning("[captcha %d] OpenCV 解缺口失败, 重试", i)
            time.sleep(1.0)
            continue
        target_x = int(gap * BG_W / raw_w)

        elapsed = (time.time() - gen_ts) * 1000
        up_t = max(int(round(elapsed)), 2000)
        track = gen_human_track(target_x, up_t_ms=up_t, seed=i * 11 + 3)
        resp = http_check(session, token, cid, track, gen_ts)

        code = resp.get("code")
        success = resp.get("success")
        data = resp.get("data") or {}
        ccode = data.get("captchaCode", "") if isinstance(data, dict) else ""
        # 发往 booking mutation 的 captchaId 取 checkCaptcha 响应的 data.captchaId
        # (无 SLIDER_ 前缀的 uuid, 与 SPA window["captcha_id"] 及生产
        #  solve_and_verify_slide_captcha 一致)。gen 的 id(带 SLIDER_ 前缀) 只用于
        #  调用 checkCaptcha 端点, 不进 mutation; 否则服务端按带前缀 id 查不到会话
        #  -> 抛「系统异常」。缺失时退化为 gen id 去 SLIDER_ 前缀(与加密派生一致)。
        ccid = (data.get("captchaId", "") if isinstance(data, dict) else "") or (
            cid[len("SLIDER_"):] if cid.startswith("SLIDER_") else cid)
        if (success is True) or (code in (200, "200") and ccode):
            logger.info("[captcha %d] 成功: gen_id=%s -> mutation captchaId=%s "
                        "captchaCode=%s (len=%d)",
                        i, str(cid)[:20], str(ccid)[:20], str(ccode)[:20],
                        len(str(ccode)))
            return str(ccid), str(ccode)
        logger.warning("[captcha %d] check 未成功: code=%s msg=%r -> 重试",
                       i, code, resp.get("msg"))
        time.sleep(1.0)
    logger.error("验证码 cycle %d 次均失败, 放弃。", max_tries)
    return None


# ============= 响应分类 =============
def classify_booking(resp) -> Tuple[str, str]:
    """把 make_appointment 的返回分类。

    返回 (key, detail), key ∈
      {SUCCESS, CAPTCHA_ERROR, NON_CAPTCHA_ERROR, REQUEST_FAILED, UNCLEAR}
    """
    if not isinstance(resp, dict):
        return "UNCLEAR", f"非 dict: {str(resp)[:200]}"

    # make_appointment 自己产生的本地失败(没到服务端业务层)
    code = resp.get("code")
    if code in _MAKE_APPT_LOCAL_FAIL:
        msgs = resp.get("messages") or []
        return "REQUEST_FAILED", f"code={code} messages={msgs}"

    # GraphQL errors(整批失败)
    errs = resp.get("errors")
    if isinstance(errs, list) and errs:
        msg = " ; ".join(str(e.get("message", e)) for e in errs if isinstance(e, dict))
        low = msg.lower()
        if any(kw in msg or kw in low for kw in CAPTCHA_KW):
            return "CAPTCHA_ERROR", f"graphql errors: {msg[:200]}"
        return "NON_CAPTCHA_ERROR", f"graphql errors: {msg[:200]}"

    # 走 data.saveAppointmentInformationAll 内层(GraphQL 正常包裹)
    inner = resp
    data = resp.get("data")
    if isinstance(data, dict):
        sub = data.get("saveAppointmentInformationAll")
        if isinstance(sub, dict):
            inner = sub

    code = inner.get("code")
    msgs = inner.get("messages") or []
    name = inner.get("name") or ""
    appt_id = inner.get("appointmentId") or inner.get("ids")

    # 成功(cas_manager 判据: code in success/0); 过去日期不该成功
    if code in ("success", "0", 0):
        return "SUCCESS", f"code={code} name={name!r} appointmentId={appt_id} messages={msgs}"

    # 验证码关键词命中 -> 验证码错误(加密错 / 会话失效 / 单次性)
    blob = json.dumps(
        {"code": code, "name": name, "messages": msgs,
         "messages_en": inner.get("messages_en"), "msg": resp.get("msg")},
        ensure_ascii=False,
    )
    low = blob.lower()
    if any(kw in blob or kw in low for kw in CAPTCHA_KW):
        return "CAPTCHA_ERROR", f"code={code} name={name!r} messages={msgs}"

    # 非成功且无验证码关键词 -> 认为验证码被接受, 卡在别的字段(日期/资源/容量)
    return "NON_CAPTCHA_ERROR", f"code={code} name={name!r} messages={msgs}"


# ============= 单发: 取全新验证码 -> (加密/明文) -> 过去日期 mutation =============
def fire_shot(token: str, id_token: str, resource_id: str, time_id: str,
              bookdate: str, kssj: str, jssj: str, *, encrypt: bool,
              label: str) -> dict:
    """取一个全新验证码会话 -> captchaCode(encrypt? 加密 : 原样) -> make_appointment。"""
    logger.info("=" * 64)
    logger.info("[%s] 取全新验证码会话 ...", label)
    got = obtain_captcha(token)
    if not got:
        logger.error("[%s] 验证码 cycle 失败, 无法发 mutation。", label)
        return {"label": label, "key": "REQUEST_FAILED",
                "detail": "captcha cycle 失败", "raw": None}
    cid, raw_code = got

    if encrypt:
        code_used = encrypt_captcha_code(cid, raw_code)
        form = "加密(AES-128-CBC base64, 同 SPA w())"
    else:
        code_used = raw_code
        form = "明文(原样直传, 对照组)"
    logger.info("[%s] captchaCode 形态: %s", label, form)
    logger.info("[%s] captchaId=%s code(raw)=%s code(used)=%s",
                label, str(cid)[:24], raw_code[:24], str(code_used)[:24])

    logger.info("[%s] 发 saveAppointmentInformationAll(过去/坏日期=%s, "
                "resource_id=%s, time_id=%s, %s-%s) ...",
                label, bookdate, str(resource_id)[:16], str(time_id)[:16], kssj, jssj)
    resp = make_appointment(
        token, time_id, resource_id, bookdate, kssj, jssj,
        id_token=id_token, captcha_id=cid, captcha_code=code_used,
    )
    key, detail = classify_booking(resp)
    logger.info("[%s] 分类: %s  %s", label, key, detail)
    logger.info("[%s] 原始响应: %s", label,
                json.dumps(resp, ensure_ascii=False)[:500] if resp else "(None)")
    return {"label": label, "key": key, "detail": detail,
            "raw": resp, "captcha_id": cid,
            "code_used_form": form, "code_used": code_used}


# ============= 主 =============
def main():
    ap = argparse.ArgumentParser(
        description="坏 slot 探针: 验证服务端是否接受加密后的 captchaCode",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--token", help="access_token(方式 A)")
    g.add_argument("--username", help="学号(方式 B, 自动 CAS 登录)")
    ap.add_argument("--password", help="密码(方式 B)")
    ap.add_argument("--id-token", default="", help="可选 id_token; --username 登录会自动带")
    ap.add_argument("--fire", action="store_true",
                    help="真正发 saveAppointmentInformationAll(过去日期); 默认 dry-run 不发")
    ap.add_argument("--control", action="store_true",
                    help="再加发一发 B(明文 captchaCode) 做 A/B 对照(隐含 --fire, 双发风险翻倍)")
    ap.add_argument("--bookdate", default="",
                    help="预约日期 YYYY-MM-DD; 默认昨天(过去日期, 必被拒)。可给 2099-12-31 等")
    ap.add_argument("--resource-id", default="", help="自带真实 resource_id(跳过自动取)")
    ap.add_argument("--time-id", default="", help="自带真实 time_id(跳过自动取)")
    ap.add_argument("--kssj", default="", help="开始时间 HH:MM; 不给则用自动取到的时段")
    ap.add_argument("--jssj", default="", help="结束时间 HH:MM; 不给则用自动取到的时段")
    args = ap.parse_args()

    if args.username and not args.password:
        ap.error("--username 需要 --password")
    if not args.token and not (args.username and args.password):
        ap.error("必须提供 --token 或 (--username + --password)")

    token, id_token = obtain_tokens(args)
    if not token:
        ap.error("取 token 失败")

    # resource_id / time_id / kssj / jssj: 优先用户给的; 否则自动取(只读)
    resource_id = args.resource_id
    time_id = args.time_id
    kssj = args.kssj
    jssj = args.jssj
    picked_date = None
    picked_full = False
    if not (resource_id and time_id):
        logger.info("未给完整 resource_id/time_id, 自动取真实值(只读 query)...")
        picked = pick_resource(token, id_token, args.kssj, args.jssj)
        if picked:
            rid, tid, pk_kssj, pk_jssj, picked_date, picked_full = picked
            if not resource_id:
                resource_id = rid
            if not time_id:
                time_id = tid
            if not kssj:
                kssj = pk_kssj
            if not jssj:
                jssj = pk_jssj
        else:
            logger.warning("自动取资源失败(预约窗口可能未开/无资源)。"
                           "将用占位值, 服务端可能先以「资源不存在」拒绝 -> 信号不足;"
                           "建议加 --resource-id/--time-id(从浏览器抓真实值)再测。")
            resource_id = resource_id or "PROBE-FAKE-RESOURCE"
            time_id = time_id or "PROBE-FAKE-TIME"
            kssj = kssj or "20:00"
            jssj = jssj or "21:00"

    # 日期: 优先用户给的 --bookdate; 否则用自动取到的资源日期(time_id 与该日期
    #  一致, 消除"过去日期 + 未来 time_id"错配); 都没有则退回昨天(过去日期)。
    bookdate = args.bookdate or picked_date or (
        datetime.now(_BEIJING_TZ) - timedelta(days=1)
    ).strftime("%Y-%m-%d")

    if picked_date and args.bookdate and args.bookdate != picked_date:
        logger.warning("你给的 --bookdate=%s 与自动取到 time_id 的日期=%s 不一致 -> "
                       "时段在该日期可能不存在, 服务端可能回「系统异常」而非验证码信号。"
                       "建议: 要么别给 --bookdate(用自动日期), 要么连 --resource-id/"
                       "--time-id 一起给(且与该日期一致)。",
                       args.bookdate, picked_date)

    logger.info("=" * 64)
    logger.info("探针参数: bookdate=%s  resource_id=%s  time_id=%s  %s-%s  id_token=%s",
                bookdate, str(resource_id)[:24], str(time_id)[:24], kssj, jssj,
                "有" if id_token else "无")

    # ---- dry-run: 只跑验证码+加密, 不发 mutation ----
    if not args.fire and not args.control:
        logger.info("[dry-run] 取一个验证码会话 -> 加密 -> 打印(不发 mutation, 零配额风险)")
        got = obtain_captcha(token)
        if not got:
            logger.error("验证码 cycle 失败; 先确认 token 有效、网络通畅。")
            return
        cid, raw_code = got
        enc = encrypt_captcha_code(cid, raw_code)
        logger.info("captchaId        = %s", cid)
        logger.info("captchaCode(raw) = %s  (len=%d)", raw_code, len(raw_code))
        logger.info("captchaCode(enc) = %s", enc)
        logger.info("加密生效? %s", "是(已变 base64, != 原样)" if enc and enc != raw_code else "否(可能缺 pycryptodome, 回退原样)")
        logger.info("将发 mutation(加 --fire 实发): bookdate=%s rid=%s tid=%s %s-%s",
                    bookdate, str(resource_id)[:16], str(time_id)[:16], kssj, jssj)
        logger.info("dry-run 结束。加 --fire 实发一发(加密); 加 --control 做 A/B 对照。")
        return

    # ---- 实发 ----
    fire = args.control or args.fire  # --control 隐含 --fire
    if picked_full:
        logger.info("⚠ 实发 mutation: bookdate=%s(满档 slot, 零下单风险)。"
                    " 若服务端把失败尝试也计入每日配额, 仍可能烧配额; 你已知晓并自行承担。",
                    bookdate)
    else:
        logger.warning("⚠ 实发 mutation: bookdate=%s。该 slot 非满档 -> 若 captcha 被接受"
                        " 且 slot 可约, 【可能真生成预约/烧当日配额】! "
                        "建议先确认能否在系统取消, 或换满档 slot 再测。你已知晓并自行承担。",
                        bookdate)

    a = fire_shot(token, id_token, resource_id, time_id, bookdate, kssj, jssj,
                  encrypt=True, label="A(加密)")

    b = None
    if args.control:
        b = fire_shot(token, id_token, resource_id, time_id, bookdate, kssj, jssj,
                      encrypt=False, label="B(明文-对照)")

    # ---- 判读 ----
    logger.info("=" * 64)
    logger.info("判读:")
    if b is None:
        # 单发 A
        k = a["key"]
        if k == "NON_CAPTCHA_ERROR":
            logger.info("  A=非验证码错误 => ✓ 加密的 captchaCode 大概率被服务端接受。")
            logger.info("  (未做对照; 加 --control 可坐实「服务端是否强制加密: 明文应被判验证码错误」)")
        elif k == "CAPTCHA_ERROR":
            logger.info("  A=验证码错误 => ✗ 加密可能有问题, 或验证码会话 TTL/单次性异常。")
            logger.info("  排查: debug_captcha/test_encrypt.py 复核加密; show_captcha_code.py 复核会话。")
        elif k == "SUCCESS":
            logger.warning("  A=成功(过去日期) => ⚠ 意外! 可能已生成预约/烧配额! 立即去系统核对并取消。")
        elif k == "REQUEST_FAILED":
            logger.info("  A=请求失败(token/user_info/网络), 排查后再试。")
        else:
            logger.info("  A=%s => 无法判定, 看原始响应。", k)
    else:
        ak, bk = a["key"], b["key"]
        if ak == "NON_CAPTCHA_ERROR" and bk == "CAPTCHA_ERROR":
            logger.info("  A(加密)=非验证码错误, B(明文)=验证码错误")
            logger.info("  => ✓✓ 服务端强制加密 captchaCode, 且我们的 AES 加密被接受(明文被拒)。加密复现正确。")
        elif ak == "CAPTCHA_ERROR":
            logger.info("  A(加密)=验证码错误 => ✗ 加密可能有问题/会话异常。")
            logger.info("  (即使 B 也错, 也先查 A: 用 test_encrypt.py 复核加密, show_captcha_code.py 复核会话。)")
        elif ak == "SUCCESS":
            logger.warning("  A(加密, 过去日期)=成功 => ⚠ 意外! 可能已生成预约/烧配额! 立即去系统核对并取消。")
        elif ak in ("NON_CAPTCHA_ERROR",) and bk in ("NON_CAPTCHA_ERROR", "SUCCESS"):
            logger.info("  A/B 都非验证码错误 => ? 服务端先校验日期/资源, 没到验证码层; 信号不足。")
            logger.info("  建议: 用真实可约日期 + 满档/坏 slot 再测, 或确认 resource_id/time_id 真实。")
        elif ak == "REQUEST_FAILED":
            logger.info("  A=请求失败(token/user_info/网络), 排查后再试。")
        else:
            logger.info("  A=%s, B=%s => 无法判定, 看原始响应。", ak, bk)

    # 落盘完整结果
    out = _HERE / "book_probe_result.json"
    out.write_text(json.dumps({"A": a, "B": b, "bookdate": bookdate,
                               "resource_id": resource_id, "time_id": time_id,
                               "kssj": kssj, "jssj": jssj},
                              ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")
    logger.info("完整结果已写入: %s", out)


if __name__ == "__main__":
    main()
