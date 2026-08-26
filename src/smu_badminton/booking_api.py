"""
预约 API 和可用性查询模块。

包含用户信息解析、预约 API 调用、可用性查询等功能。

接口规范：
- 列表类函数失败返回 []（空列表）
- 对象类函数失败返回 None
- 写操作返回统一结构 {"code": str, "messages": list}
- 所有公开函数参数顺序：必需参数在前，可选参数在后（id_token 默认 ""，session 默认 None）
"""
import base64
import random
import requests
import time
import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import (
    WF_ORIGIN,
    WF_API_URL,
    WF_CAPTCHA_URL,
    BADMINTON_TYPE_ID,
)
from .token_profile import (
    cache_profile_from_tokens,
    get_profile_by_access_token,
    build_user_info_from_profile,
)

logger = logging.getLogger(__name__)


# ============= HTTP 请求常量和辅助函数 =============

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0"
)

# 中国标准时区 (UTC+8)
_BEIJING_TZ = timezone(timedelta(hours=8))


def _bookdate_to_ms(bookdate: str) -> int:
    """将 YYYY-MM-DD 格式的日期转换为北京时间 00:00:00 的毫秒时间戳。"""
    dt = datetime.strptime(bookdate, "%Y-%m-%d").replace(tzinfo=_BEIJING_TZ)
    return int(dt.timestamp() * 1000)


def build_headers(token: str) -> Dict[str, str]:
    """构建 GraphQL 请求的通用 headers。"""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Origin": WF_ORIGIN,
        "User-Agent": USER_AGENT,
    }


def _debug(msg: str) -> None:
    """调试日志输出。"""
    from .config import BOOKING_DEBUG
    if BOOKING_DEBUG:
        logger.debug(msg)


def _graphql_url(id_token: str = "") -> str:
    """构建 GraphQL API URL，附带 id_token_hint 查询参数。"""
    if id_token:
        return f"{WF_API_URL}?id_token_hint={id_token}"
    return WF_API_URL


def _is_ssl_error(error: Exception) -> bool:
    """判断是否为 SSL 相关错误。"""
    error_str = str(error).lower()
    return 'ssl' in error_str or 'eof' in error_str or 'protocol' in error_str


def _shared_session() -> requests.Session:
    """创建带连接池的共享 Session，复用 TCP/TLS 连接。"""
    s = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20)
    s.mount('https://', adapter)
    s.mount('http://', adapter)
    return s


def _make_graphql_request(
    session,
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    log_name: str = "",
    token: str = ""
) -> Optional[requests.Response]:
    """使用共享 Session 发送 GraphQL 请求，带重试和 token 自动刷新。

    Args:
        session: requests.Session 或 requests 模块
        url: GraphQL API URL
        headers: 请求头
        payload: 请求体
        log_name: 日志名称
        token: 访问令牌（用于自动刷新）

    Returns:
        响应对象，失败返回 None
    """
    max_retries = 2
    timeout = 10

    def do_request(current_token: str) -> Optional[requests.Response]:
        """执行单次请求。"""
        current_headers = headers.copy()
        if current_token:
            current_headers["Authorization"] = f"Bearer {current_token}"
        return session.post(url, json=payload, headers=current_headers, timeout=timeout)

    for attempt in range(max_retries):
        try:
            t0 = time.time()
            resp = do_request(token)
            elapsed = (time.time() - t0) * 1000
            if log_name and elapsed > 500:
                logger.info("[性能] %s: %.0fms (attempt %d)", log_name, elapsed, attempt + 1)
            if resp.status_code == 200:
                body = resp.json()
                if "errors" in body:
                    err_msg = body["errors"][0].get("message", "") if body["errors"] else ""
                    logger.warning("%s GraphQL error: %s", log_name, err_msg)
                    # token 过期：尝试刷新
                    if "ACCESS_TOKEN_INVALID" in str(body) or "过期" in err_msg:
                        if token and attempt == 0:
                            from .token_profile import find_user_by_access_token, refresh_token_for_user
                            username, _ = find_user_by_access_token(token)
                            if username:
                                logger.info("检测到 token 过期，尝试刷新: %s", username)
                                new_tokens = refresh_token_for_user(username)
                                if new_tokens and new_tokens.get("access_token"):
                                    token = new_tokens["access_token"]
                                    logger.info("token 刷新成功，重试请求: %s", log_name)
                                    continue  # 用新 token 重试
                        return resp  # 无法刷新，返回原响应
                return resp
            logger.warning("%s failed status=%d, retrying (%d/%d)", log_name, resp.status_code, attempt + 1, max_retries)
        except Exception as e:
            logger.warning("%s exception=%s, retrying (%d/%d)", log_name, e, attempt + 1, max_retries)
            if _is_ssl_error(e) and attempt >= 1:
                break
        if attempt < max_retries - 1:
            time.sleep(0.3)
    return None


# ============= 统一返回结构 =============

class APIResult:
    """API 调用结果封装。"""

    @staticmethod
    def success(data: Any = None) -> Dict[str, Any]:
        """成功响应。"""
        return {"ok": True, "data": data}

    @staticmethod
    def error(code: str, message: str) -> Dict[str, Any]:
        """错误响应。"""
        return {"ok": False, "code": code, "message": message}


# ============= 用户信息获取 =============

def get_user_info_from_appointment(
    token: str,
    id_token: str = ""
) -> Optional[Dict[str, Any]]:
    """
    尝试从已有预约记录中推断用户信息。

    Args:
        token: 访问令牌
        id_token: ID 令牌（可选）

    Returns:
        用户信息字典，失败返回 None
    """
    from .http_utils import requests_post_with_retry

    headers = build_headers(token)

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
        response = requests_post_with_retry(_graphql_url(id_token), json=payload, headers=headers)
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


def resolve_user_info(
    token: str,
    id_token: str = ""
) -> Optional[Dict[str, Any]]:
    """
    先从 API 获取预约用户信息，失败再回退到 JWT claims。

    Args:
        token: 访问令牌
        id_token: ID 令牌（可选）

    Returns:
        用户信息字典，失败返回 None
    """
    user_info = get_user_info_from_appointment(token, id_token=id_token)
    if user_info:
        return user_info

    profile = get_profile_by_access_token(token)
    user_info = build_user_info_from_profile(profile)
    if user_info:
        _debug("resolved user info from token profile fallback")
    return user_info


# ============= 资源查询 API =============

def find_time_slots_by_resource(
    token: str,
    resources_id: str,
    date_ms: int,
    id_token: str = "",
    session: Optional[requests.Session] = None
) -> Optional[Dict[str, Any]]:
    """
    按日期时间戳查询资源时段及可预约数量。

    Args:
        token: 访问令牌
        resources_id: 资源 ID
        date_ms: 日期毫秒时间戳
        id_token: ID 令牌（可选）
        session: 可复用的 Session（可选）

    Returns:
        时间槽数据字典，失败返回 None
    """
    headers = build_headers(token)
    payload = {
        "operationName": "findResourcesTimeSlotByResourcesIdAndDate",
        "variables": {
            "resourcesId": resources_id,
            "date": date_ms
        },
        "query": """query findResourcesTimeSlotByResourcesIdAndDate($resourcesId: String!, $date: Date!) {
  findResourcesTimeSlotByResourcesIdAndDate(resourcesId: $resourcesId, date: $date) {
    id
    resources_id
    kssj
    jssj
    order
    del
    create_time
    canAppointmentNumberDesc
    canAppointmentNumberDesc_en
    canAppointmentNumber
  }
}"""
    }
    s = session or requests
    resp = _make_graphql_request(s, _graphql_url(id_token), headers, payload, f"time_slots({resources_id[:8]})", token=token)
    if not resp:
        return None
    return resp.json()


def list_resources_by_account(
    token: str,
    bookdate: str,
    type_id: Optional[str] = None,
    id_token: str = "",
    account: str = "",
    session: Optional[requests.Session] = None
) -> Optional[List[Dict[str, Any]]]:
    """
    基于 findResourcesAllByAccount 获取指定日期的资源列表（包含时间段）。

    Args:
        token: 访问令牌
        bookdate: 预约日期 (YYYY-MM-DD)
        type_id: 资源类型 ID（可选，默认羽毛球）
        id_token: ID 令牌（可选）
        account: 账户（可选）
        session: 可复用的 Session（可选）

    Returns:
        资源列表，失败返回 None
    """
    if type_id is None:
        type_id = BADMINTON_TYPE_ID
    headers = build_headers(token)
    payload = {
        "operationName": "findResourcesAllByAccount",
        "variables": {
            "typeId": type_id,
            "bookDate": bookdate,
            "bookStartTime": "",
            "bookEndTime": "",
            "item_name": [],
            "resourceName": "",
            "account": account,
            "cur_language": "zh",
            "order_by": "",
            "filter": {
                "campus_code": {"eq": ""},
                "building_code": {"eq": ""},
                "floor_code": {"eq": ""},
                "need_approve": {"eq": None}
            }
        },
        "query": "query findResourcesAllByAccount($first: Int, $offset: Int, $typeId: String, $typeName: String, $resourceName: String, $bookDate: String, $bookStartTime: String, $bookEndTime: String, $item_name: [String], $is_cyclicity: String, $cyclicity_start_date: String, $cyclicity_end_date: String, $cyclicity_start_time: String, $cyclicity_end_time: String, $cyclicity_strategy: String, $cyclicity_weekList: [String], $cyclicity_dayList: [String], $order_by: String, $cur_language: String, $filter: ResourcesFilterMap) { findResourcesAllByAccount(first: $first, offset: $offset, typeId: $typeId, typeName: $typeName, resourceName: $resourceName, bookDate: $bookDate, bookStartTime: $bookStartTime, bookEndTime: $bookEndTime, item_name: $item_name, is_cyclicity: $is_cyclicity, cyclicity_start_date: $cyclicity_start_date, cyclicity_end_date: $cyclicity_end_date, cyclicity_start_time: $cyclicity_start_time, cyclicity_end_time: $cyclicity_end_time, cyclicity_strategy: $cyclicity_strategy, cyclicity_weekList: $cyclicity_weekList, cyclicity_dayList: $cyclicity_dayList, order_by: $order_by, cur_language: $cur_language, filter: $filter) { id resources_name open_captcha_verify capacity available_number resourcesTimeSlot { id kssj jssj } } }"
    }
    s = session or requests
    t0 = time.time()
    resp = _make_graphql_request(s, _graphql_url(id_token), headers, payload, "list_resources", token=token)
    elapsed = (time.time() - t0) * 1000
    logger.info("[性能] list_resources_by_account: %.0fms", elapsed)
    if not resp:
        return None
    data = resp.json()
    if 'data' not in data or 'findResourcesAllByAccount' not in data['data']:
        return None
    return data['data']['findResourcesAllByAccount']


# ============= 预约记录查询 =============

def list_appointments_for_account(
    token: str,
    bookdate: str,
    id_token: str = "",
    session: Optional[requests.Session] = None
) -> List[Dict[str, Any]]:
    """
    拉取当前账户在指定日期的预约记录。

    Args:
        token: 访问令牌
        bookdate: 预约日期 (YYYY-MM-DD)
        id_token: ID 令牌（可选）
        session: 可复用的 Session（可选）

    Returns:
        预约记录 edges 列表，失败返回空列表 []
    """
    # 将 YYYY-MM-DD 转为当天 00:00:00 的毫秒时间戳以便对比
    bookdate_ms = _bookdate_to_ms(bookdate)

    headers = build_headers(token)
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
        "query": """query findAppointmentInformationAllForAccount($first: Int, $offset: Int, $after: String, $filter: AppointmentInformationFilterMap, $appointmentDate: [String], $only_flow: String, $updateAppointmentState: String) {
  findAppointmentInformationAllForAccount(first: $first, offset: $offset, after: $after, filter: $filter, appointmentDate: $appointmentDate, only_flow: $only_flow, updateAppointmentState: $updateAppointmentState) {
    edges {
      node {
        resources_id
        resources_name
        appointment_date
        start_time
        end_time
        state
      }
      cursor
    }
    pageInfo { endCursor startCursor }
    totalCount
  }
}"""
    }
    s = session or requests
    t0 = time.time()
    resp = _make_graphql_request(s, _graphql_url(id_token), headers, payload, "list_appointments", token=token)
    logger.info("[性能] list_appointments_for_account: %.0fms", (time.time() - t0) * 1000)
    if not resp:
        return []
    try:
        data = resp.json()
        edges = data.get('data', {}).get('findAppointmentInformationAllForAccount', {}).get('edges', [])
        # 过滤同一天的预约（appointment_date 为毫秒）
        same_day = [e for e in edges if abs(int(e.get('node', {}).get('appointment_date', 0)) - bookdate_ms) < 24*60*60*1000]
        return same_day
    except Exception as e:
        logger.warning("list_appointments_for_account parse error: %s", e)
        return []


def compute_availability_for_date(
    token: str,
    bookdate: str,
    id_token: str = ""
) -> List[Dict[str, Any]]:
    """
    计算指定日期所有资源的可用性。使用共享 Session 复用连接，全并发请求。

    Args:
        token: 访问令牌
        bookdate: 预约日期 (YYYY-MM-DD)
        id_token: ID 令牌（可选）

    Returns:
        可用性列表
    """
    t0 = time.time()
    session = _shared_session()

    try:
        # 阶段 1：并发获取资源列表 + 预约记录
        t1 = time.time()
        with ThreadPoolExecutor(max_workers=2) as init_executor:
            resources_future = init_executor.submit(
                list_resources_by_account, token, bookdate, id_token=id_token, session=session
            )
            appointments_future = init_executor.submit(
                list_appointments_for_account, token, bookdate, id_token=id_token, session=session
            )
            resources = resources_future.result()
            my_edges = appointments_future.result()
        t2 = time.time()
        logger.info("[性能] 获取资源列表+预约记录: %.0fms", (t2 - t1) * 1000)

        my_map = _build_my_bookings_map(my_edges)
        slots_data = _fetch_all_time_slots(token, bookdate, resources or [], id_token=id_token, session=session)
        return _merge_bookings(slots_data, my_map, t0)
    finally:
        session.close()


def _build_my_bookings_map(my_edges: List[Dict[str, Any]]) -> Dict[Tuple[str, str, str], bool]:
    """从预约记录构建 bookedByMe 映射。"""
    my_map: Dict[Tuple[str, str, str], bool] = {}
    for e in my_edges:
        n = e.get('node', {})
        key = (n.get('resources_id', ''), n.get('start_time', ''), n.get('end_time', ''))
        my_map[key] = True
    return my_map


def _fetch_all_time_slots(
    token: str,
    bookdate: str,
    resources: List[Dict[str, Any]],
    id_token: str = "",
    session: Optional[requests.Session] = None
) -> Dict[str, Tuple[str, Optional[Dict[str, Any]]]]:
    """获取所有场地的可用性数据（不含 bookedByMe）。返回 dict: rid -> (rname, slots_raw)。"""
    if not resources:
        return {}

    date_ms = _bookdate_to_ms(bookdate)

    rid_list = [(r.get('id'), r.get('resources_name')) for r in resources if r.get('id')]
    results_map: Dict[str, Tuple[str, Optional[Dict[str, Any]]]] = {}
    t3 = time.time()
    max_workers = max(1, min(15, len(rid_list)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(find_time_slots_by_resource, token, rid, date_ms, id_token=id_token, session=session): (rid, rname)
            for rid, rname in rid_list
        }
        for fut in as_completed(future_map):
            rid, rname = future_map[fut]
            detail = None
            try:
                detail = fut.result()
            except Exception as e:
                logger.warning("fetch time slots failed, resource_id=%s, error=%s", rid, e)
            results_map[rid] = (rname, detail)
    t4 = time.time()
    logger.info("[性能] 获取%d个场地时间槽: %.0fms", len(rid_list), (t4 - t3) * 1000)
    return results_map


def _merge_bookings(
    slots_data: Dict[str, Tuple[str, Optional[Dict[str, Any]]]],
    my_map: Dict[Tuple[str, str, str], bool],
    t0: Optional[float] = None
) -> List[Dict[str, Any]]:
    """合并可用性数据和 bookedByMe。"""
    out: List[Dict[str, Any]] = []
    for rid, (rname, detail) in slots_data.items():
        slots: List[Dict[str, Any]] = []
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

    if t0:
        logger.info("[性能] compute_availability_for_date 总耗时: %.0fms", (time.time() - t0) * 1000)
    return out


# ============= 预约前置校验 =============

def check_resource_time_slot_capacity(
    token: str,
    resource_id: str,
    time_slot_id_list: List[str],
    book_date: str,
    book_start_time: str,
    book_end_time: str,
    id_token: str = ""
) -> Optional[Dict[str, Any]]:
    """
    检查时段容量是否可约。

    Args:
        token: 访问令牌
        resource_id: 资源 ID
        time_slot_id_list: 时间槽 ID 列表
        book_date: 预约日期
        book_start_time: 开始时间
        book_end_time: 结束时间
        id_token: ID 令牌（可选）

    Returns:
        检查结果字典，失败返回 None
    """
    from .http_utils import requests_post_with_retry

    headers = build_headers(token)
    payload = {
        "operationName": "checkResourceTimeSlotCapacity",
        "variables": {
            "resourceId": resource_id,
            "appointmentId": "",
            "bookDate": book_date,
            "bookStartTime": book_start_time,
            "bookEndTime": book_end_time,
            "timeSlotIdList": time_slot_id_list,
            "borrowDateList": [],
            "borrowStartTime": "",
            "borrowEndTime": "",
            "checkSource": "",
        },
        "query": "query checkResourceTimeSlotCapacity($resourceId: String, $appointmentId: String, $bookDate: String, $bookStartTime: String, $bookEndTime: String, $timeSlotIdList: [String], $borrowDateList: [String], $borrowStartTime: String, $borrowEndTime: String, $checkSource: String) { checkResourceTimeSlotCapacity(resourceId: $resourceId, appointmentId: $appointmentId, bookDate: $bookDate, bookStartTime: $bookStartTime, bookEndTime: $bookEndTime, timeSlotIdList: $timeSlotIdList, borrowDateList: $borrowDateList, borrowStartTime: $borrowStartTime, borrowEndTime: $borrowEndTime, checkSource: $checkSource) { code name messages messages_en } }"
    }
    try:
        resp = requests_post_with_retry(_graphql_url(id_token), json=payload, headers=headers)
        if not resp or resp.status_code != 200:
            return None
        data = resp.json()
        return data.get("data", {}).get("checkResourceTimeSlotCapacity")
    except Exception as e:
        logger.warning("check_resource_time_slot_capacity error: %s", e)
        return None


def find_resource_detail(
    token: str,
    resource_id: str,
    id_token: str = ""
) -> Optional[Dict[str, Any]]:
    """
    获取资源详情，含 open_captcha_verify / capacity 等字段。

    Args:
        token: 访问令牌
        resource_id: 资源 ID
        id_token: ID 令牌（可选）

    Returns:
        资源详情字典，失败返回 None
    """
    from .http_utils import requests_post_with_retry

    headers = build_headers(token)
    payload = {
        "operationName": "findResources",
        "variables": {
            "id": resource_id,
        },
        "query": "query findResources($id: String!) { findResources(id: $id) { id open_captcha_verify capacity } }"
    }
    try:
        resp = requests_post_with_retry(_graphql_url(id_token), json=payload, headers=headers)
        if not resp or resp.status_code != 200:
            return None
        data = resp.json()
        # 注意：如果 GraphQL 返回 {"data": null}，data.get("data", {}) 返回 None 而不是 {}
        data_obj = data.get("data")
        if data_obj is None:
            logger.warning("find_resource_detail: GraphQL returned null data")
            return None
        return data_obj.get("findResources")
    except Exception as e:
        logger.warning("find_resource_detail error: %s", e)
        return None


# ============= 预约 API =============

def fetch_resource_time_id(
    token: str,
    bookdate: str,
    resources_name: str,
    kssj: str,
    jssj: str,
    id_token: str = ""
) -> Optional[Tuple[str, str, str]]:
    """
    获取资源和时间段 ID，以及验证码要求。

    Args:
        token: 访问令牌
        bookdate: 预约日期 (YYYY-MM-DD)
        resources_name: 资源名称
        kssj: 开始时间 (HH:MM)
        jssj: 结束时间 (HH:MM)
        id_token: ID 令牌（可选）

    Returns:
        (resource_id, time_id, open_captcha_verify) 元组，失败返回 None
    """
    from .http_utils import requests_post_with_retry

    headers = build_headers(token)
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
                "need_approve": {"eq": None}
            }
        },
        "query": "query findResourcesAllByAccount($first: Int, $offset: Int, $typeId: String, $typeName: String, $resourceName: String, $bookDate: String, $bookStartTime: String, $bookEndTime: String, $item_name: [String], $is_cyclicity: String, $cyclicity_start_date: String, $cyclicity_end_date: String, $cyclicity_start_time: String, $cyclicity_end_time: String, $cyclicity_strategy: String, $cyclicity_weekList: [String], $cyclicity_dayList: [String], $order_by: String, $cur_language: String, $filter: ResourcesFilterMap) { findResourcesAllByAccount(first: $first, offset: $offset, typeId: $typeId, typeName: $typeName, resourceName: $resourceName, bookDate: $bookDate, bookStartTime: $bookStartTime, bookEndTime: $bookEndTime, item_name: $item_name, is_cyclicity: $is_cyclicity, cyclicity_start_date: $cyclicity_start_date, cyclicity_end_date: $cyclicity_end_date, cyclicity_start_time: $cyclicity_start_time, cyclicity_end_time: $cyclicity_end_time, cyclicity_strategy: $cyclicity_strategy, cyclicity_weekList: $cyclicity_weekList, cyclicity_dayList: $cyclicity_dayList, order_by: $order_by, cur_language: $cur_language, filter: $filter) { id resources_name open_captcha_verify capacity available_number resourcesTimeSlot { id kssj jssj } } }"
    }
    response = requests_post_with_retry(_graphql_url(id_token), json=payload, headers=headers)
    if response is None or response.status_code != 200:
        logger.warning("fetch_resource_time_id request failed, status=%d", response.status_code if response else 'None')
        return None

    try:
        json_data = response.json()
    except Exception as e:
        logger.warning("fetch_resource_time_id parse error: %s", e)
        return None

    if 'data' not in json_data or 'findResourcesAllByAccount' not in json_data['data']:
        logger.warning("fetch_resource_time_id: 返回数据格式异常或无资源数据")
        return None

    resources = json_data['data']['findResourcesAllByAccount']
    for resource in resources:
        if resource.get('resources_name') == resources_name:
            resource_id = resource.get('id')
            open_captcha_verify = resource.get('open_captcha_verify', '0')
            for time_slot in resource.get('resourcesTimeSlot', []):
                if time_slot.get('kssj') == kssj and time_slot.get('jssj') == jssj:
                    time_id = time_slot.get('id')
                    logger.info("获取到预约时段信息: open_captcha_verify=%s", open_captcha_verify)
                    return resource_id, time_id, open_captcha_verify
    return None


def make_appointment(
    token: str,
    time_id: str,
    resource_id: str,
    bookdate: str,
    kssj: str,
    jssj: str,
    id_token: str = "",
    captcha_id: str = "",
    captcha_code: str = ""
) -> Dict[str, Any]:
    """
    执行预约。

    Args:
        token: 访问令牌
        time_id: 时间槽 ID
        resource_id: 资源 ID
        bookdate: 预约日期 (YYYY-MM-DD)
        kssj: 开始时间 (HH:MM)
        jssj: 结束时间 (HH:MM)
        id_token: ID 令牌（可选）
        captcha_id: 滑块验证码 ID（可选）
        captcha_code: 滑块验证码校验码（可选）

    Returns:
        预约结果字典，包含 code 和 messages 字段
    """
    from .http_utils import requests_post_with_retry

    _debug(f"appointment args date={bookdate}, start={kssj}, end={jssj}")

    user_info = resolve_user_info(token, id_token=id_token)
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

    headers = build_headers(token)
    payload = {
        "operationName": "saveAppointmentInformationAll",
        "variables": {
            "captchaId": captcha_id,
            "captchaCode": captcha_code,
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
                "need_meeting_signin": 0,
                "appointment_date": bookdate,
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
    response = requests_post_with_retry(_graphql_url(id_token), json=payload, headers=headers)
    if not response:
        return {"code": "REQUEST_FAILED", "messages": ["Appointment request failed after retries"]}

    try:
        resp_json = response.json()
    except Exception:
        return {"code": "INVALID_RESPONSE", "messages": [response.text[:200] if response.text else "Empty response"]}

    _debug(f"saveAppointmentInformationAll status={response.status_code}")
    return resp_json


# ============= 滑块验证码 API =============

def gen_slide_captcha(token: str) -> Optional[Dict[str, Any]]:
    """
    获取滑块验证码。

    Args:
        token: 访问令牌

    Returns:
        包含 id 和 captcha 的字典（含 backgroundImage 和 templateImage），失败返回 None
    """
    from .http_utils import requests_get_with_retry

    url = f"{WF_CAPTCHA_URL}/genCaptcha?token={token}"
    try:
        resp = requests_get_with_retry(url)
        if not resp:
            logger.error("获取滑块验证码失败: 无响应")
            return None
        data = resp.json()
        if not data.get("captcha"):
            logger.error("获取滑块验证码失败: %s", data.get("id", "未知错误"))
            return None
        return data
    except Exception as e:
        logger.error("获取滑块验证码异常: %s", e)
        return None


def _generate_track_list(slide_x: int, bg_width: int = 300, bg_height: int = 180) -> list:
    """生成模拟滑块拖拽轨迹（改进版 v2）。

    基于真实人类滑动轨迹数据分析，模拟以下特征：
    1. 明显的超调和回弹（50-60像素）
    2. 分阶段的移动速度
    3. 最后阶段的减速和停顿
    4. 自然的 Y 轴抖动

    Args:
        slide_x: 目标 X 坐标（像素，基于缩放后的背景图宽度）
        bg_width: 背景图显示宽度
        bg_height: 背景图显示高度

    Returns:
        trackList 数组，包含 down/move/up 事件
    """
    import random
    import math

    track_list = []

    # 边界检查
    if slide_x <= 0:
        base_t = random.randint(1000, 3000)
        track_list.append({"x": 0, "y": 0, "type": "down", "t": base_t})
        track_list.append({"x": 0, "y": 0, "type": "up", "t": base_t + random.randint(300, 800)})
        return track_list

    # ========== 参数配置（基于真实数据分析）==========
    # 基础时间（相对时间，模拟真实轨迹）
    base_t = random.randint(1000, 3000)

    # Y 轴基准位置
    y_base = random.randint(-2, 2)

    # 超调距离（基于真实数据：回弹约 50-60 像素）
    # slide_x=129 时 max_x=182，超调比例约 41%
    overshoot_ratio = random.uniform(0.35, 0.50)  # 超调比例 35%-50%
    overshoot_dist = int(slide_x * overshoot_ratio)
    max_x = slide_x + overshoot_dist

    # ========== 阶段 1：快速滑动到超调位置 ==========
    current_t = base_t
    current_x = 0
    current_y = y_base

    # down 事件
    track_list.append({"x": 0, "y": y_base, "type": "down", "t": current_t})
    current_t += random.randint(30, 80)

    # 快速移动到超调位置（约 70% 的轨迹点）
    # 使用较小的步长，每个点间隔约 8-12ms
    num_fast_moves = random.randint(150, 200)
    for i in range(1, num_fast_moves + 1):
        progress = i / num_fast_moves
        # 使用 ease-out 缓动：开始快，结束慢
        eased_progress = 1 - (1 - progress) ** 2

        target_x = int(max_x * eased_progress)
        if target_x <= current_x:
            continue

        # Y 轴抖动（逐渐增大）
        y_drift = int(current_y + random.uniform(-1, 1) * (1 + progress * 10))
        y_drift = max(-20, min(20, y_drift))

        dt = random.randint(6, 12)
        current_t += dt

        track_list.append({
            "x": target_x,
            "y": y_drift,
            "type": "move",
            "t": current_t
        })
        current_x = target_x
        current_y = y_drift

    # 确保到达超调位置
    if current_x < max_x:
        current_t += random.randint(8, 15)
        track_list.append({
            "x": max_x,
            "y": current_y,
            "type": "move",
            "t": current_t
        })
        current_x = max_x

    # ========== 阶段 2：回弹到目标位置 ==========
    # 回弹阶段速度变慢，间隔增大
    # 分多次回弹，每次回弹一小段

    num_bounce_moves = random.randint(15, 25)
    bounce_step = overshoot_dist // num_bounce_moves

    for i in range(1, num_bounce_moves + 1):
        target_x = max(slide_x, max_x - bounce_step * i)
        if target_x >= current_x:
            continue

        # Y 轴微小抖动
        y_drift = int(current_y + random.uniform(-1, 1))
        y_drift = max(-15, min(15, y_drift))

        # 回弹阶段间隔变大
        dt = random.randint(10, 25)
        current_t += dt

        track_list.append({
            "x": target_x,
            "y": y_drift,
            "type": "move",
            "t": current_t
        })
        current_x = target_x
        current_y = y_drift

    # ========== 阶段 3：微调停顿 ==========
    # 最后阶段有明显的停顿（模拟人类确认位置）

    # 短暂停顿
    current_t += random.randint(100, 300)

    # 微调到精确位置
    if current_x != slide_x:
        track_list.append({
            "x": slide_x,
            "y": current_y,
            "type": "move",
            "t": current_t + random.randint(50, 150)
        })
        current_t += random.randint(50, 150)

    # 再次停顿（模拟松开前的犹豫）
    current_t += random.randint(200, 500)

    # up 事件
    track_list.append({
        "x": slide_x,
        "y": current_y,
        "type": "up",
        "t": current_t
    })

    return track_list


def check_slide_captcha(
    token: str,
    captcha_id: str,
    slide_x: int,
    bg_image_width: int = 300,
    bg_image_height: int = 180,
    start_time: Optional[str] = None,
    stop_time: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    校验滑块验证码。

    Args:
        token: 访问令牌
        captcha_id: 验证码 ID（含 SLIDER_ 前缀）
        slide_x: 滑块 X 坐标（像素，基于缩放后的背景图宽度）
        bg_image_width: 背景图显示宽度（默认 300）
        bg_image_height: 背景图显示高度（默认 180）
        start_time: 拖拽开始时间（ISO 格式），默认自动生成
        stop_time: 拕拽结束时间（ISO 格式），默认自动生成

    Returns:
        校验结果字典，成功时包含 captchaCode，失败返回 None
    """
    from datetime import datetime, timezone, timedelta
    from .http_utils import requests_post_with_retry

    # 自动生成时间（使用 UTC 时间，带 Z 后缀）
    now = datetime.now(timezone.utc)
    if not start_time:
        start_time = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if not stop_time:
        # 模拟拖拽耗时约 2-4 秒（基于真实数据）
        stop_time = (now + timedelta(seconds=random.randint(2, 4))).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    track_list = _generate_track_list(slide_x, bg_image_width, bg_image_height)

    url = f"{WF_CAPTCHA_URL}/checkCaptcha?token={token}"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
    }
    payload = {
        "id": captcha_id,
        "data": {
            "bgImageWidth": bg_image_width,
            "bgImageHeight": bg_image_height,
            "startTime": start_time,
            "stopTime": stop_time,
            "trackList": track_list,
        },
    }
    try:
        resp = requests_post_with_retry(url, json=payload, headers=headers)
        if not resp:
            logger.error("校验滑块验证码失败: 无响应")
            return None
        data = resp.json()
        if data.get("code") == 200 or data.get("success"):
            logger.info("滑块验证码校验成功: captchaId=%s", captcha_id[:20] if captcha_id else "")
            return data
        logger.warning("滑块验证码校验失败: code=%s, msg=%s", data.get("code"), data.get("msg"))
        # 记录更多调试信息
        logger.debug("发送的轨迹: slide_x=%s, track_count=%d, start=%s, stop=%s",
                     slide_x, len(track_list), start_time, stop_time)
        return None
    except Exception as e:
        logger.error("校验滑块验证码异常: %s", e)
        return None


# ============= captchaCode 加密（复现 SPA w() 函数）=============
#
# SPA HAR line 6587 的 w():
#   let e = window["captcha_code"];
#   if (e.length < 16) return e;                       // 短码原样直传
#   var t = enc.Utf8.parse(captcha_id.substr(0, 16));  // key = captcha_id[:16] 的 UTF-8 字节
#   var i = enc.Utf8.parse(captcha_id.substr(1, 17));  // iv  = substr(1,17) 17 字符,
#                                                      // 但 crypto-js CBC 的 XOR 循环只跑
#                                                      // 4 word(=16 字节), 第 17 字节从不被读
#                                                      // -> 等价于 captcha_id[1:17] 16 字节
#   var o = AES.encrypt(e, t, {iv: i, mode: CBC, padding: Pkcs7});
#   return window["captcha_code"] = o.toString();       // 无 Salted__ 前缀 -> 纯 base64(密文)
#
# captcha_id 取 checkCaptcha 响应的 data.captchaId（无 SLIDER_ 前缀的 uuid，与
#   SPA window["captcha_id"] 一致）；captchaCode 是 36 字符 uuid -> 必走加密分支。
# 标准 AES-128-CBC 是确定性的, 纯 Python（pycryptodome / cryptography）即可逐字节复现, 无需浏览器。
def _aes_cbc_encrypt(key: bytes, iv: bytes, plaintext: bytes) -> bytes:
    """AES-128-CBC + PKCS7, 返回密文 bytes。优先 pycryptodome, 回退 cryptography。"""
    try:
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
        return AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plaintext, AES.block_size))
    except ImportError:
        pass
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding as _pad
        padder = _pad.PKCS7(algorithms.AES.block_size).padder()
        padded = padder.update(plaintext) + padder.finalize()
        enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        return enc.update(padded) + enc.finalize()
    except ImportError:
        raise RuntimeError(
            "captchaCode 加密需要 pycryptodome 或 cryptography, 请安装: uv pip install pycryptodome"
        )


def encrypt_captcha_code(captcha_id: str, captcha_code: str) -> str:
    """复现 SPA 的 w(): 对 >=16 字符的 captchaCode 做 AES-128-CBC 加密, 返回 base64。

    Args:
        captcha_id: checkCaptcha 响应里的 captchaId（无 SLIDER_ 前缀的 uuid）。
        captcha_code: checkCaptcha 响应里的 captchaCode（原始明文）。

    Returns:
        >=16 字符: base64 密文字符串（发往 booking mutation 的 captchaCode）。
        <16 字符: 原样返回（与 SPA 短路分支一致）。
    """
    if not captcha_code or len(captcha_code) < 16:
        return captcha_code or ""
    # 防御性去掉可能的 SLIDER_ 前缀（SPA 用无前缀 uuid 派生 key/iv）
    cid = captcha_id[len("SLIDER_"):] if captcha_id.startswith("SLIDER_") else captcha_id
    if len(cid) < 17:
        logger.warning("captcha_id 过短(<17), 无法派生 16 字节 key/iv, 原样直传: %r", captcha_id)
        return captcha_code
    key = cid[:16].encode("utf-8")
    iv = cid[1:17].encode("utf-8")
    try:
        cipher = _aes_cbc_encrypt(key, iv, captcha_code.encode("utf-8"))
        return base64.b64encode(cipher).decode("ascii")
    except Exception as e:
        # 加密失败仍原样直传（让服务端给出明确拒绝, 不在此 crash 中断抢单流程）;
        # ERROR 日志便于定位（多为缺加密库 -> 见 _aes_cbc_encrypt 的 RuntimeError）。
        logger.error("captchaCode AES 加密失败, 回退原样直传: %s", e)
        return captcha_code


def solve_and_verify_slide_captcha(token: str) -> Optional[Tuple[str, str]]:
    """
    自动获取、识别并校验滑块验证码。

    Args:
        token: 访问令牌

    Returns:
        (captcha_id, captcha_code) 元组，失败返回 None。
        captcha_id: checkCaptcha 响应的无 SLIDER_ 前缀 uuid（原样发往 mutation）。
        captcha_code: 已按 SPA w() 做 AES-128-CBC 加密的 base64 密文（make_appointment 原样提交）。
    """
    from .slide_captcha import solve_slide_captcha

    # 1. 获取滑块验证码
    captcha_data = gen_slide_captcha(token)
    if not captcha_data:
        logger.error("获取滑块验证码失败")
        return None

    captcha_id = captcha_data.get("id", "")
    captcha_info = captcha_data.get("captcha", {})
    bg_image = captcha_info.get("backgroundImage", "")
    tpl_image = captcha_info.get("templateImage", "")

    # 原始图片尺寸（如 600x360）和显示尺寸（如 300x180）
    bg_raw_width = captcha_info.get("backgroundImageWidth", 600)
    bg_raw_height = captcha_info.get("backgroundImageHeight", 360)
    tpl_raw_width = captcha_info.get("templateImageWidth", 110)
    tpl_raw_height = captcha_info.get("templateImageHeight", 360)

    # 显示尺寸 = 原始尺寸的一半（前端缩放比例）
    bg_display_width = bg_raw_width // 2
    bg_display_height = bg_raw_height // 2

    if not bg_image or not tpl_image:
        logger.error("滑块验证码图片缺失")
        return None

    # 2. 识别缺口位置（基于原始图片尺寸，启用调试模式）
    slide_x_raw = solve_slide_captcha(bg_image, tpl_image, debug=True)
    if slide_x_raw is None:
        logger.error("滑块缺口识别失败")
        return None

    # 将原始坐标缩放到显示尺寸
    slide_x = int(slide_x_raw * bg_display_width / bg_raw_width)

    logger.info("滑块缺口识别结果: raw_x=%d, display_x=%d (raw=%dx%d, display=%dx%d)",
                slide_x_raw, slide_x, bg_raw_width, bg_raw_height, bg_display_width, bg_display_height)

    # 3. 校验滑块验证码（发送轨迹数据）
    result = check_slide_captcha(
        token, captcha_id, slide_x,
        bg_image_width=bg_display_width,
        bg_image_height=bg_display_height,
    )
    if not result:
        logger.error("滑块验证码校验失败")
        return None

    # 4. 提取 captchaId 和 captchaCode
    result_data = result.get("data", {})
    # captchaId 取自 checkCaptcha 响应(原样透传给 mutation, 与 SPA 一致)
    result_captcha_id = result_data.get("captchaId", "")
    result_captcha_code = result_data.get("captchaCode", "")

    if not result_captcha_id or not result_captcha_code:
        logger.error("滑块验证码校验成功但未获取到 captchaId/captchaCode: %s", result)
        return None

    # 5. 复现 SPA w(): captchaCode 是 36 字符 uuid(>=16) -> AES-128-CBC 加密后返回 base64 密文。
    #    make_appointment 原样发往 mutation, 与 SPA 在 next() 里 w() 加密后提交的行为一致。
    #    （key/iv 全来自已知的 captcha_id, 无需浏览器; 详见上方 encrypt_captcha_code 注释。）
    encrypted_code = encrypt_captcha_code(result_captcha_id, result_captcha_code)
    logger.info("滑块验证码流程完成: captchaId=%s, captchaCode(raw)=%s, captchaCode(enc)=%s",
                result_captcha_id[:20] if result_captcha_id else "",
                result_captcha_code[:20] if result_captcha_code else "",
                encrypted_code[:24] if encrypted_code else "")
    return result_captcha_id, encrypted_code
