from pathlib import Path
from types import SimpleNamespace

import pytest

from job_hunter.evaluation.rules import Decision
from job_hunter.extraction.boss_details import JobCard, JobDetail
from job_hunter.models.preferences import Candidate, Preferences
from job_hunter.settings import ConfigurationError, load_yaml
from job_hunter.storage.run_log import RunLog
from job_hunter.workflows import scan


@pytest.mark.asyncio
@pytest.mark.parametrize('use_model', [False, True])
@pytest.mark.parametrize('limit', [5, 17])
async def test_scroll_adds_new_identities_without_rejudging_duplicates(monkeypatch, use_model, limit):
    preferences = load_yaml(Path(__file__).resolve().parents[2] / 'tests/fixtures/preferences.yaml', Preferences)
    preferences.execution.max_jobs_per_run = limit
    preferences.execution.allow_list_scroll = True
    preferences.execution.max_scroll_rounds = 1
    cards = [JobCard(f'/job_detail/test_{i}.html', f'岗位{i}', '测试公司', '上海') for i in range(17)]
    opened = []

    async def noop(*args, **kwargs):
        return {}

    async def current(*args):
        return cards[:16]

    async def more(session, seen):
        assert len(seen) == 15
        return [cards[0], cards[15], cards[16]]

    async def detail(session, card):
        opened.append(card.job_id)
        return JobDetail(card, '测试正文', '上海', '本科', reading={'status': 'DOM_COMPLETE'})

    monkeypatch.setattr(scan, 'BossAdapter', lambda _: SimpleNamespace(search=noop))
    monkeypatch.setattr(scan, 'apply_filters', noop)
    monkeypatch.setattr(scan, 'current_cards', current)
    monkeypatch.setattr(scan, 'load_more', more)
    monkeypatch.setattr(scan, 'open_detail', detail)
    monkeypatch.setattr(scan, 'evaluate', lambda *args: Decision('SKIP' if limit == 5 else 'GENERAL', '测试结果'))
    async def evaluate_model(*args):
        return Decision('SKIP' if limit == 5 else 'GENERAL', '模型测试结果', engine='test-semantic')

    evaluator = SimpleNamespace(engine='test-semantic', evaluate=evaluate_model) if use_model else None
    if use_model:
        def forbidden_fallback(*args):
            raise AssertionError('模型模式不允许悄悄调用本地规则')
        monkeypatch.setattr(scan, 'evaluate', forbidden_fallback)
    events = []
    summary = await scan.scan_in_session(SimpleNamespace(bring_to_front=noop), preferences, Candidate(), events.append, evaluator=evaluator)
    assert len(opened) == len(set(opened)) == limit
    assert summary['scroll_rounds'] == (1 if limit == 17 else 0)
    assert summary['stop_reason'] == 'job_limit'
    assert summary['communicate_attempts'] == summary['favorite_attempts'] == 0
    if use_model:
        assert summary['engine'] == 'test-semantic'
    assert [e['source'] for e in events if e['event'] == 'decision'][-2:] == (['scroll_loaded']*2 if limit == 17 else ['initial_list']*2)
    if limit == 5:
        assert summary['counts'] == {'SKIP': 5}  # 五个全不符合也必须停止，不能继续找五个合格者。


def test_resume_rejects_changed_profile_or_rules(tmp_path):
    preferences = load_yaml(Path(__file__).resolve().parents[2] / 'tests/fixtures/preferences.yaml', Preferences)
    candidate = Candidate()
    first = RunLog(preferences, candidate, tmp_path)
    first.append({'event': 'run_start', 'engine': 'test-v1'})
    first.append({'event': 'decision', 'job_id': 'test_1', 'status': 'GENERAL', 'engine': 'test-v1'})
    following = RunLog(preferences, candidate, tmp_path)
    assert following.resume(first.run_id, 'test-v1')[0]['job_id'] == 'test_1'
    with pytest.raises(ConfigurationError, match='ENGINE_CHANGED'):
        following.resume(first.run_id, 'test-v2')
    candidate.education.level = '本科'
    changed = RunLog(preferences, candidate, tmp_path)
    with pytest.raises(ConfigurationError, match='CONFIG_OR_PROFILE_CHANGED'):
        changed.resume(first.run_id, 'test-v1')


@pytest.mark.asyncio
async def test_five_incomplete_jobs_stop_without_model_calls(monkeypatch):
    preferences = load_yaml(Path(__file__).resolve().parents[2] / 'tests/fixtures/preferences.yaml', Preferences)
    preferences.execution.max_jobs_per_run = 5
    opened = []
    cards = [JobCard(f'/job_detail/test_{i}.html', f'岗位{i}', '公司', '上海') for i in range(8)]

    async def noop(*args, **kwargs):
        return {}

    async def current(*args):
        return cards

    async def detail(session, card):
        opened.append(card.job_id)
        return JobDetail(card, '待展开正文', '上海', '本科', reading={
            'status': 'UNVERIFIED', 'issues': ['仍有展开控件'], 'char_count': 6,
        })

    async def forbidden(*args, **kwargs):
        raise AssertionError('完整性未知不能调用模型，达到5岗不能补查')

    monkeypatch.setattr(scan, 'BossAdapter', lambda _: SimpleNamespace(search=noop))
    monkeypatch.setattr(scan, 'apply_filters', noop)
    monkeypatch.setattr(scan, 'current_cards', current)
    monkeypatch.setattr(scan, 'open_detail', detail)
    monkeypatch.setattr(scan, 'load_more', forbidden)
    events = []
    summary = await scan.scan_in_session(SimpleNamespace(bring_to_front=noop), preferences, Candidate(), events.append,
                                         evaluator=SimpleNamespace(engine='test-model', evaluate=forbidden))
    assert len(opened) == summary['processed'] == 5
    assert summary['counts'] == {'REVIEW': 5}
    assert all(not event['model_called'] for event in events if event['event'] == 'decision')
