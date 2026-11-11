"""可解释的本地规则基线，不调用语义模型，不按具体公司预设结论。"""

import re
import unicodedata
from dataclasses import asdict, dataclass, field

from job_hunter.extraction.boss_details import JobDetail
from job_hunter.models.preferences import Candidate, Preferences

ENGINE = 'local_rules_v3'  # 判断逻辑变化时更新版本，防止续跑混用不同规则的结果。


def norm(value):
    """统一value中的兼容字符和大小写，仅用于匹配，原文证据仍保留原样。"""
    return unicodedata.normalize('NFKC', value).lower()


@dataclass
class Evidence:
    """一条可核对的判断依据，不保存整份岗位正文。"""

    rule: str  # 命中的规则编号。
    section: str  # 来源段落，如岗位职责、任职要求或工作地址。
    quote: str  # 支持结论的短原文摘录。


@dataclass
class Decision:
    """自动初筛结果；候选倾向不等于所有任职资格均已核实。"""

    status: str  # HIGH_FIT职责高度契合、GENERAL通用、SKIP不符合、REVIEW待确认。
    reason: str  # 分类的主要原因。
    evidence: list[Evidence] = field(default_factory=list)  # 支持分类的短原文依据。
    matches: list[str] = field(default_factory=list)  # 与画像对应的职责/场景类别。
    notes: list[str] = field(default_factory=list)  # 尚未核实的信息和本地规则边界。
    engine: str = ENGINE  # 生成本结果的规则版本，用于续跑一致性检查。

    def minimal_record(self, detail):
        """从detail提取岗位身份并输出最小日志，不包含完整JD或画像。"""
        return {
            'job_id': detail.card.job_id,
            'title': detail.detail_title or detail.card.title,
            'listed_title': detail.card.title,
            'company': detail.card.company,
            **asdict(self),
            'actions': {'favorite': 'not_attempted', 'communicate': 'not_attempted'},
        }


REQUIREMENT_HEADER = re.compile(
    r'任职要求|任职资格|岗位要求|职位要求|招聘要求|我们希望你具备|你需要具备|应聘要求'
)
PREFERENCE_HEADER = re.compile(r'加分项|优先考虑|以下经历会加分|具备以下条件者优先')
PREFERENCE = re.compile(r'优先|加分|更佳|更好|优先考虑|非必[须需]|不要求|无需')
MANDATORY = re.compile(r'必须|必备|硬性|至少|要求具备')
NEGATIVE = re.compile(r'无需|不要求|不需要|非必[须需]')


def statements(text):
    """把text拆成原文条款，兼容编号条目挤在同一DOM段落的情况。"""
    # 同一条中的分号可能分隔必备项与优先项，需要分开判断。
    parts = re.split(r'\n+|[；;]\s*|(?=\b\d{1,2}\s*[、.．，])', text)
    return [p.strip() for p in parts if len(p.strip()) > 5]


def sections(text):
    """拆分text，依次返回岗位职责、任职要求、加分项三个条款列表。"""
    required = REQUIREMENT_HEADER.search(text)
    if not required:
        # Some postings label requirements as another "职位描述" section or
        # restart a numbered list with education instead of a heading.
        required = re.search(r'\n职位描述\s*\n|\n["“]?\s*1\s*[，、.]\s*本科', text)
    if required:
        duties, requirements = text[:required.start()], text[required.start():]
    else:
        duties, requirements = text, ''
    bonus = PREFERENCE_HEADER.search(requirements)
    if bonus:
        requirements, preferences = requirements[:bonus.start()], requirements[bonus.start():]
    else:
        preferences = ''
    return statements(duties), statements(requirements), statements(preferences)


def optional(line):
    """判断line是否仅表达优先/加分；明确必须的措辞不按可选项处理。"""
    return bool(PREFERENCE.search(line)) and not MANDATORY.search(line)


# 已覆盖的业务领域词表，用于比较任职必备经验与画像场景，不直接按行业淘汰。
DOMAINS = {
    '金融证券': r'金融|证券|银行|保险|投研|股票|资管|券商|量化交易',
    '汽车嵌入式': r'汽车|车载|autosar|aspice|自动驾驶',
    '芯片EDA': r'芯片|半导体|\beda\b|\brtl\b|电路仿真|版图',
    '电商': r'电商|零售|导购|供应链',
    '医疗': r'医院|医疗|临床|病历',
    '政务': r'政务|政企|政策|政务帮办',
    '游戏': r'游戏|gameplay|游戏引擎',
    '教育': r'教育|教培|教学|学情',
}

# 对比实质职责类别；仅有框架名称重叠不能构成高度契合。
RESPONSIBILITIES = {
    '智能体执行与恢复': r'agent\s*runtime|agent\s*运行时|智能体运行|执行机制|异常恢复|工具调度|工具注册与执行|沙箱|harness',
    '任务编排与上下文': r'任务拆解|任务规划|多智能体协作|上下文管理|任务调度|多步编排',
    '质量评测与追踪': r'评测|评估|验收|案例回放|结果校验|执行追踪|链路追踪|可观测|质量.*优化',
    '知识检索服务': r'知识库|知识服务|检索优化|检索增强|文档问答|材料清单|政策问答',
    '研发工具链': r'代码生成|代码修改|代码检索|测试生成|问题修复|研发平台|研发工具链|codeup',
    '数据分析工具': r'自然语言.*查询|运营.*分析|指标分析|数据工具|数据分析.*智能体',
    '实时数据工程': r'实时.*同步|增量同步|cdc|数据集成|一致性校验|数据对账',
}
TRAINING = re.compile(r'预训练|后训练|微调|\bsft\b|rlhf|\bdpo\b|grpo|强化学习|奖励模型|reward model|稀疏训练|算法研究|前沿.*研究|探索.*新算法|训练策略|模型.*训练|训练.*模型|训练优化')
TRAINING_PRACTICE = re.compile(r'能.*(?:lora|全参数).*微调|(?:训练|后训练).*(?:完整实操|完整.*经验|独立.*经验)|主导.*从训练到|(?:熟悉|熟练|掌握|具备|具有|有).{0,25}(?:\bsft\b|lora|rlhf|\bdpo\b|模型微调|模型训练|训练框架)')


def _evaluate(detail: JobDetail, preferences: Preferences, candidate: Candidate) -> Decision:
    """按硬排除、必备要求、职责优势的顺序进行本地自动初筛。

    参数：detail为当前已核对身份的岗位详情；preferences为启用规则及匹配策略；
    candidate为已确认的个人画像。返回带短原文依据的Decision。
    本方法不发浏览器命令、不调用模型，也不保存完整JD或画像。
    """
    text = detail.description
    title = detail.detail_title or detail.card.title
    duties, requirements, _ = sections(text)
    enabled = {r.id for r in preferences.hard_exclusions if r.enabled}  # 只执行已启用的硬排除。

    def result(status, reason, rule, section, quote):
        return Decision(status, reason, [Evidence(rule, section, quote[:240])])

    if 'school_mentions' in enabled:
        for line in statements(text):
            if re.search(r'985|211|双一流|一本', line):
                return result('SKIP', '命中院校字样排除（含优先）', 'school_mentions', 'JD', line)
    if 'english_working_language' in enabled:
        for line in statements(text):
            n = norm(line)
            if optional(line) or NEGATIVE.search(n):
                continue
            if re.search(r'(?:英文|英语).{0,12}(?:工作语言|作为工作|工作交流|工作沟通)|工作语言.{0,8}(?:英文|英语)|english.{0,12}working language', n):
                return result('SKIP', '要求英文作为工作语言', 'english_working_language', 'JD', line)
    if 'base_city_not_shanghai' in enabled:
        if re.search(r'(?:base|驻|常驻)\s*[:：]?\s*(?:武汉|北京|杭州|深圳|新加坡|香港|广州|成都|南京|苏州|海外)', title, re.I) and not re.search(r'(?:base|驻|常驻)\s*上海', title, re.I):
            return result('SKIP', '详情标题明确实际工作地点非上海', 'base_city_not_shanghai', '岗位标题', title)
        for line in statements(text):
            n = norm(line)
            if re.search(r'(?:实际|常驻|工作地点|办公地点|base)', n) and re.search(r'北京|深圳|杭州|广州|苏州|南京|成都|武汉|西安|海外|香港|新加坡', n) and '上海' not in n and not re.search(r'出差|偶尔|客户|合作', n):
                return result('SKIP', 'JD明确实际工作地点非上海', 'base_city_not_shanghai', 'JD', line)
        if detail.address and '上海' not in detail.address and re.search(r'北京|深圳|杭州|广州|苏州|南京|成都|武汉|西安', detail.address):
            return result('SKIP', '详情工作地址非上海', 'base_city_not_shanghai', '工作地址', detail.address)

    if re.search(r'项目经理|产品经理|销售经理|商务拓展|数据标注|运营专员|讲师|培训师|解决方案|技术服务|技术支持|\bTAM\b', title, re.I):
        return result('SKIP', '岗位主要方向不属于目标研发岗位', 'target_role_mismatch', '岗位标题', title)

    if 'algorithm_research_or_training' in enabled:
        grouped = re.split(r'(?m)(?=^\s*\d{1,2}[、.．])', '\n'.join(duties))
        substantive = [line for line in grouped if len(line.strip()) > 12]
        training = [line for line in substantive if TRAINING.search(norm(line)) and not NEGATIVE.search(line)]
        # A single incidental tuning duty does not make an application role a
        # research/training role. Multiple core duties or an explicit main remit do.
        main = any(re.search(r'主要|核心|偏应用研究', norm(line)) and TRAINING.search(norm(line)) for line in duties)
        training_title = re.search(r'训练|算法研究|算法研发|大模型算法工程师', title)
        if training and (main or training_title or len(training) >= max(2, len(substantive) / 2)):
            return result('SKIP', '主要职责为算法研究或模型训练', 'algorithm_research_or_training', '岗位职责', training[0])

    if detail.card.company in preferences.company_blacklist or any(x in detail.card.title for x in preferences.job_title_blacklist):
        return result('SKIP', '命中已配置黑名单', 'blacklist', '岗位标题', detail.card.title)

    profile_business = norm(' '.join(candidate.skills.strengths + [p.business_scenario for p in candidate.projects]))
    profile_tasks = norm(' '.join(r for p in candidate.projects for r in p.responsibilities))
    profile_tech = norm(' '.join(t for p in candidate.projects for t in p.technologies))
    training_known = bool(TRAINING.search(profile_tasks) or re.search(r'lora|模型微调', profile_tech))
    gaps = []
    for line in requirements:
        n = norm(line)
        if optional(line) or NEGATIVE.search(line):
            continue
        # Domain experience must be requested in requirements, not merely appear
        # in duties. Basic awareness alone is not project-experience evidence.
        experience = bool(re.search(r'经验|经历|做过|完成过', n))
        alternative_domain = bool(re.search(r'或|任一|其中|至少一种|至少一个', n)) and any(
            re.search(pattern, n) and re.search(pattern, profile_business)
            for pattern in DOMAINS.values()
        )
        for label, pattern in DOMAINS.items():
            if experience and not alternative_domain and re.search(pattern, n) and not re.search(pattern, profile_business):
                if preferences.matching.missing_required_domain_experience == 'reject':
                    return result('SKIP', f'任职必备的{label}项目/从业经验未被画像覆盖', 'required_domain_experience', '任职要求', line)
                gaps.append(Evidence('required_domain_experience', '任职要求', line[:240]))
        if TRAINING_PRACTICE.search(n) and not training_known and not re.search(r'包括但不限于|例如', n):
            alternative = re.search(r'或|任一|至少一种|其中一', n) and re.search(r'推理优化|应用开发|后端|rag|智能体|agent|部署', n)
            if alternative:
                gaps.append(Evidence('alternative_capability_unverified', '任职要求', line[:240]))
            else:
                return result('SKIP', '任职明确要求训练/微调实操，画像未覆盖', 'required_training_practice', '任职要求', line)
        if re.search(r'掌握|精通|熟练|具备', n):
            if re.search(r'golang|\bgo\b', n) and 'golang' not in profile_tech and not re.search(r'至少一种|之一|python|java', n):
                gaps.append(Evidence('required_skill_unverified', '任职要求', line[:240]))
        if 'agentscope' in n and 'agentscope' not in profile_tech and re.search(r'实际使用经验|深入了解', n):
            gaps.append(Evidence('required_skill_unverified', '任职要求', line[:240]))
        if re.search(r'做过算法工程|算法工程.*经验', n) and not re.search(r'算法工程', profile_tasks):
            gaps.append(Evidence('required_skill_unverified', '任职要求', line[:240]))
        if re.search(r'硕士|博士', n) and '本科' not in n and candidate.education.level == '本科':
            return result('SKIP', '明确学历要求高于已确认学历', 'required_education', '任职要求', line)

    if gaps:
        return Decision('REVIEW', '存在尚未核实的必备技术能力；本地规则无法认定已满足', gaps)
    if not text.strip() or not duties:
        return Decision('REVIEW', '未读取到完整职责正文')
    if 'base_city_not_shanghai' in enabled and '上海' not in detail.address:
        return Decision('REVIEW', '详情未提供可确认的上海工作地址')

    duty_text = norm(' '.join(duties))
    task_matches = [label for label, pattern in RESPONSIBILITIES.items()
                    if re.search(pattern, duty_text) and re.search(pattern, profile_tasks)]
    domain_matches = [label for label, pattern in DOMAINS.items()
                      if re.search(pattern, duty_text) and re.search(pattern, profile_business)]
    # High fit requires a real business/workflow match plus multiple substantive
    # responsibilities. Generic Agent/RAG/framework terms alone are insufficient.
    workflow = '智能体执行与恢复' in task_matches or '研发工具链' in task_matches
    high = len(task_matches) >= 2 and (bool(domain_matches) or workflow)
    supporting = next((line for line in duties if any(re.search(RESPONSIBILITIES[m], norm(line)) for m in task_matches)), duties[0])
    decision = result('HIGH_FIT' if high else 'GENERAL',
                      '业务/执行流程及多项实质职责对应' if high else '应用研发方向可保留，未证明有显著场景优势',
                      'profile_responsibilities', '岗位职责', supporting)
    decision.matches = task_matches + [f'业务场景：{d}' for d in domain_matches]
    decision.notes = ['本地规则初筛，不代表全部任职资格已满足；复杂语义待模型版验证']
    if not candidate.education.level:
        decision.notes.append('实际学历未填写')
    if requirements and re.search(r'相关专业', ''.join(requirements)):
        decision.notes.append('画像未记录具体专业，未据此推定不符合')
    if any(TRAINING.search(norm(line)) for line in duties):
        decision.notes.append('含训练相关职责，但未达到主要训练职责的本地判据')
    return decision


def evaluate(detail, preferences, candidate):
    """本地初筛后执行与模型一致的已确认英语及薪资策略。"""
    from job_hunter.evaluation.policy import apply_policy
    return apply_policy(_evaluate(detail, preferences, candidate), detail, preferences, candidate)
