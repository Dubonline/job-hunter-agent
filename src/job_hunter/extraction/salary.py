"""月薪读取：普通文本优先，私有字体通过左右薪资区域本机OCR交叉核对。"""
import asyncio
import json
import re
import tempfile
from pathlib import Path
from time import perf_counter

from job_hunter.browser.adapter_base import PageReadError

OCR_BINARY = Path(__file__).resolve().parents[3] / '.local/tools/salary-ocr'


def ocr_value(rows):
    """只接受高置信度且唯一的K月薪区间，不补数字、不猜字体映射。"""
    from job_hunter.evaluation.policy import salary_range

    matches = []
    for row in rows:
        # 截图区只含薪资；允许OCR保留或省略空格，不从JD中挑选数字。
        text = re.sub(r'\s+', '', row.get('text', '')).replace('−', '-').replace('—', '-')
        if row.get('confidence', 0) >= .9 and salary_range(text):
            matches.append(text)
    return matches[0] if len(matches) == 1 else None


async def recognize(path, clip=None):
    """本机识别临时PNG；clip为DOM月薪相对矩形，默认整图，超时10秒。"""
    process = await asyncio.create_subprocess_exec(
        str(OCR_BINARY), str(path), *([str(v) for v in clip] if clip else []), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 10)
        return json.loads(output) if process.returncode == 0 else []
    except TimeoutError:
        process.kill()
        await process.wait()
        return []


async def read_salary(session, card, state, state_script):
    """返回可靠月薪及最小审计信息；OCR失败返回空值，继续保持禁止投递。

    card/state为同一个已验证岗位；state_script用于截图后再次校验身份/正文。
    仅截取左右薪资元素，临时图片用完即删。两侧识别必须一致。
    """
    from job_hunter.evaluation.policy import salary_range

    started = perf_counter()
    raw = state.get('salary_text', '')
    if salary_range(raw):
        return raw, {'source':'DOM薪资文本','status':'CONFIRMED','seconds':0}
    audit = {'source':'左右薪资区域截图＋本机Apple Vision OCR','status':'UNVERIFIED'}
    if not OCR_BINARY.is_file():
        return '', {**audit,'reason':'本机OCR未安装'}
    # 选择器由实际薪资节点生成；不修改页面、不读取Cookie或网络接口。
    selectors = await session.evaluate(r"""(() => {
      const shown=e=>e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
      const path=e=>{let parts=[];while(e&&e!==document.documentElement){
        parts.unshift(e.tagName.toLowerCase()+':nth-child('+([...e.parentElement.children].indexOf(e)+1)+')');e=e.parentElement;
      }return 'html>'+parts.join('>')};
      const choose=root=>{const nodes=[...root?.querySelectorAll('[class*="salary"]')||[]].filter(shown)
        .filter(e=>e.innerText.trim()).sort((a,b)=>{const x=a.getBoundingClientRect(),y=b.getBoundingClientRect();return x.width*x.height-y.width*y.height});const e=nodes[0];if(!e)return null;
        // 只按DOM中明确K及薪数分隔符定位月薪，私有字体数字保持原样交给OCR。
        const text=e.textContent, prefix=text.match(/^[\s0-9\uE000-\uF8FF.\-–—~]+[kK](?=\s*[·•.])/);
        let clip=null;
        if(prefix){const walker=document.createTreeWalker(e,NodeFilter.SHOW_TEXT);let node,remaining=prefix[0].length;
          const range=document.createRange();range.setStart(e,0);
          while(node=walker.nextNode()){if(remaining<=node.length){range.setEnd(node,remaining);break}remaining-=node.length}
          const box=e.getBoundingClientRect(),r=range.getBoundingClientRect();
          if(r.width>0&&r.height>0&&r.left>=box.left&&r.right<=box.right&&r.top>=box.top&&r.bottom<=box.bottom)
            clip=[(r.left-box.left)/box.width,(r.top-box.top)/box.height,r.width/box.width,r.height/box.height];
        }
        return {selector:path(e),clip}};
      const card=[...document.querySelectorAll('.job-card-box')].find(e=>e.querySelector('a.job-name')?.getAttribute('href')===PATH);
      return {right:choose(document.querySelector('.job-detail-header')),left:choose(card)};
    })()""".replace('PATH', json.dumps(card.path)))
    if not selectors.get('left') or not selectors.get('right'):
        return '', {**audit,'reason':'左右薪资元素不齐全'}
    values = []
    # 仅保留薪资小区域的原始候选与置信度，便于定位失败；不保存截图或JD。
    audit['ocr_rows'] = {}
    with tempfile.TemporaryDirectory(prefix='job-salary-') as directory:
        for side in ('left', 'right'):
            region = selectors[side]
            selector = region if isinstance(region, str) else region['selector']
            clip = None if isinstance(region, str) else region.get('clip')
            await session.evaluate('document.querySelector('+json.dumps(selector)+').scrollIntoView({block:"center",behavior:"instant"})')
            await session.evaluate('document.fonts.ready.then(()=>true)')
            image = Path(directory) / (side+'.png')
            await session._request({'action':'screenshot','selector':selector,'path':str(image),'format':'png'})
            rows = await recognize(image)
            audit['ocr_rows'][side] = rows
            value = ocr_value(rows)
            if value is None and clip:
                # 附注干扰时按DOM像素范围重读，仍要求唯一高置信月薪和左右一致。
                monthly_rows = await recognize(image, clip)
                audit.setdefault('monthly_ocr_rows', {})[side] = monthly_rows
                value = ocr_value(monthly_rows)
            values.append(value)
    after = await session.evaluate(state_script)
    if any(after.get(key) != state.get(key) for key in ('id','description','title','company','salary_text')):
        raise PageReadError('SALARY_IDENTITY_CHANGED：截图期间岗位或薪资变化')
    for side, value in zip(('left', 'right'), values, strict=True):
        if value is None:
            return '', {**audit,'reason':side+'薪资识别不确定','seconds':round(perf_counter()-started,3)}
    if salary_range(values[0]) != salary_range(values[1]):
        return '', {**audit,'reason':'左右薪资识别不一致','seconds':round(perf_counter()-started,3)}
    return values[1], {**audit,'status':'CONFIRMED','left':values[0],'right':values[1],
                       'minimum_confidence':.9,'seconds':round(perf_counter()-started,3)}
