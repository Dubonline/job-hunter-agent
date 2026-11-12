"""新用户本地配置：中文向导与Agent结构化草稿共用校验，不连接浏览器或模型。"""

import os
import re
import tempfile
from math import isfinite
from pathlib import Path
from typing import Literal

import typer
import yaml
from pydantic import Field, ValidationError, model_validator

from job_hunter.browser.adapter_base import PageReadError
from job_hunter.browser.filters import RECORDED, validate_filters
from job_hunter.models.preferences import (
    Candidate,
    ConfigModel,
    Exclusion,
    Preferences,
    Search,
)
from job_hunter.settings import (
    ConfigurationError,
    UserProfile,
    _validation_fields,
    load_yaml,
    profile_path,
    selected_profile,
)

PRESETS = {
    'school_mentions': ('排除985/211、双一流、一本、重点大学/院校/高校字样（包括优先）', 'mention_including_preference'),
    'base_city_not_shanghai': ('排除实际工作地点不在上海的岗位，仅上海目标适用', 'require_evidence'),
    'english_working_language': ('排除明确以英语为工作语言的岗位', 'explicit_requirement_only'),
    'algorithm_research_or_training': ('排除主要职责为算法研究或模型训练的岗位', 'require_evidence'),
}
FILTER_LABELS = {**{key: row[0] for key, row in RECORDED.items()},
                 'district_filter': '工作区域', 'industry_filter': '公司行业',
                 'financing_filter': '融资阶段', 'position_type_filter': '职位类型',
                 'job_type_filter': '旧版职位类型（含义待确认）'}


class SetupDraft(ConfigModel):
    """Agent或向导的同一输入结构；薪资为千元/月，未知个人事实必须显式保留。"""
    search: Search
    candidate: Candidate
    hard_exclusions: list[Exclusion] = Field(default_factory=list)
    prefer: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    company_blacklist: list[str] = Field(default_factory=list)
    job_title_blacklist: list[str] = Field(default_factory=list)
    missing_required_experience: Literal['review', 'reject'] = 'review'
    english_policy: Literal['strict', 'high_fit_risk'] = 'strict'
    communication_target: int = Field(default=50, ge=0, le=150)
    record_action_lists: bool = False
    review_limit: int = Field(default=5, ge=1, le=200)
    apply_limit: int = Field(default=200, ge=1, le=200)
    scroll_rounds: int = Field(default=3, ge=0, le=200)

    @model_validator(mode='after')
    def check_intake(self):
        """要求关键问题已问过；未知可以为null，不把页面筛选当个人事实。"""
        candidate = self.candidate
        if len(self.search.keywords) != 1 or len(self.search.cities) != 1:
            raise ValueError('当前正式运行一次仅使用一个关键词和城市，请分别建立配置')
        if candidate.expected_salary_min is None or candidate.expected_salary_max is None:
            raise ValueError('请单独填写个人期望月薪上下界，不能以页面薪资代替')
        if not {'education', 'languages', 'work_experience_years'} <= candidate.model_fields_set:
            raise ValueError('请明确填写学历、工作年限和英语能力，未知填null')
        if 'level' not in candidate.education.model_fields_set:
            raise ValueError('请确认实际学历，未知填null')
        if not {'english_reading', 'english_spoken', 'english_fluent'} <= candidate.languages.model_fields_set:
            raise ValueError('英语阅读、口语、流利程度须分别填写，未知填null')
        if not candidate.target_roles or any(not value.strip() for value in candidate.target_roles):
            raise ValueError('请填写个人目标岗位方向')
        if self.english_policy == 'high_fit_risk' and candidate.languages.english_spoken is not False:
            raise ValueError('英语高契合带风险策略仅用于已确认无法口语沟通且用户明确选择的情况')
        if self.english_policy == 'high_fit_risk' and any(
            rule.enabled and rule.id == 'english_working_language' for rule in self.hard_exclusions
        ):
            raise ValueError('英语绝对排除与高契合带风险策略不能同时启用')
        if self.search.cities != ['上海'] and any(
            rule.enabled and rule.id == 'base_city_not_shanghai' for rule in self.hard_exclusions
        ):
            raise ValueError('上海实际base排除规则不能用于其他目标城市')
        if any(not project.business_scenario.strip() or not project.responsibilities
               or any(not item.strip() for item in project.responsibilities) for project in candidate.projects):
            raise ValueError('每个项目须说明真实业务场景和本人职责；未提供项目可以留空列表')
        return self


def build_profile(draft: SetupDraft) -> UserProfile:
    """从空白默认值构建配置，不读取或继承当前用户的薪资、排除条件和项目事实。"""
    candidate = draft.candidate.model_copy(deep=True)
    candidate.project_weighting.enabled = True
    from job_hunter.evaluation.recency import cumulative_technologies

    candidate.skills.technologies = cumulative_technologies(candidate)
    candidate.preferred_cities = list(draft.search.cities)
    preferences = Preferences.model_validate({
        'schema_version': 1, 'search': draft.search.model_dump(),
        'salary': {'currency': 'CNY', 'unit': 'k_per_month',
                   'target_min': candidate.expected_salary_min, 'target_max': candidate.expected_salary_max,
                   'hard_min_acceptable': None, 'reject_if_advertised_max_below_hard_min': False,
                   'include_negotiable': 'review'},
        'hard_exclusions': [rule.model_dump() for rule in draft.hard_exclusions],
        'soft_preferences': {'prefer': draft.prefer, 'avoid': draft.avoid},
        'company_blacklist': draft.company_blacklist, 'job_title_blacklist': draft.job_title_blacklist,
        'matching': {'missing_required_domain_experience': draft.missing_required_experience,
                     'english_requires_high_fit': draft.english_policy == 'high_fit_risk',
                     'salary_requires_overlap': True},
        'execution': {'mode': 'review_only', 'max_jobs_per_run': draft.review_limit,
                      'require_user_start': True, 'allow_auto_pagination': False,
                      'on_captcha': 'pause', 'on_login_expired': 'pause', 'on_ambiguous_dom': 'pause',
                      'save_debug_screenshot_on_error': False, 'skip_previously_decided': True,
                      'allow_list_scroll': draft.scroll_rounds > 0, 'max_scroll_rounds': draft.scroll_rounds,
                      'communication_target': draft.communication_target, 'apply_max_jobs': draft.apply_limit,
                      'record_action_lists': draft.record_action_lists},
    })
    return UserProfile(preferences=preferences, candidate=candidate)


def readiness(profile: UserProfile) -> list[str]:
    """只读标明尚未支持的页面条件或画像缺口，保存配置不冒充浏览器验收。"""
    issues = []
    if profile.preferences.search.cities != ['上海']:
        issues.append('当前浏览器城市切换仅上海有真实验收；其他城市可保存，正式运行会停止')
    try:
        validate_filters(profile.preferences.search)
    except PageReadError:
        issues.append('部分页面条件尚未支持，正式命令会在连接前拒绝，不能把保存当筛选已生效')
    if not profile.candidate.projects:
        issues.append('未填写项目，业务场景/职责高契合证据不足；不会自动补经历')
    if profile.candidate.languages.english_spoken is None:
        issues.append('英语口语未知，遇必备要求需评审，不视为达标')
    if any(rule.enabled and rule.id not in PRESETS for rule in profile.preferences.hard_exclusions):
        issues.append('自定义硬排除仅语义模型支持，本地rules模式拒绝忽略')
    return issues


def summary(profile: UserProfile) -> str:
    """展示用户可核对的配置和画像事实；不展示API配置、不把保存当投递授权。"""
    prefs, candidate = profile.preferences, profile.candidate
    def unknown(value):
        if isinstance(value, bool):
            return '是' if value else '否'
        return '未知' if value is None else str(value)
    lines = [
        f"检索：{'、'.join(prefs.search.keywords)}｜城市：{'、'.join(prefs.search.cities)}",
        f"页面薪资：{prefs.search.salary_filter or '不限'}｜个人期望：{candidate.expected_salary_min:g}–{candidate.expected_salary_max:g}K/月",
        '薪资准入：招聘月薪须与个人期望区间相交；面议或均无法读取时待确认。',
        f"实际学历：{unknown(candidate.education.level)}｜院校层次：{unknown(candidate.education.school_tier)}｜全日制：{unknown(candidate.education.is_full_time)}｜工作年限：{unknown(candidate.work_experience_years)}年",
        f"学历取得方式：{unknown(candidate.education.admission_route)}｜保留教育记录：{len(candidate.education.records)}条",
        f"英语阅读：{unknown(candidate.languages.english_reading)}｜可口语沟通：{unknown(candidate.languages.english_spoken)}｜流利：{unknown(candidate.languages.english_fluent)}",
        f"目标岗位：{'、'.join(candidate.target_roles)}｜技能：{'、'.join(candidate.skills.strengths) or '未提供'}",
        f"不考虑方向：{'、'.join(candidate.skills.not_target_roles) or '无'}",
        f"自述：{candidate.resume_summary or '未提供'}",
    ]
    lines += [f'页面条件 {FILTER_LABELS[key]}：{value}' for key, value in prefs.search.model_dump().items()
              if key.endswith('_filter') and value is not None and key != 'salary_filter']
    for index, project in enumerate(candidate.projects, 1):
        lines.append(f"项目{index}：{project.name}｜时间：{project.period or '未知'}｜业务：{project.business_scenario}｜本人职责：{'；'.join(project.responsibilities)}｜技术：{'、'.join(project.technologies) or '未提供'}")
    policy_labels = {'explicit_requirement_only': '仅明确必备', 'require_evidence': '须原文证据',
                     'mention_including_preference': '含优先字样'}
    lines += [f'硬排除：{rule.description}（{policy_labels[rule.policy]}）' for rule in prefs.hard_exclusions if rule.enabled]
    lines += [f"偏好：{'；'.join(prefs.soft_preferences.prefer) or '无'}｜避免：{'；'.join(prefs.soft_preferences.avoid) or '无'}",
              f"公司排除：{'、'.join(prefs.company_blacklist) or '无'}｜岗位排除：{'、'.join(prefs.job_title_blacklist) or '无'}",
              f"必备经历未覆盖：{'不符合' if prefs.matching.missing_required_domain_experience == 'reject' else '待确认'}｜英语高契合带风险：{unknown(prefs.matching.english_requires_high_fit)}",
              f"沟通目标：{prefs.execution.communication_target}（运行时还会核对当天余额）｜名单记录：{'开启' if prefs.execution.record_action_lists else '关闭'}",
              f"评审上限：{prefs.execution.max_jobs_per_run}｜投递评审上限：{prefs.execution.apply_max_jobs}｜滚动上限：{prefs.execution.max_scroll_rounds}轮",
              '保存仅配置，不启动浏览器、模型、收藏或沟通。']
    lines += [f'提示：{item}' for item in readiness(profile)]
    return '\n'.join(lines)


def save_profile(name: str, profile: UserProfile, overwrite=False) -> Path:
    """将条件和画像原子保存为单文件，权限0600；默认不覆盖已有配置。"""
    destination = profile_path(name)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists() and not overwrite:
        raise ConfigurationError('同名配置已存在；确认摘要后使用--overwrite更新')
    fd, temporary = tempfile.mkstemp(prefix='.config-', dir=destination.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            yaml.safe_dump(profile.model_dump(), stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(temporary, destination)
        else:
            os.link(temporary, destination)  # 排他创建；并发同名写入不能覆盖其他用户配置。
    except OSError:
        raise ConfigurationError('配置保存失败，原配置保留') from None
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination


def split_items(value: str) -> list[str]:
    """中文/英文逗号或分号分隔；只清理空白和重复项，不改写用户事实。"""
    return list(dict.fromkeys(item.strip() for item in re.split(r'[,，;；]', value) if item.strip()))


def ask_optional(label: str, default=None):
    """短句允许空白/未知，明确写null而非推断；已有值仅作编辑默认。"""
    value = typer.prompt(label + '（留空为未知/不限）', default=str(default) if default is not None else '', show_default=default is not None and default != '').strip()
    return None if value in {'', '未知', '不限', 'null'} else value


def ask_bool(label: str, default=None):
    """三态问题；支持是/否/未知，英语各能力独立询问。"""
    choices = {'是': True, 'y': True, 'yes': True, '否': False, 'n': False, 'no': False,
               '未知': None, '': None}
    while True:
        answer = typer.prompt(label + '（是/否/未知）', default='未知' if default is None else '是' if default else '否').strip().lower()
        if answer in choices:
            return choices[answer]
        typer.echo('请输入 是、否 或 未知。')


def ask_number(label, default=None, minimum=0, maximum=None, integer=False, positive=False):
    """现场校验数值并重问；薪资单位千元、年限单位年、次数为整数。"""
    while True:
        value = typer.prompt(label, type=int if integer else float, default=default)
        if (isfinite(value) and value >= minimum and (maximum is None or value <= maximum)
                and (not positive or value > 0)):
            return value
        typer.echo('数值超出范围，请重新填写。')


def confirm(label: str, default=False) -> bool:
    """确认题支持中文是/否，不强迫中文用户输入y/n；未知不能当成同意。"""
    while True:
        answer = ask_bool(label, default)
        if answer is not None:
            return answer
        typer.echo('这个选项需要明确选择是或否。')


def collect(existing: UserProfile | None = None) -> SetupDraft:
    """中文向导收集条件及真实经历；编辑时复用本命名配置，不读取默认用户画像。"""
    old = existing.candidate if existing else Candidate()
    prefs = existing.preferences if existing else None
    keyword = typer.prompt('检索关键词（一次一个）', default=prefs.search.keywords[0] if prefs else None)
    city = typer.prompt('检索城市（一次一个）', default=prefs.search.cities[0] if prefs else None)
    search = {'keywords': [keyword.strip()], 'cities': [city.strip()]}
    for field in Search.model_fields:
        if not field.endswith('_filter') or field == 'job_type_filter':
            continue
        options = RECORDED.get(field)
        label = FILTER_LABELS[field]
        if options:
            typer.echo(f"{label}已有选项：{'、'.join(options[2])}；其他值可保存，会提示未支持。")
        value = getattr(prefs.search, field) if prefs else None
        if isinstance(value, list):
            value = '，'.join(value)
        answer = ask_optional(label + ('（可多选，逗号分隔）' if field == 'company_size_filter' else ''), value)
        search[field] = split_items(answer) if answer and field == 'company_size_filter' else answer
    candidate = old.model_dump()
    candidate['target_roles'] = split_items(typer.prompt('个人目标岗位方向（逗号分隔）', default='，'.join(old.target_roles) or keyword))
    candidate['education'] = {
        'level': ask_optional('实际最高学历', old.education.level),
        'school_tier': ask_optional('实际院校层次', old.education.school_tier),
        'is_full_time': ask_bool('是否全日制', old.education.is_full_time),
        'admission_route': old.education.admission_route,
        'records': [record.model_dump() for record in old.education.records],
    }
    years = ask_optional('实际工作年限（年，可填0；不要推断）', old.work_experience_years)
    candidate['work_experience_years'] = years
    candidate['expected_salary_min'] = ask_number('个人期望月薪下界（千元，独立于页面筛选）', old.expected_salary_min, positive=True)
    candidate['expected_salary_max'] = ask_number('个人期望月薪上界（千元）', old.expected_salary_max, minimum=candidate['expected_salary_min'])
    candidate['languages'] = {
        'english_reading': ask_optional('英语阅读能力（如基础识读/阅读技术文档）', old.languages.english_reading),
        'english_spoken': ask_bool('能否英语口语沟通', old.languages.english_spoken),
        'english_fluent': ask_bool('英语是否流利', old.languages.english_fluent),
    }
    while candidate['languages']['english_fluent'] is True and candidate['languages']['english_spoken'] is False:
        typer.echo('英语流利与不能口语沟通存在冲突，请重新确认口语能力。')
        candidate['languages']['english_spoken'] = ask_bool('能否英语口语沟通')
    candidate['skills'] = {
        'strengths': split_items(ask_optional('已掌握的技能（逗号分隔）', '，'.join(old.skills.strengths)) or ''),
        'not_target_roles': split_items(ask_optional('不考虑的岗位方向（逗号分隔）', '，'.join(old.skills.not_target_roles)) or ''),
        'technologies': old.skills.technologies,
    }
    candidate['resume_summary'] = ask_optional('真实经历简述（不填写姓名/联系方式）', old.resume_summary) or ''
    projects = list(candidate['projects'])
    if projects:
        typer.echo(f'已有{len(projects)}个项目，默认保留。')
        if confirm('清空项目并重新录入？', default=False):
            projects = []
    while confirm('添加一段项目经历？只写真实业务场景、本人职责和技术', default=False):
        projects.append({'name': typer.prompt('项目代号（建议使用匿名名称）'),
                         'period': ask_optional('项目时间'),
                         'business_scenario': typer.prompt('业务场景/解决的问题'),
                         'responsibilities': split_items(typer.prompt('本人实际职责（分号分隔）')),
                         'technologies': split_items(ask_optional('实际使用技术（逗号分隔）') or '')})
    candidate['projects'] = projects
    exclusions = []
    enabled = {rule.id for rule in prefs.hard_exclusions if rule.enabled} if prefs else set()
    for key, (description, policy) in PRESETS.items():
        if key == 'base_city_not_shanghai' and city.strip() != '上海':
            continue
        if confirm(description + '？', default=key in enabled):
            exclusions.append({'id': key, 'enabled': True, 'description': description, 'policy': policy})
    exclusions += [rule.model_dump() for rule in prefs.hard_exclusions if rule.id not in PRESETS] if prefs else []
    while confirm('添加其他硬排除？（需语义模型按原文证据判断）', default=False):
        exclusions.append({'id': f'user_rule_{len(exclusions)+1}', 'enabled': True,
                           'description': typer.prompt('准确描述排除条件，并说明必需/优先是否都排除'),
                           'policy': 'require_evidence'})
    risk = False
    if candidate['languages']['english_spoken'] is False:
        risk = confirm('英语口语不符时，仅职责高度契合岗位允许带风险申请？',
                             default=prefs.matching.english_requires_high_fit if prefs else False)
    values = {'search': search, 'candidate': candidate, 'hard_exclusions': exclusions,
              'english_policy': 'high_fit_risk' if risk else 'strict',
              'missing_required_experience': 'reject' if confirm('必备业务经历未被画像覆盖时直接判不符合？', default=prefs.matching.missing_required_domain_experience == 'reject' if prefs else False) else 'review'}
    for field, label, default in (
        ('prefer', '软偏好', prefs.soft_preferences.prefer if prefs else []),
        ('avoid', '尽量避免（非硬排除）', prefs.soft_preferences.avoid if prefs else []),
        ('company_blacklist', '明确排除的公司', prefs.company_blacklist if prefs else []),
        ('job_title_blacklist', '明确排除的岗位词', prefs.job_title_blacklist if prefs else []),
    ):
        values[field] = split_items(ask_optional(label + '（逗号分隔）', '，'.join(default)) or '')
    values['communication_target'] = ask_number('以后显式apply的沟通目标（0–150，保存不授权投递）', prefs.execution.communication_target if prefs else 50, maximum=150, integer=True)
    values['record_action_lists'] = confirm('保存成功收藏/沟通名单？', default=prefs.execution.record_action_lists if prefs else False)
    for field, label, default in (
        ('review_limit', '只读评审上限（1–200）', prefs.execution.max_jobs_per_run if prefs else 5),
        ('apply_limit', '投递时评审新岗上限（1–200）', prefs.execution.apply_max_jobs if prefs else 200),
        ('scroll_rounds', '列表滚动安全上限（0–200轮，沟通目标优先）', prefs.execution.max_scroll_rounds if prefs else 3),
    ):
        values[field] = ask_number(label, default, minimum=0 if field == 'scroll_rounds' else 1,
                                   maximum=200, integer=True)
    return SetupDraft.model_validate(values)


def configure(console, from_json: Path | None = None, save=False, overwrite=False, show=False):
    """CLI入口：草稿默认只预览，交互式展示摘要后保存；从不启动业务运行。"""
    name = selected_profile.get()
    if show:
        if name is None:
            raise ConfigurationError('请用 --profile 名称 configure --show 查看命名配置')
        console.print(summary(load_yaml(profile_path(name), UserProfile)), markup=False)
        return
    if from_json is not None and name is None:
        raise ConfigurationError('结构化草稿须用 --profile 指定新用户配置名称')
    name = name or typer.prompt('新用户配置名称（字母/数字/短横线/下划线）')
    destination = profile_path(name)
    try:
        if from_json is not None:
            try:
                draft = SetupDraft.model_validate_json(from_json.read_text(encoding='utf-8'))
            except (OSError, UnicodeError):
                raise ConfigurationError('无法读取配置草稿') from None
        else:
            existing = load_yaml(destination, UserProfile) if destination.exists() else None
            draft = collect(existing)
        profile = build_profile(draft)
    except ValidationError as error:
        # 不打印Pydantic的input内容，避免把私人经历或误填的密钥带入错误日志。
        raise ConfigurationError(f'配置草稿无效：{_validation_fields(error)}') from None
    console.print(summary(profile), markup=False)
    if from_json is None:
        save = confirm('确认以上内容并保存？（不会启动投递）', default=True)
        if save and destination.exists():
            overwrite = confirm('更新这个同名配置？默认用户配置不受影响', default=False)
            if not overwrite:
                return
    if not save:
        console.print('仅预览，未保存。', markup=False)
        return
    path = save_profile(name, profile, overwrite)
    console.print(f'已保存：{path}\n只读使用：job-agent --profile {name} scan\n保存没有授予投递权限。', markup=False)
