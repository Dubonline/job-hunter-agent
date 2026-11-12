"""真实Word中间件＋配置DeepSeek协议，以模拟网络验证脱敏及来源约束。"""

import json

import pytest
from docx import Document
from typer.testing import CliRunner

from job_hunter import cli
from job_hunter.evaluation.model import ConfiguredModelClient, ModelError
from job_hunter.models.preferences import Candidate, ProjectWeighting
from job_hunter.resume_import import make_draft
from job_hunter.resume_semantic import (
    generate_profile,
    semantic_input,
    validated_analysis,
)
from job_hunter.settings import Settings


@pytest.fixture
def resume(tmp_path):
    document = Document()
    for line in ['PRIVATE_PERSON', '5年工作经验 | 求职意向：应用开发 | 期望薪资：25-30K',
                 '个人优势', '1. 熟悉Python，了解Java。', '项目经历',
                 'PRIVATE_PROJECT  开发  2026.01-至今', '技术栈：Python、Java',
                 '项目介绍：为PRIVATE_PROJECT建设内部业务工具。', '个人职责：',
                 '1. 负责接口实现。', '2. 参与测试。', '教育经历', 'PRIVATE_SCHOOL 本科 2020-2022',
                 'PRIVATE_COLLEGE 大专 2017-2020']:
        document.add_paragraph(line)
    path = tmp_path / 'sample.docx'
    document.save(path)
    return path


@pytest.fixture
def settings():
    return Settings(_env_file=None, llm_base_url='https://api.deepseek.com/anthropic',
                    llm_model='deepseek-configured[1m]', llm_api_key='never-print-secret')


@pytest.mark.asyncio
async def test_configured_deepseek_receives_text_not_images_and_cannot_add_facts(resume, settings, monkeypatch):
    captured = {}
    async def request(self, system, content, max_tokens):
        captured.update(json.loads(content))
        assert self.settings.llm_model == 'deepseek-configured[1m]'
        return json.dumps({'highlight_evidence_keys': ['e0'], 'primary_project_indices': [0]}), {'output_tokens': 10}
    monkeypatch.setattr(ConfiguredModelClient, 'request', request)
    base = Candidate(project_weighting=ProjectWeighting(enabled=True, focus_since='2024-01'))
    draft = await generate_profile(resume, base, None, settings)
    sent = json.dumps(captured)
    assert all(value not in sent for value in ('PRIVATE_PERSON', 'PRIVATE_PROJECT', 'PRIVATE_SCHOOL', 'PRIVATE_COLLEGE', 'never-print-secret'))
    assert 'image' not in captured
    assert draft.model_analysis['model'] == 'deepseek-configured[1m]'
    assert draft.state == 'pending_review'
    assert next(item for item in draft.proposals if item.field == 'education.level').value == '本科'
    assert len(next(item for item in draft.proposals if item.field == 'education.records').value) == 2


@pytest.mark.asyncio
async def test_other_provider_is_not_used(resume, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError('不应请求其他目的地')
    monkeypatch.setattr(ConfiguredModelClient, 'request', forbidden)
    settings = Settings(_env_file=None, llm_base_url='https://other.test', llm_model='other', llm_api_key='secret')
    with pytest.raises(ModelError, match='REQUIRES_CONFIGURED_DEEPSEEK'):
        await generate_profile(resume, None, None, settings)


@pytest.mark.parametrize('output', [
    {'highlight_evidence_keys': ['not-found'], 'primary_project_indices': []},
    {'highlight_evidence_keys': ['e0'], 'primary_project_indices': [100]},
    {'highlight_evidence_keys': ['e0'], 'primary_project_indices': [], 'invented_skills': ['Magic']},
])
def test_unverifiable_analysis_fails(resume, output):
    draft = make_draft(resume, None, None)
    _, index, weights = semantic_input(draft, None)
    with pytest.raises(ModelError):
        validated_analysis(json.dumps(output), index, weights, 'configured', {})


def test_agent_requests_resume_before_analysis(monkeypatch):
    called = []
    async def record(path):
        called.append(path)
    monkeypatch.setattr(cli, '_resume_intake', record)
    result = CliRunner().invoke(cli.app, ['resume-intake'], input='/tmp/customer.docx\n')
    assert result.exit_code == 0, result.output
    assert '请提供PDF或Word' in result.output
    assert str(called[0]) == '/tmp/customer.docx'


@pytest.mark.asyncio
async def test_model_failure_never_becomes_local_success(resume, settings, monkeypatch):
    async def fail(*args, **kwargs):
        raise ModelError('MODEL_TRANSPORT_FAILED')
    monkeypatch.setattr(ConfiguredModelClient, 'request', fail)
    with pytest.raises(ModelError, match='MODEL_TRANSPORT_FAILED'):
        await generate_profile(resume, None, None, settings)
