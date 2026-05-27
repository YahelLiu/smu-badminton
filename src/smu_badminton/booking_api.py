"""
预约 API 和可用性查询模块。

包含用户信息解析、预约 API 调用、可用性查询等功能。
"""
import requests
import time
import json
import base64
import logging
from typing import Any, Dict
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import (
    WF_ORIGIN,
    WF_API_URL,
    BADMINTON_TYPE_ID,
    TOKEN_PROFILE_TTL_SEC,
    DEFAULT_DEPT_CODE,
    DEFAULT_DEPT_NAME,
    DEFAULT_DEPT_NAME_EN,
    DEFAULT_USER_EMAIL,
    DEFAULT_USER_PHONE,
)

logger = logging.getLogger(__name__)


def _debug(msg: str):
    """调试日志输出。"""
    from .config import BOOKING_DEBUG
    if BOOKING_DEBUG:
        logger.debug(msg)


# ============= Token Profile 缓存 =============

_TOKEN_PROFILE_CACHE: Dict[str, Dict[str, Any]] = {}
_TOKEN_LOCK: Any = None  # 延迟初始化，避免模块导入时的锁问题


def _get_token_lock():
    """延迟初始化 token 锁。"""
    global _TOKEN_LOCK
    if _TOKEN_LOCK is None:
        import threading
        _TOKEN_LOCK = threading.Lock()
    return _TOKEN_LOCK


def _decode_jwt_payload(token: str) -> Dict[str, Any] | None:
    """解析 JWT payload（不校验签名）。"""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload.encode("utf-8"))
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _profile_from_claims(claims: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if not claims:
        return None

    user_code = (
        claims.get("userCode")
        or claims.get("userName")
        or claims.get("account")
        or claims.get("loginName")
        or claims.get("sub")
    )
    if not user_code:
        return None

    display_name = claims.get("name") or claims.get("realName") or claims.get("cn") or str(user_code)
    dept_code = claims.get("deptCode") or DEFAULT_DEPT_CODE
    dept_name = claims.get("deptName") or DEFAULT_DEPT_NAME
    dept_name_en = claims.get("deptNameEn") or DEFAULT_DEPT_NAME_EN
    email = claims.get("email") or DEFAULT_USER_EMAIL or f"{user_code}@stu.shmtu.edu.cn"
    phone = claims.get("phone") or claims.get("mobile") or claims.get("telephone") or DEFAULT_USER_PHONE

    return {
        "user_code": str(user_code),
        "display_name": str(display_name),
        "dept_code": str(dept_code),
        "dept_name": str(dept_name),
        "dept_name_en": str(dept_name_en),
        "email": str(email),
        "phone": str(phone),
    }


def _cache_profile_from_tokens(tokens: Dict[str, Any] | None):
    if not tokens:
        return
    access_token = tokens.get("access_token")
    if not access_token:
        return

    profile = (
        _profile_from_claims(_decode_jwt_payload(tokens.get("id_token", "")))
        or _profile_from_claims(_decode_jwt_payload(access_token))
    )
    if not profile:
        return

    with _get_token_lock():
        _TOKEN_PROFILE_CACHE[access_token] = {"profile": profile, "ts": time.time()}


def _get_profile_by_access_token(access_token: str) -> Dict[str, Any] | None:
    now = time.time()
    with _get_token_lock():
        entry = _TOKEN_PROFILE_CACHE.get(access_token)
        if entry and now - float(entry.get("ts", 0)) < float(TOKEN_PROFILE_TTL_SEC):
            return entry.get("profile")
    return _profile_from_claims(_decode_jwt_payload(access_token))


def _build_user_info_from_profile(profile: Dict[str, Any] | None) -> Dict[str, Any] | None:
    if not profile:
        return None

    user_code = profile.get("user_code", "")
    display_name = profile.get("display_name", user_code)
    dept_code = profile.get("dept_code", "")
    dept_name = profile.get("dept_name", "")
    dept_name_en = profile.get("dept_name_en", "")
    email = profile.get("email", "")
    phone = profile.get("phone", "")

    participant_info = {
        "participant_id": user_code,
        "participant_name": display_name,
        "participant_dept_id": dept_code,
        "participant_dept_name": dept_name,
        "mobile": phone,
        "email": email,
        "operate_user_id": user_code,
        "operate_user_name": display_name,
    }
    return {
        "created_user": user_code,
        "created_user_name": display_name,
        "appointment_user": user_code,
        "appointment_user_name": display_name,
        "dept_code": dept_code,
        "dept_name": dept_name,
        "dept_name_en": dept_name_en,
        "email": email,
        "phone": phone,
        "participant_info": participant_info,
    }


# ============= 用户信息获取 =============

def get_user_info_from_appointment(token):
    """尝试从已有预约记录中推断用户信息。"""
    from .cas_login_requests import requests_post_with_retry

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Origin": WF_ORIGIN,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0",
    }

    payload = {
        "operationName": "findAppointmentInformationAllForAccount",
        "variables": {
            "first": 1,
            "offset": 0,
            "updateAppointmentState": "0",
            "filter": {
                "resources_id": {},
                "state": {"eq": 0}
            }
        },
        "query": """query findAppointmentInformationAllForAccount($first: Int, $offset: Int, $after: String, $filter: AppointmentInformationFilterMap, $appointmentDate: [String], $only_flow: String, $updateAppointmentState: String) {
          findAppointmentInformationAllForAccount(first: $first, offset: $offset, after: $after, filter: $filter, appointmentDate: $appointmentDate, only_flow: $only_flow, updateAppointmentState: $updateAppointmentState) {
            edges {
              node {
                created_user
                created_user_name
                appointment_user
                appointment_user_name
                dept_code
                dept_name
                dept_name_en
                email
                phone
                appointmentParticipantList {
                  participant_id
                  participant_name
                  participant_dept_id
                  participant_dept_name
                  mobile
                  email
                  operate_user_id
                  operate_user_name
                }
              }
            }
          }
        }"""
    }

    try:
        response = requests_post_with_retry(WF_API_URL, json=payload, headers=headers)
        if response is None or response.status_code != 200:
            _debug(f"get_user_info_from_appointment failed, status={response.status_code if response else 'None'}")
            return None

        data = response.json()
        edges = data.get("data", {}).get("findAppointmentInformationAllForAccount", {}).get("edges", [])
        if not edges:
            return None

        node = edges[0].get("node", {}) if isinstance(edges[0], dict) else {}
        if not node:
            return None

        participant_list = node.get("appointmentParticipantList") or []
        participant = participant_list[0] if participant_list else {}

        return {
            "created_user": node.get("created_user", ""),
            "created_user_name": node.get("created_user_name", ""),
            "appointment_user": node.get("appointment_user", ""),
            "appointment_user_name": node.get("appointment_user_name", ""),
            "dept_code": node.get("dept_code", ""),
            "dept_name": node.get("dept_name", ""),
            "dept_name_en": node.get("dept_name_en", ""),
            "email": node.get("email", ""),
            "phone": node.get("phone", ""),
            "participant_info": participant,
        }
    except Exception as e:
        _debug(f"get_user_info_from_appointment exception: {e}")
        return None


def resolve_user_info(token: str) -> Dict[str, Any] | None:
    """先从 API 获取预约用户信息，失败再回退到 JWT claims。"""
    user_info = get_user_info_from_appointment(token)
    if user_info:
        return user_info

    profile = _get_profile_by_access_token(token)
    user_info = _build_user_info_from_profile(profile)
    if user_info:
        _debug("resolved user info from token profile fallback")
    return user_info


# ============= 资源查询 API =============

def find_time_slots_by_resource(token, resources_id, date_ms):
    """按日期时间戳查询资源时段及可预约数量。"""
    from .cas_login_requests import requests_post_with_retry

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Origin": WF_ORIGIN,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0",
    }
    payload = {
        "operationName": "findResourcesTimeSlotByResourcesIdAndDate",
        "variables": {
            "resourcesId": resources_id,
            "date": date_ms
        },
        "query": """query findResourcesTimeSlotByResourcesIdAndDate($resourcesId: String!, $date: Date!) {\n  findResourcesTimeSlotByResourcesIdAndDate(resourcesId: $resourcesId, date: $date) {\n    id\n    resources_id\n    kssj\n    jssj\n    order\n    del\n    create_time\n    canAppointmentNumberDesc\n    canAppointmentNumberDesc_en\n    canAppointmentNumber\n  }\n}\n"""
    }
    resp = requests_post_with_retry(WF_API_URL, json=payload, headers=headers)
    if not resp:
        return None
    return resp.json()


def list_resources_by_account(token, bookdate, type_id=None):
    """
    基于 findResourcesAllByAccount 获取指定日期的资源列表（包含时间段）。
    返回 JSON 数据结构中的 resources 列表，失败返回 None。
    """
    from .cas_login_requests import requests_post_with_retry

    if type_id is None:
        type_id = BADMINTON_TYPE_ID
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Origin": WF_ORIGIN,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
    }
    payload = {
        "operationName": "findResourcesAllByAccount",
        "variables": {
            "first": 100,
            "offset": 0,
            "typeId": type_id,
            "bookDate": bookdate,
            "cur_language": "zh_CN"
        },
        "query": "query findResourcesAllByAccount($first: Int, $offset: Int, $typeId: String, $typeName: String, $resourceName: String, $bookDate: String, $bookStartTime: String, $bookEndTime: String, $item_name: [String], $is_cyclicity: String, $cyclicity_start_date: String, $cyclicity_end_date: String, $cyclicity_start_time: String, $cyclicity_end_time: String, $cyclicity_strategy: String, $cyclicity_weekList: [String], $cyclicity_dayList: [String], $order_by: String, $cur_language: String, $filter: ResourcesFilterMap) { findResourcesAllByAccount(first: $first, offset: $offset, typeId: $typeId, typeName: $typeName, resourceName: $resourceName, bookDate: $bookDate, bookStartTime: $bookStartTime, bookEndTime: $bookEndTime, item_name: $item_name, is_cyclicity: $is_cyclicity, cyclicity_start_date: $cyclicity_start_date, cyclicity_end_date: $cyclicity_end_date, cyclicity_start_time: $cyclicity_start_time, cyclicity_end_time: $cyclicity_end_time, cyclicity_strategy: $cyclicity_strategy, cyclicity_weekList: $cyclicity_weekList, cyclicity_dayList: $cyclicity_dayList, order_by: $order_by, cur_language: $cur_language, filter: $filter) { id resources_name available_number resourcesTimeSlot { id kssj jssj } } }"
    }
    resp = requests_post_with_retry(WF_API_URL, json=payload, headers=headers)
    if not resp:
        return None
    data = resp.json()
    if 'data' not in data or 'findResourcesAllByAccount' not in data['data']:
        return None
    return data['data']['findResourcesAllByAccount']


def check_resource_availability_on_date(token, resources_id, bookdate):
    """
    检查某个资源在指定日期的所有时间段是否可约。
    返回列表: [{kssj, jssj, canAppointmentNumber}]
    """
    dt = datetime.strptime(bookdate, "%Y-%m-%d")
    date_ms = int(dt.timestamp() * 1000)
    detail = find_time_slots_by_resource(token, resources_id, date_ms)
    if not detail or 'data' not in detail or 'findResourcesTimeSlotByResourcesIdAndDate' not in detail['data']:
        return []
    slots = detail['data']['findResourcesTimeSlotByResourcesIdAndDate']
    results = []
    for s in slots:
        results.append({
            'kssj': s.get('kssj'),
            'jssj': s.get('jssj'),
            'canAppointmentNumber': s.get('canAppointmentNumber')
        })
    return results


def find_resources_id_by_name(token, bookdate, resources_name):
    """按显示名称查找指定日期的资源 ID。"""
    resources = list_resources_by_account(token, bookdate)
    if not resources:
        return None
    for r in resources:
        if r.get('resources_name') == resources_name:
            return r.get('id')
    return None


def demo_check_availability(token, bookdate, resources_name=None):
    """
    测试程序：
    - 若提供 resources_name：查询其资源 ID，并输出该资源当天所有时间段可预约数量
    - 若不提供：输出当天所有资源及其每个时间段的可预约数量
    """
    if resources_name:
        resources_id = find_resources_id_by_name(token, bookdate, resources_name)
        if not resources_id:
            logger.warning("resource not found: %s", resources_name)
            return
        results = check_resource_availability_on_date(token, resources_id, bookdate)
        logger.info("resource %s (%s) on %s:", resources_name, resources_id, bookdate)
        for row in results:
            logger.info("  %s-%s: %s", row['kssj'], row['jssj'], row['canAppointmentNumber'])
    else:
        resources = list_resources_by_account(token, bookdate)
        if not resources:
            logger.warning("未获取到资源列表")
            return
        for r in resources:
            rid = r.get('id')
            rname = r.get('resources_name')
            results = check_resource_availability_on_date(token, rid, bookdate)
            logger.info("resource %s (%s) on %s:", rname, rid, bookdate)
            for row in results:
                logger.info("  %s-%s: %s", row['kssj'], row['jssj'], row['canAppointmentNumber'])


# ============= 预约记录查询 =============

def list_appointments_for_account(token, bookdate):
    """
    拉取当前账户在指定日期的预约记录，返回 edges 列表。
    """
    from .cas_login_requests import requests_post_with_retry

    # 将 YYYY-MM-DD 转为当天 00:00:00 的毫秒时间戳以便对比
    dt = datetime.strptime(bookdate, "%Y-%m-%d")
    bookdate_ms = int(dt.timestamp() * 1000)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Origin": WF_ORIGIN,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0",
    }
    payload = {
        "operationName": "findAppointmentInformationAllForAccount",
        "variables": {
            "first": 100,
            "offset": 0,
            "updateAppointmentState": "0",
            "filter": {
                "state": {"eq": 0}
            }
        },
        "query": """query findAppointmentInformationAllForAccount($first: Int, $offset: Int, $after: String, $filter: AppointmentInformationFilterMap, $appointmentDate: [String], $only_flow: String, $updateAppointmentState: String) {\n  findAppointmentInformationAllForAccount(first: $first, offset: $offset, after: $after, filter: $filter, appointmentDate: $appointmentDate, only_flow: $only_flow, updateAppointmentState: $updateAppointmentState) {\n    edges {\n      node {\n        resources_id\n        resources_name\n        appointment_date\n        start_time\n        end_time\n        state\n      }\n      cursor\n    }\n    pageInfo { endCursor startCursor }\n    totalCount\n  }\n}\n"""
    }
    resp = requests_post_with_retry(WF_API_URL, json=payload, headers=headers)
    if not resp:
        return []
    data = resp.json()
    edges = data.get('data', {}).get('findAppointmentInformationAllForAccount', {}).get('edges', [])
    # 过滤同一天的预约（appointment_date 为毫秒）
    same_day = [e for e in edges if abs(int(e.get('node', {}).get('appointment_date', 0)) - bookdate_ms) < 24*60*60*1000]
    return same_day


def compute_availability_for_date(token, bookdate):
    """计算指定日期所有资源的可用性。"""
    t0 = time.time()

    # 并发获取资源列表和用户预约记录
    t1 = time.time()
    with ThreadPoolExecutor(max_workers=2) as init_executor:
        resources_future = init_executor.submit(list_resources_by_account, token, bookdate)
        appointments_future = init_executor.submit(list_appointments_for_account, token, bookdate)
        resources = resources_future.result()
        my_edges = appointments_future.result()
    t2 = time.time()
    logger.info(f"[性能] 获取资源列表+预约记录: {(t2-t1)*1000:.0f}ms")

    if not resources:
        return []

    my_map = {}
    for e in my_edges:
        n = e.get('node', {})
        key = (n.get('resources_id'), n.get('start_time'), n.get('end_time'))
        my_map[key] = True

    dt = datetime.strptime(bookdate, "%Y-%m-%d")
    date_ms = int(dt.timestamp() * 1000)

    rid_list = [(r.get('id'), r.get('resources_name')) for r in resources]
    results_map = {}
    # 并发数最多到 15，所有场地同时查询
    t3 = time.time()
    max_workers = max(1, min(15, len(rid_list)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(find_time_slots_by_resource, token, rid, date_ms): (rid, rname) for rid, rname in rid_list}
        for fut in as_completed(future_map):
            rid, rname = future_map[fut]
            detail = None
            try:
                detail = fut.result()
            except Exception:
                detail = None
            results_map[rid] = (rname, detail)
    t4 = time.time()
    logger.info(f"[性能] 获取{len(rid_list)}个场地时间槽: {(t4-t3)*1000:.0f}ms")

    out = []
    for rid, (rname, detail) in results_map.items():
        slots = []
        if detail and 'data' in detail:
            for s in detail['data'].get('findResourcesTimeSlotByResourcesIdAndDate', []):
                kssj = s.get('kssj')
                jssj = s.get('jssj')
                booked = my_map.get((rid, kssj, jssj), False)
                slots.append({
                    'kssj': kssj,
                    'jssj': jssj,
                    'canAppointmentNumber': s.get('canAppointmentNumber'),
                    'bookedByMe': booked,
                })
        out.append({'resources_id': rid, 'resources_name': rname, 'slots': slots})

    t5 = time.time()
    logger.info(f"[性能] compute_availability_for_date 总耗时: {(t5-t0)*1000:.0f}ms")
    return out


# ============= 预约 API =============

def fetch_resource_time_id(token, bookdate, resources_name, kssj, jssj):
    """获取资源和时间段 ID。"""
    from .cas_login_requests import requests_post_with_retry

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Origin": WF_ORIGIN,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
    }
    payload = {
        "operationName": "findResourcesAllByAccount",
        "variables": {
            "first": 100,
            "offset": 0,
            "typeId": BADMINTON_TYPE_ID,
            "bookDate": bookdate,
            "cur_language": "zh_CN"
        },
        "query": "query findResourcesAllByAccount($first: Int, $offset: Int, $typeId: String, $typeName: String, $resourceName: String, $bookDate: String, $bookStartTime: String, $bookEndTime: String, $item_name: [String], $is_cyclicity: String, $cyclicity_start_date: String, $cyclicity_end_date: String, $cyclicity_start_time: String, $cyclicity_end_time: String, $cyclicity_strategy: String, $cyclicity_weekList: [String], $cyclicity_dayList: [String], $order_by: String, $cur_language: String, $filter: ResourcesFilterMap) { findResourcesAllByAccount(first: $first, offset: $offset, typeId: $typeId, typeName: $typeName, resourceName: $resourceName, bookDate: $bookDate, bookStartTime: $bookStartTime, bookEndTime: $bookEndTime, item_name: $item_name, is_cyclicity: $is_cyclicity, cyclicity_start_date: $cyclicity_start_date, cyclicity_end_date: $cyclicity_end_date, cyclicity_start_time: $cyclicity_start_time, cyclicity_end_time: $cyclicity_end_time, cyclicity_strategy: $cyclicity_strategy, cyclicity_weekList: $cyclicity_weekList, cyclicity_dayList: $cyclicity_dayList, order_by: $order_by, cur_language: $cur_language, filter: $filter) { id resources_name available_number resourcesTimeSlot { id kssj jssj } } }"
    }
    response = requests_post_with_retry(WF_API_URL, json=payload, headers=headers)
    if response is None or response.status_code != 200:
        logger.warning("request failed, status=%d", response.status_code if response else 'None')
        return None

    json_data = response.json()
    if 'data' not in json_data or 'findResourcesAllByAccount' not in json_data['data']:
        logger.warning("返回数据格式异常或无资源数据")
        return None

    resources = json_data['data']['findResourcesAllByAccount']
    for resource in resources:
        if resource.get('resources_name') == resources_name:
            resource_id = resource.get('id')
            for time_slot in resource.get('resourcesTimeSlot', []):
                if time_slot.get('kssj') == kssj and time_slot.get('jssj') == jssj:
                    time_id = time_slot.get('id')
                    logger.debug("获取到预约时段信息")
                    return resource_id, time_id
    return None


def make_appointment(token, time_id, resource_id, bookdata, kssj, jssj):
    """执行预约。"""
    from .cas_login_requests import requests_post_with_retry

    _debug(f"appointment args date={bookdata}, start={kssj}, end={jssj}, resource_id={resource_id}, time_id={time_id}")

    user_info = resolve_user_info(token)
    if not user_info:
        return {
            "code": "USER_INFO_UNAVAILABLE",
            "messages": ["Cannot resolve user profile; no appointment history and no usable JWT claims."],
        }

    created_user = user_info.get("created_user") or user_info.get("appointment_user") or ""
    created_user_name = user_info.get("created_user_name") or user_info.get("appointment_user_name") or created_user
    appointment_user = user_info.get("appointment_user") or created_user
    appointment_user_name = user_info.get("appointment_user_name") or created_user_name
    dept_code = user_info.get("dept_code", "")
    dept_name = user_info.get("dept_name", "")
    dept_name_en = user_info.get("dept_name_en", "")
    email = user_info.get("email", "")
    phone = user_info.get("phone", "")

    participant = dict(user_info.get("participant_info") or {})
    participant.setdefault("participant_id", appointment_user)
    participant.setdefault("participant_name", appointment_user_name)
    participant.setdefault("participant_dept_id", dept_code)
    participant.setdefault("participant_dept_name", dept_name)
    participant.setdefault("operate_user_id", created_user)
    participant.setdefault("operate_user_name", created_user_name)
    participant.setdefault("mobile", phone)
    participant.setdefault("email", email)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Origin": WF_ORIGIN,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
    }
    payload = {
        "operationName": "saveAppointmentInformationAll",
        "variables": {
            "captchaId": "",
            "captchaCode": "",
            "timeSlotIdList": [time_id],
            "model": {
                "created_user": created_user,
                "created_user_name": created_user_name,
                "appointment_user": appointment_user,
                "appointment_user_name": appointment_user_name,
                "state": 0,
                "resources_id": resource_id,
                "dept_code": dept_code,
                "dept_name": dept_name,
                "dept_name_en": dept_name_en,
                "email": email,
                "phone": phone,
                "person_times": 1,
                "wemeet_enable": "0",
                "theme": "",
                "theme_en": None,
                "enclosure": "",
                "enclosure_name": "",
                "enclosure_size": "",
                "remark": "",
                "participants_scope": None,
                "entourage": None,
                "event": None,
                "event_en": None,
                "appointmentParticipantList": [
                    {
                        "participant_id": participant.get("participant_id") or appointment_user,
                        "participant_name": participant.get("participant_name") or appointment_user_name,
                        "participant_dept_id": participant.get("participant_dept_id") or dept_code,
                        "participant_dept_name": participant.get("participant_dept_name") or dept_name,
                        "operate_user_id": participant.get("operate_user_id") or created_user,
                        "operate_user_name": participant.get("operate_user_name") or created_user_name,
                        "mobile": participant.get("mobile") or phone,
                        "email": participant.get("email") or email,
                    }
                ],
                "appointmentCollectionList": [],
                "appointment_date": bookdata,
                "start_time": kssj,
                "end_time": jssj,
            },
        },
        "query": """mutation saveAppointmentInformationAll($captchaId: String, $captchaCode: String, $model: InputAppointmentInformation!, $timeSlotIdList: [String], $borrowDateList: [String], $borrowStartTime: String, $borrowEndTime: String) {
          saveAppointmentInformationAll(captchaId: $captchaId, captchaCode: $captchaCode, model: $model, timeSlotIdList: $timeSlotIdList, borrowDateList: $borrowDateList, borrowStartTime: $borrowStartTime, borrowEndTime: $borrowEndTime) {
            code
            name
            messages
            messages_en
            ids
            appointmentId
            processURL
            auditStatus
          }
        }""",
    }

    _debug(f"saveAppointmentInformationAll payload timeSlotIdList={payload['variables']['timeSlotIdList']}")
    response = requests_post_with_retry(WF_API_URL, json=payload, headers=headers)
    if not response:
        return {"code": "REQUEST_FAILED", "messages": ["Appointment request failed after retries"]}

    try:
        resp_json = response.json()
    except Exception:
        return {"code": "INVALID_RESPONSE", "messages": [response.text[:200]]}

    _debug(f"saveAppointmentInformationAll status={response.status_code}")
    _debug(f"saveAppointmentInformationAll response={resp_json}")
    return resp_json
