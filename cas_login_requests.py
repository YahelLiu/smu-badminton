import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs, urlencode, quote, urljoin, unquote
from lxml import html
from PIL import Image
from io import BytesIO
from cas_ocr import predict_validate_code
import os
import time
import json
import base64
import threading
from typing import Any, Dict
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import (
    WF_ORIGIN,
    WF_API_URL,
    WF_HOME_URL,
    CAS_ORIGIN,
    CAS_LOGIN_URL,
    CAS_CAPTCHA_URL,
    BADMINTON_TYPE_ID,
    OAUTH_CLIENT_ID,
)

DEBUG_BOOKING = os.getenv("BOOKING_DEBUG", "0").lower() in {"1", "true", "yes", "on"}


def _debug(msg: str):
    if DEBUG_BOOKING:
        print(f"[DEBUG] {msg}")


_TOKEN_PROFILE_CACHE: Dict[str, Dict[str, Any]] = {}
_TOKEN_PROFILE_TTL_SEC = int(os.getenv("TOKEN_PROFILE_TTL_SEC", "3600"))


def _decode_jwt_payload(token: str) -> Dict[str, Any] | None:
    """Decode JWT payload without verifying signature."""
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
    dept_code = claims.get("deptCode") or os.getenv("DEFAULT_DEPT_CODE", "")
    dept_name = claims.get("deptName") or os.getenv("DEFAULT_DEPT_NAME", "")
    dept_name_en = claims.get("deptNameEn") or os.getenv("DEFAULT_DEPT_NAME_EN", "")
    email = claims.get("email") or os.getenv("DEFAULT_USER_EMAIL", f"{user_code}@stu.shmtu.edu.cn")
    phone = claims.get("phone") or claims.get("mobile") or claims.get("telephone") or os.getenv("DEFAULT_USER_PHONE", "")

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

    with _TOKEN_LOCK:
        _TOKEN_PROFILE_CACHE[access_token] = {"profile": profile, "ts": time.time()}


def _get_profile_by_access_token(access_token: str) -> Dict[str, Any] | None:
    now = time.time()
    with _TOKEN_LOCK:
        entry = _TOKEN_PROFILE_CACHE.get(access_token)
        if entry and now - float(entry.get("ts", 0)) < float(_TOKEN_PROFILE_TTL_SEC):
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


def _build_wf_authorize_url(ret_url: str | None = None) -> str:
    ret = ret_url or f"{WF_ORIGIN}/yy-sys/pc/home"
    callback = f"{WF_ORIGIN}/yy-sys/oidc-callback?retUrl={ret}"
    params = {
        "client_id": OAUTH_CLIENT_ID,
        "redirect_uri": callback,
        "response_type": "id_token token",
        "scope": "data openid process task app submit process_edit start profile",
        "state": os.urandom(16).hex(),
        "nonce": os.urandom(16).hex(),
    }
    return f"{WF_ORIGIN}/sso/oauth2/authorize?{urlencode(params, quote_via=quote)}"


def _absolute_url(base_url: str, maybe_relative: str) -> str:
    if not maybe_relative:
        return ""
    return urljoin(base_url, maybe_relative)


def _resolve_cas_login_url(session: requests.Session, login_url: str | None, timeout: int = 20) -> str:
    """Resolve CAS login URL by following WF -> SSO redirects."""
    start = (login_url or "").strip()
    lower_start = start.lower()

    # direct CAS login URL
    if "cas.shmtu.edu.cn/cas/login" in lower_start:
        return start

    # already an oauth2 authorize URL
    if "/sso/oauth2/authorize" in lower_start:
        pass
    # already sso login URL
    elif "/sso/login" in lower_start:
        pass
    # yy-sys or other wf pages: rebuild authorize url with retUrl
    elif start and lower_start.startswith(WF_ORIGIN.lower()):
        start = _build_wf_authorize_url(ret_url=start)
    # empty/invalid input fallback
    else:
        start = _build_wf_authorize_url(ret_url=WF_HOME_URL)

    current = start
    headers = {"User-Agent": "Mozilla/5.0"}
    for _ in range(10):
        resp = session.get(current, headers=headers, timeout=timeout, allow_redirects=False)
        if "cas.shmtu.edu.cn/cas/login" in current.lower():
            return current

        location = _absolute_url(current, resp.headers.get("Location", ""))
        if not location:
            if resp.status_code == 200 and "cas.shmtu.edu.cn/cas/login" in current.lower():
                return current
            break

        if "cas.shmtu.edu.cn/cas/login" in location.lower():
            return location
        current = location

    raise RuntimeError("Cannot resolve CAS login URL from WF redirect chain")


def get_user_info_from_appointment(token):
    """Try to infer user profile from existing appointment records."""
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
        response = requests.post(WF_API_URL, json=payload, headers=headers, timeout=15)
        if response.status_code != 200:
            _debug(f"get_user_info_from_appointment failed, status={response.status_code}")
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
    """Resolve appointment user info from API first, then from JWT claims."""
    user_info = get_user_info_from_appointment(token)
    if user_info:
        return user_info

    profile = _get_profile_by_access_token(token)
    user_info = _build_user_info_from_profile(profile)
    if user_info:
        _debug("resolved user info from token profile fallback")
    return user_info


def get_captcha_and_params(login_url, captcha_url):
    """Resolve CAS login URL from WF flow, then fetch captcha/execution."""
    session = requests.Session()
    captcha_url = (captcha_url or CAS_CAPTCHA_URL).strip()

    cas_login_url = _resolve_cas_login_url(session, login_url, timeout=20)

    # 1. fetch CAS login page and parse hidden params
    resp = session.get(cas_login_url, timeout=20)
    tree = html.fromstring(resp.text)

    execution_candidates = tree.xpath("//input[@name='execution']/@value")
    if not execution_candidates:
        execution_candidates = tree.xpath('//*[@id="login-form-controls"]//input[@name="execution"]/@value')
    if not execution_candidates:
        raise RuntimeError("execution not found on CAS login page")
    execution_value = execution_candidates[0]

    # 2. download captcha and OCR in memory
    captcha_response = session.get(captcha_url, timeout=15)
    result, *_ = predict_validate_code(captcha_response.content)

    return session, cas_login_url, execution_value, result


def cas_login(login_url, captcha_url, username, password):
    """Login via WF->SSO->CAS redirect chain and get OIDC tokens."""
    session, cas_login_url, execution_value, captcha = get_captcha_and_params(login_url, captcha_url)

    data = {
        'username': username,
        'password': password,
        'execution': execution_value,
        '_eventId': 'submit',
        'validateCode': captcha,
    }

    post_resp = session.post(cas_login_url, data=data, allow_redirects=False, timeout=20)
    if post_resp.status_code not in (301, 302, 303) or 'Location' not in post_resp.headers:
        _debug(f"cas_login failed status={post_resp.status_code}")
        return session, None

    redirect_url = _absolute_url(cas_login_url, post_resp.headers['Location'])
    final_url, _ = follow_redirects(session, redirect_url)
    tokens_direct = extract_oidc_tokens(final_url)
    if tokens_direct:
        return session, tokens_direct

    parsed = urlparse(final_url)
    query = parse_qs(parsed.query)
    if 'redirect_uri' in query and 'ticket' in query:
        redirect_uri = unquote(query['redirect_uri'][0])
        ticket = query['ticket'][0]
        oidc_url = f"{redirect_uri}&ticket={ticket}"
        resp = session.get(oidc_url, allow_redirects=True, timeout=20)
        tokens = extract_oidc_tokens(resp.url)
        if tokens:
            return session, tokens

    _debug("cas_login finished but tokens not found")
    return session, None


def follow_redirects(session, start_url):
    """Follow redirect chain and return final URL and response body."""
    current_url = start_url
    max_redirects = 10
    redirect_count = 0

    while redirect_count < max_redirects:
        resp = session.get(current_url, allow_redirects=False, timeout=20)
        if resp.status_code in (301, 302, 303) and 'Location' in resp.headers:
            current_url = _absolute_url(current_url, resp.headers['Location'])
            redirect_count += 1
        else:
            return current_url, resp.text

    return current_url, ""


def extract_oidc_tokens(url):
    """Extract OIDC tokens from URL fragment or query parameters."""
    parsed_url = urlparse(url)
    
    # 妫€鏌RL fragment涓槸鍚﹀寘鍚玹oken
    if parsed_url.fragment:
        fragment_params = parse_qs(parsed_url.fragment)
        
        tokens = {}
        if 'access_token' in fragment_params:
            tokens['access_token'] = fragment_params['access_token'][0]
        if 'id_token' in fragment_params:
            tokens['id_token'] = fragment_params['id_token'][0]
        
        return tokens
    
    # 妫€鏌RL query鍙傛暟涓槸鍚﹀寘鍚玹oken
    query_params = parse_qs(parsed_url.query)
    tokens = {}
    if 'access_token' in query_params:
        tokens['access_token'] = query_params['access_token'][0]
    if 'id_token' in query_params:
        tokens['id_token'] = query_params['id_token'][0]
    
    return tokens if tokens else None

def requests_post_with_retry(url, json, headers, max_retries=10, retry_interval=1):
    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=json, headers=headers, timeout=15)
            if response.status_code == 200:
                return response
            else:
                print(f"request failed status={response.status_code}, retrying ({attempt + 1}/{max_retries})")
        except Exception as e:
            print(f"request exception={e}, retrying ({attempt + 1}/{max_retries})")
        time.sleep(retry_interval)
    print("request failed too many times, giving up")
    return None

def requests_get_with_retry(url, max_retries=10, retry_interval=1):
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=15)
            if response.status_code == 200:
                return response
            else:
                print(f"request failed status={response.status_code}, retrying ({attempt + 1}/{max_retries})")
        except Exception as e:
            print(f"request exception={e}, retrying ({attempt + 1}/{max_retries})")
        time.sleep(retry_interval)
    print("request failed too many times, giving up")
    return None

def find_time_slots_by_resource(token, resources_id, date_ms):
    """Query a resource's timeslots and available count by date timestamp."""
    url = WF_API_URL
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
    resp = requests_post_with_retry(url, json=payload, headers=headers)
    if not resp:
        return None
    return resp.json()

def list_resources_by_account(token, bookdate, type_id=None):
    """
    鍩轰簬 findResourcesAllByAccount 鑾峰彇鎸囧畾鏃ユ湡鐨勮祫婧愬垪琛紙鍖呭惈鏃堕棿娈碉級銆?
    杩斿洖 JSON 鏁版嵁缁撴瀯涓殑 resources 鍒楄〃锛屽け璐ヨ繑鍥?None銆?
    """
    if type_id is None:
        type_id = BADMINTON_TYPE_ID
    url = WF_API_URL
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
    resp = requests_post_with_retry(url, json=payload, headers=headers)
    if not resp:
        return None
    data = resp.json()
    if 'data' not in data or 'findResourcesAllByAccount' not in data['data']:
        return None
    return data['data']['findResourcesAllByAccount']

def check_resource_availability_on_date(token, resources_id, bookdate):
    """
    鏌ヨ鏌愪釜璧勬簮鍦ㄦ寚瀹氭棩鏈熺殑鎵€鏈夋椂闂存鏄惁鍙绾︺€?
    杩斿洖鍒楄〃: [{kssj, jssj, canAppointmentNumber}]
    """
    from datetime import datetime
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
    """Find resource id by display name on a specific date."""
    resources = list_resources_by_account(token, bookdate)
    if not resources:
        return None
    for r in resources:
        if r.get('resources_name') == resources_name:
            return r.get('id')
    return None

def demo_check_availability(token, bookdate, resources_name=None):
    """
    娴嬭瘯绋嬪簭锛?
    - 鑻ユ彁渚?resources_name锛氭煡璇㈠叾璧勬簮ID骞惰緭鍑鸿璧勬簮褰撳ぉ鎵€鏈夋椂闂存鍙绾︽暟閲?
    - 鑻ヤ笉鎻愪緵锛氳緭鍑哄綋澶╂墍鏈夎祫婧愬強鍏舵瘡涓椂闂存鐨勫彲棰勭害鏁伴噺
    """
    if resources_name:
        resources_id = find_resources_id_by_name(token, bookdate, resources_name)
        if not resources_id:
            print(f"resource not found: {resources_name}")
            return
        results = check_resource_availability_on_date(token, resources_id, bookdate)
        print(f"resource {resources_name} ({resources_id}) on {bookdate}:")
        for row in results:
            print(f"  {row['kssj']}-{row['jssj']}: {row['canAppointmentNumber']}")
    else:
        resources = list_resources_by_account(token, bookdate)
        if not resources:
            print("鏈幏鍙栧埌璧勬簮鍒楄〃")
            return
        for r in resources:
            rid = r.get('id')
            rname = r.get('resources_name')
            results = check_resource_availability_on_date(token, rid, bookdate)
            print(f"resource {rname} ({rid}) on {bookdate}:")
            for row in results:
                print(f"  {row['kssj']}-{row['jssj']}: {row['canAppointmentNumber']}")

def list_appointments_for_account(token, bookdate):
    """
    鎷夊彇褰撳墠璐︽埛鍦ㄦ寚瀹氭棩鏈熺殑棰勭害璁板綍锛岃繑鍥?edges 鍒楄〃銆?
    """
    from datetime import datetime
    # 灏?YYYY-MM-DD 杞崲涓哄綋澶?00:00:00 鐨勬绉掓椂闂存埑浠ヤ究瀵规瘮
    dt = datetime.strptime(bookdate, "%Y-%m-%d")
    bookdate_ms = int(dt.timestamp() * 1000)

    url = WF_API_URL
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
    resp = requests_post_with_retry(url, json=payload, headers=headers)
    if not resp:
        return []
    data = resp.json()
    edges = data.get('data', {}).get('findAppointmentInformationAllForAccount', {}).get('edges', [])
    # 杩囨护鍚屼竴澶╃殑棰勭害锛坅ppointment_date 涓烘绉掞級
    same_day = [e for e in edges if abs(int(e.get('node', {}).get('appointment_date', 0)) - bookdate_ms) < 24*60*60*1000]
    return same_day

def compute_availability_for_date(token, bookdate):
    # 骞惰鑾峰彇璧勬簮鍒楄〃鍜岀敤鎴烽绾﹁褰?
    with ThreadPoolExecutor(max_workers=2) as init_executor:
        resources_future = init_executor.submit(list_resources_by_account, token, bookdate)
        appointments_future = init_executor.submit(list_appointments_for_account, token, bookdate)
        resources = resources_future.result()
        my_edges = appointments_future.result()
    
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
    # 澧炲姞骞跺彂鏁板埌15锛屾墍鏈夊満鍦板悓鏃惰姹?
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
    return out


def fetch_resource_time_id(token, bookdate, resources_name, kssj, jssj):
    url = WF_API_URL
    headers = {
        "Authorization":f"Bearer {token}",
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
    response = requests_post_with_retry(url, json=payload, headers=headers)
    if response.status_code != 200:
        print(f"request failed, status={response.status_code}")
        return None

    json_data = response.json()
    if 'data' not in json_data or 'findResourcesAllByAccount' not in json_data['data']:
        print("杩斿洖鏁版嵁鏍煎紡寮傚父鎴栨棤璧勬簮鏁版嵁")
        return None

    resources = json_data['data']['findResourcesAllByAccount']
    for resource in resources:
        if resource.get('resources_name') == resources_name:
            resource_id = resource.get('id')
            for time_slot in resource.get('resourcesTimeSlot', []):
                if time_slot.get('kssj') == kssj and time_slot.get('jssj') == jssj:
                    time_id = time_slot.get('id')
                    print("鑾峰緱棰勭害鍒楄〃鍐呭")
                    return resource_id, time_id
    return None

def make_appointment(token, time_id, resource_id, bookdata, kssj, jssj):
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


def get_network_time():
    url = "https://cube.meituan.com/ipromotion/cube/toc/component/base/getServerCurrentTime"
    try:
        response = requests_get_with_retry(url)
        response.raise_for_status()
        json_data = response.json()
        timestamp_ms = int(json_data['data'])
        timestamp_s = timestamp_ms / 1000
        dt_utc = datetime.fromtimestamp(timestamp_s, tz=timezone.utc)
        dt_local = dt_utc.astimezone(timezone(timedelta(hours=8)))  # 鍋囪涓滃叓鍖?
        return dt_local
    except Exception as e:
        print("鑾峰彇缃戠粶鏃堕棿澶辫触:", e)
        return None

def get_target_datetime_from_network(target_time_str, bookdate=None):
    """
    璁＄畻鐩爣鎶㈢エ鏃堕棿銆?
    
    濡傛灉鎻愪緵浜?bookdate锛堥绾︽棩鏈燂級锛屽垯鐩爣鏃堕棿涓?bookdate - 7澶?+ target_time_str銆?
    渚嬪锛氶绾?2025-12-18锛宼arget_time_str='21:00:00'锛屽垯鐩爣鏃堕棿涓?2025-12-11 21:00:00銆?
    
    濡傛灉鏈彁渚?bookdate锛屽垯浣跨敤褰撳墠缃戠粶鏃ユ湡 + target_time_str锛堝吋瀹规棫閫昏緫锛夈€?
    """
    beijing_tz = timezone(timedelta(hours=8))
    
    if bookdate:
        # 棰勭害鏃ユ湡鍓?澶╃殑鎸囧畾鏃堕棿
        book_date_obj = datetime.strptime(bookdate, "%Y-%m-%d")
        target_date = book_date_obj - timedelta(days=7)
        target_date_str = target_date.strftime("%Y-%m-%d")
    else:
        # 鍏煎鏃ч€昏緫锛氫娇鐢ㄥ綋鍓嶇綉缁滄棩鏈?
        now = None
        while now is None:
            now = get_network_time()
            if now is None:
                time.sleep(1)
        target_date_str = now.strftime("%Y-%m-%d")
    
    full_datetime_str = f"{target_date_str} {target_time_str}"
    target_time = datetime.strptime(full_datetime_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=beijing_tz)
    return target_time

def login_with_retry(login_url, captcha_url, username, password, max_retries=5):
    prefer_stable_first = os.getenv("CAS_LOGIN_STABLE_FIRST", "0").lower() in {"1", "true", "yes", "on"}
    primary = cas_login_stable if prefer_stable_first else cas_login
    secondary = cas_login if prefer_stable_first else cas_login_stable

    for attempt in range(1, max_retries + 1):
        _debug(f"login attempt {attempt}/{max_retries}")

        for login_func, tag in ((primary, "primary"), (secondary, "fallback")):
            try:
                _debug(f"login path={tag}, func={login_func.__name__}")
                _session, tokens = login_func(login_url, captcha_url, username, password)
            except Exception as e:
                _debug(f"login func={login_func.__name__} exception: {e}")
                tokens = None

            if tokens and tokens.get("access_token"):
                _cache_profile_from_tokens(tokens)
                return tokens

        time.sleep(0.3)

    return None


_TOKEN_CACHE = {}
_TOKEN_LOCK = threading.Lock()

def get_token_cached(login_url, captcha_url, username, password, ttl_seconds=900):
    now = time.time()
    tokens_cached = None
    with _TOKEN_LOCK:
        entry = _TOKEN_CACHE.get(username)
        if entry and now - float(entry.get("ts", 0)) < float(ttl_seconds) and entry.get("tokens") and entry["tokens"].get("access_token"):
            tokens_cached = entry["tokens"]
    if tokens_cached:
        _cache_profile_from_tokens(tokens_cached)
        return tokens_cached

    tokens = login_with_retry(login_url, captcha_url, username, password, max_retries=5)
    if not tokens or not tokens.get("access_token"):
        return None

    with _TOKEN_LOCK:
        _TOKEN_CACHE[username] = {"tokens": tokens, "ts": now}
    _cache_profile_from_tokens(tokens)
    return tokens


def clear_token_cache(username: str = None):
    """Clear token cache. If username is provided, clear only that user."""
    with _TOKEN_LOCK:
        if username:
            entry = _TOKEN_CACHE.pop(username, None)
            if entry and entry.get("tokens") and entry["tokens"].get("access_token"):
                _TOKEN_PROFILE_CACHE.pop(entry["tokens"]["access_token"], None)
        else:
            _TOKEN_CACHE.clear()
            _TOKEN_PROFILE_CACHE.clear()


# 绾跨▼鏁帮紝寮哄埗涓?锛岄伩鍏嶅悓涓€璧勬簮鏃舵澶氬苟鍙?
num_threads = 5
barrier = threading.Barrier(num_threads)

def book_task_with_network_date(thread_id, target_time_str, token, bookdate, kssj, jssj, resource_id, time_id):
    target_time = get_target_datetime_from_network(target_time_str, bookdate)
    print(f"绾跨▼{thread_id} 鐨勭洰鏍囨椂闂达細{target_time}")
    while True:
        now = get_network_time()
        if now:
            diff_sec = (target_time - now).total_seconds()
            if diff_sec <= 0:
                break
            elif diff_sec > 10:
                print("鏃堕棿宸窛杩囧ぇ锛屼紤鎭?S")
                time.sleep(5)
            elif diff_sec > 1:
                print("浼戞伅0.5S")
                time.sleep(0.5)
            else:
                print("浼戞伅0.1S")
                time.sleep(0.1)
        else:
            print("鏈幏寰楃綉缁滄椂闂达紝璇锋鏌ュ搴旂殑鎿嶄綔")
            time.sleep(1)

    print(f"thread {thread_id} reached target time, waiting barrier")
    barrier.wait()
    print(f"thread {thread_id} starts booking")
    response = make_appointment(token, time_id, resource_id, bookdate, kssj, jssj)
    print(f"thread {thread_id} booking response: {response}")

def run_concurrent_booking_threads(target_time_str, token, bookdate, kssj, jssj, resources_name):
    # 鍏堣皟鐢ㄤ竴娆¤幏鍙栬祫婧怚D鍜屾椂闂碔D
    result = fetch_resource_time_id(token, bookdate, resources_name, kssj, jssj)
    if not result:
        print("failed to get resource_id/time_id")
        return
    resource_id, time_id = result

    threads = []
    for i in range(num_threads):
        t = threading.Thread(
            target=book_task_with_network_date,
            args=(i+1, target_time_str, token, bookdate, kssj, jssj, resource_id, time_id)
        )
        t.start()
        threads.append(t)

    for t in threads:
        t.join()

    print("all booking threads finished")

def test_user_info(token):
    """娴嬭瘯鐢ㄦ埛淇℃伅鑾峰彇鍔熻兘"""
    print("娴嬭瘯鐢ㄦ埛淇℃伅鑾峰彇...")
    user_info = get_user_info_from_appointment(token)
    if user_info:
        print("鎴愬姛鑾峰彇鐢ㄦ埛淇℃伅:")
        for key, value in user_info.items():
            if key != 'participant_info':
                print(f"  {key}: {value}")
        print("  participant_info:")
        for key, value in user_info['participant_info'].items():
            print(f"    {key}: {value}")
    else:
        print("鑾峰彇鐢ㄦ埛淇℃伅澶辫触")

if __name__ == '__main__':
    # 閰嶇疆淇℃伅锛堜粠鐜鍙橀噺璇诲彇锛?
    LOGIN_URL = os.getenv("LOGIN_URL", WF_HOME_URL or CAS_LOGIN_URL)
    CAPTCHA_URL = CAS_CAPTCHA_URL
    USERNAME = os.getenv('CAS_USERNAME', '202300000000')
    PASSWORD = os.getenv('CAS_PASSWORD', 'XXXXXXX')
    bookdate = "2025-12-06"
    kssj = '16:00'
    jssj = '17:00'
    resources_name = "羽毛球10号场地"
    target_time_str = "12:12:00"  # 浣犳兂鎶㈢エ鐨勫噯鐐规椂闂?
    
    # 鐧诲綍鑾峰彇token
    print("寮€濮嬬櫥褰?..")
    tokens = login_with_retry(LOGIN_URL, CAPTCHA_URL, USERNAME, PASSWORD, max_retries=5)
    
    if tokens and tokens.get('access_token'):
        print("\n" + "="*50)
        print("鐧诲綍鎴愬姛锛佸紑濮嬫祴璇曠敤鎴蜂俊鎭幏鍙?..")
        
        # 娴嬭瘯鐢ㄦ埛淇℃伅鑾峰彇
        test_user_info(tokens['access_token'])
        
        print("\n" + "="*50)
        print("寮€濮嬫姠绁ㄦ祦绋?..")
        
        # 寮€濮嬫姠绁?
        run_concurrent_booking_threads(target_time_str, tokens['access_token'], bookdate, kssj, jssj, resources_name)
    else:
        print("login failed, cannot continue")
# ---- Stable login fallback (keeps original cas_login) ----
from lxml import html as lxml_html

def _stable_extract_execution(html_text: str):
    tree = lxml_html.fromstring(html_text)
    xps = [
        "//input[@name='execution']/@value",
        "//input[@id='execution']/@value",
        "//*[@id='login-form-controls']//input[@name='execution']/@value",
        "//form//input[@name='execution']/@value",
    ]
    for xp in xps:
        vals = tree.xpath(xp)
        if vals:
            return vals[0]
    return None


def _stable_detect_event_order(html_text: str):
    return ["passwordlessLogin", "submit"] if "passwordlessLogin" in html_text else ["submit", "passwordlessLogin"]


def _stable_download_captcha(session: requests.Session, captcha_url: str) -> str:
    captcha_url = (captcha_url or CAS_CAPTCHA_URL).strip()
    # OCR captcha in memory
    r = session.get(captcha_url, timeout=15)
    code, *_ = predict_validate_code(r.content)
    return code


def cas_login_stable(login_url, captcha_url, username, password):
    """More resilient login via WF->SSO->CAS redirect chain."""
    session = None
    for _attempt in range(1, 4):
        session = requests.Session()
        headers_get = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        }
        try:
            cas_url = _resolve_cas_login_url(session, login_url, timeout=20)
            login_resp = session.get(cas_url, headers=headers_get, timeout=20)
        except Exception as e:
            _debug(f"cas_login_stable resolve/get failed: {e}")
            continue

        execution_value = _stable_extract_execution(login_resp.text)
        if not execution_value:
            continue

        event_order = _stable_detect_event_order(login_resp.text)
        captcha = _stable_download_captcha(session, captcha_url)
        headers_post = {
            "User-Agent": headers_get["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": CAS_ORIGIN,
            "Referer": cas_url,
        }

        for evt in event_order:
            data = {
                "username": username,
                "password": password,
                "execution": execution_value,
                "_eventId": evt,
                "validateCode": captcha,
            }
            post_resp = session.post(cas_url, data=data, headers=headers_post, allow_redirects=False, timeout=20)
            if post_resp.status_code in (301, 302, 303) and "Location" in post_resp.headers:
                redirect_url = _absolute_url(cas_url, post_resp.headers["Location"])
                final_url, _ = follow_redirects(session, redirect_url)
                tokens_direct = extract_oidc_tokens(final_url)
                if tokens_direct:
                    return session, tokens_direct

                parsed = urlparse(final_url)
                query = parse_qs(parsed.query)
                if "redirect_uri" in query and "ticket" in query:
                    redirect_uri = unquote(query["redirect_uri"][0])
                    ticket = query["ticket"][0]
                    oidc_url = f"{redirect_uri}&ticket={ticket}"
                    r2 = session.get(oidc_url, allow_redirects=True, timeout=20)
                    tokens = extract_oidc_tokens(r2.url)
                    if tokens:
                        return session, tokens
                return session, None

        time.sleep(0.5)

    return session or requests.Session(), None

