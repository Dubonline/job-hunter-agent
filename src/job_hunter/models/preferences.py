from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EducationRecord(ConfigModel):
    """保留每段教育原始记录，不因最后一条是大专而覆盖最高学历。"""
    school: str
    level: str
    major: str | None = None
    period: str | None = None


class Education(ConfigModel):
    level: str | None = None
    school_tier: str | None = None
    is_full_time: bool | None = None
    admission_route: str | None = None  # 如“统招专升本”，必须有用户确认或原文依据。
    records: list[EducationRecord] = Field(default_factory=list)


class Skills(ConfigModel):
    strengths: list[str] = Field(default_factory=list)
    not_target_roles: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)  # 累积已确认技术，不按项目年代清除。


class Languages(ConfigModel):
    english_fluent: bool | None = None
    english_reading: str | None = None  # 已确认的识读水平，不推断口语能力。
    english_spoken: bool | None = None  # 是否能够口语沟通。

    @model_validator(mode='after')
    def check_english_consistency(self) -> Self:
        """拒绝“流利且不能口语沟通”的冲突，不替用户推断未知能力。"""
        if self.english_fluent is True and self.english_spoken is False:
            raise ValueError('英语流利与不能口语沟通不能同时成立')
        return self


class ProjectExperience(ConfigModel):
    name: str
    period: str | None = None
    business_scenario: str
    responsibilities: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)


class ProjectWeighting(ConfigModel):
    """权重是匹配策略，不是履历事实；旧画像默认关闭以保持兼容，新入口启用。"""
    enabled: bool = False
    focus_since: str | None = None  # YYYY-MM；明确指定时优先于滚动窗口，不猜缺失项目日期。
    focus_window_months: int = Field(default=30, ge=1, le=120)  # 未指定起点时重点窗口，单位月。
    half_life_months: int = Field(default=18, ge=1, le=120)  # 场景权重衰减半期，单位月。
    high_fit_support_threshold: float = Field(default=1.5, ge=1, le=3, allow_inf_nan=False)  # 不同实质依据数×场景权重，默认至少1.5。

    @model_validator(mode='after')
    def check_focus_month(self) -> Self:
        import re

        if self.focus_since is not None and not re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])', self.focus_since):
            raise ValueError('项目重点起点须为YYYY-MM')
        return self


class Candidate(ConfigModel):
    profile_version: Literal[1] = 1
    education: Education = Field(default_factory=Education)
    skills: Skills = Field(default_factory=Skills)
    languages: Languages = Field(default_factory=Languages)
    work_experience_years: NonNegative | None = None
    expected_salary_min: NonNegative | None = None  # 月薪期望下界，单位千元。
    expected_salary_max: NonNegative | None = None  # 月薪期望上界，单位千元。
    resume_summary: str = ""
    target_roles: list[str] = Field(default_factory=list)
    preferred_cities: list[str] = Field(default_factory=list)
    projects: list[ProjectExperience] = Field(default_factory=list)
    project_weighting: ProjectWeighting = Field(default_factory=ProjectWeighting)
    confirmed_highlight_fields: list[str] = Field(default_factory=list)  # 用户确认的优势字段路径；事实仍来自原技能/项目。
    confirmed_primary_projects: list[int] = Field(default_factory=list)  # 用户确认的重点项目索引，不删除其他历史项目。
    match_basis: Literal["business_scenario_and_responsibilities"] = (
        "business_scenario_and_responsibilities"
    )

    @model_validator(mode='after')
    def check_expected_salary(self) -> Self:
        """月薪单位千元；有值时须成对、为正数且下界不大于上界。"""
        lower, upper = self.expected_salary_min, self.expected_salary_max
        if (lower is None) != (upper is None):
            raise ValueError('期望月薪上下界须同时填写或同时未知')
        if lower is not None and (lower <= 0 or upper <= 0 or lower > upper):
            raise ValueError('期望月薪须大于0且下界不大于上界')
        return self


class Search(ConfigModel):
    keywords: list[str] = Field(min_length=1)
    cities: list[str] = Field(min_length=1)
    experience_filter: str | None = None
    education_filter: str | None = None
    salary_filter: str | None = None
    district_filter: str | None = None
    industry_filter: str | None = None
    company_size_filter: str | list[str] | None = None  # 公司规模支持多选；其他筛选保持原有含义。
    financing_filter: str | None = None
    job_type_filter: str | None = (
        None  # Legacy ambiguous field; do not use for both types.
    )
    position_type_filter: str | None = None
    job_hunting_type_filter: str | None = None

    def active_page_filters(self) -> list[str]:
        return [
            key
            for key, value in self.model_dump().items()
            if key.endswith("_filter") and value is not None
        ]

    @model_validator(mode="after")
    def check_search_terms(self) -> Self:
        if any(not term.strip() for term in self.keywords + self.cities):
            raise ValueError("keywords and cities must be nonempty")
        return self


class Salary(ConfigModel):
    currency: Literal["CNY"]
    unit: Literal["k_per_month"]
    target_min: NonNegative
    target_max: NonNegative
    hard_min_acceptable: NonNegative | None = None
    reject_if_advertised_max_below_hard_min: bool
    include_negotiable: Literal["review"]

    @model_validator(mode="after")
    def check_target_range(self) -> Self:
        if self.target_min > self.target_max:
            raise ValueError("target_min must be <= target_max")
        return self


class Exclusion(ConfigModel):
    id: str = Field(min_length=1)
    enabled: bool
    description: str
    policy: Literal[
        "explicit_requirement_only", "require_evidence", "mention_including_preference"
    ]


class SoftPreferences(ConfigModel):
    prefer: list[str]
    avoid: list[str]


class Execution(ConfigModel):
    """控制评审边界；列表滚动与点击分页是两个不同权限。"""
    mode: Literal["review_only"]
    max_jobs_per_run: int = Field(gt=0)  # 本轮最多判断的去重岗位数量，包含显式续用结果。
    require_user_start: Literal[True]
    allow_auto_pagination: Literal[False]
    on_captcha: Literal["pause"]
    on_login_expired: Literal["pause"]
    on_ambiguous_dom: Literal["pause"]
    save_debug_screenshot_on_error: bool
    skip_previously_decided: bool
    allow_list_scroll: bool = False  # 是否允许滚动岗位列表加载下一批，不授权沟通。
    max_scroll_rounds: int = Field(default=0, ge=0, le=200)  # 列表扩展安全上限；apply优先按已确认沟通目标停止，不保证凑满。
    communication_target: int = Field(default=50, ge=0, le=150)  # 本次沟通人数，CLI可覆盖；0不启动投递。
    apply_max_jobs: int = Field(default=200, ge=1, le=200)  # 投递时最多评审的新岗位数，不为凑满人数无限搜索。
    record_action_lists: bool = False  # 默认不保存公司/岗位名单；不影响额度与防重最小日志。


class Matching(ConfigModel):
    """画像匹配策略，不改变BOSS页面的检索条件。"""

    # 必备业务项目经验未被画像覆盖时：reject判不符合，review保留待确认。
    missing_required_domain_experience: Literal['reject', 'review'] = 'review'
    english_requires_high_fit: bool = False  # 英语要求仅允许职责高度契合岗位带风险申请。
    salary_requires_overlap: bool = False  # 招聘月薪与期望月薪至少有一个交点。


class Preferences(ConfigModel):
    schema_version: Literal[1]
    search: Search
    salary: Salary
    hard_exclusions: list[Exclusion]
    soft_preferences: SoftPreferences
    company_blacklist: list[str]
    job_title_blacklist: list[str]
    execution: Execution
    matching: Matching = Field(default_factory=Matching)

    @model_validator(mode="after")
    def check_exclusion_ids(self) -> Self:
        if len({rule.id for rule in self.hard_exclusions}) != len(self.hard_exclusions):
            raise ValueError("hard_exclusions IDs must be unique")
        return self
