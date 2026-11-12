"""详情响应只作核对，不因读到JSON就认定完整或放行。"""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from job_hunter.browser.adapter_base import PageReadError
from job_hunter.extraction.boss_details import JobCard, JobDetail
from job_hunter.extraction.detail_response import (
    compare_response,
    matching_requests,
    parse_response,
    prefer_response,
    read_captured,
)


def sample(description='职责\n要求', salary='25-30K', job_id='test'):
    return json.dumps({'code': 0, 'zpData': {'jobInfo': {
        'encryptId': job_id, 'postDescription': description, 'salaryDesc': salary,
    }, 'bossInfo': {'name': '不应输出的招聘者'}, 'private': '不应输出的令牌'}})


def detail(description='职责\n要求'):
    return JobDetail(JobCard('/job_detail/test.html', '职位', '公司', '上海'),
                     description, '上海', '', salary_text='25-30K',
                     reading={'status': 'DOM_COMPLETE', 'salary': {'source': '本机OCR'}})


@pytest.mark.parametrize(('description', 'relation'), [
    ('职责\n\n要求', 'MATCH'), ('职责\n要求\n补充必备条件', 'DOM_SHORTER'),
    ('职责', 'RESPONSE_SHORTER'), ('其他内容', 'DIFFERENT'),
])
def test_completeness_comparison(description, relation):
    result = compare_response(sample(description), detail())
    assert result['jd_comparison'] == relation
    assert result['salary_ranges_match'] is True
    assert 'description' not in result
    assert '不应输出' not in json.dumps(result, ensure_ascii=False)


def test_wrong_identity_stops():
    with pytest.raises(PageReadError, match='IDENTITY_MISMATCH'):
        compare_response(sample(job_id='other'), detail())


@pytest.mark.parametrize('body', [None, '{}', 'not json', '[]', '{"code":1}', sample(description='')])
def test_unknown_payload_never_passes(body):
    with pytest.raises(PageReadError):
        compare_response(body, detail())


def test_unknown_salary_is_not_guessed():
    result = compare_response(sample(salary='\ue001-\ue002K'), detail())
    assert result['response_salary_parseable'] is False
    assert result['salary_ranges_match'] is None


def test_only_official_get_detail_requests():
    urls = ['https://www.zhipin.com/wapi/zpgeek/job/detail.json?token=private',
            'https://evil.test/wapi/zpgeek/job/detail.json',
            'https://www.zhipin.com/wapi/zpgeek/chat/detail.json']
    rows = [{'url': url, 'method': 'GET'} for url in urls]
    rows.append({'url': urls[0], 'method': 'POST'})
    assert matching_requests(rows) == [rows[0]]


def test_response_priority_and_salary_annotation_preserved():
    observed = detail()
    merged = prefer_response(observed, parse_response(sample('职责\n\n要求', '25-30K·13薪'), 'test'), 'RECEIVED')
    assert merged.description == '职责\n\n要求'
    assert merged.salary_text == '25-30K·13薪'
    assert merged.reading['source'].startswith('页面自然详情响应')
    assert merged.reading['salary']['dom_ocr'] == observed.reading['salary']
    assert merged.reading['status'] == 'DOM_COMPLETE'
    assert observed.salary_text == '25-30K'


@pytest.mark.parametrize(('description', 'salary', 'error'), [
    ('职责\n要求\n补充', '25-30K', 'JD_CONFLICT'),
    ('职责', '25-30K', 'JD_CONFLICT'),
    ('职责\n要求', '30-40K', 'SALARY_CONFLICT'),
])
def test_conflicts_stop_instead_of_fallback(description, salary, error):
    with pytest.raises(PageReadError, match=error):
        prefer_response(detail(), parse_response(sample(description, salary), 'test'), 'RECEIVED')


def test_missing_response_preserves_dom_fallback():
    observed = detail()
    merged = prefer_response(observed, None, 'NO_NEW_RESPONSE')
    assert merged.description == observed.description
    assert merged.salary_text == observed.salary_text
    assert merged.reading['detail_response']['status'] == 'FALLBACK'


def test_unknown_response_salary_uses_ocr_and_does_not_upgrade_completeness():
    observed = replace(detail(), reading={'status': 'UNVERIFIED', 'salary': {'source': '本机OCR'}})
    merged = prefer_response(observed, parse_response(sample(salary='面议'), 'test'), 'RECEIVED')
    assert merged.salary_text == '25-30K'
    assert merged.reading['status'] == 'UNVERIFIED'
    assert merged.reading['salary']['source'] == '本机OCR'


def test_explicit_response_salary_survives_unknown_ocr():
    observed = replace(detail(), salary_text='')
    merged = prefer_response(observed, parse_response(sample(), 'test'), 'RECEIVED')
    assert merged.salary_text == '25-30K'
    assert merged.reading['salary']['status'] == 'CONFIRMED'
    assert merged.reading['salary']['cross_check'] == 'UNVERIFIED'


@pytest.mark.parametrize('with_response', [False, True])
def test_conflicting_visual_salaries_never_become_unknown_fallback(with_response):
    observed = replace(detail(), salary_text='', reading={'salary': {'reason': '左右薪资识别不一致'}})
    info = parse_response(sample(), 'test') if with_response else None
    with pytest.raises(PageReadError, match='SALARY_CONFLICT'):
        prefer_response(observed, info, 'RECEIVED' if info else 'NO_NEW_RESPONSE')


@pytest.mark.asyncio
async def test_previous_response_is_never_reused():
    session = SimpleNamespace(require_alive=lambda: None, _request=AsyncMock(return_value={
        'requests': [{'url': 'https://www.zhipin.com/wapi/zpgeek/job/detail.json',
                      'method': 'GET', 'requestId': 'old', 'status': 200}],
    }))
    info, reason = await read_captured(session, {'old'}, 'test', timeout=.001)
    assert info is None
    assert reason == 'NO_NEW_RESPONSE'
    assert all(call.args[0]['action'] == 'requests' for call in session._request.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['wrong_id', 'http_error', 'multiple'])
async def test_bad_network_response_stops(mode):
    row = {'url': 'https://www.zhipin.com/wapi/zpgeek/job/detail.json',
           'method': 'GET', 'requestId': 'new', 'status': 403 if mode == 'http_error' else 200}
    rows = [row, {**row, 'requestId': 'other'}] if mode == 'multiple' else [row]
    session = SimpleNamespace(require_alive=lambda: None, _request=AsyncMock(side_effect=[
        {'requests': rows}, {'responseBody': sample(job_id='other')},
    ]))
    with pytest.raises(PageReadError):
        await read_captured(session, set(), 'test')
