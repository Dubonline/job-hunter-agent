import io
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from job_hunter.evaluation import model
from job_hunter.extraction.boss_details import JobCard, JobDetail
from job_hunter.models.preferences import Candidate, Preferences
from job_hunter.settings import Settings, load_yaml


@pytest.fixture
def evaluator():
    settings = Settings(_env_file=None, llm_api_key='test-secret', llm_base_url='https://example.test/anthropic', llm_model='configured-model[1m]')
    preferences = load_yaml(Path(__file__).resolve().parents[2] / 'tests/fixtures/preferences.yaml', Preferences)
    preferences.matching.salary_requires_overlap = False
    preferences.matching.english_requires_high_fit = False
    candidate = Candidate.model_validate({'education': {'level': '本科'}, 'projects': [{
        'name': 'PRIVATE_PROJECT_NAME', 'business_scenario': '企业研发工具',
        'responsibilities': ['Agent执行机制', '质量评测'], 'technologies': ['Python'],
    }]})
    return model.ModelEvaluator(settings, preferences, candidate)


@pytest.fixture
def detail():
    return JobDetail(JobCard('/job_detail/test.html', 'Agent工程师', '测试公司', '上海'),
                     '岗位职责：负责Agent执行与质量评测。\n任职要求：有金融业务项目经验。', '上海徐汇区', '本科')


def output(evaluator):
    return {'status': 'HIGH_FIT', 'reason': '执行机制与质量评测对应', 'hard_checks': [
        {'rule_id': rule['id'], 'result': 'PASS', 'evidence_ids': [], 'reason': '无排除证据'}
        for rule in evaluator.rules
    ], 'requirements': [], 'profile_matches': [{
        'passage_id': 'd0', 'project_index': 0,
        'profile_field': 'responsibilities', 'profile_item_index': 0, 'kind': 'responsibility',
    }], 'notes': []}


def test_required_unmet_overrides_positive_result(evaluator, detail):
    data = output(evaluator)
    data['requirements'] = [{'passage_id': 'r0', 'type': 'domain_experience',
                             'kind': 'mandatory', 'judgement': 'unmet', 'profile_basis': '画像未提供金融业务项目'}]
    decision = evaluator.validate(json.dumps(data), detail, {})
    assert decision.status == 'SKIP'
    assert decision.engine == evaluator.engine
    assert decision.evidence[0].quote in detail.description


def test_preferred_unmet_does_not_exclude(evaluator, detail):
    data = output(evaluator)
    data['requirements'] = [{'passage_id': 'r0', 'type': 'domain_experience',
                             'kind': 'preferred', 'judgement': 'unmet', 'profile_basis': '画像未提供'}]
    assert evaluator.validate(json.dumps(data), detail, {}).status == 'HIGH_FIT'


@pytest.mark.parametrize('invalid', ['invented_quote', 'invented_project', 'missing_rule'])
def test_rejects_unverifiable_output(evaluator, detail, invalid):
    data = output(evaluator)
    if invalid == 'invented_quote':
        data['profile_matches'][0]['passage_id'] = 'not-exist'
    elif invalid == 'invented_project':
        data['profile_matches'][0]['profile_item_index'] = 99
    else:
        data['hard_checks'].pop()
    with pytest.raises(model.ModelError):
        evaluator.validate(json.dumps(data), detail, {})


def test_hard_failure_and_unknown_are_not_overridden_by_fit(evaluator, detail):
    data = output(evaluator)
    data['hard_checks'][0].update(result='UNKNOWN')
    assert evaluator.validate(json.dumps(data), detail, {}).status == 'REVIEW'
    data['hard_checks'][0].update(result='FAIL', evidence_ids=['r0'])
    assert evaluator.validate(json.dumps(data), detail, {}).status == 'SKIP'


def test_generic_stack_cannot_make_high_fit(evaluator, detail):
    data = output(evaluator)
    data['profile_matches'][0]['kind'] = 'generic_technology'
    assert evaluator.validate(json.dumps(data), detail, {}).status == 'GENERAL'
    assert 'PRIVATE_PROJECT_NAME' not in json.dumps(evaluator.profile)


@pytest.mark.asyncio
async def test_messages_protocol_keeps_configured_model(evaluator, monkeypatch):
    captured = {}

    def open_request(request, timeout):
        captured.update(json.loads(request.data))
        assert request.full_url == 'https://example.test/anthropic/v1/messages'
        assert request.get_header('X-api-key') == 'test-secret'
        return io.BytesIO(json.dumps({'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': '{"ok":true}'}], 'usage': {'output_tokens': 5}}).encode())

    monkeypatch.setattr(model, 'build_opener', lambda *_: SimpleNamespace(open=open_request))
    assert (await evaluator.probe())['protocol'] == 'messages'
    assert captured['model'] == 'configured-model[1m]'
    assert 'PRIVATE_PROJECT_NAME' not in json.dumps(captured)


def test_http_error_does_not_expose_key(evaluator, monkeypatch):
    def reject(request, timeout):
        raise HTTPError(request.full_url, 401, 'test-secret', {}, io.BytesIO(b'test-secret'))

    monkeypatch.setattr(model, 'build_opener', lambda *_: SimpleNamespace(open=reject))
    with pytest.raises(model.ModelError) as error:
        evaluator._request('system', 'data', 100)
    assert '401' in str(error.value)
    assert 'test-secret' not in str(error.value)


@pytest.mark.asyncio
async def test_validation_repair_is_bounded(evaluator, detail, monkeypatch):
    replies = iter([('not-json', {'output_tokens': 2}), (json.dumps(output(evaluator)), {'output_tokens': 10})])

    async def request(*args, **kwargs):
        return next(replies)

    monkeypatch.setattr(evaluator, 'request', request)
    decision = await evaluator.evaluate(detail, evaluator.preferences, Candidate())
    assert decision.validation_retries == 1
    assert decision.usage['output_tokens'] == 12


def test_duty_cannot_be_used_as_required_domain_experience(evaluator, detail):
    data = output(evaluator)
    data['requirements'] = [{'passage_id': 'd0', 'type': 'domain_experience',
                             'kind': 'mandatory', 'judgement': 'unmet', 'profile_basis': '画像未覆盖'}]
    with pytest.raises(model.ModelError, match='REQUIREMENT_SECTION_INVALID'):
        evaluator.validate(json.dumps(data), detail, {})


def test_passages_keep_short_education_and_preferred_boundary():
    job = JobDetail(JobCard('/job_detail/test.html', '工程师', '公司', '上海'),
                    '岗位职责\n负责金融业务工具研发。\n任职要求\n硕士以上\n有政务经验者优先考虑\n加分项\n有金融项目经验', '上海', '')
    source = model.passages(job)
    assert source['d0']['text'] == '负责金融业务工具研发。'
    assert source['r0']['text'] == '硕士以上'
    assert source['r1']['text'] == '有政务经验者优先考虑'
    assert source['p0']['section'] == '加分项'


def test_preferred_section_cannot_be_hardened(evaluator):
    job = JobDetail(JobCard('/job_detail/test.html', '工程师', '公司', '上海'),
                    '岗位职责\n负责Agent执行与质量评测。\n任职要求\n熟悉Python\n加分项\n有金融业务项目经验。', '上海', '')
    data = output(evaluator)
    data['requirements'] = [{'passage_id': 'p0', 'type': 'domain_experience',
                             'kind': 'mandatory', 'judgement': 'unmet', 'profile_basis': '画像未提供金融经验'}]
    decision = evaluator.validate(json.dumps(data), job, {})
    assert decision.status == 'HIGH_FIT'
    assert decision.requirements[0]['kind'] == 'preferred'


def test_requirement_match_keeps_source_and_cannot_claim_duty_fit(evaluator, detail):
    data = output(evaluator)
    data['profile_matches'][0]['passage_id'] = 'r0'
    decision = evaluator.validate(json.dumps(data), detail, {})
    assert decision.status == 'GENERAL'
    assert decision.profile_matches[0]['section'] == '任职要求'
    assert decision.evidence[0].section == '任职要求'


def test_explicit_skill_in_duty_can_be_assessed(evaluator, detail):
    data = output(evaluator)
    data['requirements'] = [{'passage_id': 'd0', 'type': 'skill', 'kind': 'mandatory',
                             'judgement': 'met', 'profile_basis': '画像有Agent开发经验'}]
    decision = evaluator.validate(json.dumps(data), detail, {})
    assert decision.status == 'HIGH_FIT'
    assert decision.requirements[0]['section'] == '岗位职责'
