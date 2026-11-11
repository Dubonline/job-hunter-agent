"""正式投递的动作分流、未知停止及新岗位去重。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from job_hunter.browser.adapter_base import PageReadError
from job_hunter.evaluation.rules import Decision
from job_hunter.extraction.boss_details import JobCard, JobDetail
from job_hunter.models.preferences import Candidate, Preferences
from job_hunter.settings import load_yaml
from job_hunter.workflows import apply as workflow
from job_hunter.workflows import scan


@pytest.mark.asyncio
@pytest.mark.parametrize('status',['HIGH_FIT','GENERAL','SKIP','REVIEW'])
async def test_action_routing(monkeypatch,status):
    calls=[]
    async def favorite(*args): calls.append('favorite')
    async def send(*args): calls.append('send')
    monkeypatch.setattr(workflow,'ensure_favorite',favorite)
    monkeypatch.setattr(workflow,'send_reviewed',send)
    monkeypatch.setattr(workflow,'previous_attempt',lambda _:False)
    session=SimpleNamespace(evaluate=AsyncMock(return_value={'success':False,'buttons':[{'text':'立即沟通','disabled':False}]}))
    detail=JobDetail(JobCard('/job_detail/new.html','Agent','公司','上海'),'职责','上海','')
    await workflow.ApplyActions(lambda e:None)(session,detail,{},Decision(status,'判断'))
    assert calls == (['favorite','send'] if status=='HIGH_FIT' else ['send'] if status=='GENERAL' else [])


@pytest.mark.asyncio
async def test_unknown_favorite_prevents_send(monkeypatch):
    async def unknown(*args): raise PageReadError('FAVORITE_RESULT_UNKNOWN')
    send=AsyncMock()
    monkeypatch.setattr(workflow,'ensure_favorite',unknown)
    monkeypatch.setattr(workflow,'send_reviewed',send)
    monkeypatch.setattr(workflow,'previous_attempt',lambda _:False)
    session=SimpleNamespace(evaluate=AsyncMock(return_value={'success':False,'buttons':[{'text':'立即沟通','disabled':False}]}))
    detail=JobDetail(JobCard('/job_detail/new.html','Agent','公司','上海'),'职责','上海','')
    with pytest.raises(PageReadError):
        await workflow.ApplyActions(lambda e:None)(session,detail,{},Decision('HIGH_FIT','判断'))
    send.assert_not_awaited()


def test_history_includes_unknown_and_reviewed_jobs(tmp_path):
    (tmp_path/'run.jsonl').write_text('{"event":"decision","job_id":"reviewed"}\n{"event":"communicate_intent","job_id":"unknown"}\n')
    assert {'reviewed','unknown'} <= workflow.history_ids(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize('fail',[False,True])
async def test_new_five_excludes_old_and_unknown_stops_round(monkeypatch,fail):
    prefs=load_yaml(Path('tests/fixtures/preferences.yaml'),Preferences)
    prefs.execution.max_jobs_per_run=5
    prefs.execution.allow_list_scroll=False
    cards=[JobCard(f'/job_detail/job{i}.html',f'Agent{i}','公司','上海') for i in range(8)]
    async def noop(*args,**kwargs): return {}
    reader=AsyncMock(side_effect=lambda session,card:JobDetail(card,'职责','上海','',reading={'status':'DOM_COMPLETE'}))
    monkeypatch.setattr(scan,'BossAdapter',lambda _:SimpleNamespace(search=noop))
    monkeypatch.setattr(scan,'apply_filters',noop)
    monkeypatch.setattr(scan,'current_cards',AsyncMock(return_value=cards))
    monkeypatch.setattr(scan,'open_detail',reader)
    evaluator=SimpleNamespace(engine='test',evaluate=AsyncMock(return_value=Decision('GENERAL','判断')))
    async def action(*args):
        if fail:
            raise PageReadError('UNKNOWN')
        return {'communicate_attempts':1,'favorite_attempts':0}
    events=[]
    session=SimpleNamespace(bring_to_front=AsyncMock(),evaluate=AsyncMock(return_value={}))
    call=scan.scan_in_session(session,prefs,Candidate(),events.append,evaluator=evaluator,
                             excluded_ids={'job0','job1'},action_handler=action)
    if fail:
        with pytest.raises(PageReadError):
            await call
        assert reader.await_count==1
        assert events[-1]['actions']['status']=='stopped'
    else:
        summary=await call
        assert summary['processed']==5 and summary['communicate_attempts']==5
        assert [c.args[1].job_id for c in reader.await_args_list]==[f'job{i}' for i in range(2,7)]
