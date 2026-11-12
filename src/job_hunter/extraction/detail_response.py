"""正式详情响应读取及双源核对：只读取页面自然请求，不另发站点请求。"""

import asyncio
import json
import re
from dataclasses import replace
from time import perf_counter
from urllib.parse import urlsplit

from job_hunter.browser.adapter_base import PageReadError
from job_hunter.evaluation.policy import salary_range

DETAIL_PATH = '/wapi/zpgeek/job/detail.json'


def parse_response(body: str, job_id: str) -> dict:
    """只提取同岗位成功响应的JD和薪资；正文只留内存，未知结构或错岗即停。"""
    if not isinstance(body, str) or len(body) > 2 * 1024 * 1024:
        raise PageReadError('DETAIL_RESPONSE_BODY_UNAVAILABLE_OR_TOO_LARGE')
    try:
        data = json.loads(body)
        info = data['zpData']['jobInfo']
        if data.get('code') != 0 or not isinstance(info, dict):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise PageReadError('DETAIL_RESPONSE_SCHEMA_UNCONFIRMED') from None
    if info.get('encryptId') != job_id:
        raise PageReadError('DETAIL_RESPONSE_IDENTITY_MISMATCH')
    description = info.get('postDescription')
    if not isinstance(description, str) or not description.strip():
        raise PageReadError('DETAIL_RESPONSE_DESCRIPTION_EMPTY')
    raw_salary = info.get('salaryDesc')
    return {'description': description,
            'salary': raw_salary if isinstance(raw_salary, str) else ''}


def compare_response(body: str, detail) -> dict:
    """返回无JD的双源比较摘要；只忽略空白，不把字段存在当完整性证明。"""
    return compare_fields(parse_response(body, detail.card.job_id), detail)


def compare_fields(info: dict, detail) -> dict:
    """比较已校验响应与DOM/OCR快照；原文只用于内存比较，不进入结果。"""
    description = info['description']
    # 比较时只忽略空白排版，不删除正文、标点或HTML标签来制造一致。
    response_text = re.sub(r'\s+', '', description)
    dom_text = re.sub(r'\s+', '', detail.description)
    relation = ('MATCH' if response_text == dom_text else
                'DOM_SHORTER' if dom_text and dom_text in response_text else
                'RESPONSE_SHORTER' if response_text in dom_text else 'DIFFERENT')
    response_salary = info['salary']
    response_range = salary_range(response_salary)
    observed_range = salary_range(detail.salary_text)
    return {
        'status': 'COMPARED', 'job_id': detail.card.job_id,
        'source': DETAIL_PATH, 'identity_match': True,
        'response_jd_chars': len(description), 'dom_jd_chars': len(detail.description),
        'jd_comparison': relation, 'dom_audit_status': detail.reading.get('status'),
        'response_salary': response_salary, 'response_salary_parseable': bool(response_range),
        'observed_salary': detail.salary_text,
        'observed_salary_source': detail.reading.get('salary', {}).get('source'),
        'salary_ranges_match': response_range == observed_range if response_range and observed_range else None,
        'scope': '仅比较此岗位此响应与已加载DOM，不证明其他岗位或服务端字段绝对完整',
    }


def matching_requests(rows):
    """仅取官方详情GET请求；完整URL、请求头和Cookie不进入诊断结果。"""
    matches = []
    for row in rows:
        parsed = urlsplit(row.get('url', ''))
        if (parsed.scheme == 'https' and parsed.hostname == 'www.zhipin.com'
                and parsed.path == DETAIL_PATH and row.get('method') == 'GET'):
            matches.append(row)
    return matches


async def begin_capture(session) -> set[str]:
    """打开卡片前启用同连接Network追踪，排除先前请求，不开启HAR或Runtime日志。"""
    initial = await session._request({'action': 'requests', 'filter': DETAIL_PATH})
    return {row['requestId'] for row in initial.get('requests', [])}


async def read_captured(session, previous_ids: set[str], job_id: str, timeout=2):
    """最多等timeout秒（默认2秒）读本次自然响应；无响应/正文不可取时允许DOM兜底。

    只允许一个新详情请求，已观察到错误状态、无效结构、身份冲突时抛错停止；
    重读响应缓存不会重发HTTP请求，超时不重开岗位、不换目标或重连。
    """
    deadline = asyncio.get_running_loop().time() + timeout
    reason = 'NO_NEW_RESPONSE'
    while asyncio.get_running_loop().time() < deadline:
        session.require_alive()
        result = await session._request({'action': 'requests', 'filter': DETAIL_PATH})
        rows = [row for row in matching_requests(result.get('requests', []))
                if row['requestId'] not in previous_ids]
        if len({row['requestId'] for row in rows}) > 1:
            raise PageReadError('DETAIL_RESPONSE_MULTIPLE_NEW_REQUESTS')
        for row in rows:
            if row.get('status') is None:
                reason = 'RESPONSE_PENDING'
                continue
            if row['status'] != 200:
                raise PageReadError('DETAIL_RESPONSE_HTTP_STATUS_UNCONFIRMED')
            response = await session._request({'action': 'request_detail', 'requestId': row['requestId']})
            body = response.get('responseBody')
            if body is None:
                reason = 'BODY_UNAVAILABLE'
                continue
            return parse_response(body, job_id), 'RECEIVED'
        await asyncio.sleep(.15)
    return None, reason


def prefer_response(detail, info, reason):
    """正式来源合并：响应优先，DOM/OCR核对；冲突抛错，整轮停止。

    无响应沿用旧兜底，不提升完整性；响应薪资未知时保留可靠DOM/OCR。
    响应月薪明确而OCR未知时采用响应，独立核对标UNVERIFIED，不误报双源一致。
    """
    reading = {**detail.reading, 'audit_version': 'response_dom_v1'}
    # OCR明确读出两个不同月薪属于冲突，不能当成普通“未识别”交给响应覆盖。
    if detail.reading.get('salary', {}).get('reason') == '左右薪资识别不一致':
        raise PageReadError('DETAIL_RESPONSE_SALARY_CONFLICT：左右薪资核对已出现冲突')
    if info is None:
        reading['detail_response'] = {'status': 'FALLBACK', 'reason': reason}
        return replace(detail, reading=reading)
    comparison = compare_fields(info, detail)
    if comparison['jd_comparison'] != 'MATCH':
        raise PageReadError('DETAIL_RESPONSE_JD_CONFLICT：响应与DOM正文不一致')
    if comparison['salary_ranges_match'] is False:
        raise PageReadError('DETAIL_RESPONSE_SALARY_CONFLICT：响应与DOM/OCR月薪不一致')
    reading['detail_response'] = comparison
    reading['dom_source'] = detail.reading.get('source')
    reading['source'] = '页面自然详情响应：postDescription（已与DOM全文核对）'
    reading['scope'] = '同岗位响应与已加载DOM一致；不保证服务端业务字段绝对完整'
    reading['char_count'] = len(info['description'])
    salary = detail.salary_text
    if comparison['response_salary_parseable']:
        salary = info['salary']
        reading['salary'] = {
            'source': '页面自然详情响应：salaryDesc', 'status': 'CONFIRMED',
            'cross_check': 'MATCH' if comparison['salary_ranges_match'] else 'UNVERIFIED',
            'dom_ocr': detail.reading.get('salary', {}),
        }
    return replace(detail, description=info['description'], salary_text=salary, reading=reading)


async def probe_detail_response(session) -> dict:
    """在已有列表打开一个非当前岗位，直接验证scan/apply共用读取，无模型/动作。"""
    from job_hunter.extraction.boss_details import (
        DETAIL_STATE,
        current_cards,
        open_detail,
    )

    started = perf_counter()
    cards = await current_cards(session)
    state = await session.evaluate(DETAIL_STATE)
    card = next((card for card in cards if card.job_id != state.get('id')), None)
    if card is None:
        raise PageReadError('DETAIL_RESPONSE_NO_ALTERNATE_CARD：不重复切岗强制请求')
    detail = await open_detail(session, card)
    return {**detail.reading['detail_response'], 'job_id': card.job_id,
            'selected_jd_source': detail.reading['source'],
            'selected_salary_source': detail.reading.get('salary', {}).get('source'),
            'selected_salary': detail.salary_text, 'completeness_status': detail.reading['status'],
            'seconds': round(perf_counter() - started, 3),
            'model_calls': 0, 'favorite_clicks': 0, 'communication_clicks': 0,
            'pipeline': '正式open_detail，与scan/apply共用'}
