"""正式投递动作：高度契合收藏后沟通，通用只沟通；任何未知停止整轮。"""
import asyncio
import json
from pathlib import Path

from job_hunter.browser.adapter_base import PageReadError
from job_hunter.browser.pointer import point
from job_hunter.extraction.boss_details import DETAIL_STATE
from job_hunter.extraction.identity import job_key
from job_hunter.workflows.communicate import (
    COMMUNICATION_STATE,
    previous_attempt,
    send_reviewed,
    snapshot,
)

FAVORITE_STATE = r"""(() => {
 const nodes=[...document.querySelectorAll('.job-detail-header .op-btn-like')]
   .filter(e=>e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}));
 if(nodes.length!==1)return {status:'unknown',count:nodes.length};
 const e=nodes[0],text=e.innerText.replace(/\s+/g,''),pressed=e.getAttribute('aria-pressed');
 let status='unknown';
 if(['已收藏','取消收藏'].includes(text)||pressed==='true')status='saved';
 else if(text==='收藏'&&!/active|selected|collected/.test(e.className))status='unsaved';
 return {status,text,class:e.className,disabled:e.matches('[disabled],[aria-disabled="true"],.disabled'),
   selector:'.job-detail-header .op-btn-like'};
})()"""


def history_ids(directory=Path('.local/runs')):
    """一次读取最小日志，返回曾出现过的岗位ID；旧岗位不计入新5岗名额。

    包括旧评审、已发送和结果未知岗位；损坏日志停止，不能因读失败重复投递。
    """
    known = set()  # 只由本机历史记录排除，公开版不内置私人岗位。
    for file in directory.glob('*.jsonl'):
        try:
            for line in file.read_text().splitlines():
                event=json.loads(line)
                if event.get('job_id'):
                    known.add(event['job_id'])
        except (OSError,ValueError):
            raise PageReadError('HISTORY_UNREADABLE：不能安全判断新岗位或已投岗位') from None
    return known


def history_keys(directory=Path('.local/runs')):
    """复用旧日志或新指纹排除同公司同名岗位，默认关闭名单时仍可跨运行防重。"""
    from job_hunter.storage.daily import events
    keys=set()
    for row in events(directory):
        if row.get('identity_key'):
            keys.add(row['identity_key'])
        elif row.get('job_id') and row.get('company') and (row.get('listed_title') or row.get('title')):
            keys.add(job_key(row['company'],row.get('listed_title') or row['title']))
    return keys


async def ensure_favorite(session,job_id,before,emit):
    """仅收藏当前已核对的岗位；不反向取消收藏，结果未知不再点击。"""
    current=await session.evaluate(DETAIL_STATE)
    if current.get('id')!=job_id or snapshot(current)!=snapshot(before):
        raise PageReadError('FAVORITE_JD_CHANGED')
    state=await session.evaluate(FAVORITE_STATE)
    if state['status']=='saved':
        emit({'event':'favorite_result','job_id':job_id,'status':'already_saved'})
        return
    if state['status']!='unsaved' or state.get('disabled'):
        raise PageReadError('FAVORITE_STATE_UNKNOWN')
    selector=state['selector']
    await session.evaluate('document.querySelector('+json.dumps(selector)+').scrollIntoView({block:"center",behavior:"instant"})')
    emit({'event':'favorite_intent','job_id':job_id,'maximum_attempts':1})
    await point(session,selector,click=True)
    for _ in range(30):
        state=await session.evaluate(FAVORITE_STATE)
        if state['status']=='saved':
            after=await session.evaluate(DETAIL_STATE)
            if after.get('id')!=job_id:
                raise PageReadError('FAVORITE_IDENTITY_CHANGED')
            emit({'event':'favorite_result','job_id':job_id,'status':'confirmed'})
            return
        await asyncio.sleep(.2)
    emit({'event':'favorite_result','job_id':job_id,'status':'unknown'})
    raise PageReadError('FAVORITE_RESULT_UNKNOWN：停止，不重试、不沟通')


class ApplyActions:
    """由显式apply命令注入scan的动作处理器，保留每岗动作及尝试次数。"""

    def __init__(self,emit,target=None,directory=Path('.local/runs'),record_lists=False):
        """target为本次成功沟通人数；None仅兼容定向测试，正式CLI始终传入目标。"""
        self.emit=emit  # 同步落盘与进度显示，不保存整份JD。
        self.last_actions={}  # 当前岗动作，异常也供最终结果表引用。
        self.target=target
        self.sent=0  # 成功弹框确认数，留页未知也不能回退为未发送。
        self.directory=directory
        self.record_lists=record_lists

    def stop_reason(self):
        """每岗开始前检查目标与当日额度；不再读取额外岗位来凑数量。"""
        if self.target is None:
            return None
        if self.sent >= self.target:
            return 'communication_target'
        from job_hunter.storage.daily import snapshot
        state=snapshot(self.directory)
        if state['platform_blocked']:
            return 'platform_daily_limit'
        if state['remaining'] == 0:
            return 'daily_limit'
        return None

    async def __call__(self,session,detail,before,decision):
        job_id=detail.card.job_id
        self.last_actions={'favorite':'not_attempted','communicate':'not_attempted','stay':'not_attempted',
                           'favorite_attempts':0,'communicate_attempts':0}

        def emit(event):
            """先落盘再更新内存统计，避免未知操作被误记为未尝试。"""
            event={**event,'identity_key':job_key(detail.card.company,detail.card.title)}
            if self.record_lists and event['event'] in {'favorite_result','communicate_result'} and event.get('status') in {'confirmed','confirmed_sent'}:
                event={**event,'company':detail.card.company,'title':detail.detail_title or detail.card.title}
            self.emit(event)
            kind=event['event']
            if kind=='favorite_intent':
                self.last_actions.update(favorite='attempted',favorite_attempts=1)
            elif kind=='favorite_result':
                self.last_actions['favorite']=event['status']
            elif kind=='communicate_intent':
                self.last_actions.update(communicate='attempted',communicate_attempts=1)
            elif kind=='communicate_result':
                self.last_actions['communicate']=event['status']
                if event['status']=='confirmed_sent':
                    self.sent+=1
            elif kind=='stay_on_page':
                self.last_actions['stay']=event['status']

        if decision.status not in {'HIGH_FIT','GENERAL'}:
            return self.last_actions.copy()
        if self.stop_reason():
            raise PageReadError('APPLY_QUOTA_OR_TARGET_REACHED：停止，不收藏或沟通')
        # 收藏前先核对未联系状态；已联系不再附带收藏。
        if previous_attempt(job_id):
            raise PageReadError('APPLY_PREVIOUS_ATTEMPT')
        state=await session.evaluate(COMMUNICATION_STATE)
        if state.get('daily_limit'):
            emit({'event':'platform_daily_limit'})
            raise PageReadError('PLATFORM_DAILY_LIMIT：平台今日沟通额度已耗尽')
        if state['success'] or state.get('message_visible') or len(state['buttons'])!=1:
            raise PageReadError('APPLY_COMMUNICATION_STATE_UNKNOWN')
        button=state['buttons'][0]
        if button['text']=='继续沟通':
            self.last_actions['communicate']='already_contacted'
            return self.last_actions.copy()
        if button['text']!='立即沟通' or button['disabled']:
            raise PageReadError('APPLY_BUTTON_UNAVAILABLE')
        if decision.status=='HIGH_FIT':
            await ensure_favorite(session,job_id,before,emit)
        await send_reviewed(session,job_id,before,emit)
        return self.last_actions.copy()
