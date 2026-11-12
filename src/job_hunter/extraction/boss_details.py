"""正式详情读取：同岗位响应优先，DOM/OCR核对及兜底；原文仅留内存。"""

import asyncio
import json
import re
from dataclasses import dataclass, field

from job_hunter.browser.adapter_base import PageReadError
from job_hunter.browser.boss_adapter import BossAdapter
from job_hunter.browser.pointer import point
from job_hunter.extraction.identity import job_key


@dataclass(frozen=True)
class JobCard:
    """检索列表中的岗位身份，不包含详情正文。"""

    path: str  # 官方详情路径；不保存带安全令牌的完整URL。
    title: str  # 列表标题，可能是详情标题的简称。
    company: str  # 列表显示的公司名称。
    location: str  # 列表地点；不能代替JD中的实际base检查。

    @property
    def job_id(self):
        """从详情路径提取稳定岗位标识，用于去重和详情身份核对。"""
        return self.path.removeprefix('/job_detail/').removesuffix('.html')


@dataclass(frozen=True)
class JobDetail:
    """已核对身份的详情；响应优先并与DOM核对，无响应时保留DOM兜底。

    description不进入对象默认日志表示；innerText包含已渲染的屏幕外文本，
    reading记录实际来源与审计范围，不将单次双源一致说成服务端业务绝对完整。
    """
    card: JobCard
    description: str = field(repr=False)
    address: str
    tags: str
    detail_company: str = ''
    detail_title: str = ''  # 详情完整标题，可能比卡片多出实际驻地等重要信息。
    salary_text: str = ''  # 当前详情显示的招聘月薪原文；无法解析时不猜测。
    reading: dict = field(default_factory=dict)  # 响应/DOM/OCR来源、核对及长度；不包含完整正文。


async def current_cards(session) -> list[JobCard]:
    """从session当前结果页读取并去重岗位卡片；不自动点击或读取正文。"""
    await BossAdapter(session).require_signed_in()
    rows = await session.evaluate("""[...document.querySelectorAll('.job-card-box')].map(e=>({
      path:e.querySelector('a.job-name')?.getAttribute('href'),
      title:e.querySelector('a.job-name')?.innerText.trim(),
      company:e.querySelector('.boss-name')?.innerText.trim(),
      location:e.querySelector('.company-location')?.innerText.trim()||''
    }))""")
    if not rows:
        raise PageReadError('M3_RESULTS_UNKNOWN：未确认结果，不按空结果处理')
    cards = []
    seen = {}  # 同一ID必须有一致公司及名称，不能静默吞掉冲突卡片。
    keys = set()  # 公司及名称相同的不同ID，只保留一个候选。
    for row in rows:
        if not (
            isinstance(row.get('path'), str)
            and re.fullmatch(r'/job_detail/[A-Za-z0-9_-]+\.html', row['path'])
            and row.get('title') and row.get('company')
        ):
            raise PageReadError('M3_JOB_CARD_IDENTITY_UNKNOWN')
        key = job_key(row['company'], row['title'])
        if row['path'] in seen and seen[row['path']] != key:
            raise PageReadError('M3_DUPLICATE_CARD_IDENTITY_CONFLICT')
        if row['path'] not in seen and key not in keys:
            cards.append(JobCard(**row))
            keys.add(key)
        seen[row['path']] = key
    return cards


DETAIL_STATE = r"""(() => {
 const root=document.querySelector('.job-detail-box');
 const attr=root?.querySelector('.job-detail-header .op-btn-chat')?.getAttribute('ka')||'';
 const href=root?.querySelector('a.more-job-btn[href*="/job_detail/"]')?.getAttribute('href');
 const path=href?new URL(href,location.href).pathname:'';
 const desc=root?.querySelector('.job-detail-body .desc');
 const text=desc?.innerText.trim()||'';
 const rect=desc?.getBoundingClientRect();
 const shown=e=>e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
 const expand=[...root?.querySelectorAll('a,button,[role="button"],span')||[]]
   .filter(shown).map(e=>e.innerText?.trim()).filter(t=>/^(展开|展开全部|展开更多|显示全部|查看完整职位描述|查看全部职位描述|点击展开)$/.test(t));
 const loading=[...root?.querySelectorAll('[aria-busy="true"],.loading,.skeleton,[class*="loading"]')||[]].some(shown);
 return {id:path.match(/^\/job_detail\/([A-Za-z0-9_-]+)\.html$/)?.[1] ||
   (attr.startsWith('cpc_job_list_chat_')?attr.slice('cpc_job_list_chat_'.length):null),
   title:root?.querySelector('.job-detail-info .job-name')?.innerText.trim(),
   company:root?.querySelector('.job-boss-info .boss-info-attr')?.innerText.split('·')[0].trim(),
   description:text,
   salary_text:(()=>{
     // 薪资可能是职位信息的兄弟节点，不能限定在.job-detail-info内。
     const values=[...root?.querySelectorAll('.job-detail-header [class*="salary"],.job-salary')||[]]
       .filter(e=>e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))
       .map(e=>e.innerText.trim()).filter(Boolean);
     const unique=[...new Set(values)];return unique.length===1?unique[0]:'';
   })(),
   address:root?.querySelector('.job-address-desc')?.innerText.trim()||'',
   tags:root?.querySelector('.job-header-info .tag-list')?.innerText||'',
   audit:{char_count:text.length,desc_bottom:rect?.bottom||0,viewport_height:innerHeight,
     line_count:text.split('\n').length,expand_controls:[...new Set(expand)],loading,
     line_clamp:desc?getComputedStyle(desc).webkitLineClamp:'none',
     scroll_height:desc?.scrollHeight||0,client_height:desc?.clientHeight||0}};
})()"""


async def open_detail(session, card: JobCard) -> JobDetail:
    """打开card，核对稳定DOM与本次自然响应后返回；任何数据冲突停止。

    只点击岗位卡片，不点击公司链接、收藏或沟通。相同岗位ID的详情可能
    使用更完整的标题，须保留详情全称供实际base和岗位方向判断。
    """
    await BossAdapter(session).require_signed_in()
    # Accept identities originating in the validated current-page catalog only.
    if not re.fullmatch(r'/job_detail/[A-Za-z0-9_-]+\.html', card.path):
        raise PageReadError('M3_JOB_CARD_IDENTITY_UNKNOWN')
    from job_hunter.extraction.detail_response import (
        begin_capture,
        prefer_response,
        read_captured,
    )

    previous_ids = await begin_capture(session)  # 本次点击前的请求集合，防止串用历史响应。
    selector = f'.job-card-box:has(a.job-name[href="{card.path}"]) .job-info'
    await session.evaluate(
        f'[...document.querySelectorAll({json.dumps(selector)})].find(e=>e.checkVisibility({{checkOpacity:true,checkVisibilityCSS:true}}))?.scrollIntoView({{block:"center",behavior:"instant"}})'
    )
    await point(session, selector, click=True, card_identity={
        'path':card.path,'company':card.company,'title':card.title,
    })
    previous = None
    for _ in range(80):
        state = await session.evaluate(DETAIL_STATE)
        if (
            state.get('id') == card.job_id
            and state.get('title')
            and state.get('company')
            and state.get('description')
        ):
            if same_content(state, previous):
                detail = await verify_loaded_dom(session, card, state)
                response, reason = await read_captured(session, previous_ids, card.job_id)
                after = await session.evaluate(DETAIL_STATE)
                if not same_content(after, state):
                    raise PageReadError('DETAIL_RESPONSE_DOM_CHANGED_DURING_COMPARISON')
                return prefer_response(detail, response, reason)
            previous = state
        else:
            previous = None
        await asyncio.sleep(.1)
    observed = {key: state.get(key) for key in ('id', 'title', 'company')}
    raise PageReadError('M3_DETAIL_IDENTITY_OR_CONTENT_NOT_CONFIRMED：' + json.dumps(
        {'expected_id': card.job_id, 'expected_title': card.title, 'observed': observed}, ensure_ascii=False
    ))


def same_content(state, previous):
    """核对身份/正文是否稳定，忽略滚动导致的视口坐标变化。"""
    return previous is not None and all(state.get(key) == previous.get(key) for key in ('id', 'title', 'company', 'description', 'address', 'tags', 'salary_text'))


async def verify_loaded_dom(session, card, before):
    """滚到当前右侧正文末尾后重读，验证已加载DOM；不声称已比对后台全文。

    session为同一连接，card为已核对的岗位，before为滚动前快照。
    有展开/加载/末尾省略迹象时保留待确认，不调用模型猜测缺失正文。
    """
    await session.evaluate("document.querySelector('.job-detail-body .desc')?.scrollIntoView({block:'end',behavior:'instant'})")
    previous = None
    state = before
    stable = False
    for _ in range(30):
        await asyncio.sleep(.15)
        state = await session.evaluate(DETAIL_STATE)
        if state.get('id') != card.job_id:
            raise PageReadError('M3_DETAIL_CHANGED_DURING_COMPLETENESS_CHECK')
        if same_content(state, previous) and not state['audit']['loading']:
            stable = True
            break
        previous = state
    audit = state['audit']
    issues = []
    if not stable or not state.get('description'):
        issues.append('正文未稳定或仍在加载')
    if audit['expand_controls']:
        issues.append('仍有正文展开控件：' + '、'.join(audit['expand_controls']))
    if re.search(r'(?:\.\.\.|…+)\s*$', state['description']):
        issues.append('正文末尾含省略标记，未确认是否预览截断')
    reading = {
        'audit_version': 'loaded_dom_v1',
        'source': '页面JS：.job-detail-body .desc.innerText',
        'scope': '已加载DOM正文；未与站点后台全文对照',
        'status': 'DOM_COMPLETE' if not issues else 'UNVERIFIED',
        'status_text': '当前DOM正文已读全，末尾滚动后稳定，未见展开/加载提示' if not issues else '完整性待确认',
        'char_count': len(state['description']), 'before_char_count': len(before['description']),
        'line_count': audit['line_count'],
        'offscreen_text_before_probe': before['audit']['desc_bottom'] > before['audit']['viewport_height'],
        'same_text_after_end_scroll': before['description'] == state['description'],
        'end_in_viewport_after_probe': 0 <= audit['desc_bottom'] <= audit['viewport_height']+2,
        'expand_controls': audit['expand_controls'], 'loading': audit['loading'],
        'css_line_clamp': audit['line_clamp'], 'issues': issues,
    }
    from job_hunter.extraction.salary import read_salary

    salary, salary_audit = await read_salary(session, card, state, DETAIL_STATE)
    reading['salary'] = salary_audit
    return JobDetail(card, state['description'], state['address'], state['tags'], state['company'], state['title'], salary_text=salary, reading=reading)


async def load_more(session, seen: set[str]) -> list[JobCard]:
    """在session中滚动岗位列表，返回未包含在seen岗位ID集合中的卡片。

    优先滚动实际列表容器，兼容整页滚动布局；有界等待后未观察到新身份
    返回空列表，不把加载不明宣称为官方已无岗位。
    """
    await BossAdapter(session).require_signed_in()
    # 前一次加载可能在等待窗口结束后才到达；优先消费已出现的新身份，避免重复滚动。
    available = [card for card in await current_cards(session) if card.job_id not in seen]
    if available:
        return available
    await session.bring_to_front()
    changed = await session.evaluate("""(() => {
      const cards=[...document.querySelectorAll('.job-card-box')];
      if(!cards.length)return false;
      const last=cards.at(-1);
      last.scrollIntoView({block:'end',behavior:'instant'});
      let container=last.parentElement;
      while(container){const s=getComputedStyle(container);
        if(/auto|scroll/.test(s.overflowY)&&container.scrollHeight>container.clientHeight)break;
        container=container.parentElement;
      }
      const target=container||document.scrollingElement;
      target.scrollTo({top:target.scrollHeight,behavior:'instant'});
      return true;
    })()""")
    if not changed:
        raise PageReadError('M3_JOB_LIST_SCROLL_TARGET_UNKNOWN')
    for _ in range(50):  # 最多约10秒；真实页面异步加载可能超过原先4秒窗口。
        await asyncio.sleep(.2)
        cards = await current_cards(session)
        new = [card for card in cards if card.job_id not in seen]
        if new:
            return new
    return []  # No observed new identities; do not claim a verified empty page.
