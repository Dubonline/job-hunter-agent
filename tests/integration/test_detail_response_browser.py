"""临时Chrome与本地HTTP页面验证原生响应读取，不接触真实BOSS或权限弹框。"""

import asyncio
import json

import pytest

from job_hunter.browser.session import CHROME_PATH, DEFAULT_BINARY, AgentSession
from job_hunter.extraction import detail_response


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['match', 'wrong_id', 'jd_conflict', 'salary_conflict', 'no_response'])
@pytest.mark.skipif(not CHROME_PATH.exists() or not DEFAULT_BINARY.exists(), reason='需要本机Chrome及现有修复版')
async def test_same_connection_natural_response(tmp_path, monkeypatch, mode):
    from job_hunter.browser.adapter_base import PageReadError

    monkeypatch.setenv('JOB_AGENT_CHROME_AUTO_ALLOW', '0')
    hits = []  # 本地HTTP请求路径，用于确认仅有页面自然GET，无额外接口重发。
    page = """<a href="https://www.zhipin.com/web/geek/chat">消息</a>
      <a ka="header-resume" href="#resume">简历</a>
      <div class="job-card-box"><div class="job-info" style="padding:20px">
      <a class="job-name" href="/job_detail/local_test.html">本地职位</a></div>
      <span class="boss-name">本地公司</span><span class="company-location">上海</span></div>
      <div class="job-detail-box"></div><script>
      window.actionClicks=0;
      document.querySelector('.job-info').onclick=async()=>{
        if(!window.skipFetch){const r=await fetch('/wapi/zpgeek/job/detail.json');await r.json();}
        document.querySelector('.job-detail-box').innerHTML=
          '<div class="job-detail-header"><div class="job-detail-info"><span class="job-name">本地职位</span></div>'+
          '<span class="job-salary">25-30K</span><a class="op-btn-chat" ka="cpc_job_list_chat_local_test" onclick="window.actionClicks++">立即沟通</a></div>'+
          '<div class="job-detail-body"><div class="desc">负责应用研发。要求工程经验。</div></div>'+
          '<div class="job-boss-info"><div class="boss-info-attr">本地公司 · 招聘者</div></div>';
      };</script>"""

    async def respond(reader, writer):
        try:
            request = await reader.readuntil(b'\r\n\r\n')
            path = request.split(b' ')[1].decode()
            hits.append(path)
            is_detail = path == detail_response.DETAIL_PATH
            body = (json.dumps({'code': 0, 'zpData': {'jobInfo': {
                'encryptId': 'other' if mode == 'wrong_id' else 'local_test',
                'postDescription': '另一个JD' if mode == 'jd_conflict' else '负责应用研发。要求工程经验。',
                'salaryDesc': '30-40K' if mode == 'salary_conflict' else '25-30K·13薪',
            }}}) if is_detail else page).encode()
            content_type = 'application/json' if is_detail else 'text/html; charset=utf-8'
            writer.write(f'HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode() + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(respond, '127.0.0.1', 0)
    origin = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
    def allowed(url):
        return url.startswith(origin + '/')
    for module in ('attached', 'guard', 'boss_adapter'):
        monkeypatch.setattr(f'job_hunter.browser.{module}.is_boss_url', allowed)
    official_match = detail_response.matching_requests
    monkeypatch.setattr(detail_response, 'matching_requests', lambda rows: official_match([
        {**row, 'url': row['url'].replace(origin, 'https://www.zhipin.com', 1)} for row in rows
    ]))
    profile = tmp_path / 'profile'
    process = await asyncio.create_subprocess_exec(
        str(CHROME_PATH), '--headless=new', '--remote-debugging-port=0',
        f'--user-data-dir={profile}', '--no-first-run', '--no-default-browser-check', origin + '/',
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(10):
            while not (profile / 'DevToolsActivePort').exists():
                await asyncio.sleep(.05)
        port, path = (profile / 'DevToolsActivePort').read_text().splitlines()[:2]
        async with AgentSession(f'ws://127.0.0.1:{port}{path}') as session:
            if mode == 'no_response':
                await session.evaluate('window.skipFetch=true')
            expected_error = {'wrong_id': 'IDENTITY_MISMATCH', 'jd_conflict': 'JD_CONFLICT',
                              'salary_conflict': 'SALARY_CONFLICT'}.get(mode)
            if expected_error:
                with pytest.raises(PageReadError, match=expected_error):
                    await detail_response.probe_detail_response(session)
            else:
                summary = await detail_response.probe_detail_response(session)
                if mode == 'no_response':
                    assert summary['status'] == 'FALLBACK'
                    assert not summary['selected_jd_source'].startswith('页面自然详情响应')
                else:
                    assert summary['status'] == 'COMPARED'
                    assert summary['jd_comparison'] == 'MATCH'
                    assert summary['salary_ranges_match'] is True
                    assert summary['selected_jd_source'].startswith('页面自然详情响应')
                    assert summary['selected_salary'] == '25-30K·13薪'
            assert await session.evaluate('window.actionClicks') == 0
            assert hits.count(detail_response.DETAIL_PATH) == (0 if mode == 'no_response' else 1)
            assert session.guard.page_session
    finally:
        process.terminate()
        await process.wait()
        server.close()
        await server.wait_closed()
