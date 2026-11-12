"""场景衰减不清除历史技能；当前优势不能由旧项目或技术名堆叠。"""

from datetime import date
from pathlib import Path

from job_hunter.evaluation.recency import (
    cumulative_technologies,
    project_weights,
    recent_support,
)
from job_hunter.evaluation.rules import evaluate
from job_hunter.extraction.boss_details import JobCard, JobDetail
from job_hunter.models.preferences import (
    Candidate,
    Preferences,
    ProjectExperience,
    ProjectWeighting,
)
from job_hunter.settings import load_yaml


def project(period, tech='Python'):
    return ProjectExperience(name='匿名', period=period, business_scenario='企业研发工具',
                             responsibilities=['Agent执行机制和异常恢复', '执行追踪与质量评测'], technologies=[tech])


def test_weights_and_duration_are_separate_from_skill_facts():
    candidate = Candidate(projects=[project(value, str(index)) for index, value in enumerate([
        '2026.05—至今', '2026.04-2026.08', '2025.08-2026.02', '2024.01-2024.05',
        '2023.05-2023.10', '2022.06-2022.10', None,
    ])], project_weighting=ProjectWeighting(enabled=True, focus_since='2024-01'))
    rows = project_weights(candidate, date(2026, 11, 11))
    assert rows[0]['duration_months'] == 7
    assert rows[0]['scenario_weight'] == 1
    assert all(rows[i]['scenario_weight'] > rows[i+1]['scenario_weight'] for i in range(5))
    assert all(row['focus'] for row in rows[:4])
    assert all(row['scenario_weight'] < .25 for row in rows[4:6])
    assert rows[-1]['date_status'] == 'UNKNOWN'
    assert cumulative_technologies(candidate) == [str(i) for i in range(7)]
    assert candidate.work_experience_years is None  # 不叠加项目时长。


def test_future_or_reversed_dates_do_not_become_recent():
    candidate = Candidate(projects=[project('2027.01-至今'), project('2026.10-2026.05')])
    assert all(row['date_status'] == 'UNKNOWN' for row in project_weights(candidate, date(2026, 11, 11)))


def test_high_fit_requires_recent_combined_evidence():
    candidate = Candidate(projects=[project('2022.01-2023.01'), project('2026.01-至今')],
                          project_weighting=ProjectWeighting(enabled=True, focus_since='2024-01'))
    weights = project_weights(candidate, date(2026, 11, 11))
    one = {'project_index': 1, 'kind': 'responsibility', 'section': '岗位职责', 'profile_item_index': 0, 'passage_id': 'd0'}
    two = {**one, 'profile_item_index': 1, 'passage_id': 'd1'}
    assert recent_support([one, two], weights)
    assert not recent_support([one, one], weights)
    assert not recent_support([{**one, 'kind': 'generic_technology'}, two], weights)
    assert not recent_support([{**one, 'project_index': 0}, {**two, 'project_index': 0}], weights)


def test_older_focus_project_requires_more_substantive_support():
    candidate = Candidate(projects=[project('2023.01-2024.05')],
                          project_weighting=ProjectWeighting(enabled=True, focus_since='2024-01'))
    weights = project_weights(candidate, date(2026, 11, 11))
    one = {'project_index': 0, 'kind': 'responsibility', 'section': '岗位职责', 'profile_item_index': 0, 'passage_id': 'd0'}
    two = {**one, 'profile_item_index': 1, 'passage_id': 'd1'}
    assert not recent_support([one, two], weights)
    assert recent_support([one, two, {**one, 'kind': 'business', 'passage_id': 'd2'}], weights)


def test_local_rules_old_scene_only_is_general_not_rejected():
    prefs = load_yaml(Path('tests/fixtures/preferences.yaml'), Preferences)
    prefs.matching.salary_requires_overlap = False
    prefs.matching.english_requires_high_fit = False
    job = JobDetail(JobCard('/job_detail/t.html', 'Agent工程师', '公司', '上海'),
                    '岗位职责\n负责Agent执行机制、异常恢复。\n建设执行追踪与质量评测。', '上海', '')
    candidate = Candidate(education={'level': '本科'}, projects=[project('2022.01-2023.01')],
                          project_weighting=ProjectWeighting(enabled=True, focus_since='2024-01'))
    assert evaluate(job, prefs, candidate).status == 'GENERAL'
    candidate.projects[0].period = '2026.01-至今'
    assert evaluate(job, prefs, candidate).status == 'HIGH_FIT'


def test_key_university_requirement_does_not_change_bachelor_fact():
    prefs = load_yaml(Path('tests/fixtures/preferences.yaml'), Preferences)
    candidate = Candidate(education={'level': '本科', 'is_full_time': True, 'admission_route': '统招专升本'})
    job = JobDetail(JobCard('/job_detail/t.html', 'Agent工程师', '公司', '上海'),
                    '岗位职责\n负责应用开发。\n任职要求\n重点大学毕业者优先。', '上海', '')
    decision = evaluate(job, prefs, candidate)
    assert decision.status == 'SKIP'
    assert decision.evidence[0].rule == 'school_mentions'
    assert candidate.education.level == '本科'
