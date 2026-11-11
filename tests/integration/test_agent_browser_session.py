"""Temporary Chrome only; URL-policy overrides are confined to this local fixture test."""

import asyncio
import json

import pytest
from websockets.asyncio.client import connect

from job_hunter import cli
from job_hunter.browser.adapter_base import LoginStatus, PageReadError
from job_hunter.browser.boss_adapter import BossAdapter
from job_hunter.browser.session import CHROME_PATH, DEFAULT_BINARY, ROOT, AgentSession


@pytest.mark.asyncio
@pytest.mark.skipif(
    not CHROME_PATH.exists() or not DEFAULT_BINARY.exists(),
    reason="Local Chrome and patched agent-browser required",
)
async def test_formal_attach_and_disconnect_preserve_pages(
    tmp_path, monkeypatch, capsys
):
    fixture = tmp_path / "login.html"
    fixture.write_text(
        '<a href="https://www.zhipin.com/web/geek/chat">消息</a>'
        '<a ka="header-resume" href="#resume">简历</a>'
        '<form method="get"><input type="hidden" name="city" value="101020100">'
        '<input name="query" class="ipt-search"><button class="btn-search">搜索</button></form>'
        '<div class="job-card-box"><a class="job-name" href="/job_detail/initial.html">本地模拟职位</a></div>'
    )
    from job_hunter.browser.filters import RECORDED, apply_filters
    from job_hunter.models.preferences import Search

    dropdowns = '<style>.condition-filter-select{display:inline-block;position:relative;margin:10px}.current-select{padding:12px}.filter-select-dropdown{display:none;position:absolute;background:white;z-index:10;min-width:120px}.condition-filter-select:hover .filter-select-dropdown{display:block}li{padding:10px;cursor:pointer}</style><div class="filter-condition-inner">'
    for field, (label, kind, values) in RECORDED.items():
        dropdowns += f'<div class="condition-filter-select"><div class="current-select">{label}</div><div class="filter-select-dropdown"><ul>'
        options = list(values.items()) + ([('1000-9999人', '305')] if kind == 'scale' else [])
        for text, code in options:
            selected_class = 'active' if kind == 'scale' and code == '305' else ''
            dropdowns += f"<li class=\"{selected_class}\" ka=\"sel-job-rec-{kind}-{code}\" onclick=\"this.classList.toggle('active');this.closest('.condition-filter-select').querySelector('.current-select').textContent='已选';const a=document.querySelector('.job-card-box a.job-name');if(a?.getAttribute('href').includes('/initial'))a.setAttribute('href','/job_detail/initial_'+(window.fixtureRevision=(window.fixtureRevision||0)+1)+'.html')\">{text}</li>"
        dropdowns += "</ul></div></div>"
    fixture.write_text(fixture.read_text() + dropdowns + "</div>")
    fixture_url = fixture.as_uri() + "?city=101020100"

    def allowed(url):
        from urllib.parse import urlsplit

        return (
            urlsplit(url).scheme == "file" and urlsplit(url).path == str(fixture)
        ) or url == "null" + str(fixture)

    for module in ["attached", "guard", "boss_adapter"]:
        monkeypatch.setattr(f"job_hunter.browser.{module}.is_boss_url", allowed)
    profile = tmp_path / "profile"
    process = await asyncio.create_subprocess_exec(
        str(CHROME_PATH),
        "--headless=new",
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        fixture_url,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )

    async def targets(endpoint):
        async with connect(endpoint, proxy=None) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
            return json.loads(await ws.recv())["result"]["targetInfos"]

    try:
        async with asyncio.timeout(10):
            while not (profile / "DevToolsActivePort").exists():
                await asyncio.sleep(0.05)
        port, path = (profile / "DevToolsActivePort").read_text().splitlines()[:2]
        endpoint = f"ws://127.0.0.1:{port}{path}"
        before = await targets(endpoint)
        target = next(t["targetId"] for t in before if t.get("url") == fixture_url)
        await cli._attach(endpoint, None, False)
        assert "已登录" in capsys.readouterr().out
        assert any(t["targetId"] == target for t in await targets(endpoint))
        async with AgentSession(endpoint) as session:
            await session.bring_to_front()
            from job_hunter.browser.recorder import SearchRecorder

            recorder = SearchRecorder(session)
            assert (await recorder.start())["active"]
            adapter = BossAdapter(session)
            assert await adapter.check_login() == LoginStatus.SIGNED_IN
            assert await adapter.check_login() == LoginStatus.SIGNED_IN
            result = await adapter.search("大模型应用开发", "上海")
            assert result["query"] == "大模型应用开发"
            assert result["titles"] == ["本地模拟职位"]
            selected = await apply_filters(
                session,
                Search(
                    keywords=["大模型应用开发"],
                    cities=["上海"],
                    salary_filter="20-50K",
                    experience_filter="3-5年",
                    education_filter="本科",
                    job_hunting_type_filter="全职",
                    company_size_filter=['20-99人', '100-499人', '500-999人'],
                ),
            )
            assert len(selected) == 5
            assert await apply_filters(
                session,
                Search(
                    keywords=["大模型应用开发"],
                    cities=["上海"],
                    salary_filter="20-50K",
                    experience_filter="3-5年",
                    education_filter="本科",
                    job_hunting_type_filter="全职",
                    company_size_filter=['20-99人', '100-499人', '500-999人'],
                ),
            ) == selected  # Reapplying must not toggle selected options off.
            recording = await recorder.status()
            assert recording["active"]
            assert sum(e["type"] == "installed" for e in recording["events"]) == 2
            assert any(e["type"] == "click" and e.get("selector") == "button.btn-search"
                       for e in recording["events"])
            assert any(e["type"] == "hover" for e in recording["events"])
            assert any(e["type"] == "click" and e.get("text") == "本科"
                       for e in recording["events"])
            assert (await recorder.stop())["active"]
            assert not (await recorder.status())["active"]
            await session.evaluate("""(() => {
                const form=document.querySelector('form');
                form.className='search-input-box';
                form.innerHTML='<input type="hidden" name="city" value="101020100">'
                  +'<input class="input" name="query">'
                  +'<a class="search-btn" ka="job_search_btn_click" href="#">搜索</a>';
                form.querySelector('a').onclick=e=>{e.preventDefault();form.submit()};
                return true;
            })()""")
            resubmitted = await adapter.search('大模型应用开发', '上海', force_submit=True)
            assert resubmitted['query'] == '大模型应用开发'
            await apply_filters(session, Search(
                keywords=['大模型应用开发'], cities=['上海'], salary_filter='20-50K',
                experience_filter='3-5年', education_filter='本科', job_hunting_type_filter='全职',
                company_size_filter=['20-99人', '100-499人', '500-999人'],
            ))
            from job_hunter.extraction.boss_details import current_cards, open_detail

            await session.evaluate("""(() => {
              document.querySelector('.job-card-box').innerHTML=
                '<div class="job-info"><a class="job-name" href="/job_detail/local_123.html">本地模拟职位</a></div>'
                +'<span class="boss-name">本地公司</span><span class="company-location">上海</span>';
              const detail=document.createElement('div');detail.className='job-detail-box';
              document.body.appendChild(detail);
              document.querySelector('.job-info').onclick=()=>{
                window.cardOpenCount=(window.cardOpenCount||0)+1;
                detail.innerHTML='<div class="job-detail-header"><div class="job-header-info">'
                  +'<div class="job-detail-info"><span class="job-name">本地模拟职位（业务补充）</span></div>'
                  +'<ul class="tag-list"><li>上海</li></ul></div>'
                  +'<a class="op-btn-chat" ka="cpc_job_list_chat_local_123">立即沟通</a></div>'
                  +'<div class="job-detail-body"><div class="desc">'+(window.fixtureDescription||'负责企业知识库应用研发与评测。')+'</div>'
                  +(window.fixtureExpand?'<button>展开全部</button>':'')
                  +'<div class="job-boss-info"><div class="boss-info-attr">本地公司 · 招聘者</div></div>'
                  +'<div class="job-address-desc">上海测试地址</div></div>';
                detail.querySelector('.op-btn-chat').onclick=()=>{window.chatClicked=true};
                detail.querySelector('.desc').style.whiteSpace='pre-wrap';
              };
              const duplicateSpacer=document.createElement('div');duplicateSpacer.style.height='1800px';
              document.body.appendChild(duplicateSpacer);
              document.body.appendChild(document.querySelector('.job-card-box').cloneNode(true));
              return true;
            })()""")
            cards = await current_cards(session)
            assert len(cards) == 1
            detail = await open_detail(session, cards[0])
            assert detail.description == '负责企业知识库应用研发与评测。'
            assert detail.address == '上海测试地址'
            assert detail.detail_title == '本地模拟职位（业务补充）'
            assert detail.reading['status'] == 'DOM_COMPLETE'
            assert await session.evaluate('!!window.chatClicked') is False
            assert '负责企业知识库' not in repr(detail)
            # 两张等价卡片同时在视口内：仅打开一次；公司或名称冲突则不点击。
            await session.evaluate("""(() => {
              const cards=[...document.querySelectorAll('.job-card-box')];
              cards.forEach((e,i)=>{e.style.cssText=`position:fixed;left:20px;top:${120+i*120}px;width:300px;z-index:100`;
                e.querySelector('.job-info').style.height='60px'});
              cards[1].querySelector('.job-info').onclick=cards[0].querySelector('.job-info').onclick;
              window.cardOpenCount=0;return true;
            })()""")
            await open_detail(session,cards[0])
            assert await session.evaluate('window.cardOpenCount') == 1
            await session.evaluate("document.querySelectorAll('.job-card-box .boss-name')[1].textContent='冲突公司';true")
            with pytest.raises(PageReadError,match='IDENTITY_CONFLICT'):
                await open_detail(session,cards[0])
            assert await session.evaluate('window.cardOpenCount') == 1
            await session.evaluate("""(() => {
              const cards=[...document.querySelectorAll('.job-card-box')];
              cards[1].querySelector('.boss-name').textContent='本地公司';
              cards.forEach(e=>{e.removeAttribute('style');e.querySelector('.job-info').removeAttribute('style')});return true;
            })()""")
            await session.evaluate("window.fixtureDescription='屏幕外正文验证。\\n'.repeat(70)+'末尾验证标识';true")
            long_detail = await open_detail(session, cards[0])
            assert long_detail.description.endswith('末尾验证标识')
            assert long_detail.reading['offscreen_text_before_probe']
            assert long_detail.reading['same_text_after_end_scroll']
            assert long_detail.reading['status'] == 'DOM_COMPLETE'
            await session.evaluate('window.fixtureExpand=true;true')
            folded_detail = await open_detail(session, cards[0])
            assert folded_detail.reading['status'] == 'UNVERIFIED'
            assert folded_detail.reading['expand_controls'] == ['展开全部']
            await session.evaluate('window.fixtureExpand=false;window.fixtureDescription=null;true')
            from job_hunter.extraction.boss_details import load_more
            from job_hunter.models.preferences import Candidate, Preferences
            from job_hunter.settings import load_yaml
            from job_hunter.storage.run_log import RunLog
            from job_hunter.workflows.scan import scan_in_session

            prefs = load_yaml(ROOT / 'tests/fixtures/preferences.yaml', Preferences)
            prefs.search.keywords = ['大模型应用开发']  # 本地夹具使用固定关键词，不依赖用户默认检索变更。
            prefs.execution.max_jobs_per_run = 1
            prefs.execution.allow_list_scroll = False
            profile_data = Candidate()
            log = RunLog(prefs, profile_data, tmp_path / 'runs')
            events = []

            def emit(event):
                events.append(event)
                log.append(event)

            summary = await scan_in_session(session, prefs, profile_data, emit)
            assert summary['processed'] == 1
            assert summary['communicate_attempts'] == summary['favorite_attempts'] == 0
            records = [json.loads(line) for line in log.path.read_text().splitlines()]
            assert any(record['event'] == 'decision' for record in records)
            assert all('description' not in record for record in records)
            assert await session.evaluate('!!window.chatClicked') is False
            await session.evaluate("""(() => {
              const spacer=document.createElement('div');spacer.style.height='1800px';
              document.body.appendChild(spacer);
              window.addEventListener('scroll',()=>{
                if(window.addedCard || scrollY+innerHeight<document.scrollingElement.scrollHeight-10)return;window.addedCard=true;
                const next=document.createElement('div');next.className='job-card-box';
                next.innerHTML='<div class="job-info"><a class="job-name" href="/job_detail/local_456.html">下一批岗位</a></div>'
                  +'<span class="boss-name">新公司</span><span class="company-location">上海</span>';
                document.body.appendChild(next);
              });return true;
            })()""")
            new_cards = await load_more(session, {cards[0].job_id})
            assert [card.job_id for card in new_cards] == ['local_456']
            # 新增动作的真实鼠标/弹框定向验证：只点一次并留在当前页。
            from job_hunter.evaluation.rules import Decision
            from job_hunter.workflows.communicate import communicate_one

            await session.evaluate("""(() => {
              document.body.innerHTML='<a href="https://www.zhipin.com/web/geek/chat">消息</a>'
                +'<a ka="header-resume" href="#resume">简历</a>'
                +'<div class="job-card-box"><div class="job-info"><a class="job-name" href="/job_detail/fixture_harness.html">Agent Harness开发</a></div>'
                +'<span class="boss-name">示例测试公司</span><span class="company-location">上海</span></div>'
                +'<div class="job-detail-box"><div class="job-detail-header"><div class="job-detail-info"><span class="job-name">Agent Harness开发</span></div><span class="job-salary">20-40K</span>'
                +'<button class="op-btn-chat" ka="cpc_job_list_chat_fixture_harness">立即沟通</button></div>'
                +'<div class="job-detail-body"><div class="desc">岗位职责：Agent执行编排与恢复。</div></div>'
                +'<div class="job-boss-info"><div class="boss-info-attr">示例测试公司 · 招聘者</div></div>'
                +'<div class="job-address-desc">上海</div></div>';
              window.sentCount=0;window.continueCount=0;
              document.querySelector('.op-btn-chat').onclick=()=>{
                window.sentCount++;const dialog=document.createElement('div');dialog.className='arbitrary-custom-popup';
                dialog.innerHTML='<span>已向 BOSS 发送消息</span><p>如需修改打招呼内容，请在【消息通知-设置招呼语】页面修改</p><button>留在此页</button><button>继续沟通</button>';
                document.body.appendChild(dialog);dialog.querySelector('button').onclick=()=>{dialog.remove();document.querySelector('.op-btn-chat').textContent='继续沟通'};
                dialog.querySelectorAll('button')[1].onclick=()=>window.continueCount++;
              };return true;
            })()""")

            class FrozenEvaluator:
                async def evaluate(self, detail, preferences, candidate):
                    assert detail.salary_text == '20-40K'  # 覆盖真实页面薪资位于信息区外的布局。
                    return Decision('HIGH_FIT', '本地夹具职责对应')

            # 新收藏动作仅点一次，二次确认不取消；沿用真实op-btn-like控件。
            from job_hunter.extraction.boss_details import DETAIL_STATE
            from job_hunter.workflows.apply import ensure_favorite

            await session.evaluate("""(() => {
              window.favoriteCount=0;const button=document.createElement('a');
              button.className='op-btn op-btn-like';button.textContent='收藏';
              button.onclick=()=>{window.favoriteCount++;button.textContent='已收藏'};
              document.querySelector('.job-detail-header').appendChild(button);return true;
            })()""")
            favorite_before=await session.evaluate(DETAIL_STATE)
            favorite_events=[]
            await ensure_favorite(session,'fixture_harness',favorite_before,favorite_events.append)
            await ensure_favorite(session,'fixture_harness',favorite_before,favorite_events.append)
            assert await session.evaluate('window.favoriteCount') == 1
            assert favorite_events[-1]['status'] == 'already_saved'
            action_events = []
            await communicate_one(session, 'fixture_harness', prefs, profile_data, FrozenEvaluator(), action_events.append)
            assert await session.evaluate('window.sentCount') == 1
            assert await session.evaluate('window.continueCount') == 0
            assert [e['status'] for e in action_events if e['event'] == 'communicate_result'] == ['confirmed_sent']
            assert action_events[-1]['event'] == 'stay_on_page'
            # 只读识别明确的日限额提示；JD正文提及额度不能触发平台停止标记。
            from job_hunter.workflows.communicate import COMMUNICATION_STATE

            await session.evaluate("document.querySelector('.desc').textContent='职责：管理每日沟通上限';true")
            assert not (await session.evaluate(COMMUNICATION_STATE))['daily_limit']
            await session.evaluate("const alert=document.createElement('div');alert.setAttribute('role','alert');alert.id='quota-alert';alert.textContent='今日沟通人数已达上限';document.body.appendChild(alert);true")
            assert (await session.evaluate(COMMUNICATION_STATE))['daily_limit']
            await session.evaluate("document.querySelector('#quota-alert').remove();document.querySelector('.desc').textContent='岗位职责：Agent执行编排与恢复。';true")
            # 独立强制进入截图兜底；实际截图为明文数字，模拟DOM编码字段。
            from job_hunter.extraction.boss_details import DETAIL_STATE
            from job_hunter.extraction.salary import read_salary

            await session.evaluate("document.querySelector('.job-card-box').insertAdjacentHTML('beforeend','<span class=\"salary\">20-40K</span>');true")
            ocr_state_script = "(() => {const state=" + DETAIL_STATE + ";state.salary_text='\\ue001-\\ue002K';return state})()"
            ocr_before = await session.evaluate(ocr_state_script)
            ocr_salary, audit = await read_salary(session,
                (await current_cards(session))[0], ocr_before, ocr_state_script)
            assert ocr_salary == '20-40K'
            assert audit['status'] == 'CONFIRMED'
            assert audit['left'] == audit['right'] == '20-40K'

            # 带薪数附注时从真实DOM定位月薪像素，不把附注OCR乱码当成月薪数字。
            await session.evaluate("document.querySelector('.job-card-box .salary').textContent='20-40K·16薪';document.querySelector('.job-salary').textContent='20-40K·16薪';true")
            ocr_before = await session.evaluate(ocr_state_script)
            ocr_salary, audit = await read_salary(session,
                (await current_cards(session))[0], ocr_before, ocr_state_script)
            assert ocr_salary.startswith('20-40K')
            assert audit['status'] == 'CONFIRMED'
            await session.evaluate("document.querySelector('.job-salary').textContent='20-40K';true")

            with pytest.raises(PageReadError, match='ALREADY_CONTACTED'):
                await communicate_one(session, 'fixture_harness', prefs, profile_data, FrozenEvaluator(), action_events.append)
            assert await session.evaluate('window.sentCount') == 1
            await session.guard.upstream.close()
            await asyncio.wait_for(session.guard.stopped.wait(), 2)
            count = session.counter
            with pytest.raises(PageReadError):
                await adapter.check_login()
            assert (
                session.counter == count
            )  # No IPC command, daemon restart, or reconnect.
        assert session.process.returncode is not None
        assert process.returncode is None
        assert any(t["targetId"] == target for t in await targets(endpoint))
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                process.kill()
                await process.wait()


@pytest.fixture(autouse=True)
def isolated_permission_monitor(monkeypatch):
    """模拟CDP/临时无头Chrome测试不接触日常Chrome的原生窗口。"""
    monkeypatch.setenv('JOB_AGENT_CHROME_AUTO_ALLOW', '0')
