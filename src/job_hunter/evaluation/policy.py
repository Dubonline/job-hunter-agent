"""已确认的英语与月薪准入策略；不依赖模型自行记住边界。"""
import re
import unicodedata

from job_hunter.evaluation.rules import Evidence


def salary_range(text):
    """解析明确的K月薪区间；私有字体、面议、年薪等返回未知，不猜数字。"""
    value = unicodedata.normalize('NFKC', text).strip()
    match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*[-–—~至]\s*(\d+(?:\.\d+)?)\s*[kK](?:\s*[·•]\s*\d+薪)?', value)
    if not match:
        return None
    lower, upper = map(float, match.groups())
    return (lower, upper) if 0 < lower <= upper else None


def english_requirement(detail):
    """返回涉及英语沟通/工作语言的非加分原文；基础阅读和优先项不阻断。"""
    lines = (detail.detail_title or detail.card.title) + '\n' + detail.description
    for line in lines.splitlines():
        if re.search(r'优先|加分|无需|不要求|非必[须需]', line):
            continue
        if re.search(r'(?:英语|英文).{0,16}(?:口语|流利|沟通|交流|工作语言)|工作语言.{0,8}(?:英语|英文)|english.{0,15}(?:fluent|speaking|communication|working language)', line, re.I):
            return line[:240]
    return ''


def apply_policy(decision, detail, preferences, candidate):
    """仅收紧准入，不将不符合/待确认提升为可投；期望单位为千元/月。"""
    if preferences.matching.english_requires_high_fit:
        quote = english_requirement(detail)
        if quote:
            decision.evidence.append(Evidence('english_conditional', '岗位英语要求', quote))
            if decision.status != 'HIGH_FIT':
                decision.status, decision.reason = 'SKIP', '无法英语口语沟通，且岗位未达到职责高度契合'
            else:
                decision.notes.append('英语口语不符；按用户授权仅因职责高度契合带风险申请，不宣称英语达标')
    if preferences.matching.salary_requires_overlap:
        advertised = salary_range(detail.salary_text)
        lower = candidate.expected_salary_min if candidate.expected_salary_min is not None else preferences.salary.target_min
        upper = candidate.expected_salary_max if candidate.expected_salary_max is not None else preferences.salary.target_max
        if advertised is None:
            if decision.status in {'HIGH_FIT', 'GENERAL'}:
                decision.status, decision.reason = 'REVIEW', '招聘月薪无法可靠读取，不允许自动沟通'
        elif advertised[0] > upper or advertised[1] < lower:
            decision.status, decision.reason = 'SKIP', f"招聘月薪与期望{lower:g}–{upper:g}K无交集"
            decision.evidence.append(Evidence('salary_overlap', '招聘薪资', detail.salary_text))
    return decision
