"""薪资OCR只有高置信度、唯一、左右一致才可采用。"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from job_hunter.extraction import salary
from job_hunter.extraction.boss_details import JobCard


@pytest.mark.parametrize(('rows','expected'), [
    ([{'text':'15-25K','confidence':1}], '15-25K'),
    ([{'text':'15-25K','confidence':.7}], None),
    ([{'text':'15-25K','confidence':1},{'text':'20-40K','confidence':1}], None),
    ([{'text':'I5-25K','confidence':1}], None),
    ([{'text':'30-60K.16#','confidence':1}], None),
])
def test_unique_ocr(rows, expected):
    assert salary.ocr_value(rows) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('right', ['15-25K','20-40K'])
async def test_two_regions_must_agree(monkeypatch, tmp_path, right):
    binary=tmp_path/'ocr'
    binary.touch()
    monkeypatch.setattr(salary,'OCR_BINARY',binary)
    monkeypatch.setattr(salary,'recognize',AsyncMock(side_effect=[
        [{'text':'15-25K','confidence':1}], [{'text':right,'confidence':1}],
    ]))
    state={'id':'test','salary_text':'\ue001-\ue002K','description':'职责'}
    session=SimpleNamespace(evaluate=AsyncMock(side_effect=[{'left':'#left','right':'#right'},True,True,True,True,state]),_request=AsyncMock())
    value,audit=await salary.read_salary(session,JobCard('/job_detail/test.html','Agent','公司','上海'),state,'state')
    assert value == ('15-25K' if right == '15-25K' else '')
    assert audit['status'] == ('CONFIRMED' if right == '15-25K' else 'UNVERIFIED')


@pytest.mark.asyncio
@pytest.mark.parametrize(('right_rows', 'confirmed'), [
    ([{'text':'30-60K','confidence':1}], True),
    ([{'text':'30-60K','confidence':.89}], False),
    ([{'text':'35-60K','confidence':1}], False),
    ([{'text':'30-60K','confidence':1}, {'text':'35-60K','confidence':1}], False),
])
async def test_monthly_crop_keeps_confidence_and_agreement(monkeypatch, tmp_path, right_rows, confirmed):
    """薪数附注失败只能靠月薪像素重读；低置信、多候选、左右冲突仍拦截。"""
    binary = tmp_path / 'ocr'
    binary.touch()
    monkeypatch.setattr(salary, 'OCR_BINARY', binary)
    recognize = AsyncMock(side_effect=[
        [{'text':'30-60K.16#','confidence':1}], [{'text':'30-60K','confidence':1}],
        [{'text':'30-60K•16#7','confidence':1}], right_rows,
    ])
    monkeypatch.setattr(salary, 'recognize', recognize)
    state = {'id':'test','salary_text':'\ue001-\ue002K·16薪','description':'职责'}
    regions = {side: {'selector':'#'+side, 'clip':[0,0,.6,1]} for side in ('left','right')}
    session = SimpleNamespace(evaluate=AsyncMock(side_effect=[regions,True,True,True,True,state]), _request=AsyncMock())
    value, audit = await salary.read_salary(session, JobCard('/job_detail/test.html','Agent','公司','上海'), state, 'state')
    assert (value == '30-60K') is confirmed
    assert (audit['status'] == 'CONFIRMED') is confirmed
    assert recognize.call_args_list[1].args[1] == [0,0,.6,1]
    assert audit['ocr_rows']['left'][0]['text'] == '30-60K.16#'
