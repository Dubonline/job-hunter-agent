from pathlib import Path

import pytest

from job_hunter.evaluation.rules import evaluate
from job_hunter.extraction.boss_details import JobCard, JobDetail
from job_hunter.models.preferences import Candidate, Preferences
from job_hunter.settings import load_yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def preferences():
    prefs = load_yaml(ROOT / 'tests/fixtures/preferences.yaml', Preferences)
    prefs.matching.missing_required_domain_experience = 'reject'
    # 旧硬规则测试隔离新准入策略；新边界由test_candidate_policy覆盖。
    prefs.matching.salary_requires_overlap = False
    prefs.matching.english_requires_high_fit = False
    next(r for r in prefs.hard_exclusions if r.id == 'english_working_language').enabled = True
    return prefs


@pytest.fixture
def candidate():
    return Candidate.model_validate({
        'education': {'level': '本科'}, 'work_experience_years': 5,
        'projects': [{
            'name': '测试画像', 'business_scenario': '政务知识服务和企业研发工具',
            'responsibilities': ['知识库与检索优化', 'Agent执行机制和异常恢复', '任务拆解与上下文管理', '执行追踪与质量评测'],
            'technologies': ['Java', 'Python', 'FastAPI', 'LangGraph'],
        }],
    })


def detail(text):
    return JobDetail(JobCard('/job_detail/test_1.html', 'AI应用工程师', '测试公司', '上海'), text, '上海徐汇区测试地址', '本科')


@pytest.mark.parametrize(('text', 'status', 'rule'), [
    ('岗位职责\n负责大模型应用研发与知识检索优化。\n任职要求\n具备金融行业项目经验。', 'SKIP', 'required_domain_experience'),
    ('岗位职责\n负责大模型应用研发与知识检索优化。\n任职要求\n熟悉Python。\n加分项\n有金融行业项目经验。', 'GENERAL', 'profile_responsibilities'),
    ('岗位职责\n负责金融场景的大模型Agent应用开发。\n任职要求\n熟练使用Python，有后端开发经验。', 'GENERAL', 'profile_responsibilities'),
    ('岗位职责\n负责大模型应用研发。\n任职要求\n985/211院校优先。', 'SKIP', 'school_mentions'),
    ('岗位职责\n1、负责RAG应用服务化和检索优化。\n2、负责数据库接口与后端架构开发。\n3、支持模型训练与部署。\n任职要求\n熟练Python。', 'GENERAL', 'profile_responsibilities'),
    ('岗位职责\n1、负责多模态模型训练与调优。\n2、探索新算法并开展前沿模型研究。\n3、推进业务落地。\n任职要求\n熟练Python。', 'SKIP', 'algorithm_research_or_training'),
    ('岗位职责\n负责大模型应用研发。\n任职要求\n熟练掌握Python、Java、Go中至少一种。', 'GENERAL', 'profile_responsibilities'),
    ('岗位职责\n负责大模型应用研发。\n任职要求\n掌握Golang编程语言并独立开发。', 'REVIEW', 'required_skill_unverified'),
    ('岗位职责\n负责大模型应用研发。\n任职要求\n能对开源模型进行LoRA微调或全参数微调。', 'SKIP', 'required_training_practice'),
    ('岗位职责\n负责大模型应用研发。\n任职要求\n需以英文作为工作语言。', 'SKIP', 'english_working_language'),
    ('岗位职责\n负责大模型应用研发。\n任职要求\n英语可作为工作语言者优先。', 'GENERAL', 'profile_responsibilities'),
])
def test_requirements_are_not_duties_or_bonus(preferences, candidate, text, status, rule):
    decision = evaluate(detail(text), preferences, candidate)
    assert decision.status == status
    assert decision.evidence[0].rule == rule
    assert ''.join(decision.evidence[0].quote.split()) in ''.join(text.split())


def test_substantial_workflow_match_not_framework_names(preferences, candidate):
    strong = evaluate(detail('岗位职责\n负责Agent运行时和异常恢复，建设上下文管理和执行追踪评测。\n任职要求\n熟悉Python。'), preferences, candidate)
    generic = evaluate(detail('岗位职责\n使用LangGraph、RAG、Multi-Agent完成大模型应用研发。\n任职要求\n熟悉Python。'), preferences, candidate)
    assert strong.status == 'HIGH_FIT'
    assert generic.status == 'GENERAL'


def test_alternative_domain_accepts_known_experience(preferences, candidate):
    decision = evaluate(detail('岗位职责\n负责大模型应用研发。\n任职要求\n有金融或政务领域项目经验。'), preferences, candidate)
    assert decision.status == 'GENERAL'


def test_hard_requirement_precedes_strengths(preferences, candidate):
    decision = evaluate(detail('岗位职责\n负责Agent运行时、异常恢复和评测。\n任职要求\n具有芯片设计项目经验。'), preferences, candidate)
    assert decision.status == 'SKIP'


def test_major_is_not_domain_project_experience(preferences, candidate):
    decision = evaluate(detail('岗位职责\n负责大模型应用研发。\n任职要求\n电子、微电子、计算机相关专业，3年以上后端开发经验。'), preferences, candidate)
    assert decision.status == 'GENERAL'


def test_required_clause_before_preferred_clause(preferences, candidate):
    decision = evaluate(detail('岗位职责\n负责Agent运行时与评测。\n任职要求\n对AgentScope有实际使用经验或较深入了解；熟悉LangGraph者优先。'), preferences, candidate)
    assert decision.status == 'REVIEW'


def test_training_title_requires_body_evidence(preferences, candidate):
    job = detail('岗位职责\n负责开源大模型微调、评估与安全能力优化。\n任职要求\n熟练Python。')
    from dataclasses import replace
    job = replace(job, card=replace(job.card, title='大模型安全训练微调专家'))
    assert evaluate(job, preferences, candidate).evidence[0].rule == 'algorithm_research_or_training'


def test_support_role_cannot_be_high_fit_from_coding_words(preferences, candidate):
    from dataclasses import replace
    job = detail('岗位职责\n为客户提供代码生成效果优化、问题响应和评测。\n任职要求\n熟悉Python。')
    job = replace(job, card=replace(job.card, title='大模型技术服务TAM'))
    assert evaluate(job, preferences, candidate).status == 'SKIP'


def test_training_alternative_is_not_treated_as_all_required(preferences, candidate):
    decision = evaluate(detail('岗位职责\n负责大模型应用研发。\n任职要求\n具备模型训练、微调、推理优化或分布式训练经验。'), preferences, candidate)
    assert decision.status == 'REVIEW'


def test_extended_detail_title_can_reveal_actual_base(preferences, candidate):
    from dataclasses import replace
    job = replace(detail('岗位职责\n负责大模型应用研发。'), detail_title='AI应用工程师（驻新加坡）')
    decision = evaluate(job, preferences, candidate)
    assert decision.status == 'SKIP'
    assert decision.evidence[0].rule == 'base_city_not_shanghai'


def test_same_industry_product_manager_is_not_engineering_fit(preferences, candidate):
    from dataclasses import replace
    job = replace(detail('岗位职责\n负责政务知识库产品规划、质量评测、迭代和用户需求。'), detail_title='AI政务产品经理')
    decision = evaluate(job, preferences, candidate)
    assert decision.status == 'SKIP'
    assert decision.evidence[0].rule == 'target_role_mismatch'
