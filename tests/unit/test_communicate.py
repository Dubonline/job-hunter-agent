"""单岗动作未知时必须终止且不重复点击。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from job_hunter.browser.adapter_base import PageReadError
from job_hunter.evaluation.rules import Decision
from job_hunter.extraction.boss_details import JobCard, JobDetail
from job_hunter.workflows import communicate as workflow


@pytest.mark.asyncio
async def test_unknown_send_never_retries(monkeypatch):
    card = JobCard('/job_detail/test.html', 'Agent Harness开发', '示例公司', '上海')
    detail = JobDetail(card, '职责', '上海', '', reading={'status':'DOM_COMPLETE'})
    session = SimpleNamespace(bring_to_front=AsyncMock(), evaluate=AsyncMock())
    state = {'id':'test','description':'职责'}
    button = {'text':'立即沟通','disabled':False,'selector':'button'}
    session.evaluate.side_effect = [state, state, {'success':False,'buttons':[button]}, True] + [{'success':False,'buttons':[]}] * 40
    monkeypatch.setattr(workflow, 'previous_attempt', lambda job: job == 'fixture_previous')
    monkeypatch.setattr(workflow, 'current_cards', AsyncMock(return_value=[card]))
    monkeypatch.setattr(workflow, 'open_detail', AsyncMock(return_value=detail))
    monkeypatch.setattr(workflow.BossAdapter, 'require_signed_in', AsyncMock())
    pointer = AsyncMock()
    monkeypatch.setattr(workflow, 'point', pointer)
    monkeypatch.setattr(workflow.asyncio, 'sleep', AsyncMock())
    evaluator = SimpleNamespace(evaluate=AsyncMock(return_value=Decision('HIGH_FIT','职责对应')))
    events = []
    with pytest.raises(PageReadError, match='RESULT_UNKNOWN'):
        await workflow.communicate_one(session,'test',None,None,evaluator,events.append)
    assert pointer.await_count == 1
    assert [e['event'] for e in events].count('communicate_intent') == 1
    assert events[-1]['status'] == 'unknown'


def test_unknown_attempt_persists_across_runs(tmp_path):
    (tmp_path/'run.jsonl').write_text('{"event":"communicate_intent","job_id":"test"}\n')
    assert workflow.previous_attempt('test', tmp_path)
    assert not workflow.previous_attempt('other', tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['GENERAL', 'REVIEW'])
async def test_current_selection_is_bounded_and_sends_at_most_one(monkeypatch, status):
    rejected = JobCard('/job_detail/fixture_previous.html','Harness','示例公司','上海')
    cards = [rejected] + [JobCard(f'/job_detail/job{i}.html','Agent','公司','上海') for i in range(8)]
    session = SimpleNamespace(bring_to_front=AsyncMock(), evaluate=AsyncMock())
    session.evaluate.side_effect = lambda script: {'id':'test'} if script == workflow.DETAIL_STATE else {'success':False,'buttons':[{'text':'立即沟通','disabled':False}]}
    monkeypatch.setattr(workflow, 'current_cards', AsyncMock(return_value=cards))
    monkeypatch.setattr(workflow, 'previous_attempt', lambda job: job == 'fixture_previous')

    async def detail_for(session, card):
        return JobDetail(card,'职责','上海','',reading={'status':'DOM_COMPLETE'})

    reader = AsyncMock(side_effect=detail_for)
    monkeypatch.setattr(workflow, 'open_detail', reader)
    sender = AsyncMock()
    monkeypatch.setattr(workflow, 'send_reviewed', sender)
    evaluator = SimpleNamespace(evaluate=AsyncMock(return_value=Decision(status,'正式判断')))
    result = await workflow.select_current_one(session,None,None,evaluator,lambda event: None)
    expected = 1 if status == 'GENERAL' else 5
    assert reader.await_count == expected
    assert result['processed'] == expected
    assert sender.await_count == (1 if status == 'GENERAL' else 0)
    assert all(call.args[1].job_id != rejected.job_id for call in reader.await_args_list)


@pytest.mark.asyncio
async def test_dismiss_only_existing_dialog(monkeypatch):
    state={'id':'test'}
    dialog={'success':True,'stay':['#stay'],'message_visible':1,'dialog':{'text':'已向BOSS发送消息'}}
    session=SimpleNamespace(bring_to_front=AsyncMock(),evaluate=AsyncMock(side_effect=[
        state,'/web/geek/jobs',dialog,{'success':False,'message_visible':0},state,'/web/geek/jobs',
    ]))
    pointer=AsyncMock()
    monkeypatch.setattr(workflow,'point',pointer)
    events=[]
    await workflow.dismiss_sent_dialog(session,'test',events.append)
    pointer.assert_awaited_once_with(session,'#stay',click=True)
    assert events[-1]['dialog_closed'] and events[-1]['same_job']
    assert events[-1]['communicate_attempts'] == 0
