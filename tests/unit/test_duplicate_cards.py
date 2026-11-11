"""重复卡片的等价身份、跨ID防重及冲突即停。"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from job_hunter.browser.adapter_base import PageReadError
from job_hunter.browser.pointer import point
from job_hunter.extraction.boss_details import current_cards
from job_hunter.extraction.identity import job_key
from job_hunter.models.preferences import Candidate, Preferences
from job_hunter.settings import ConfigurationError, load_yaml
from job_hunter.storage.run_log import RunLog
from job_hunter.workflows.apply import history_keys


@pytest.mark.asyncio
@pytest.mark.parametrize(('company','title','conflict'),[('公司','岗位',False),('另一公司','岗位',True),('公司','另一岗位',True)])
async def test_same_id_requires_same_company_and_title(monkeypatch,company,title,conflict):
    from job_hunter.extraction import boss_details
    monkeypatch.setattr(boss_details.BossAdapter,'require_signed_in',AsyncMock())
    rows=[{'path':'/job_detail/a.html','company':'公司','title':'岗位','location':'上海'},
          {'path':'/job_detail/a.html','company':company,'title':title,'location':'上海'}]
    session=SimpleNamespace(evaluate=AsyncMock(return_value=rows))
    if conflict:
        with pytest.raises(PageReadError,match='IDENTITY_CONFLICT'):
            await current_cards(session)
    else:
        assert len(await current_cards(session))==1


@pytest.mark.asyncio
async def test_same_company_title_different_ids_once(monkeypatch):
    from job_hunter.extraction import boss_details
    monkeypatch.setattr(boss_details.BossAdapter,'require_signed_in',AsyncMock())
    session=SimpleNamespace(evaluate=AsyncMock(return_value=[
        {'path':f'/job_detail/{i}.html','company':'公司','title':title,'location':'上海'}
        for i,title in enumerate(['岗位',' 岗位 ','不同岗位'])]))
    assert len(await current_cards(session))==2
    assert job_key('公司','岗位')==job_key('公 司','岗 位')
    assert job_key('另一公司','岗位')!=job_key('公司','岗位')


@pytest.mark.asyncio
async def test_conflicting_pointer_never_sends_mouse_event():
    session=SimpleNamespace(evaluate=AsyncMock(return_value={'error':'M3_DUPLICATE_CARD_IDENTITY_CONFLICT'}),_request=AsyncMock())
    with pytest.raises(PageReadError,match='IDENTITY_CONFLICT'):
        await point(session,'.job-info',click=True,card_identity={'path':'/job_detail/a.html','company':'公司','title':'岗位'})
    session._request.assert_not_awaited()


def test_hashed_identity_survives_name_recording_off_and_blocks_alias(tmp_path):
    prefs=load_yaml(Path('tests/fixtures/preferences.yaml'),Preferences)
    log=RunLog(prefs,Candidate(),tmp_path,compact_apply=True)
    key=job_key('私人公司','私人岗位')
    log.append({'event':'decision','job_id':'a','company':'私人公司','title':'私人岗位','identity_key':key,'status':'GENERAL'})
    log.append({'event':'communicate_intent','job_id':'a','identity_key':key})
    assert key in history_keys(tmp_path)
    assert '私人' not in log.path.read_text()
    with pytest.raises(ConfigurationError,match='EQUIVALENT_PREVIOUS_ATTEMPT'):
        log.append({'event':'communicate_intent','job_id':'alias','identity_key':key})
