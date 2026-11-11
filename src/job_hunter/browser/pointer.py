"""对视口内可见、无遮挡的页面元素发送原生鼠标事件。"""

import asyncio
import json

from job_hunter.browser.adapter_base import PageReadError


async def point(session, selector, click=False, card_identity=None):
    """定位并悬浮，按需点击；不使用桌面鼠标，不强制点击隐藏元素。

    参数：session为当前受保护的浏览器会话；selector为CSS选择器；
    click为False时仅移动鼠标，为True时再发送按下和释放事件。
    card_identity仅供打开详情，含path/company/title；验证等价重复卡片后择一。
    默认None保持唯一节点限制，收藏/沟通等动作不能使用重复卡片例外。
    """
    script = r"""(() => {
          const identity=EXPECTED_CARD;
          let es=[...document.querySelectorAll(SELECTOR)].filter(e=>
            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}));
          if(identity){
            const norm=s=>(s||'').normalize('NFKC').replace(/\s+/g,'');
            if(es.some(e=>{const card=e.closest('.job-card-box'),link=card?.querySelector('a.job-name');
              return !card||link?.getAttribute('href')!==identity.path||
                norm(link?.innerText)!==norm(identity.title)||
                norm(card.querySelector('.boss-name')?.innerText)!==norm(identity.company)
            }))return {error:'M3_DUPLICATE_CARD_IDENTITY_CONFLICT'};
          }
          es=es.filter(e=>{
              const r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;
              return x>=0&&y>=0&&x<innerWidth&&y<innerHeight;
            });
          if(identity){
            es=es.filter(e=>{const r=e.getBoundingClientRect();return e.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2))})
              .sort((a,b)=>{const x=a.getBoundingClientRect(),y=b.getBoundingClientRect();return x.y-y.y||x.x-y.x});
          }else if(es.length!==1)return null;
          if(!es.length)return null;
          const e=es[0],r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2;
          if(x<0||y<0||x>=innerWidth||y>=innerHeight)return null;
          if(!e.contains(document.elementFromPoint(x,y)))return null;
          return {x,y};
        })()""".replace('SELECTOR', json.dumps(selector)).replace('EXPECTED_CARD', json.dumps(card_identity))
    previous = None  # 上一次采样的坐标；连续两次相同才认为布局稳定。
    position = None  # 最终确认的页面视口坐标，单位为CSS像素。
    for _ in range(15):
        current = await session.evaluate(script)
        if current and current.get('error'):
            raise PageReadError(current['error'])
        if current and current == previous:
            position = current
            break
        previous = current
        await asyncio.sleep(.1)
    if not position:
        state = await session.evaluate("""(() => ({visibility:document.visibilityState,
          targets:[...document.querySelectorAll(SELECTOR)].map(e=>{
            const r=e.getBoundingClientRect(),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
            return {visible:e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}),
              x:r.x,y:r.y,w:r.width,h:r.height,hit:hit?.className};
          })}))()""".replace('SELECTOR', json.dumps(selector)))
        raise PageReadError('M3_POINTER_TARGET_NOT_VISIBLE_OR_OBSTRUCTED：' + json.dumps(state, ensure_ascii=False))
    await session._request({'action': 'mouse', 'eventType': 'mouseMoved', **position})
    if click:
        for event in ('mousePressed', 'mouseReleased'):
            await session._request({
                'action': 'mouse', 'eventType': event, 'button': 'left',
                'clickCount': 1, **position,
            })
