"""项目场景/职责按最近实践时间加权，技能与技术栈累积；不从时间跨度推工作年限。"""

import re
import unicodedata
from datetime import date, datetime
from zoneinfo import ZoneInfo

from job_hunter.models.preferences import Candidate

VERSION = 'project_recency_v1'
PERIOD = re.compile(r'^(\d{4})[./-](\d{1,2})\s*[-–—~至]\s*(?:(\d{4})[./-](\d{1,2})|(至今|现在|present))$', re.I)


def month_number(value: date) -> int:
    return value.year * 12 + value.month - 1


def project_weights(candidate: Candidate, as_of: date | None = None) -> list[dict]:
    """以结束月/至今计算最近实践；时间跨度含首尾月，不相加为总工作年限。

    重点项目权重为0.5+0.5×2^(-距结束月/半期)；重点之前为0.25×
    2^(-距重点起点/半期)。未知/矛盾日期不当作近期，不删除技能事实。
    """
    today = as_of or datetime.now(ZoneInfo('Asia/Shanghai')).date()
    now = month_number(today)
    policy = candidate.project_weighting
    focus = now - policy.focus_window_months
    if policy.focus_since:
        year, month = map(int, policy.focus_since.split('-'))
        focus = year * 12 + month - 1
    result = []
    for index, project in enumerate(candidate.projects):
        row = {'project_index': index, 'as_of': today.isoformat(), 'date_status': 'UNKNOWN',
               'focus': False, 'scenario_weight': 0.0, 'duration_months': None,
               'skill_retained': True, 'reason': '日期未知或格式不足，不猜测最近实践'}
        match = PERIOD.fullmatch(unicodedata.normalize('NFKC', project.period or '').strip())
        if match:
            year, month = int(match[1]), int(match[2])
            start = year * 12 + month - 1
            end_month = int(match[4]) if match[4] else today.month
            end = int(match[3]) * 12 + end_month - 1 if match[3] else now
            if 1 <= month <= 12 and 1 <= end_month <= 12 and start <= end <= now:
                focused = end >= focus
                age = now - end
                weight = (.5 + .5 * 2 ** (-age / policy.half_life_months) if focused else
                          .25 * 2 ** (-(focus - end) / policy.half_life_months))
                row.update(date_status='KNOWN', focus=focused, scenario_weight=round(weight, 4),
                           duration_months=end-start+1, months_since_last_practice=age,
                           reason='重点窗口内的场景/职责证据' if focused else '较早场景，只作辅助证据')
        result.append(row)
    return result


def cumulative_technologies(candidate: Candidate) -> list[str]:
    """汇总明确列出的技术，旧项目也保留；不从描述关键词推断新技能。"""
    seen, result = set(), []
    for value in candidate.skills.technologies + [item for project in candidate.projects for item in project.technologies]:
        key = re.sub(r'\s+', '', unicodedata.normalize('NFKC', value)).casefold()
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def recent_support(matches: list[dict], weights: list[dict], threshold=1.5) -> bool:
    """高契合须有同一近期项目的组合实质证据，不能由多段旧项目或技术名堆成。"""
    for row in weights:
        if not row['focus'] or row['scenario_weight'] < .5:
            continue
        selected = [item for item in matches if item['project_index'] == row['project_index']
                    and item['section'] in {'岗位职责', 'JD'} and item['kind'] != 'generic_technology']
        business = any(item['kind'] == 'business' for item in selected)
        responsibilities = {item['profile_item_index'] for item in selected if item['kind'] == 'responsibility'}
        passages = {item['passage_id'] for item in selected}
        score = row['scenario_weight'] * (int(business) + len(responsibilities))
        if score >= threshold and len(passages) >= 2 and ((business and responsibilities) or len(responsibilities) >= 2):
            return True
    return False
