from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

NonNegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Education(ConfigModel):
    level: str | None = None
    school_tier: str | None = None
    is_full_time: bool | None = None


class Skills(ConfigModel):
    strengths: list[str] = Field(default_factory=list)
    not_target_roles: list[str] = Field(default_factory=list)


class Languages(ConfigModel):
    english_fluent: bool | None = None
    english_reading: str | None = None  # 已确认的识读水平，不推断口语能力。
    english_spoken: bool | None = None  # 是否能够口语沟通。


class ProjectExperience(ConfigModel):
    name: str
    period: str | None = None
    business_scenario: str
    responsibilities: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)


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
    match_basis: Literal["business_scenario_and_responsibilities"] = (
        "business_scenario_and_responsibilities"
    )


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
    max_scroll_rounds: int = Field(default=0, ge=0, le=20)  # 当前运行最多扩展列表的次数。
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
