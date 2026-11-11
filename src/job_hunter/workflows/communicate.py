"""明确授权的单岗沟通：单次点击，确认成功后仅留在此页，不附带消息或收藏。"""
import asyncio
import hashlib
import json
from pathlib import Path

from job_hunter.browser.adapter_base import PageReadError
from job_hunter.browser.boss_adapter import BossAdapter
from job_hunter.browser.pointer import point
from job_hunter.extraction.boss_details import DETAIL_STATE, current_cards, open_detail

# 只读取当前详情的沟通按钮和指定成功弹框，不采集聊天记录。
COMMUNICATION_STATE = r"""(() => {
 // 不依赖弹框固定CSS类名；按平台文案及两个操作标签定位最小可见容器。
 const normalized=e=>(e?.innerText||'').replace(/\s+/g,'').toUpperCase();
 const shown=e=>e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
 const path=e=>{let parts=[];while(e&&e!==document.documentElement){
   parts.unshift(e.tagName.toLowerCase()+':nth-child('+([...e.parentElement.children].indexOf(e)+1)+')');e=e.parentElement;
 }return 'html>'+parts.join('>')};
 const all=[...document.querySelectorAll('body *')].filter(shown);
 const leaves=text=>all.filter(e=>normalized(e)===text&&!([...e.children].some(c=>shown(c)&&normalized(c)===text)));
 const message=all.filter(e=>normalized(e).includes('已向BOSS发送消息')&&!([...e.children].some(c=>shown(c)&&normalized(c).includes('已向BOSS发送消息'))));
 const stayLeaves=leaves('留在此页'),continueLeaves=leaves('继续沟通');
 let dialog=null;
 if(message.length===1){
   let node=message[0];
   while(node&&node!==document.body){
     if(normalized(node).length<600&&stayLeaves.filter(e=>node.contains(e)).length===1&&continueLeaves.filter(e=>node.contains(e)).length===1){dialog=node;break}
     node=node.parentElement;
   }
 }
 const target=leaf=>{
   const control=leaf.closest('a,button,[role="button"],.btn');
   return control&&dialog?.contains(control)&&normalized(control)==='留在此页'?control:leaf;
 };
 const stay=dialog?stayLeaves.filter(e=>dialog.contains(e)).map(target):[];
 const buttons=[...document.querySelectorAll('.job-detail-box .op-btn-chat')].filter(shown);
 // 仅识别可见系统弹框/提示中的明确日限额文案，不把JD中的数字或普通按钮当限额。
 const notices=all.filter(e=>e.matches('[role="dialog"],[role="alert"],.toast,.toast-msg,.dialog-container,.boss-dialog,.greet-boss-container')&&normalized(e).length<1000);
 const dailyLimit=notices.some(e=>/(今日|今天|每日|每天).{0,40}(沟通|打招呼).{0,40}(上限|用完|耗尽)|(?:沟通|打招呼).{0,30}(?:今日|今天).{0,30}(?:上限|用完|耗尽)/.test(normalized(e)));
 return {buttons:buttons.map(e=>({text:e.innerText.trim(),disabled:e.matches('[disabled],[aria-disabled="true"],.disabled'),selector:path(e)})),
   daily_limit:dailyLimit,
   success:!!dialog,stay:stay.map(path),message_visible:message.length,
   dialog:dialog?{tag:dialog.tagName,class:dialog.className,text:message[0].innerText.trim(),
     stay_tag:stay[0]?.tagName,stay_class:stay[0]?.className}:null};
})()"""


def snapshot(state):
    """只在内存计算岗位关键信息指纹，防止评审后JD、月薪或身份改变。"""
    fields = ['id', 'title', 'company', 'description', 'address', 'tags', 'salary_text']
    return hashlib.sha256(json.dumps({k: state.get(k) for k in fields}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def previous_attempt(job_id, directory=Path('.local/runs')):
    """已有尝试（包括未知结果）即禁止再次提交，不自动重试。"""
    for file in directory.glob('*.jsonl'):
        for line in file.read_text().splitlines():
            event = json.loads(line)
            if event.get('event') == 'communicate_intent' and event.get('job_id') == job_id:
                return True
    return False


async def communicate_one(session, job_id, preferences, candidate, evaluator, emit):
    """仅复核指定的一个岗位；不搜索、不滚动找替代、不收藏、不额外发消息。

    job_id必须是用户授权岗位；emit必须同步落盘，点击前先记录尝试。
    evaluator使用冻结的正式模型与策略。状态不明、已有沟通或资格不符即停止。
    """
    if previous_attempt(job_id):
        raise PageReadError('COMMUNICATE_PREVIOUS_ATTEMPT：禁止重复未知或已完成尝试')
    await session.bring_to_front()
    cards = [c for c in await current_cards(session) if c.job_id == job_id]
    if len(cards) != 1:
        raise PageReadError('COMMUNICATE_TARGET_NOT_PRESENT：不搜索或另选岗位')
    detail = await open_detail(session, cards[0])
    if detail.reading.get('status') != 'DOM_COMPLETE':
        raise PageReadError('COMMUNICATE_JD_UNVERIFIED')
    before = await session.evaluate(DETAIL_STATE)
    emit({'event':'detail_verified','job_id':job_id,'title':detail.detail_title,'company':detail.card.company,
          'reading':detail.reading,'salary':detail.salary_text})
    decision = await evaluator.evaluate(detail, preferences, candidate)
    emit({'event':'decision', **decision.minimal_record(detail)})
    if decision.status != 'HIGH_FIT':
        raise PageReadError('COMMUNICATE_NOT_HIGH_FIT：'+decision.reason)
    await send_reviewed(session, job_id, before, emit)


async def send_reviewed(session, job_id, before, emit):
    """复用已评审快照，在同一连接核对未改变和沟通状态后提交一次。

    before为评审前详情快照；emit必须同步落盘。该入口不重新评审或另选岗位。
    """
    if previous_attempt(job_id):
        raise PageReadError('COMMUNICATE_PREVIOUS_ATTEMPT')
    await BossAdapter(session).require_signed_in()
    current = await session.evaluate(DETAIL_STATE)
    if snapshot(current) != snapshot(before) or current.get('id') != job_id:
        raise PageReadError('COMMUNICATE_JD_CHANGED')
    state = await session.evaluate(COMMUNICATION_STATE)
    if state.get('daily_limit'):
        emit({'event':'platform_daily_limit'})
        raise PageReadError('PLATFORM_DAILY_LIMIT：不再发送')
    if state['success'] or len(state['buttons']) != 1:
        raise PageReadError('COMMUNICATE_STATE_UNKNOWN')
    button = state['buttons'][0]
    if button['text'] != '立即沟通' or button['disabled']:
        raise PageReadError('COMMUNICATE_ALREADY_CONTACTED_OR_UNAVAILABLE：'+button['text'])
    await session.evaluate('document.querySelector('+json.dumps(button['selector'])+').scrollIntoView({block:"center",behavior:"instant"})')
    # 意图必须先落盘；之后任何异常都不重试，不宣称点击成功。
    emit({'event':'communicate_intent','job_id':job_id,'button_text':button['text'],'maximum_attempts':1})
    await point(session, button['selector'], click=True)
    for _ in range(40):
        state = await session.evaluate(COMMUNICATION_STATE)
        if state.get('daily_limit'):
            emit({'event':'platform_daily_limit'})
            emit({'event':'communicate_result','job_id':job_id,'status':'unknown','retry':False})
            raise PageReadError('PLATFORM_DAILY_LIMIT：结果未知，不重试')
        if state['success']:
            pathname = await session.evaluate('location.pathname')
            await close_sent_dialog(session, job_id, state, pathname, emit)
            return
        await asyncio.sleep(.25)
    emit({'event':'communicate_result','job_id':job_id,'status':'unknown','retry':False})
    raise PageReadError('COMMUNICATE_RESULT_UNKNOWN：不重试')


async def select_current_one(session, preferences, candidate, evaluator, emit, limit=5, candidate_ids=None):
    """从当前已加载结果最多评审limit岗，投递首个通过者；不搜索、不补查。

    limit按实际读取岗位数计，跳过本机已有沟通尝试。
    candidate_ids可限定为同批已出现的身份，只复核这些岗位，不扩大样本。
    通用及高度契合均可投；英语特殊要求仍由正式策略限制为高度契合。
    一旦进入沟通尝试，无论成功/未知都不再选择其他岗位。
    """
    from time import perf_counter

    await session.bring_to_front()
    cards = await current_cards(session)
    if candidate_ids is not None:
        allowed = set(candidate_ids)  # 修复后仅复核同批候选；不从其他岗位补足。
        cards = [card for card in cards if card.job_id in allowed]
    processed = 0
    for card in cards:
        if previous_attempt(card.job_id):
            continue
        if processed >= limit:
            break
        started = perf_counter()
        detail = await open_detail(session, card)
        processed += 1
        emit({'event':'detail_verified','ordinal':processed,'job_id':card.job_id,'company':card.company,
              'title':detail.detail_title,'salary':detail.salary_text,'reading':detail.reading})
        if detail.reading.get('status') != 'DOM_COMPLETE':
            emit({'event':'candidate_skipped','job_id':card.job_id,'reason':'正文完整性未知'})
            continue
        before = await session.evaluate(DETAIL_STATE)
        state = await session.evaluate(COMMUNICATION_STATE)
        if state['success'] or len(state['buttons']) != 1:
            raise PageReadError('COMMUNICATE_STATE_UNKNOWN：不继续选择')
        button = state['buttons'][0]
        if button['text'] != '立即沟通' or button['disabled']:
            emit({'event':'candidate_skipped','job_id':card.job_id,'reason':'已沟通过或按钮不可用'})
            continue
        decision = await evaluator.evaluate(detail, preferences, candidate)
        emit({'event':'decision', 'ordinal':processed,'seconds':round(perf_counter()-started,3),
              **decision.minimal_record(detail)})
        if decision.status not in {'HIGH_FIT','GENERAL'}:
            continue
        await send_reviewed(session, card.job_id, before, emit)
        return {'processed':processed,'communicate_attempts':1,'favorite_attempts':0}
    emit({'event':'no_eligible_candidate','processed':processed,'communicate_attempts':0})
    return {'processed':processed,'communicate_attempts':0,'favorite_attempts':0}


async def dismiss_sent_dialog(session, job_id, emit):
    """只处理用户已确认发送的当前弹框；绝不重新点击沟通或进入聊天。

    job_id为原尝试岗位；先核对当前详情身份和成功弹框，点击留页后
    同时确认弹框消失、路径与岗位身份未变，才记录成功。
    """
    await session.bring_to_front()
    before = await session.evaluate(DETAIL_STATE)
    pathname = await session.evaluate('location.pathname')
    state = await session.evaluate(COMMUNICATION_STATE)
    emit({'event':'sent_dialog_observed','job_id':job_id,'state':state})
    if before.get('id') != job_id or not state['success'] or len(state['stay']) != 1:
        raise PageReadError('SENT_DIALOG_NOT_CONFIRMED：只读结束，不点击任何按钮')
    await close_sent_dialog(session, job_id, state, pathname, emit, reconciles=True)


async def close_sent_dialog(session, job_id, state, pathname, emit, reconciles=False):
    """共用留页处理：成功弹框证据确定后，验证关闭及原岗位/路径均保留。

    state为已识别的成功弹框；pathname为操作前路径；reconciles表示补核历史未知。
    """
    if not state['success'] or len(state['stay']) != 1:
        raise PageReadError('COMMUNICATE_SENT_STAY_BUTTON_UNKNOWN')
    emit({'event':'communicate_result','job_id':job_id,'status':'confirmed_sent',
          'evidence':'当前弹框：'+state['dialog']['text'],'reconciles_prior_unknown':reconciles})
    await point(session,state['stay'][0],click=True)
    for _ in range(20):
        state = await session.evaluate(COMMUNICATION_STATE)
        if not state['success'] and state['message_visible'] == 0:
            after = await session.evaluate(DETAIL_STATE)
            current_path = await session.evaluate('location.pathname')
            if after.get('id') != job_id or pathname != current_path:
                raise PageReadError('STAY_DESTINATION_UNKNOWN：不再操作')
            emit({'event':'stay_on_page','job_id':job_id,'status':'confirmed',
                  'dialog_closed':True,'same_job':True,'same_path':True,
                  'communicate_attempts':0,'favorite_attempts':0,'extra_messages':0})
            return
        await asyncio.sleep(.2)
    raise PageReadError('STAY_RESULT_UNKNOWN：不重复点击')
