"""新用户配置只写隔离的本地档案；不访问浏览器/模型，不继承现有用户事实。"""

import copy
import json
import stat

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from job_hunter import cli
from job_hunter.models.preferences import Candidate
from job_hunter.onboarding import (
    SetupDraft,
    build_profile,
    readiness,
    save_profile,
    summary,
)
from job_hunter.settings import (
    ConfigurationError,
    load_user_config,
    profile_path,
    selected_profile,
)


@pytest.fixture
def draft():
    return {
        'search': {'keywords': ['测试应用开发'], 'cities': ['上海'], 'salary_filter': '20-50K'},
        'candidate': {
            'education': {'level': None}, 'work_experience_years': None,
            'expected_salary_min': 30, 'expected_salary_max': 40,
            'languages': {'english_reading': '基础识读', 'english_spoken': False, 'english_fluent': False},
            'target_roles': ['应用开发工程师'], 'skills': {'strengths': ['Python']},
            'projects': [{'name': '匿名项目', 'business_scenario': '内部知识检索',
                          'responsibilities': ['负责接口实现'], 'technologies': ['Python']}],
        },
    }


def test_blank_defaults_do_not_copy_current_user(draft):
    profile = build_profile(SetupDraft.model_validate(draft))
    assert profile.preferences.hard_exclusions == []
    assert profile.preferences.company_blacklist == []
    assert not profile.preferences.matching.english_requires_high_fit
    assert profile.preferences.matching.salary_requires_overlap
    assert profile.preferences.execution.communication_target == 50
    assert not profile.preferences.execution.record_action_lists
    assert profile.candidate.education.level is None
    assert profile.candidate.work_experience_years is None
    assert profile.candidate.expected_salary_min == 30
    assert profile.preferences.search.salary_filter == '20-50K'
    assert '30–40K/月' in summary(profile)


@pytest.mark.parametrize('change', ['salary_missing', 'salary_reversed', 'salary_zero', 'language_missing',
                                 'language_conflict', 'multiple_cities', 'empty_project', 'risk_unknown'])
def test_invalid_or_unasked_facts_cannot_save(draft, change):
    candidate = draft['candidate']
    if change == 'salary_missing':
        candidate.pop('expected_salary_min')
    elif change == 'salary_reversed':
        candidate['expected_salary_max'] = 20
    elif change == 'salary_zero':
        candidate['expected_salary_min'] = 0
    elif change == 'language_missing':
        candidate['languages'].pop('english_spoken')
    elif change == 'language_conflict':
        candidate['languages']['english_fluent'] = True
    elif change == 'multiple_cities':
        draft['search']['cities'].append('北京')
    elif change == 'empty_project':
        candidate['projects'][0]['responsibilities'] = []
    else:
        candidate['languages']['english_spoken'] = None
        draft['english_policy'] = 'high_fit_risk'
    with pytest.raises(ValidationError):
        build_profile(SetupDraft.model_validate(draft))


def test_unsupported_filter_is_preserved_and_reported(draft):
    draft['search']['salary_filter'] = '30-40K'
    profile = build_profile(SetupDraft.model_validate(draft))
    assert profile.preferences.search.salary_filter == '30-40K'
    assert any('页面条件尚未支持' in item for item in readiness(profile))


def test_atomic_private_named_profile_and_explicit_overwrite(tmp_path, monkeypatch, draft):
    monkeypatch.chdir(tmp_path)
    profile = build_profile(SetupDraft.model_validate(draft))
    path = save_profile('demo', profile)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(ConfigurationError):
        save_profile('demo', profile)
    token = selected_profile.set('demo')
    try:
        prefs, candidate = load_user_config()
        assert prefs.search.keywords == ['测试应用开发']
        assert candidate.projects[0].responsibilities == ['负责接口实现']
    finally:
        selected_profile.reset(token)
    with pytest.raises(ConfigurationError):
        profile_path('../private')


def test_cli_preview_save_show_and_formal_config_selection(tmp_path, monkeypatch, draft):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'config').mkdir()
    original = tmp_path / 'config/candidate.yaml'
    original.write_text('原用户画像不应读取或覆盖')
    path = tmp_path / 'draft.json'
    path.write_text(json.dumps(draft))
    def forbidden(*args, **kwargs):
        raise AssertionError('配置入口不得连接浏览器或模型')
    monkeypatch.setattr(cli, 'AgentSession', forbidden)
    monkeypatch.setattr(cli, 'ModelEvaluator', forbidden)
    runner = CliRunner()
    command = ['--profile', 'new-user', 'configure', '--from-json', str(path)]
    result = runner.invoke(cli.app, command)
    assert result.exit_code == 0, result.output
    assert '仅预览' in result.output
    assert not profile_path('new-user').exists()
    result = runner.invoke(cli.app, command + ['--save'])
    assert result.exit_code == 0, result.output
    assert profile_path('new-user').exists()
    assert original.read_text() == '原用户画像不应读取或覆盖'
    assert selected_profile.get() is None
    result = runner.invoke(cli.app, ['--profile', 'new-user', 'configure', '--show'])
    assert result.exit_code == 0, result.output
    assert '30–40K/月' in result.output
    result = runner.invoke(cli.app, ['--profile', 'new-user', 'search-plan'])
    assert result.exit_code == 0, result.output
    assert '测试应用开发' in result.output


def test_bad_json_does_not_leak_input_or_write(tmp_path, monkeypatch, draft):
    monkeypatch.chdir(tmp_path)
    draft['secret'] = 'private-value-never-print'
    path = tmp_path / 'draft.json'
    path.write_text(json.dumps(draft))
    result = CliRunner().invoke(cli.app, ['--profile', 'demo', 'configure', '--from-json', str(path), '--save'])
    assert result.exit_code == 1
    assert 'private-value-never-print' not in result.output
    assert not profile_path('demo').exists()


def test_candidate_salary_pair_validation():
    with pytest.raises(ValidationError):
        Candidate(expected_salary_min=20)


def test_unknown_english_is_not_inferred(draft):
    draft['candidate']['languages'] = {'english_reading': None, 'english_spoken': None, 'english_fluent': None}
    profile = build_profile(SetupDraft.model_validate(draft))
    assert profile.candidate.languages.english_spoken is None
    assert profile.candidate.languages.english_fluent is None


def test_policy_uses_the_selected_users_salary_in_reason(draft):
    from job_hunter.evaluation.policy import apply_policy
    from job_hunter.evaluation.rules import Decision
    from job_hunter.extraction.boss_details import JobCard, JobDetail

    profile = build_profile(SetupDraft.model_validate(draft))
    detail = JobDetail(JobCard('/job_detail/test.html', '开发', '测试公司', '上海'),
                       '真实职责', '上海', '', salary_text='20-25K')
    decision = apply_policy(Decision('GENERAL', '测试'), detail, profile.preferences, profile.candidate)
    assert decision.status == 'SKIP'
    assert '30–40K' in decision.reason


@pytest.mark.asyncio
async def test_custom_exclusions_cannot_be_silently_ignored_by_rules(monkeypatch, draft):
    draft['hard_exclusions'] = [{'id': 'custom', 'enabled': True, 'description': '只接受明确条件', 'policy': 'require_evidence'}]
    profile = build_profile(SetupDraft.model_validate(draft))
    monkeypatch.setattr(cli, 'load_user_config', lambda: (profile.preferences, profile.candidate))
    monkeypatch.setattr(cli, 'load_settings', lambda: None)
    def forbidden(*args, **kwargs):
        raise AssertionError('不应建立浏览器或模型连接')
    monkeypatch.setattr(cli, 'AgentSession', forbidden)
    monkeypatch.setattr(cli, 'ModelEvaluator', forbidden)
    with pytest.raises(ConfigurationError, match='自定义硬排除需要model'):
        await cli._scan(None, None, None, None, engine_mode='rules')


def test_english_risk_is_explicit_and_conflicting_exclusion_is_rejected(draft):
    draft['english_policy'] = 'high_fit_risk'
    profile = build_profile(SetupDraft.model_validate(draft))
    assert profile.preferences.matching.english_requires_high_fit
    conflicted = copy.deepcopy(draft)
    conflicted['hard_exclusions'] = [{'id': 'english_working_language', 'enabled': True,
                                      'description': '排除英语工作语言', 'policy': 'explicit_requirement_only'}]
    with pytest.raises(ValidationError):
        SetupDraft.model_validate(conflicted)


def test_chinese_interactive_wizard_can_save_without_yaml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # 新建配置：9个页面筛选留空；个人事实允许未知，月薪必须单独填写。
    answers = ['测试应用开发', '上海'] + [''] * 9 + [
        '', '本科', '', '是', '0', '20', '30', '基础识读', '否', '否', 'Python', '',
        '参与内部应用开发', '否', '否', '否', '否', '否', '否', '否', '否',
        '', '', '', '', '0', '否', '5', '200', '3', '是',
    ]
    result = CliRunner().invoke(cli.app, ['--profile', 'interactive', 'configure'], input='\n'.join(answers) + '\n')
    assert result.exit_code == 0, result.output
    assert profile_path('interactive').exists()
    assert '保存没有授予投递权限' in result.output
