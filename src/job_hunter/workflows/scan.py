"""正式有界流程：默认只评审，显式apply入口注入已授权动作。"""

from collections import Counter
from time import perf_counter

from job_hunter.browser.boss_adapter import BossAdapter
from job_hunter.browser.filters import apply_filters, validate_filters
from job_hunter.evaluation.rules import ENGINE, Decision, Evidence, evaluate
from job_hunter.extraction.boss_details import (
    DETAIL_STATE,
    current_cards,
    load_more,
    open_detail,
)
from job_hunter.extraction.identity import job_key


async def scan_in_session(session, preferences, candidate, emit, keyword=None, city=None, prior_decisions=None, evaluator=None, excluded_ids=None, action_handler=None, excluded_keys=None):
    """在同一个浏览器连接中完成自动评审，返回本轮汇总。

    参数：
        session：已建立的受守卫保护的浏览器会话，异常后不自动重连。
        preferences：页面检索条件、排除规则、匹配策略及运行数量上限。
        candidate：个人画像，仅在内存中参与判断，不整份写入运行日志。
        emit：事件回调，负责显示结果并保存最小日志。
        keyword/city：本轮覆盖的关键词/城市；为None时使用配置默认值。
        prior_decisions：显式续跑时已校验版本的旧判断，避免重复读取。
        evaluator：已初始化的语义评审器；为None时显式使用本地规则兼容路径。
        excluded_ids：本轮跳过的历史岗位ID，不占新岗位名额。
        action_handler：仅显式apply注入的动作处理器；默认None时不操作收藏/沟通。
        excluded_keys：历史公司＋岗位名称指纹；不保存名单原文，同名职位不重复投递。
    """
    # excluded_ids只跳过旧岗位，不占用本轮新岗位名额；动作回调仅由显式apply入口提供。
    excluded_ids = set(excluded_ids or [])
    known_keys = set(excluded_keys or [])  # 跨ID的公司/名称等价岗位；本轮完成后加入。
    validate_filters(preferences.search)
    keyword = keyword or preferences.search.keywords[0]
    city = city or preferences.search.cities[0]
    await session.bring_to_front()
    adapter = BossAdapter(session)
    await adapter.search(keyword, city)
    applied = await apply_filters(session, preferences.search)
    emit({'event': 'filters_confirmed', 'keyword': keyword, 'city': city, 'filters': applied})
    queue = await current_cards(session)  # 当前批次的岗位卡片，正文尚未读取。
    initial = len(queue)
    emit({'event': 'batch_loaded', 'loaded': initial, 'limit': preferences.execution.max_jobs_per_run})
    if preferences.execution.allow_list_scroll:
        queue = queue[:15]
    prior_decisions = prior_decisions or []
    seen = {event['job_id'] for event in prior_decisions}  # 按岗位ID去重，不按标题/公司去重。
    counts = Counter(event['status'] for event in prior_decisions)  # 每个岗位只计一次分类。
    for event in prior_decisions:
        emit({**event, 'resumed': True})
    scrolls = 0  # 当前连接已执行的列表扩展次数；不包括翻页按钮操作。
    action_counts = Counter()  # 已完成岗位的动作次数；异常时保留逐岗事件。
    stop_reason = 'job_limit'
    maximum = preferences.execution.max_jobs_per_run  # 包含续用结果在内的本轮岗位数量上限。
    while len(seen) < maximum:
        pending = [c for c in queue if c.job_id not in seen and c.job_id not in excluded_ids
                   and job_key(c.company,c.title) not in known_keys]
        for card in pending:
            identity_key = job_key(card.company,card.title)
            if identity_key in known_keys:
                continue
            action_stop = getattr(action_handler, 'stop_reason', lambda: None)()
            if action_stop:
                stop_reason = action_stop
                break
            if len(seen) >= maximum:
                break
            job_started = perf_counter()  # 单岗耗时从打开卡片开始，不混入开发调试时间。
            detail = await open_detail(session, card)
            read_seconds = perf_counter() - job_started
            emit({'event': 'detail_read', 'ordinal': len(seen)+1, 'company': card.company,
                  'title': detail.detail_title or card.title, 'reading': detail.reading,
                  'read_seconds': round(read_seconds, 3)})
            before = await session.evaluate(DETAIL_STATE) if action_handler is not None else None
            model_started = perf_counter()
            model_called = evaluator is not None and detail.reading.get('status') == 'DOM_COMPLETE'
            if detail.reading.get('status') != 'DOM_COMPLETE':
                decision = Decision('REVIEW', '正文完整性未确认，不进行岗位匹配判断',
                                    [Evidence('jd_completeness', '读取完整性', '；'.join(detail.reading.get('issues', ['缺少完整性记录'])))],
                                    notes=['未调用模型，不额外补查岗位'], engine=evaluator.engine if evaluator else ENGINE)
            else:
                emit({'event': 'evaluating', 'ordinal': len(seen)+1, 'company': card.company, 'title': detail.detail_title or card.title})
                decision = await evaluator.evaluate(detail, preferences, candidate) if evaluator is not None else evaluate(detail, preferences, candidate)
            model_seconds = perf_counter() - model_started if model_called else 0
            if hasattr(session, 'require_alive'):
                session.require_alive()  # 模型等待期间若浏览器失联，不再记录为可继续操作的会话。
            seen.add(card.job_id)
            known_keys.add(identity_key)
            counts[decision.status] += 1
            emit({
                'event': 'decision', 'ordinal': len(seen),
                'batch': (len(seen)-1)//15+1,
                'source': 'initial_list' if scrolls == 0 else 'scroll_loaded',
                'reading': detail.reading, 'model_called': model_called,
                'timing': {'read_seconds': round(read_seconds, 3), 'model_seconds': round(model_seconds, 3),
                           'total_seconds': round(perf_counter()-job_started, 3)},
                **decision.minimal_record(detail),
                'identity_key':identity_key,
            })
            if action_handler is not None:
                action_started = perf_counter()
                actions = {'favorite':'not_attempted','communicate':'not_attempted'}
                try:
                    actions = await action_handler(session, detail, before, decision)
                    action_counts.update({key:actions.get(key,0) for key in ('favorite_attempts','communicate_attempts')})
                except BaseException as error:
                    emit({'event':'job_result','ordinal':len(seen),**decision.minimal_record(detail),'identity_key':identity_key,
                          'salary':detail.salary_text,'actions':{**getattr(action_handler,'last_actions',actions),'status':'stopped','reason':str(error)},
                          'timing':{'read_seconds':round(read_seconds,3),'model_seconds':round(model_seconds,3),
                                    'action_seconds':round(perf_counter()-action_started,3),
                                    'total_seconds':round(perf_counter()-job_started,3)}})
                    raise
                emit({'event':'job_result','ordinal':len(seen),**decision.minimal_record(detail),'identity_key':identity_key,
                      'salary':detail.salary_text,'actions':actions,
                      'timing':{'read_seconds':round(read_seconds,3),'model_seconds':round(model_seconds,3),
                                'action_seconds':round(perf_counter()-action_started,3),
                                'total_seconds':round(perf_counter()-job_started,3)}})
            del detail
        action_stop = getattr(action_handler, 'stop_reason', lambda: None)()
        if action_stop:
            stop_reason = action_stop
            break
        if len(seen) >= maximum:
            break
        if not preferences.execution.allow_list_scroll or scrolls >= preferences.execution.max_scroll_rounds:
            stop_reason = 'scroll_limit' if preferences.execution.allow_list_scroll else 'initial_list_complete'
            break
        scrolls += 1
        emit({'event': 'scroll', 'round': scrolls, 'processed': len(seen)})
        queue = await load_more(session, seen | excluded_ids)
        if not queue:
            stop_reason = 'no_new_jobs_observed'
            break
    summary = {
        'event': 'summary', 'engine': evaluator.engine if evaluator is not None else ENGINE, 'initial_loaded': initial,
        'processed': len(seen), 'scroll_rounds': scrolls,
        'resumed': len(prior_decisions),
        'counts': dict(counts), 'stop_reason': stop_reason,
        'favorite_attempts': action_counts['favorite_attempts'], 'communicate_attempts': action_counts['communicate_attempts'],
    }
    emit(summary)
    return summary
