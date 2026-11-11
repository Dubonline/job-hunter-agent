"""Visible DOM evidence only. No login, messaging, or resume actions."""

import asyncio
import json
import uuid

from job_hunter.browser.adapter_base import LoginStatus, PageReadError
from job_hunter.browser.attached import is_boss_url
from job_hunter.browser.pointer import point
from job_hunter.browser.session import AgentSession

LOGIN_EVIDENCE = r"""(() => {
 const visible=e=>!!e.getClientRects().length;
 const count=s=>[...document.querySelectorAll(s)].filter(visible).length;
 const exact=(s,re)=>[...document.querySelectorAll(s)].filter(visible)
   .filter(e=>re.test(e.textContent.trim())).length;
 return {url:location.origin+location.pathname,
   messages:count('a[href*="/web/geek/chat"]'),resume:count('a[ka="header-resume"]'),
   login:exact('a,button',/^登录(?:\s*\/\s*注册)?$/),
   verification:exact('div,span,p,h1,h2',/^(请完成安全验证|安全验证|请完成验证|访问异常|账号异常)$/)};
})()"""


def classify_login(evidence: dict) -> LoginStatus:
    messages, resume, login = (
        evidence.get(k, 0) for k in ("messages", "resume", "login")
    )
    if evidence.get("verification") or (login and (messages or resume)):
        return LoginStatus.UNKNOWN
    if messages == resume == 1 and not login:
        return LoginStatus.SIGNED_IN
    if login == 1 and not messages and not resume:
        return LoginStatus.SIGNED_OUT
    return LoginStatus.UNKNOWN


class BossAdapter:
    def __init__(self, session: AgentSession):
        self.session = session

    async def check_login(self) -> LoginStatus:
        evidence = await self.session.evaluate(LOGIN_EVIDENCE)
        if not isinstance(evidence, dict):
            self.session.guard.trip("BOSS_PAGE_UNKNOWN")
            raise PageReadError("BOSS_PAGE_UNKNOWN")
        if not is_boss_url(evidence.get("url", "")):
            self.session.guard.trip("BOSS_PAGE_ORIGIN_CHANGED")
            raise PageReadError("BOSS_PAGE_ORIGIN_CHANGED")
        return classify_login(evidence)

    async def require_signed_in(self):
        status = await self.check_login()
        if status != LoginStatus.SIGNED_IN:
            self.session.guard.trip("BOSS_LOGIN_NOT_READY")
            raise PageReadError(f"BOSS_LOGIN_NOT_READY：{status}，请人工处理后重新启动")

    async def search(self, keyword: str, city: str, force_submit: bool = False) -> dict:
        if not keyword.strip() or len(keyword) > 100:
            raise PageReadError("BOSS_SEARCH_INPUT_INVALID")
        await self.require_signed_in()
        state = await self.session.evaluate(
            "({path:location.pathname,city:new URL(location.href).searchParams.get('city'),query:new URL(location.href).searchParams.get('query'),homeInput:!!document.querySelector('input.ipt-search')?.checkVisibility(),inputs:[...document.querySelectorAll('input.ipt-search,.search-input-box input.input')].filter(e=>e.checkVisibility()).length,buttons:[...document.querySelectorAll('button.btn-search,a.search-btn[ka=\"job_search_btn_click\"]')].filter(e=>e.checkVisibility()).length})"
        )
        # Homepage and result-page controls have been read from the real DOM.
        # Other city mappings remain unverified.
        if city != "上海" or not (
            state["path"] == "/shanghai/" or state["city"] == "101020100"
        ):
            raise PageReadError("M3_CITY_NOT_READY：请先人工选择上海；城市切换尚未验收")
        already_searched = (
            state.get("query") == keyword and state["city"] == "101020100"
        )
        marker = None
        if not already_searched or force_submit:
            if state["inputs"] != 1 or state["buttons"] != 1:
                raise PageReadError(
                    "M3_SEARCH_CONTROLS_UNKNOWN：无法唯一确认当前页搜索控件"
                )
            marker = '__jobAgentSearch_' + uuid.uuid4().hex
            await self.session.evaluate(
                f'window[{json.dumps(marker)}]=document.querySelector(".job-card-box")||document.querySelector(".job-card-wrapper");true'
            )
            await self.session._request(
                {"action": "fill", "selector": "input.ipt-search" if state["homeInput"] else ".search-input-box input.input", "value": keyword}
            )
            await point(
                self.session,
                'button.btn-search,a.search-btn[ka="job_search_btn_click"]',
                click=True,
            )
        expression = """(() => {
          const u=new URL(location.href);
          return {query:u.searchParams.get('query'),city:u.searchParams.get('city'),
            titles:[...document.querySelectorAll('.job-card-wrapper,.job-card-box,.job-list-box li')]
            .filter(e=>e.getClientRects().length).slice(0,10).map(e=>e.innerText.split('\\n')[0])};
        })()"""
        previous = None
        for attempt in range(30):
            result = await self.session.evaluate(expression)
            replaced = marker is None or await self.session.evaluate(
                f'!window[{json.dumps(marker)}]?.isConnected && document.readyState==="complete"'
            )
            if (
                result.get("query") == keyword
                and result.get("city") == "101020100"
                and result.get("titles")
                and (replaced or (already_searched and attempt >= 10))
                and result == previous
            ):
                if marker:
                    await self.session.evaluate(f'delete window[{json.dumps(marker)}]')
                return result
            previous = result
            await asyncio.sleep(0.2)
        raise PageReadError("M3_RESULTS_UNKNOWN：无法确认结果，不按空结果处理")
