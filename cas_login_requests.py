import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, parse_qs
from lxml import html
from PIL import Image
from io import BytesIO
from cas_ocr import predict_validate_code
import os
import time
import json
import threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from config import WF_ORIGIN, WF_API_URL, CAS_ORIGIN, CAS_LOGIN_URL, CAS_CAPTCHA_URL, BADMINTON_TYPE_ID

def get_user_info_from_appointment(token):
    """通过查询预约信息获取用户信息"""
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
    
    url = WF_API_URL
    try:
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code == 200:
            data = response.json()
            if 'data' in data and 'findAppointmentInformationAllForAccount' in data['data']:
                edges = data['data']['findAppointmentInformationAllForAccount']['edges']
                if edges and len(edges) > 0:
                    node = edges[0]['node']
                    return {
                        'created_user': node.get('created_user', ''),
                        'created_user_name': node.get('created_user_name', ''),
                        'appointment_user': node.get('appointment_user', ''),
                        'appointment_user_name': node.get('appointment_user_name', ''),
                        'dept_code': node.get('dept_code', ''),
                        'dept_name': node.get('dept_name', ''),
                        'dept_name_en': node.get('dept_name_en', ''),
                        'email': node.get('email', ''),
                        'phone': node.get('phone', ''),
                        'participant_info': node.get('appointmentParticipantList', [{}])[0] if node.get('appointmentParticipantList') else {}
                    }
        print(f"获取用户信息失败: {response.status_code}")
        return None
    except Exception as e:
        print(f"获取用户信息异常: {e}")
        return None

def get_captcha_and_params(login_url, captcha_url):
    """获取验证码和登录表单参数"""
    session = requests.Session()
    
    # 1. 获取登录页，解析隐藏参数
    resp = session.get(login_url)
    tree = html.fromstring(resp.text)
    
    # 提取execution值（使用你提供的xpath）
    execution_value = tree.xpath('//*[@id="login-form-controls"]/section[5]/input[1]/@value')[0]

    # 2. 获取验证码图片（直接在内存中处理，不保存到文件）
    captcha_response = session.get(captcha_url)
    result, expr, *_ = predict_validate_code(captcha_response.content)

    return session, execution_value, result

def cas_login(login_url, captcha_url, username, password):
    """CAS登录并获取OIDC token"""
    # 1. 获取验证码和参数
    session, execution_value, captcha = get_captcha_and_params(login_url, captcha_url)

    # 2. 构造登录表单数据
    data = {
        'username': username,
        'password': password,
        'execution': execution_value,
        '_eventId': 'submit',
        'validateCode': captcha  # 验证码字段名应该是validateCode，不是captcha
    }
    
    # 3. 提交登录表单
    post_resp = session.post(login_url, data=data, allow_redirects=False)
    
    # 4. 检查登录结果
    if post_resp.status_code == 302 and 'Location' in post_resp.headers:
        redirect_url = post_resp.headers['Location']
        
        # 5. 跟踪重定向链，获取最终的OIDC回调URL和响应内容
        final_url, response_content = follow_redirects(session, redirect_url)
        # 8. 进一步：用ticket访问redirect_uri，获取token
        from urllib.parse import parse_qs, urlparse, unquote
        parsed = urlparse(final_url)
        query = parse_qs(parsed.query)
        if 'redirect_uri' in query and 'ticket' in query:
            redirect_uri = unquote(query['redirect_uri'][0])
            ticket = query['ticket'][0]
            # 拼接带ticket的redirect_uri
            oidc_url = f"{redirect_uri}&ticket={ticket}"
            resp = session.get(oidc_url, allow_redirects=True)
            # 再次尝试从URL和内容中提取token
            tokens = extract_oidc_tokens(resp.url)
            if tokens:
                print("最终获取到OIDC token：")
                print("Access Token:", tokens.get('access_token'))
                print("ID Token:", tokens.get('id_token'))
                return session, tokens
        else:
            print("未能从URL或响应内容中提取到OIDC token")
            return session, None
    else:
        print("登录失败，状态码：", post_resp.status_code)
        print("响应内容：", post_resp.text)
        return session, None

def follow_redirects(session, start_url):
    """跟踪重定向链，获取最终URL和响应内容"""
    current_url = start_url
    max_redirects = 10
    redirect_count = 0
    
    while redirect_count < max_redirects:
        
        resp = session.get(current_url, allow_redirects=False)
        
        if resp.status_code == 302 and 'Location' in resp.headers:
            current_url = resp.headers['Location']
            redirect_count += 1
        else:
            # 没有更多重定向，返回当前URL和响应内容
            return current_url, resp.text
    
    return current_url, ""

def extract_oidc_tokens(url):
    """从URL中提取OIDC token（如果使用fragment方式）"""
    parsed_url = urlparse(url)
    
    # 检查URL fragment中是否包含token
    if parsed_url.fragment:
        fragment_params = parse_qs(parsed_url.fragment)
        
        tokens = {}
        if 'access_token' in fragment_params:
            tokens['access_token'] = fragment_params['access_token'][0]
        if 'id_token' in fragment_params:
            tokens['id_token'] = fragment_params['id_token'][0]
        
        return tokens
    
    # 检查URL query参数中是否包含token
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
                print(f"请求失败，状态码：{response.status_code}，重试中...({attempt+1}/{max_retries})")
        except Exception as e:
            print(f"请求异常：{e}，重试中...({attempt+1}/{max_retries})")
        time.sleep(retry_interval)
    print("请求多次失败，放弃。")
    return None

def requests_get_with_retry(url, max_retries=10, retry_interval=1):
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=15)
            if response.status_code == 200:
                return response
            else:
                print(f"请求失败，状态码：{response.status_code}，重试中...({attempt+1}/{max_retries})")
        except Exception as e:
            print(f"请求异常：{e}，重试中...({attempt+1}/{max_retries})")
        time.sleep(retry_interval)
    print("请求多次失败，放弃。")
    return None

def find_time_slots_by_resource(token, resources_id, date_ms):
    """
    根据资源ID与日期(毫秒时间戳)查询该资源当日的时间段与可预约数量。
    返回后端JSON或None。
    """
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
    基于 findResourcesAllByAccount 获取指定日期的资源列表（包含时间段）。
    返回 JSON 数据结构中的 resources 列表，失败返回 None。
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
    查询某个资源在指定日期的所有时间段是否可预约。
    返回列表: [{kssj, jssj, canAppointmentNumber}]
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
    """在指定日期下，通过名称查找资源ID。找不到返回 None。"""
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
    - 若提供 resources_name：查询其资源ID并输出该资源当天所有时间段可预约数量
    - 若不提供：输出当天所有资源及其每个时间段的可预约数量
    """
    if resources_name:
        resources_id = find_resources_id_by_name(token, bookdate, resources_name)
        if not resources_id:
            print(f"未找到资源: {resources_name}")
            return
        results = check_resource_availability_on_date(token, resources_id, bookdate)
        print(f"资源 {resources_name} ({resources_id}) 在 {bookdate} 的可预约情况：")
        for row in results:
            print(f"  {row['kssj']}-{row['jssj']}: {row['canAppointmentNumber']}")
    else:
        resources = list_resources_by_account(token, bookdate)
        if not resources:
            print("未获取到资源列表")
            return
        for r in resources:
            rid = r.get('id')
            rname = r.get('resources_name')
            results = check_resource_availability_on_date(token, rid, bookdate)
            print(f"资源 {rname} ({rid}) 在 {bookdate}：")
            for row in results:
                print(f"  {row['kssj']}-{row['jssj']}: {row['canAppointmentNumber']}")

def list_appointments_for_account(token, bookdate):
    """
    拉取当前账户在指定日期的预约记录，返回 edges 列表。
    """
    from datetime import datetime
    # 将 YYYY-MM-DD 转换为当天 00:00:00 的毫秒时间戳以便对比
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
    # 过滤同一天的预约（appointment_date 为毫秒）
    same_day = [e for e in edges if abs(int(e.get('node', {}).get('appointment_date', 0)) - bookdate_ms) < 24*60*60*1000]
    return same_day

def compute_availability_for_date(token, bookdate):
    # 并行获取资源列表和用户预约记录
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
    # 增加并发数到15，所有场地同时请求
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
        print(f"请求失败，状态码：{response.status_code}")
        return None

    json_data = response.json()
    if 'data' not in json_data or 'findResourcesAllByAccount' not in json_data['data']:
        print("返回数据格式异常或无资源数据")
        return None

    resources = json_data['data']['findResourcesAllByAccount']
    for resource in resources:
        if resource.get('resources_name') == resources_name:
            resource_id = resource.get('id')
            for time_slot in resource.get('resourcesTimeSlot', []):
                if time_slot.get('kssj') == kssj and time_slot.get('jssj') == jssj:
                    time_id = time_slot.get('id')
                    print("获得预约列表内容")
                    return resource_id, time_id
    return None

def make_appointment(token, time_id, resource_id, bookdata, kssj, jssj):
    # 调试输出
    print(f"[DEBUG] 预约参数 - 日期: {bookdata}, 开始: {kssj}, 结束: {jssj}")
    print(f"[DEBUG] resource_id: {resource_id}, time_id: {time_id}")
    
    # 获取用户信息
    user_info = get_user_info_from_appointment(token)
    if not user_info:
        print("无法获取用户信息，使用默认值")
        # 使用默认值作为备选
        user_info = {
            'created_user': '202300000001',
            'created_user_name': '张伟',
            'appointment_user': '202300000001',
            'appointment_user_name': '张伟',
            'dept_code': '400100',
            'dept_name': '物流科学与工程研究院',
            'dept_name_en': '',
            'email': 'student001@stu.shmtu.edu.cn',
            'phone': '13800001234',
            'participant_info': {
                'participant_id': '202300000001',
                'participant_name': '张伟',
                'participant_dept_id': '400100',
                'participant_dept_name': '物流科学与工程研究院',
                'mobile': '13800001234',
                'email': 'student001@stu.shmtu.edu.cn',
                'operate_user_id': '202300000001',
                'operate_user_name': '张伟'
            }
        }

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
                "created_user": user_info['created_user'],
                "created_user_name": user_info['created_user_name'],
                "appointment_user": user_info['appointment_user'],
                "appointment_user_name": user_info['appointment_user_name'],
                "state": 0,
                "resources_id": resource_id,
                "dept_code": user_info['dept_code'],
                "dept_name": user_info['dept_name'],
                "dept_name_en": user_info['dept_name_en'],
                "email": user_info['email'],
                "phone": user_info['phone'],
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
                        "participant_id": user_info['participant_info']['participant_id'],
                        "participant_name": user_info['participant_info']['participant_name'],
                        "participant_dept_id": user_info['participant_info']['participant_dept_id'],
                        "participant_dept_name": user_info['participant_info']['participant_dept_name'],
                        "operate_user_id": user_info['participant_info']['operate_user_id'],
                        "operate_user_name": user_info['participant_info']['operate_user_name'],
                        "mobile": user_info['participant_info']['mobile'],
                        "email": user_info['participant_info']['email']
                    }
                ],
                "appointmentCollectionList": [],
                "appointment_date": bookdata,
                "start_time": kssj,
                "end_time": jssj
            }
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
        }"""
    }

    url = WF_API_URL
    
    # 添加调试输出 - 打印关键参数
    print(f"[DEBUG] Payload variables:")
    print(f"  - timeSlotIdList: {payload['variables']['timeSlotIdList']}")
    print(f"[DEBUG] Model fields:")
    print(f"  - resources_id: {payload['variables']['model']['resources_id']}")
    print(f"  - appointment_date: {payload['variables']['model']['appointment_date']}")
    print(f"  - start_time: {payload['variables']['model']['start_time']}")
    print(f"  - end_time: {payload['variables']['model']['end_time']}")
    print(f"  - appointmentParticipantList length: {len(payload['variables']['model']['appointmentParticipantList'])}")
    
    response = requests_post_with_retry(url, json=payload, headers=headers)

    print("Status Code:", response.status_code)
    print("Response:", response.json())
    return response.json()

def get_network_time():
    url = "https://cube.meituan.com/ipromotion/cube/toc/component/base/getServerCurrentTime"
    try:
        response = requests_get_with_retry(url)
        response.raise_for_status()
        json_data = response.json()
        timestamp_ms = int(json_data['data'])
        timestamp_s = timestamp_ms / 1000
        dt_utc = datetime.fromtimestamp(timestamp_s, tz=timezone.utc)
        dt_local = dt_utc.astimezone(timezone(timedelta(hours=8)))  # 假设东八区
        return dt_local
    except Exception as e:
        print("获取网络时间失败:", e)
        return None

def get_target_datetime_from_network(target_time_str, bookdate=None):
    """
    计算目标抢票时间。
    
    如果提供了 bookdate（预约日期），则目标时间为 bookdate - 7天 + target_time_str。
    例如：预约 2025-12-18，target_time_str='21:00:00'，则目标时间为 2025-12-11 21:00:00。
    
    如果未提供 bookdate，则使用当前网络日期 + target_time_str（兼容旧逻辑）。
    """
    beijing_tz = timezone(timedelta(hours=8))
    
    if bookdate:
        # 预约日期前7天的指定时间
        book_date_obj = datetime.strptime(bookdate, "%Y-%m-%d")
        target_date = book_date_obj - timedelta(days=7)
        target_date_str = target_date.strftime("%Y-%m-%d")
    else:
        # 兼容旧逻辑：使用当前网络日期
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
    for attempt in range(1, max_retries + 1):
        print(f"第{attempt}次尝试登录...")
        session, tokens = cas_login(login_url, captcha_url, username, password)
        if tokens and tokens.get('access_token'):
            print("登录成功！")
            return tokens
        else:
            print("未能获取到token，准备重试...")
    print(f"连续{max_retries}次尝试均未获取到token，登录失败。")
    return None

_TOKEN_CACHE = {}
_TOKEN_LOCK = threading.Lock()

def get_token_cached(login_url, captcha_url, username, password, ttl_seconds=900):
    now = time.time()
    with _TOKEN_LOCK:
        entry = _TOKEN_CACHE.get(username)
        if entry and now - float(entry.get("ts", 0)) < float(ttl_seconds) and entry.get("tokens") and entry["tokens"].get("access_token"):
            return entry["tokens"]
    tokens = login_with_retry(login_url, captcha_url, username, password, max_retries=5)
    if not tokens or not tokens.get("access_token"):
        return None
    with _TOKEN_LOCK:
        _TOKEN_CACHE[username] = {"tokens": tokens, "ts": now}
    return tokens

def clear_token_cache(username: str = None):
    """清除 token 缓存。如果指定 username 则只清除该用户的缓存，否则清除所有。"""
    with _TOKEN_LOCK:
        if username:
            _TOKEN_CACHE.pop(username, None)
        else:
            _TOKEN_CACHE.clear()

# 线程数，强制为1，避免同一资源时段多并发
num_threads = 5
barrier = threading.Barrier(num_threads)

def book_task_with_network_date(thread_id, target_time_str, token, bookdate, kssj, jssj, resource_id, time_id):
    target_time = get_target_datetime_from_network(target_time_str, bookdate)
    print(f"线程{thread_id} 的目标时间：{target_time}")
    while True:
        now = get_network_time()
        if now:
            diff_sec = (target_time - now).total_seconds()
            if diff_sec <= 0:
                break
            elif diff_sec > 10:
                print("时间差距过大，休息5S")
                time.sleep(5)
            elif diff_sec > 1:
                print("休息0.5S")
                time.sleep(0.5)
            else:
                print("休息0.1S")
                time.sleep(0.1)
        else:
            print("未获得网络时间，请检查对应的操作")
            time.sleep(1)

    print(f"线程{thread_id} 已到目标时间，等待屏障")
    barrier.wait()
    print(f"线程{thread_id} 开始抢票")
    response = make_appointment(token, time_id, resource_id, bookdate, kssj, jssj)
    print(f"线程{thread_id} 抢票响应：{response}")

def run_concurrent_booking_threads(target_time_str, token, bookdate, kssj, jssj, resources_name):
    # 先调用一次获取资源ID和时间ID
    result = fetch_resource_time_id(token, bookdate, resources_name, kssj, jssj)
    if not result:
        print("未获取到资源ID或时间ID，退出。")
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

    print("所有线程抢票完成。")

def test_user_info(token):
    """测试用户信息获取功能"""
    print("测试用户信息获取...")
    user_info = get_user_info_from_appointment(token)
    if user_info:
        print("成功获取用户信息:")
        for key, value in user_info.items():
            if key != 'participant_info':
                print(f"  {key}: {value}")
        print("  participant_info:")
        for key, value in user_info['participant_info'].items():
            print(f"    {key}: {value}")
    else:
        print("获取用户信息失败")

if __name__ == '__main__':
    # 配置信息（从环境变量读取）
    LOGIN_URL = CAS_LOGIN_URL
    CAPTCHA_URL = CAS_CAPTCHA_URL
    USERNAME = os.getenv('CAS_USERNAME', '202300000000')
    PASSWORD = os.getenv('CAS_PASSWORD', 'XXXXXXX')
    bookdate = "2025-12-06"
    kssj = '16:00'
    jssj = '17:00'
    resources_name = '羽毛球10号场地'
    target_time_str = "12:12:00"  # 你想抢票的准点时间
    
    # 登录获取token
    print("开始登录...")
    tokens = login_with_retry(LOGIN_URL, CAPTCHA_URL, USERNAME, PASSWORD, max_retries=5)
    
    if tokens and tokens.get('access_token'):
        print("\n" + "="*50)
        print("登录成功！开始测试用户信息获取...")
        
        # 测试用户信息获取
        test_user_info(tokens['access_token'])
        
        print("\n" + "="*50)
        print("开始抢票流程...")
        
        # 开始抢票
        run_concurrent_booking_threads(target_time_str, tokens['access_token'], bookdate, kssj, jssj, resources_name)
    else:
        print("登录失败，无法进行后续操作")
# ---- Stable login fallback (keeps original cas_login) ----
from lxml import html as lxml_html
from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs, unquote as _unquote

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
    """下载验证码并识别（直接在内存中处理，不保存到文件）"""
    r = session.get(captcha_url, timeout=15)
    code, *_ = predict_validate_code(r.content)
    return code


def cas_login_stable(login_url, captcha_url, username, password):
    """More resilient CAS login:
    - Extract execution dynamically
    - Try multiple _eventId values (submit/passwordlessLogin)
    - Always send Referer/Origin headers
    - Refresh captcha and retry up to 3 times on failure
    Returns: (session, tokens or None)
    """
    origin = CAS_ORIGIN
    session = None
    for attempt in range(1, 4):
        session = requests.Session()
        headers_get = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"}
        login_resp = session.get(login_url, headers=headers_get, timeout=20)
        execution_value = _stable_extract_execution(login_resp.text)
        if not execution_value:
            continue
        event_order = _stable_detect_event_order(login_resp.text)
        captcha = _stable_download_captcha(session, captcha_url)
        headers_post = {
            "User-Agent": headers_get["User-Agent"],
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": origin,
            "Referer": login_url,
        }
        for evt in event_order:
            data = {
                "username": username,
                "password": password,
                "execution": execution_value,
                "_eventId": evt,
                "validateCode": captcha,
            }
            post_resp = session.post(login_url, data=data, headers=headers_post, allow_redirects=False, timeout=20)
            if post_resp.status_code in (301, 302, 303) and "Location" in post_resp.headers:
                redirect_url = post_resp.headers["Location"]
                # Follow redirects until we see OIDC redirect_uri + ticket
                current_url = redirect_url
                for _ in range(10):
                    r = session.get(current_url, allow_redirects=False, timeout=20)
                    if r.status_code in (301, 302, 303) and "Location" in r.headers:
                        current_url = r.headers["Location"]
                    else:
                        break
                parsed = _urlparse(current_url)
                query = _parse_qs(parsed.query)
                if "redirect_uri" in query and "ticket" in query:
                    redirect_uri = _unquote(query["redirect_uri"][0])
                    ticket = query["ticket"][0]
                    oidc_url = f"{redirect_uri}&ticket={ticket}"
                    r2 = session.get(oidc_url, allow_redirects=True, timeout=20)
                    tokens = extract_oidc_tokens(r2.url)
                    if tokens:
                        print("Successfully obtained OIDC tokens")
                        print("Access Token:", tokens.get("access_token"))
                        print("ID Token:", tokens.get("id_token"))
                        return session, tokens
                return session, None
        # Try another attempt (refresh captcha / refetch execution)
        time.sleep(0.5)
    return session or requests.Session(), None
