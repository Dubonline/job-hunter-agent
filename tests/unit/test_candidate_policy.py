"""用户具体薪资边界与条件英语准入，避免高契合绕过其他门槛。"""
from pathlib import Path

import pytest

from job_hunter.evaluation.policy import apply_policy
from job_hunter.evaluation.rules import Decision
from job_hunter.extraction.boss_details import JobCard, JobDetail
from job_hunter.models.preferences import Candidate, Preferences
from job_hunter.settings import load_yaml


@pytest.mark.parametrize(('salary', 'expected'), [('15-25K','HIGH_FIT'), ('20-40K','HIGH_FIT'), ('30-40K','HIGH_FIT'), ('40-50K','SKIP'), ('15-24K','SKIP'), ('\ue001-\ue002K','REVIEW'), ('面议','REVIEW')])
def test_monthly_overlap(salary, expected):
    prefs = load_yaml(Path('tests/fixtures/preferences.yaml'), Preferences)
    detail = JobDetail(JobCard('/job_detail/test.html','Agent','公司','上海'),'职责','上海','',salary_text=salary)
    assert apply_policy(Decision('HIGH_FIT','职责对应'),detail,prefs,Candidate()).status == expected


@pytest.mark.parametrize(('status','text','expected'), [('HIGH_FIT','英语口语流利','HIGH_FIT'), ('GENERAL','英语口语流利','SKIP'), ('REVIEW','英文作为工作语言','SKIP'), ('GENERAL','英语口语流利优先','GENERAL'), ('SKIP','英语口语流利','SKIP')])
def test_conditional_english(status, text, expected):
    prefs = load_yaml(Path('tests/fixtures/preferences.yaml'), Preferences)
    detail = JobDetail(JobCard('/job_detail/test.html','Agent','公司','上海'),text,'上海','',salary_text='20-40K')
    assert apply_policy(Decision(status,'原结论'),detail,prefs,Candidate()).status == expected
