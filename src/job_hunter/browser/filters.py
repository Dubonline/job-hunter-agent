"""使用已记录的真实选项；按准确选中集合确认单选/多选，不拼接后台筛选URL。"""

import asyncio
import json

from job_hunter.browser.adapter_base import PageReadError
from job_hunter.browser.pointer import point
from job_hunter.models.preferences import Search

RECORDED = {
    "job_hunting_type_filter": ("求职类型", "jobType", {"全职": "1901"}),
    "salary_filter": ("薪资待遇", "salary", {"20-50K": "406"}),
    "experience_filter": ("工作经验", "exp", {"3-5年": "105"}),
    "education_filter": ("学历要求", "degree", {"本科": "203"}),
    "company_size_filter": ("公司规模", "scale", {"20-99人": "302", "100-499人": "303", "500-999人": "304"}),
}


def validate_filters(search: Search):
    """连接前校验所有非空条件；公司规模允许非空多选，未知值不得静默忽略。"""
    for field in search.active_page_filters():
        requested = getattr(search, field)
        values = requested if isinstance(requested, list) else [requested]
        if field not in RECORDED or not values or len(values) != len(set(values)) or any(value not in RECORDED[field][2] for value in values):
            raise PageReadError(f"M3_FILTER_NOT_READY：尚未录制此条件 {field}")


async def filter_catalog(session):
    """读取菜单标题、真实选项和active状态；不点击页面。"""
    return await session.evaluate(r"""[...document.querySelectorAll('.filter-condition-inner > div')]
      .filter(e=>e.getClientRects().length).map(e=>({
        label:e.querySelector('.current-select,.cur-area-label,.cur-city-label')?.innerText.trim(),
        options:[...e.querySelectorAll('li[ka]')].map(o=>({text:o.textContent.trim(),ka:o.getAttribute('ka'),selected:o.classList.contains('active')}))
      })).filter(e=>e.label)""")


async def apply_filters(session, search: Search):
    """应用配置中的条件，逐次确认准确选中集合；规模多选只保留指定档位。"""
    validate_filters(search)
    catalog = await filter_catalog(session)
    if not catalog:
        raise PageReadError("M3_FILTER_CONTROLS_UNKNOWN")
    # Unspecified controls are read, never clicked. Report observed old selections.
    placeholders = {
        "工作区域",
        "职位类型",
        "求职类型",
        "薪资待遇",
        "工作经验",
        "学历要求",
        "公司行业",
        "公司规模",
        "融资阶段",
        "不限",
    }
    configured = {RECORDED[k][1] for k in search.active_page_filters()}
    before_results = await result_signature(session)
    changed = False  # 只有实际点击过筛选项，才等待结果刷新，避免读取旧岗位列表。
    for item in catalog:
        options = item["options"]
        prefix = next(
            (
                kind
                for _, kind, _ in RECORDED.values()
                if any(o["ka"].startswith(f"sel-job-rec-{kind}-") for o in options)
            ),
            None,
        )
        if prefix not in configured and item["label"] not in placeholders | set(
            search.cities
        ):
            raise PageReadError(
                "M3_EXISTING_FILTER_CONFLICT：发现未指定项的遗留条件，请核对"
            )
    applied = {}
    for field in search.active_page_filters():
        label, kind, values = RECORDED[field]
        desired = getattr(search, field)
        desired_values = desired if isinstance(desired, list) else [desired]
        wanted = {f"sel-job-rec-{kind}-{values[value]}" for value in desired_values}
        anchor = next(iter(wanted))
        group = f'.condition-filter-select:has(li[ka="{anchor}"])'
        groups = [
            row
            for row in catalog
            if any(o["ka"] == anchor for o in row["options"])
        ]
        if len(groups) != 1:
            raise PageReadError(f"M3_FILTER_OPTION_UNKNOWN：{label}")
        options = groups[0]['options']
        if any(not any(option['ka'] == f'sel-job-rec-{kind}-{values[value]}' and option['text'] == value for option in options) for value in desired_values):
            raise PageReadError(f'M3_FILTER_OPTION_UNKNOWN：{label}')
        selected = {o['ka'] for o in options if o['selected'] and not o['ka'].endswith('-0')}
        if selected - wanted and field != 'company_size_filter':
            raise PageReadError(f'M3_EXISTING_FILTER_CONFLICT：{label}')
        # 多选通过差集删除残留、补选缺项，不点击已匹配项，避免切换成取消选中。
        for ka in sorted(selected - wanted) + [f'sel-job-rec-{kind}-{values[value]}' for value in desired_values if f'sel-job-rec-{kind}-{values[value]}' not in selected]:
            option = f'{group} li[ka="{ka}"]'
            await point(session, group + ' > .current-select')
            for _ in range(15):
                if await session.evaluate(f'document.querySelector({json.dumps(option)})?.checkVisibility({{checkOpacity:true,checkVisibilityCSS:true}})'):
                    break
                await asyncio.sleep(.1)
            else:
                raise PageReadError(f'M3_FILTER_NOT_VISIBLE：{label}')
            await point(session, option, click=True)
            changed = True
            selected = selected ^ {ka}  # 本次点击应只切换这个选项。
            await confirm_selection(session, group, selected, label)
        await confirm_selection(session, group, wanted, label)
        applied[field] = desired
        catalog = await filter_catalog(session)
    if changed:
        await point(session, '.search-input-box input.input,input.ipt-search')
        previous = None
        stable = 0
        for _ in range(40):
            current = await result_signature(session)
            stable = stable + 1 if current and current == previous else 0
            if current and current != before_results and stable >= 2:
                break
            previous = current
            await asyncio.sleep(.2)
        else:
            raise PageReadError('M3_FILTER_RESULTS_NOT_CONFIRMED：筛选已选择，但未确认新结果')
    return applied


async def confirm_selection(session, group, expected, label):
    """等待group菜单的active集合与expected完全相同，label用于中文错误说明。"""
    for _ in range(25):
        current = await session.evaluate(
            f"[...document.querySelectorAll({json.dumps(group + ' li.active')})].map(e=>e.getAttribute('ka')).filter(k=>!k.endsWith('-0'))"
        )
        if set(current) == expected:
            return
        await asyncio.sleep(.1)
    raise PageReadError(f'M3_FILTER_NOT_CONFIRMED：{label}')


async def result_signature(session):
    """仅取当前岗位路径集合，用于证明筛选后结果已刷新，不保存JD。"""
    return await session.evaluate("[...document.querySelectorAll('.job-card-box a.job-name')].map(e=>e.getAttribute('href'))")
