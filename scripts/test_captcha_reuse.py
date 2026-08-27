"""
滑块验证码复用性与有效期实测脚本。

目的：回答两个问题
1. 一次 checkCaptcha 通过后得到的 (captchaId, captchaCode) 能否用于多次预约 mutation？
2. 这组凭证从签发到失效有多长的有效期？

安全设计：
- 探测目标优先选「已约满 (canAppointmentNumber == 0)」的时段；--date 为过去日期（如昨天/今天）
  时任何时段天然不可成约，同样安全；
- 提交前先做一次「阴性对照」：用真实 captchaId + 从未通过滑块校验的垃圾 code 提交，
  确认服务端确实先校验验证码（否则结论按不可判定处理）；
- 每次探测前后对比本人预约快照，若意外产生预约立即调用官方取消接口回滚。

判定逻辑（2026-08-27 已实测定案）：
- 阴性对照（死凭证）→「系统异常」＝服务端验证码层失败形态；
  有效凭证 → 业务层容量类错误（如「超过预约人次（1次）限制」）⇒ 服务端先验验证码；
- 同一凭证第二次提交 →「验证码不能重复使用」⇒ 【定案：一次性消费】。
  设计含义：T-0 每一枪必须自带独立凭证，预取期按并发枪数各解一份。
- 有效期：凭证本体 ≥3 分钟内仍可被服务端识别（返回的是"重复使用"而非"过期"），
  对「barrier 前预取」的时间窗口足够。

上游频控（实测）：saveAppointmentInformationAll 按账号限流，约 2 连发内安全，
第 3 次立即封禁 3 分钟。脚本从第 3 次 save 起自动等待 --gap 秒（默认 200），
全程约 3~4 分钟属正常。

用法：
    .venv/bin/python scripts/test_captcha_reuse.py [--date YYYY-MM-DD] [--delays 90,240] [--dry-run]
凭据来源：环境变量 BB_TEST_USER / BB_TEST_PASS，否则交互式输入。
"""
import argparse
import getpass
import os
import re
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from smu_badminton import config  # noqa: E402  触发 .env 加载
from smu_badminton.http_utils import requests_post_with_retry  # noqa: E402
from smu_badminton.booking_api import (  # noqa: E402
    build_headers,
    list_resources_by_account,
    find_time_slots_by_resource,
    make_appointment,
    solve_and_verify_slide_captcha,
    list_appointments_for_account,
    gen_slide_captcha,
    _bookdate_to_ms,
)
from smu_badminton.cas_manager import get_token_cached  # noqa: E402

MAX_PROBES = 5  # 全程最多向 saveAppointmentInformationAll 发起的探测次数


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _graphql(token: str, id_token: str, payload: dict) -> dict | None:
    from smu_badminton.booking_api import _graphql_url

    resp = requests_post_with_retry(
        _graphql_url(id_token), json=payload, headers=build_headers(token)
    )
    if not resp or resp.status_code != 200:
        return None
    try:
        return resp.json()
    except Exception:
        return None


def cancel_appointment(token: str, id_token: str, appointment_id: str) -> bool:
    """官方取消流程：checkAppointmentCancelTime + updateAppointmentInformationState(state=1)。"""
    check = _graphql(
        token, id_token,
        {
            "operationName": "checkAppointmentCancelTime",
            "variables": {"id": appointment_id},
            "query": "query checkAppointmentCancelTime($id: String) { checkAppointmentCancelTime(id: $id) { errcode msg msg_en } }",
        },
    )
    log(f"  取消前检查: {json_s(check)}")
    upd = _graphql(
        token, id_token,
        {
            "operationName": "updateAppointmentInformationState",
            "variables": {"id": appointment_id, "state": "1", "reason": "无", "dataSource": "1"},
            "query": 'mutation updateAppointmentInformationState($id: ID!, $state: String!, $reason: String, $dataSource: String) { updateAppointmentInformationState(id: $id, state: $state, reason: $reason, dataSource: $dataSource) { errcode msg msg_en } }',
        },
    )
    ok = isinstance(upd, dict) and upd.get("data", {}).get("updateAppointmentInformationState", {}).get("errcode") == "0"
    log(f"  取消结果: {'成功' if ok else json_s(upd)}")
    return ok


def json_s(obj) -> str:
    import json as _json
    try:
        return _json.dumps(obj, ensure_ascii=False)[:300]
    except Exception:
        return str(obj)[:300]


def pick_full_slot(tokens: dict, bookdate: str, allow_any: bool = False):
    """在指定日期找一个已约满的时段。

    allow_any=True（过去日期场景）时，找不到约满时段就退回任意一个时段——
    过去日期本身不可能成约，天然安全。

    返回 (resource_id, resources_name, kssj, jssj, time_id) 或 None。
    """
    id_token = tokens.get("id_token", "")
    resources = list_resources_by_account(tokens["access_token"], bookdate, id_token=id_token)
    if not resources:
        return None
    full = []
    for r in resources:
        rid = r.get("id")
        detail = find_time_slots_by_resource(tokens["access_token"], rid, _bookdate_to_ms(bookdate), id_token=id_token)
        if not detail or "data" not in detail:
            continue
        for s in detail["data"].get("findResourcesTimeSlotByResourcesIdAndDate") or []:
            if int(s.get("canAppointmentNumber") or 0) <= 0:
                full.append((rid, r.get("resources_name"), s.get("kssj"), s.get("jssj"), s.get("id")))
    if not full and allow_any:
        # 过去日期兜底：任取一个时段
        for r in resources:
            rid = r.get("id")
            detail = find_time_slots_by_resource(tokens["access_token"], rid, _bookdate_to_ms(bookdate), id_token=id_token)
            slots = (detail or {}).get("data", {}).get("findResourcesTimeSlotByResourcesIdAndDate") or []
            if slots:
                s = slots[len(slots) // 2]
                return (rid, r.get("resources_name"), s.get("kssj"), s.get("jssj"), s.get("id"))
    if not full:
        return None
    full.sort(key=lambda x: x[2] or "")
    return full[0]


def dead_creds(token: str) -> tuple[str, str]:
    """生成阴性对照凭证：真实 captchaId 但从未通过滑块校验，code 为垃圾值。"""
    data = gen_slide_captcha(token)
    if not data:
        return "", ""
    return data.get("id", ""), "deadbeef-junk-code"


def snapshot_appts(tokens: dict, bookdate: str) -> set:
    edges = list_appointments_for_account(tokens["access_token"], bookdate, id_token=tokens.get("id_token", ""))
    return {e.get("node", {}).get("id") for e in edges if isinstance(e, dict)}


def classify(resp) -> str:
    """将 make_appointment 的响应分类为 success / captcha_error / other / banned。"""
    if not isinstance(resp, dict):
        return "other"
    code = str(resp.get("code", ""))
    if code in ("0", "success"):
        return "success"
    text = (code + " " + " ".join(str(m) for m in resp.get("messages") or [])).lower()
    if "频繁" in text or "禁用" in text or "禁止" in text or "解禁" in text:
        return "banned"
    if "captcha" in text or "验证" in text or "slider" in text:
        return "captcha_error"
    return "other"


PROBE_COUNT = 0
_MIN_GAP_SEC = 0.0     # 第 3 次及以后的 save 探测之间的最小间隔（阶段间自动等待）
_last_save_ts = [0.0]


def probe(tokens: dict, slot: dict, creds):
    """用给定验证码凭证向目标时段发起一次预约探测，返回分类与原始响应。

    上游对 saveAppointmentInformationAll 有每账号频控（实测约 2 连发内安全，
    第 3 次触发 3 分钟封禁），故从第 3 次起强制间隔 _MIN_GAP_SEC 秒。
    """
    global PROBE_COUNT
    PROBE_COUNT += 1
    if PROBE_COUNT >= 3 and _last_save_ts[0] > 0:
        wait = _MIN_GAP_SEC - (time.time() - _last_save_ts[0])
        while wait > 0:
            log(f"  [节流] 距上游解禁还需 {wait:.0f}s，等待...")
            time.sleep(min(wait, 30))
            wait = _MIN_GAP_SEC - (time.time() - _last_save_ts[0])
    captcha_id, captcha_code = creds
    resp = make_appointment(
        tokens["access_token"], slot["time_id"], slot["resource_id"], slot["date"],
        slot["kssj"], slot["jssj"],
        id_token=tokens.get("id_token", ""),
        captcha_id=captcha_id, captcha_code=captcha_code,
    )
    _last_save_ts[0] = time.time()
    # make_appointment 返回的是 GraphQL 完整包体 {"data":{"saveAppointmentInformationAll":{...}}}，
    # 取内层业务对象做分类/取 appointmentId，否则所有响应都会被误判为 other
    flat = resp
    if isinstance(resp, dict):
        d = resp.get("data")
        if isinstance(d, dict) and isinstance(d.get("saveAppointmentInformationAll"), dict):
            flat = d["saveAppointmentInformationAll"]
    kind = classify(flat)
    log(f"  探测#{PROBE_COUNT}: {kind} resp={json_s(flat)}")
    return kind, flat


def solve_captcha_retry(token: str, attempts: int = 3):
    """解滑块验证码，失败（如 checkCaptcha 4001）自动重取重试。"""
    for i in range(1, attempts + 1):
        creds = solve_and_verify_slide_captcha(token)
        if creds:
            return creds
        log(f"  验证码第 {i} 次求解失败，重试...")
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="滑块验证码复用性/有效期实测")
    ap.add_argument("--date", default=None, help="探测日期 YYYY-MM-DD（默认今天；过去日期如昨天天然安全且推荐）")
    ap.add_argument("--delays", default="60,240", help="--deep-ttl 模式的延迟秒数列表，逗号分隔")
    ap.add_argument("--gap", type=int, default=200, help="第3次起 save 探测的最小间隔秒数（上游频控约2连发/3分钟封禁）")
    ap.add_argument("--deep-ttl", action="store_true", help="追加更长延迟档的有效期测量（耗时显著增加）")
    ap.add_argument("--dry-run", action="store_true", help="只选时段+解一次验证码，不发任何预约请求")
    args = ap.parse_args()
    globals()["_MIN_GAP_SEC"] = float(args.gap)

    username = os.environ.get("BB_TEST_USER", "")
    password = os.environ.get("BB_TEST_PASS", "")
    if not username:
        username = input("学号: ").strip()
    if not password:
        password = getpass.getpass("密码: ")

    log(f"登录中: {username[:4]}***")
    tokens = get_token_cached(config.CAS_LOGIN_URL, config.CAS_CAPTCHA_URL, username, password, ttl_seconds=900)
    if not tokens or not tokens.get("access_token"):
        log("登录失败，终止")
        return 1
    access_masked = tokens["access_token"][:12] + "..."
    log(f"登录成功 access_token={access_masked}")

    from datetime import datetime, timedelta
    today_str = datetime.now().strftime("%Y-%m-%d")
    date = args.date or today_str
    is_past = date < today_str

    slot = pick_full_slot(tokens, date, allow_any=is_past)
    if not slot:
        log(f"{date} 找不到可用探测时段（既无约满时段，也非过去日期不可兜底），终止。")
        return 1
    rid, rname, kssj, jssj, tid = slot
    slot_d = {"resource_id": rid, "resources_name": rname, "kssj": kssj, "jssj": jssj, "time_id": tid, "date": date}
    log(f"探测目标: {rname} {date} {kssj}-{jssj} time_id={tid[:16]}... {'(过去日期,天然安全)' if is_past else '(约满时段)'}")

    before = snapshot_appts(tokens, date)
    log(f"测试前该日期已有预约数: {len(before)}")

    if args.dry_run:
        log("dry-run 结束：未发送任何预约请求。")
        return 0

    # ---------- 阶段0：阴性对照 ----------
    # 真实 captchaId + 垃圾 code（从未通过滑块校验）：
    #   实测返回「系统异常」——与有效凭证的业务错误明显不同，
    #   证明服务端先校验验证码、后走业务逻辑。
    log("阶段0 阴性对照: 未通过校验的死验证码提交")
    dc = dead_creds(tokens["access_token"])
    if not dc[0]:
        log("无法生成对照验证码，终止")
        return 1
    kc, _ = probe(tokens, slot_d, dc)

    # ---------- 阶段1：第一次有效提交 ----------
    log("阶段1 复用性: 有效验证码首次提交")
    creds1 = solve_captcha_retry(tokens["access_token"])
    if not creds1:
        log("验证码多次求解均失败，终止")
        return 1
    t_a = time.time()
    k1, r1 = probe(tokens, slot_d, creds1)

    # 第 3 次起 probe() 会自动节流等待，这里直接发同一凭证的第二次提交
    k2, r2 = probe(tokens, slot_d, creds1)
    gap_used = time.time() - t_a

    verdict_reuse = "无法判定"
    ttl_lower = 0.0
    if kc == "captcha_error":
        verdict_reuse = f"对照即报验证码错误（预期为系统异常类），判读基准异常，结果存疑"
    elif k1 == "success":
        verdict_reuse = "首次提交竟成功成约！即将自动取消！"
    elif k1 == "captcha_error":
        verdict_reuse = "有效凭证首次提交即验证码错误 → 异常（可能滑块识别不准），看原始响应"
    elif k1 == "other" and k2 == "other":
        verdict_reuse = (f"同一凭证间隔 {gap_used:.0f}s 的两次提交均通过验证进入业务层"
                         f"（均为容量/业务类拒绝）→ 可复用，且有效期 ≥ {gap_used/60:.1f} 分钟")
        ttl_lower = gap_used
    elif k1 == "other" and k2 == "captcha_error":
        m = ";".join(str(x) for x in (r2.get("messages") or [])) if isinstance(r2, dict) else ""
        verdict_reuse = f"第二次提交报验证码错误（{m}）→ 验证码一次性消费，每枪需自带凭证"
    elif k2 == "banned":
        verdict_reuse = f"第二次提交触发上游频控封禁，复用性本次不可判定（但再次印证频控存在；可稍后单独重跑本脚本看 k2）"
    else:
        verdict_reuse = f"k1={k1}, k2={k2} 组合未覆盖，请人工查看上方原始响应"

    log(f">>> 复用性结论: {verdict_reuse}")

    # 安全网：万一意外成约立即取消
    for kk, rr in ((k1, r1), (k2, r2)):
        if kk == "success" and isinstance(rr, dict):
            aid = rr.get("appointmentId")
            if aid:
                cancel_appointment(tokens["access_token"], tokens.get("id_token", ""), aid)

    # ---------- 阶段2：更长有效期上界测量（默认关闭） ----------
    if args.deep_ttl:
        delays = [int(x) for x in args.delays.split(",") if x.strip()]
        bounds = []
        for d in delays:
            log(f"阶段2 深度TTL: 新解一个验证码，目标延迟 {d}s")
            fresh_creds = solve_captcha_retry(tokens["access_token"])
            if not fresh_creds:
                log("  新验证码获取失败，跳过该延迟档")
                continue
            t_signed = time.time()
            kd, rd = probe(tokens, slot_d, fresh_creds)  # 内部自动节流
            actual_delay = time.time() - t_signed
            bounds.append((actual_delay, kd))
            if kd == "success":
                created_id = rd.get("appointmentId") if isinstance(rd, dict) else None
                log("  !! TTL 探测竟成功成约，立即取消 !!")
                if created_id:
                    cancel_appointment(tokens["access_token"], tokens.get("id_token", ""), created_id)
                break
        ttl_verdict = "；".join(f"{d:.0f}s->{k}" for d, k in bounds) or "未执行"
        log(f">>> 深度有效期数据点: {ttl_verdict}")

    after = snapshot_appts(tokens, date)
    log(f"测试后该日期预约数: {len(after)}（测试前 {len(before)}）")
    if len(after) != len(before):
        new_ids = after - before
        log(f"!! 检测到新增预约 {new_ids}，尝试自动取消 !!" )
        for aid in new_ids:
            cancel_appointment(tokens["access_token"], tokens.get("id_token", ""), aid)

    print("\n========== 最终结论 ==========")
    print(f"复用性: {verdict_reuse}")
    if ttl_lower > 0:
        print(f"有效期下界: ≥ {ttl_lower:.0f}s（同一凭证两次业务通过的时间差）")
    if any(m == "banned" for m in (kc, k1, k2)):
        print("频控提示: 上游对预约接口按账号限流（实测约2连发内安全，第3次封禁3分钟）——"
              "生产端 num_threads>2 的并发抢票请求会被直接拒绝，需重新设计并发策略")
    print(f"(共发送 {PROBE_COUNT} 次探测请求)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
