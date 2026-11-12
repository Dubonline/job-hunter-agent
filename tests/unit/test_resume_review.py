"""选择/更正/确认后才保存，保留用户原有已确认事实与画像版本保护。"""

import json
from zipfile import ZipFile

import pytest
from typer.testing import CliRunner

from job_hunter import cli
from job_hunter.models.preferences import Candidate
from job_hunter.onboarding import SetupDraft, build_profile, save_profile
from job_hunter.resume_import import make_draft, write_draft
from job_hunter.resume_review import apply_review, load_target
from job_hunter.settings import ConfigurationError


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    profile = build_profile(SetupDraft.model_validate({
        'search': {'keywords': ['开发'], 'cities': ['上海']},
        'candidate': {'education': {'level': None}, 'work_experience_years': 3,
                      'expected_salary_min': 20, 'expected_salary_max': 30,
                      'languages': {'english_reading': '基础识读', 'english_spoken': False, 'english_fluent': False},
                      'target_roles': ['开发'], 'skills': {'strengths': ['原已确认技能']}},
    }))
    save_profile('demo', profile)
    file = tmp_path / 'sample.docx'
    with ZipFile(file, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>5年工作经验 | 期望薪资：25-35K</w:t></w:r></w:p></w:body></w:document>')
    draft = make_draft(file, profile.candidate, 'demo')
    path, review = write_draft(draft)
    return profile, file, draft, path


def test_cli_import_and_preview_never_change_active_profile(prepared, monkeypatch):
    profile, file, draft, path = prepared
    def forbidden(*args, **kwargs):
        raise AssertionError('不得连接浏览器或模型')
    monkeypatch.setattr(cli, 'AgentSession', forbidden)
    monkeypatch.setattr(cli, 'ModelEvaluator', forbidden)
    runner = CliRunner()
    result = runner.invoke(cli.app, ['--profile', 'demo', 'resume-import', str(file)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli.app, ['resume-review', str(path), '--show'])
    assert result.exit_code == 0, result.output
    assert load_target('demo')[1] == profile.candidate
    result = runner.invoke(cli.app, ['resume-review', str(path), '--accept', 'f01'])
    assert result.exit_code == 0, result.output
    assert load_target('demo')[1] == profile.candidate
    assert not (path.parent / 'confirmed.json').exists()


def test_accept_only_selected_fields_and_keep_unknown_english(prepared):
    profile, file, draft, path = prepared
    result = CliRunner().invoke(cli.app, ['resume-review', str(path), '--accept', 'f01', '--save'])
    assert result.exit_code == 0, result.output
    updated = load_target('demo')[1]
    assert updated.work_experience_years == 5
    assert updated.expected_salary_min == 20  # 未选薪资保留原值。
    assert updated.languages == profile.candidate.languages
    assert updated.skills == profile.candidate.skills
    assert (path.parent / 'confirmed.json').exists()
    with pytest.raises(ConfigurationError, match='已确认过'):
        apply_review(draft, {'work_experience_years': 5}, path.parent)


def test_user_correction_is_not_labeled_as_original_resume(prepared):
    profile, file, draft, path = prepared
    with pytest.raises(ConfigurationError, match='用户更正'):
        apply_review(draft, {'work_experience_years': 6}, path.parent)
    apply_review(draft, {'work_experience_years': 6}, path.parent,
                 {'work_experience_years': {'origin': 'user_input', 'value': 6}})
    assert load_target('demo')[1].work_experience_years == 6
    assert json.loads(path.read_text())['proposals'][0]['value'] == 5
    assert json.loads((path.parent / 'confirmed.json').read_text())['user_corrections']['work_experience_years']['value'] == 6


def test_changed_active_profile_blocks_overwrite(prepared):
    profile, file, draft, path = prepared
    profile.candidate = Candidate.model_validate({**profile.candidate.model_dump(), 'work_experience_years': 4})
    save_profile('demo', profile, overwrite=True)
    with pytest.raises(ConfigurationError, match='画像在导入后发生变化'):
        apply_review(draft, {'work_experience_years': 5}, path.parent)
    assert load_target('demo')[1].work_experience_years == 4


def test_wrong_profile_and_implicit_all_are_rejected(prepared):
    profile, file, draft, path = prepared
    runner = CliRunner()
    result = runner.invoke(cli.app, ['--profile', 'other', 'resume-review', str(path), '--show'])
    assert result.exit_code == 1
    result = runner.invoke(cli.app, ['resume-review', str(path), '--accept', 'all', '--save'])
    assert result.exit_code == 1
    assert load_target('demo')[1] == profile.candidate


def test_interactive_user_correction_then_confirmation(prepared):
    profile, file, draft, path = prepared
    result = CliRunner().invoke(cli.app, ['resume-review', str(path)], input='是\n是\n6\n否\n是\n')
    assert result.exit_code == 0, result.output
    assert load_target('demo')[1].work_experience_years == 6
    assert load_target('demo')[1].expected_salary_min == 20
    audit = json.loads((path.parent / 'confirmed.json').read_text())
    assert audit['user_corrections']['work_experience_years']['origin'] == 'user_input'


def test_default_user_updates_only_candidate_file(prepared):
    import yaml

    from job_hunter.settings import load_user_config

    profile, file, draft, path = prepared
    folder = file.parent / 'config'
    folder.mkdir()
    preferences_path = folder / 'preferences.local.yaml'
    preferences_path.write_text(yaml.safe_dump(profile.preferences.model_dump()))
    (folder / 'candidate.yaml').write_text(yaml.safe_dump(profile.candidate.model_dump()))
    previous = preferences_path.read_bytes()
    draft = make_draft(file, profile.candidate, None)
    root_path, review = write_draft(draft)
    apply_review(draft, {'work_experience_years': 5}, root_path.parent)
    assert load_user_config()[1].work_experience_years == 5
    assert preferences_path.read_bytes() == previous
    assert (folder / 'candidate.yaml').stat().st_mode & 0o777 == 0o600


def test_accepting_new_projects_keeps_historical_technologies():
    from job_hunter.resume_review import patched_candidate

    old = Candidate(skills={'strengths': ['旧的真实技能自述']}, projects=[{
        'name': '旧项目', 'period': '2020.01-2021.01', 'business_scenario': '旧场景',
        'responsibilities': ['旧职责'], 'technologies': ['LegacyTool'],
    }])
    updated = patched_candidate(old, {'skills.strengths': ['新的真实技能自述'], 'projects': [{
        'name': '新项目', 'period': '2026.01-至今', 'business_scenario': '新场景',
        'responsibilities': ['新职责'], 'technologies': ['Python'],
    }]})
    assert updated.skills.technologies == ['LegacyTool', 'Python']
    assert updated.skills.strengths == ['旧的真实技能自述', '新的真实技能自述']
