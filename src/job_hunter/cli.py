import asyncio
import shlex
import sys
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import typer
from rich.console import Console

from job_hunter.browser.adapter_base import LoginStatus, PageReadError
from job_hunter.browser.attached import discover_endpoint, is_boss_url
from job_hunter.browser.boss_adapter import BossAdapter
from job_hunter.browser.filters import apply_filters, validate_filters
from job_hunter.browser.guard import ConnectionGuard
from job_hunter.browser.session import CHROME_PATH, AgentSession
from job_hunter.evaluation.model import ModelEvaluator
from job_hunter.evaluation.rules import ENGINE
from job_hunter.settings import (
    ConfigurationError,
    load_settings,
    load_user_config,
    profile_path,
    selected_profile,
)
from job_hunter.storage.run_log import RunLog
from job_hunter.workflows.scan import scan_in_session

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
console = Console()
errors = Console(stderr=True)
Endpoint = Annotated[str | None, typer.Option(envvar="BROWSER_CDP_ENDPOINT")]
PageIndex = Annotated[int | None, typer.Option(min=1)]


@app.callback()
def select_profile(
    context: typer.Context,
    profile: Annotated[str | None, typer.Option(envvar='JOB_AGENT_PROFILE', help='选择命名用户配置；省略则沿用原config配置')] = None,
):
    """只选择本次命令的配置，不自动切换浏览器账号、启动评审或投递。"""
    if profile is not None:
        try:
            profile_path(profile)
        except ConfigurationError as error:
            errors.print(str(error), markup=False)
            raise typer.Exit(1) from None
    token = selected_profile.set(profile)
    context.call_on_close(lambda: selected_profile.reset(token))


@app.command('configure')
def configure_command(
    from_json: Annotated[Path | None, typer.Option(help='Agent提供的结构化草稿；默认仅预览')] = None,
    save: Annotated[bool, typer.Option(help='校验并展示摘要后保存草稿；不启动业务')] = False,
    overwrite: Annotated[bool, typer.Option(help='显式更新同名命名配置，不修改原config用户')] = False,
    show: Annotated[bool, typer.Option(help='只读显示所选命名配置摘要')] = False,
):
    """中文新用户配置向导；不编辑YAML、不连接浏览器或模型、不投递。"""
    from job_hunter.onboarding import configure

    try:
        configure(console, from_json, save, overwrite, show)
    except ConfigurationError as error:
        errors.print(str(error), markup=False)
        raise typer.Exit(1) from None


@app.command('resume-import')
def resume_import_command(file: Annotated[Path, typer.Argument(help='本机.docx或文本PDF路径')]):
    """本地提取简历，生成有原文依据的待确认画像草稿，不覆盖正式画像。"""
    from job_hunter.resume_import import make_draft, write_draft
    from job_hunter.resume_review import preview

    try:
        name = selected_profile.get()
        configured = profile_path(name).is_file() if name else Path('config/preferences.yaml').is_file()
        candidate = load_user_config()[1] if configured else None
        draft = make_draft(file, candidate, name)
        path, review = write_draft(draft)
        console.print(preview(draft, candidate), markup=False)
        console.print(f'本地提取：{draft.source.pages or "DOCX按段落定位"}页/段落模式，{len(draft.source.lines)}行，候选{len(draft.proposals)}组。', markup=False)
        console.print(f'草稿：{path}\n原文依据：{review}\n正式画像未修改。', markup=False)
    except (ConfigurationError, OSError) as error:
        errors.print(str(error) if isinstance(error, ConfigurationError) else '草稿保存失败，请核对本机文件权限', markup=False)
        raise typer.Exit(1) from None


@app.command('resume-intake')
def resume_intake_command(file: Annotated[Path | None, typer.Argument(help='客户提供的本机PDF或.docx；省略则先询问')] = None):
    """客户简历入口：先请求文件，本地解析，再用配置的DeepSeek生成有据待确认画像。"""
    if file is None:
        supplied = typer.prompt('请提供PDF或Word(.docx)简历（本机路径或拖入文件）').strip().strip('\"\'')
        if not supplied:
            raise typer.Exit(1)
        file = Path(supplied)
    run(_resume_intake(file))


async def _resume_intake(file: Path):
    """不启动BOSS或投递；模型失败立即报错，不把纯本地提取冒充DeepSeek画像。"""
    from job_hunter.resume_import import write_draft
    from job_hunter.resume_review import preview
    from job_hunter.resume_semantic import generate_profile

    name = selected_profile.get()
    configured = profile_path(name).is_file() if name else Path('config/preferences.yaml').is_file()
    candidate = load_user_config()[1] if configured else None
    settings = load_settings()
    console.print('本机解析PDF/Word；仅必要脱敏文字与证据编号送至配置的DeepSeek；不发送图片。', markup=False)
    draft = await generate_profile(file, candidate, name, settings)
    try:
        path, review = write_draft(draft)
    except OSError:
        raise ConfigurationError('画像草稿保存失败，请核对本机文件权限') from None
    console.print(preview(draft, candidate), markup=False)
    console.print(f'DeepSeek有据画像已生成，仍待用户确认。\n草稿：{path}\n原文与权重：{review}', markup=False)


@app.command('resume-review')
def resume_review_command(
    draft: Annotated[Path, typer.Argument(help='resume-import生成的draft.json')],
    show: Annotated[bool, typer.Option(help='只读显示摘要，不保存')] = False,
    accept: Annotated[str | None, typer.Option(help='已由用户确认的具体f编号，逗号分隔；默认逐项交互')] = None,
    save: Annotated[bool, typer.Option(help='仅保存明确选择且已确认的字段，不启动业务')] = False,
):
    """原文核对、用户更正及确认保存；不把草稿直接视为用户事实。"""
    from job_hunter.resume_review import review_command

    try:
        review_command(draft, console, show, accept, save)
    except (ConfigurationError, OSError) as error:
        errors.print(str(error) if isinstance(error, ConfigurationError) else '画像保存失败，请核对本机文件权限', markup=False)
        raise typer.Exit(1) from None


def run(awaitable):
    try:
        return asyncio.run(awaitable)
    except (ConfigurationError, PageReadError) as error:
        errors.print(str(error), markup=False)
        raise typer.Exit(1) from None
    except KeyboardInterrupt:
        console.print("会话已停止；不关闭用户 Chrome")


def preparation():
    console.print(
        "请提前打开 Chrome、登录 BOSS 并开启远程调试；macOS Chrome点击助手默认自动处理远程调试允许弹框。"
    )


@app.command()
def doctor():
    """检查配置与修复版安装，不连接 Chrome，不创建 JD 数据库。"""
    try:
        settings = load_settings()
        load_user_config()
        if not settings.agent_browser_binary.is_file():
            raise ConfigurationError(
                "修复版 agent-browser 尚未安装，见 docs/INSTALL.md"
            )
        console.print(f"Python {sys.version.split()[0]}；配置有效；修复版存在")
        console.print(
            "Chrome 已安装" if CHROME_PATH.exists() else "Chrome 未在默认位置找到"
        )
        missing = settings.missing_model_settings()
        if missing:
            console.print(f"模型配置待补充：{', '.join(missing)}（不影响 M2）")
    except ConfigurationError as error:
        errors.print(str(error), markup=False)
        raise typer.Exit(1) from None


@app.command()
def login():
    """显示人工登录说明，不启动、登录或连接浏览器。"""
    preparation()
    console.print(
        "登录由你在官方页面完成；智投只识别状态。随后运行 job-agent attach 或 session。"
    )


@app.command()
def attach(
    endpoint: Endpoint = None,
    page: PageIndex = None,
    list_pages: Annotated[bool, typer.Option()] = False,
):
    """一次连接识别登录态后退出；不关闭用户页面。"""
    preparation()
    run(_attach(endpoint, page, list_pages))


async def _attach(endpoint, index, listing):
    settings = load_settings()
    endpoint = endpoint or discover_endpoint()
    if listing:
        guard = ConnectionGuard(endpoint, 1)
        try:
            await guard.start()
            for n, target in enumerate(
                (
                    t
                    for t in guard.targets
                    if t.get("type") == "page" and is_boss_url(t.get("url", ""))
                ),
                1,
            ):
                console.print(f"{n}: {urlsplit(target['url']).path}", markup=False)
        finally:
            await guard.close()
        return
    async with AgentSession(endpoint, index, settings.agent_browser_binary) as active:
        console.print(f"登录状态：{await BossAdapter(active).check_login()}")
    console.print("已断开客户端，用户 Chrome 保持打开")


async def _read_command():
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    fd = sys.stdin.fileno()

    def ready():
        loop.remove_reader(fd)
        if not future.done():
            future.set_result(sys.stdin.readline())

    loop.add_reader(fd, ready)
    try:
        return await future
    finally:
        loop.remove_reader(fd)


@app.command()
def session(
    endpoint: Endpoint = None,
    page: PageIndex = None,
    observe: Annotated[bool, typer.Option(help="只识别状态，不执行搜索")] = False,
):
    """同一连接内连续检查状态或搜索；异常终止，不自动重连。"""
    preparation()
    run(_session(endpoint, page, observe))


async def _session(endpoint, index, observe):
    settings = load_settings()
    async with AgentSession(
        endpoint or discover_endpoint(), index, settings.agent_browser_binary
    ) as active:
        adapter = BossAdapter(active)
        status = await adapter.check_login()
        console.print(f"登录状态：{status}")
        if status != LoginStatus.SIGNED_IN:
            raise PageReadError("BOSS_LOGIN_NOT_READY：请人工处理后重新启动")
        console.print(
            '命令：status | search "大模型应用开发" 上海 | quit', markup=False
        )
        while True:
            console.print("智投> ", end="")
            reading = asyncio.create_task(_read_command())
            stopping = asyncio.create_task(active.guard.stopped.wait())
            try:
                done, _ = await asyncio.wait(
                    {reading, stopping}, return_when=asyncio.FIRST_COMPLETED
                )
                if stopping in done:
                    active.require_alive()
                line = reading.result()
            finally:
                reading.cancel()
                stopping.cancel()
                await asyncio.gather(reading, stopping, return_exceptions=True)
            if not line:
                break
            try:
                args = shlex.split(line)
            except ValueError:
                console.print("命令引号不匹配")
                continue
            if args == ["quit"]:
                break
            if args == ["status"]:
                await adapter.require_signed_in()
                console.print(f"登录状态：{LoginStatus.SIGNED_IN}")
            elif len(args) == 3 and args[0] == "search" and not observe:
                await _search_in_session(adapter, args[1], args[2])
            else:
                console.print("命令不识别或当前只读模式不允许")
    console.print("会话结束；未关闭用户 Chrome")


async def _search_in_session(adapter, keyword, city):
    preferences, _ = load_user_config()
    validate_filters(preferences.search)
    result = await adapter.search(keyword, city)
    console.print(f"关键词：{result['query']}；城市：{city}")
    for n, title in enumerate(result["titles"], 1):
        console.print(f"{n}. {title}", markup=False)
    applied = await apply_filters(adapter.session, preferences.search)
    console.print(f"已核对页面条件：{applied}", markup=False)
    console.print("仅确认结果与筛选；未执行立即沟通")


@app.command()
def search(
    endpoint: Endpoint = None,
    page: PageIndex = None,
    keyword: Annotated[str | None, typer.Option()] = None,
    city: Annotated[str | None, typer.Option()] = None,
):
    """从配置读取默认检索条件，在同一连接中提交搜索。"""
    preparation()
    run(_search(endpoint, page, keyword, city))


async def _search(endpoint, index, keyword, city):
    settings = load_settings()
    preferences, _ = load_user_config()
    validate_filters(preferences.search)
    keyword = keyword or preferences.search.keywords[0]
    city = city or preferences.search.cities[0]
    async with AgentSession(
        endpoint or discover_endpoint(), index, settings.agent_browser_binary
    ) as active:
        await _search_in_session(BossAdapter(active), keyword, city)


@app.command("search-plan")
def search_plan():
    """显示页面检索条件，不连接浏览器，不输出个人画像。"""
    try:
        preferences, _ = load_user_config()
        console.print(preferences.search.model_dump(exclude_none=True), markup=False)
        console.print("检索条件作用于 BOSS 页面；JD 硬规则与个人画像用于后续逐岗判断。")
    except ConfigurationError as error:
        errors.print(str(error), markup=False)
        raise typer.Exit(1) from None


@app.command()
def scan(
    endpoint: Endpoint = None,
    page: PageIndex = None,
    limit: Annotated[int | None, typer.Option(min=1, max=200, help='本轮最多判断的去重岗位数，包含续用结果')] = None,
    scroll_rounds: Annotated[int | None, typer.Option(min=0, max=200, help='最多扩展列表的次数；0表示不扩展')] = None,
    resume_run: Annotated[str | None, typer.Option(help='显式沿用同条件、画像和规则版本的运行判断')] = None,
    engine: Annotated[Literal['model', 'rules'], typer.Option(help='model调用.env模型；rules显式使用本地规则')] = 'model',
):
    """正式自动评审：默认模型判断，支持显式规则版；不收藏、不沟通。"""
    preparation()
    console.print('引擎：.env语义模型，逐条核验原文证据。' if engine == 'model' else '引擎：本地规则初筛。')
    run(_scan(endpoint, page, limit, scroll_rounds, resume_run, engine))


async def _scan(endpoint, index, limit, scroll_rounds, resume_run=None, engine_mode='model'):
    """加载配置并启动正式扫描，异常时记录停止并释放自有连接。

    endpoint为本机Chrome调试端点；index为BOSS页面序号；limit和scroll_rounds
    覆盖本轮数量/滚动上限；resume_run为显式续跑的旧运行ID，为None时新建判断。
    engine_mode决定语义模型或显式规则版；模型失败不自动切回规则判断。
    """
    settings = load_settings()
    preferences, candidate = load_user_config()
    if limit is not None:
        preferences.execution.max_jobs_per_run = limit
    if scroll_rounds is not None:
        preferences.execution.max_scroll_rounds = scroll_rounds
        preferences.execution.allow_list_scroll = scroll_rounds > 0
    validate_filters(preferences.search)
    if engine_mode == 'rules':
        from job_hunter.onboarding import PRESETS

        if any(rule.enabled and rule.id not in PRESETS for rule in preferences.hard_exclusions):
            raise ConfigurationError('自定义硬排除需要model引擎；本地rules不会静默忽略')
    evaluator = ModelEvaluator(settings, preferences, candidate) if engine_mode == 'model' else None
    engine_id = evaluator.engine if evaluator is not None else ENGINE
    log = RunLog(preferences, candidate)
    prior = log.resume(resume_run, engine_id) if resume_run else []
    if evaluator is not None:
        console.print({'event': 'model_probe_passed', **await evaluator.probe()}, markup=False)
    log.append({'event': 'run_start', 'engine': engine_id, 'mode': 'review_only', 'resumed_from': resume_run})
    console.print(f'最小运行记录：{log.path}', markup=False)
    labels = {'HIGH_FIT': '职责高度契合', 'GENERAL': '通用匹配', 'SKIP': '不符合', 'REVIEW': '待确认'}

    def emit(event):
        log.append(event)
        if event['event'] == 'decision':
            if event.get('resumed'):
                return
            console.print(
                f"{event['ordinal']}. [{labels[event['status']]}] {event['company']} / {event['title']}：{event['reason']}",
                markup=False,
            )
            for evidence in event['evidence']:
                console.print(f"  依据({evidence['section']})：{evidence['quote']}", markup=False)
            if event['matches']:
                console.print('  对应：' + '、'.join(event['matches']), markup=False)
            if event.get('timing'):
                console.print(f"  耗时：读取{event['timing']['read_seconds']}秒；模型{event['timing']['model_seconds']}秒；合计{event['timing']['total_seconds']}秒", markup=False)
        elif event['event'] == 'detail_read':
            reading = event['reading']
            console.print(f"{event['ordinal']}. 已读取 {event['company']} / {event['title']}；{reading.get('char_count', 0)}字；{reading.get('status_text', '完整性未知')}", markup=False)
            console.print(f"  来源：{reading.get('source')}；屏幕外正文：{reading.get('offscreen_text_before_probe')}；末尾滚动前后相同：{reading.get('same_text_after_end_scroll')}", markup=False)
        else:
            console.print(event, markup=False)

    try:
        async with AgentSession(
            endpoint or discover_endpoint(), index, settings.agent_browser_binary
        ) as active:
            if prior:
                console.print(f'沿用同版本已判断岗位{len(prior)}个，只读取剩余新身份。')
            await scan_in_session(active, preferences, candidate, emit, prior_decisions=prior, evaluator=evaluator)
    except BaseException as error:
        log.append({'event': 'run_end', 'status': 'stopped', 'error_type': type(error).__name__, 'reason': str(error), 'actions': 'none'})
        raise
    log.append({'event': 'run_end', 'status': 'completed', 'actions': 'none'})
    console.print('扫描结束；用户Chrome保持打开；未收藏、未沟通。')


@app.command('communicate-one')
def communicate_one_command(
    job_id: Annotated[str, typer.Option(help='明确授权的唯一岗位ID；不重扫或选择替代')],
    endpoint: Endpoint = None,
    page: PageIndex = None,
):
    """重新评审一个指定岗位并最多沟通一次；成功后留在此页，不收藏。"""
    preparation()
    run(_communicate_one(endpoint, page, job_id))


async def _communicate_one(endpoint, index, job_id):
    """冻结配置后使用同一连接完成复核/单次动作；日志区别已发送和未知。"""
    from job_hunter.workflows.communicate import communicate_one

    settings = load_settings()
    preferences, candidate = load_user_config()
    evaluator = ModelEvaluator(settings, preferences, candidate)
    log = RunLog(preferences, candidate)

    def emit(event):
        log.append(event)
        console.print(event, markup=False)

    emit({'event': 'run_start', 'mode': 'communicate_one', 'engine': evaluator.engine, 'job_id': job_id})
    console.print(f'单岗运行记录：{log.path}', markup=False)
    try:
        async with AgentSession(endpoint or discover_endpoint(), index, settings.agent_browser_binary) as active:
            await communicate_one(active, job_id, preferences, candidate, evaluator, emit)
    except BaseException as error:
        emit({'event': 'run_end', 'status': 'stopped', 'error_type': type(error).__name__, 'reason': str(error)})
        raise
    emit({'event': 'run_end', 'status': 'completed', 'favorite_attempts': 0, 'communicate_attempts': 1})
    console.print('单岗运行结束；用户Chrome保持打开。')


@app.command('communicate-current-one')
def communicate_current_one_command(
    endpoint: Endpoint = None, page: PageIndex = None,
    candidate_id: Annotated[list[str] | None, typer.Option(help='仅复核当前已加载列表内的指定候选ID，可重复传入')] = None,
):
    """只读当前筛选结果，最多评审5岗并沟通一个；不重搜、不收藏。"""
    preparation()
    run(_communicate_current_one(endpoint, page, candidate_id))


async def _communicate_current_one(endpoint, index, candidate_ids=None):
    """按本次授权使用同一连接挑选一个候选，所有实际结果同步记录。"""
    from job_hunter.workflows.communicate import select_current_one

    settings = load_settings()
    preferences, candidate = load_user_config()
    evaluator = ModelEvaluator(settings, preferences, candidate)
    log = RunLog(preferences, candidate)

    def emit(event):
        log.append(event)
        console.print(event, markup=False)

    emit({'event':'run_start','mode':'communicate_current_one','engine':evaluator.engine,'limit':5})
    console.print(f'本次记录：{log.path}', markup=False)
    try:
        async with AgentSession(endpoint or discover_endpoint(), index, settings.agent_browser_binary) as active:
            summary = await select_current_one(active, preferences, candidate, evaluator, emit, candidate_ids=candidate_ids)
    except BaseException as error:
        emit({'event':'run_end','status':'stopped','error_type':type(error).__name__,'reason':str(error)})
        raise
    emit({'event':'run_end','status':'completed',**summary})


@app.command('detail-response-check')
def detail_response_check(endpoint: Endpoint = None, page: PageIndex = None):
    """只读比较一个岗位的页面自然详情响应、DOM及薪资；不调用模型或投递。"""
    run(_detail_response_check(endpoint, page))


async def _detail_response_check(endpoint, index):
    """同一连接完成一次诊断，仅保存无JD、无Cookie的比较摘要。"""
    import json
    from datetime import datetime
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from job_hunter.extraction.detail_response import probe_detail_response

    settings = load_settings()
    console.print('只读诊断：当前列表1个岗位；无模型、收藏、沟通；不重连补查。')
    async with AgentSession(endpoint or discover_endpoint(), index, settings.agent_browser_binary) as active:
        await active.bring_to_front()
        summary = await probe_detail_response(active)
    stamp = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S-%f')
    directory = Path(__file__).resolve().parents[2] / '.local/diagnostics/detail-response'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{stamp}.json'
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    console.print(summary, markup=False)
    console.print(f'诊断摘要：{path}；连接已退出，Chrome保留。', markup=False)


@app.command('salary-check')
def salary_check(job_id: Annotated[str, typer.Option(help='当前已加载的唯一岗位ID，只验证读取')], endpoint: Endpoint = None, page: PageIndex = None):
    """只验证指定岗位的JD及薪资读取，不调用模型、不收藏、不沟通。"""
    run(_salary_check(endpoint, page, job_id))


async def _salary_check(endpoint, index, job_id):
    """同一连接读取一个现有岗位，展示薪资来源和交叉核对状态。"""
    from job_hunter.extraction.boss_details import current_cards, open_detail

    settings = load_settings()
    async with AgentSession(endpoint or discover_endpoint(), index, settings.agent_browser_binary) as active:
        await active.bring_to_front()
        cards = [c for c in await current_cards(active) if c.job_id == job_id]
        if len(cards) != 1:
            raise PageReadError('SALARY_CHECK_TARGET_NOT_PRESENT')
        detail = await open_detail(active, cards[0])
        console.print({'job_id':job_id,'company':detail.card.company,'title':detail.detail_title,
                       'salary':detail.salary_text,'salary_reading':detail.reading.get('salary'),
                       'actions':'只读，无模型、收藏或沟通'},markup=False)


@app.command('dismiss-sent-dialog')
def dismiss_sent_dialog_command(
    job_id: Annotated[str, typer.Option(help='原沟通尝试岗位，只关闭其当前成功弹框')],
    endpoint: Endpoint = None, page: PageIndex = None,
):
    """识别当前已发送弹框并点击留在此页；不重新发送、不进入聊天。"""
    run(_dismiss_sent_dialog(endpoint,page,job_id))


async def _dismiss_sent_dialog(endpoint,index,job_id):
    """同步记录已发送事实及留页结果，保留原未知日志，禁止再次投递。"""
    from job_hunter.workflows.communicate import dismiss_sent_dialog, previous_attempt

    if not previous_attempt(job_id):
        raise PageReadError('DIALOG_PRIOR_ATTEMPT_MISSING')
    settings=load_settings()
    preferences,candidate=load_user_config()
    log=RunLog(preferences,candidate)

    def emit(event):
        log.append(event)
        console.print(event,markup=False)

    emit({'event':'run_start','mode':'dismiss_sent_dialog','job_id':job_id})
    try:
        async with AgentSession(endpoint or discover_endpoint(),index,settings.agent_browser_binary) as active:
            await dismiss_sent_dialog(active,job_id,emit)
    except BaseException as error:
        emit({'event':'run_end','status':'stopped','reason':str(error)})
        raise
    emit({'event':'run_end','status':'completed','communicate_attempts':0})


@app.command('apply')
def apply_command(
    endpoint: Endpoint = None, page: PageIndex = None,
    count: Annotated[int | None, typer.Option(min=0,max=150,help='本次目标沟通人数，默认配置50；不得超过今日剩余额度')] = None,
    limit: Annotated[int | None,typer.Option(min=1,max=200,help='最多评审的新岗位数，默认200；与沟通人数独立，仍受滚动上限约束')] = None,
    record_lists: Annotated[bool | None,typer.Option('--record-lists/--no-record-lists',help='是否记录成功收藏/沟通的公司及岗位名单，默认关闭')] = None,
):
    """按本次目标及每日150硬上限投递，结束自动更新今日简报。"""
    run(_apply(endpoint,page,count,limit,record_lists))


@app.command('brief')
def daily_brief():
    """只读取本机记录，生成今日简报；不连接浏览器或模型。"""
    from job_hunter.storage.daily import write_brief
    try:
        text,path=write_brief()
        console.print(text,markup=False)
        console.print(f'简报：{path}',markup=False)
    except ConfigurationError as error:
        errors.print(str(error),markup=False)
        raise typer.Exit(1) from None


async def _apply(endpoint,index,count=None,limit=None,record_lists=None):
    """先校验并展示关键设置，再建模/连接；返回None，异常时停止并更新简报。

    count为成功沟通人数，None沿用配置50，0不连接；limit为评审新岗数，
    None沿用配置200；record_lists为名单开关，None沿用配置false。
    endpoint/index只选择已授权浏览器目标，不改变每日额度或发送权限。
    """
    from job_hunter.storage.daily import snapshot, validate_target, write_brief
    from job_hunter.workflows.apply import ApplyActions, history_ids, history_keys

    settings=load_settings()
    preferences,candidate=load_user_config()
    target=preferences.execution.communication_target if count is None else count
    maximum=preferences.execution.apply_max_jobs if limit is None else limit
    record_lists=preferences.execution.record_action_lists if record_lists is None else record_lists
    state=snapshot()
    validate_target(target,state)
    preferences.execution.max_jobs_per_run=maximum
    validate_filters(preferences.search)
    salary_min=candidate.expected_salary_min if candidate.expected_salary_min is not None else preferences.salary.target_min
    salary_max=candidate.expected_salary_max if candidate.expected_salary_max is not None else preferences.salary.target_max
    console.print(f"本次沟通目标 {target}｜今日剩余 {state['remaining']}/150（本机记录）｜名单记录 {'开启' if record_lists else '关闭'}",markup=False)
    console.print(f"检索 {'、'.join(preferences.search.keywords)}｜{'、'.join(preferences.search.cities)}｜期望月薪 {salary_min:g}–{salary_max:g}K｜页面薪资 {preferences.search.salary_filter or '不限'}",markup=False)
    console.print(f'最多评审 {maximum} 个新岗位｜最多滚动 {preferences.execution.max_scroll_rounds} 轮',markup=False)
    if target==0:
        text,path=write_brief()
        console.print('本次目标为0，未启动投递。',markup=False)
        console.print(text,markup=False)
        return
    preparation()
    excluded=history_ids()
    excluded_keys=history_keys()
    evaluator=ModelEvaluator(settings,preferences,candidate)
    log=RunLog(preferences,candidate,compact_apply=True)
    processed=0

    def emit(event):
        """仅输出客户关心的摘要；底层保留动作意图和防重事实，不落逐岗验收文档。"""
        nonlocal processed
        log.append(event)
        if event['event']=='job_result':
            processed+=1

    emit({'event':'run_start','mode':'apply','engine':evaluator.engine,'limit':maximum,
          'communication_target':target,'record_lists':record_lists,'excluded_old_ids':len(excluded)})
    actions=ApplyActions(emit,target=target,directory=log.path.parent,record_lists=record_lists)
    try:
        async with AgentSession(endpoint or discover_endpoint(),index,settings.agent_browser_binary) as active:
            summary=await scan_in_session(active,preferences,candidate,emit,evaluator=evaluator,
                                         excluded_ids=excluded,action_handler=actions,excluded_keys=excluded_keys)
    except BaseException as error:
        emit({'event':'run_end','status':'stopped','processed':processed,'reason':str(error)})
        raise
    else:
        emit({'event':'run_end','status':'completed','processed':processed,'stop_reason':summary['stop_reason']})
        labels={'communication_target':'本次沟通目标已完成','daily_limit':'今日额度已用完',
                'platform_daily_limit':'平台今日额度已耗尽','job_limit':'达到评审上限',
                'scroll_limit':'达到滚动上限','no_new_jobs_observed':'未观察到新岗位',
                'initial_list_complete':'当前列表已处理完'}
        console.print(f"本次已沟通 {actions.sent}/{target}｜{labels.get(summary['stop_reason'],summary['stop_reason'])}",markup=False)
    finally:
        text,path=write_brief()
        console.print(text,markup=False)
        console.print(f'简报：{path}',markup=False)
