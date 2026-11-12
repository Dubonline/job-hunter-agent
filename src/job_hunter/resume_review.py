"""用户复核简历草稿后才更新画像；个人更正与简历原文依据分别记录。"""

import json
import os
import tempfile
from pathlib import Path

import typer
import yaml
from pydantic import ValidationError

from job_hunter.models.preferences import Candidate
from job_hunter.onboarding import (
    ask_number,
    ask_optional,
    confirm,
    save_profile,
    split_items,
)
from job_hunter.resume_import import (
    ResumeDraft,
    candidate_hash,
    private_write,
    validate_sources,
)
from job_hunter.settings import (
    ConfigurationError,
    UserProfile,
    _validation_fields,
    load_user_config,
    selected_profile,
)

LABELS = {'education.level': '候选最高学历', 'education.records': '教育经历原文记录', 'work_experience_years': '明确工作年限',
          'expected_salary': '期望月薪（千元）', 'target_roles': '目标岗位',
          'preferred_cities': '期望城市', 'skills.strengths': '技能/优势原文',
          'resume_summary': '工作经历原文', 'projects': '项目经历'}


def load_target(name):
    """仅加载草稿绑定的配置；不切换BOSS账号或打开模型。"""
    token = selected_profile.set(name)
    try:
        return load_user_config()
    finally:
        selected_profile.reset(token)


def current_value(candidate: Candidate, field: str):
    if field == 'expected_salary':
        return [candidate.expected_salary_min, candidate.expected_salary_max]
    value = candidate.model_dump()
    for key in field.split('.'):
        value = value[key]
    return value


def patched_candidate(candidate: Candidate, changes: dict) -> Candidate:
    """只应用用户选择的字段；缺失/未知字段不会清空已经确认的个人事实。"""
    values = candidate.model_dump()
    from job_hunter.evaluation.recency import cumulative_technologies

    technologies = cumulative_technologies(candidate)
    for field, value in changes.items():
        if field not in LABELS:
            raise ConfigurationError('不支持修改这个画像字段')
        if field == 'expected_salary':
            if not isinstance(value, list) or len(value) != 2:
                raise ConfigurationError('期望月薪须为两个千元数值')
            values['expected_salary_min'], values['expected_salary_max'] = value
        elif '.' in field:
            group, key = field.split('.')
            values[group][key] = list(dict.fromkeys(values[group][key] + value)) if field == 'skills.strengths' else value
        else:
            values[field] = value
    try:
        updated = Candidate.model_validate(values)
    except ValidationError as error:
        raise ConfigurationError(f'更正后的画像无效：{_validation_fields(error)}') from None
    updated.skills.technologies = list(dict.fromkeys(technologies + cumulative_technologies(updated)))
    if any(not project.name.strip() or not project.business_scenario.strip() or not project.responsibilities
           for project in updated.projects):
        raise ConfigurationError('项目须有名称、真实业务场景和本人职责；不能用空项目代替未知')
    return updated


def describe(value) -> str:
    if isinstance(value, list):
        if len(value) == 2 and all(isinstance(item, (int, float)) for item in value):
            return f'{value[0]:g}–{value[1]:g}K/月'
        if len(value) <= 3 and all(isinstance(item, str) and len(item) < 50 for item in value):
            return '、'.join(value)
        return f'{len(value)}项'
    if isinstance(value, str) and len(value) > 100:
        return f'{len(value)}字原文'
    return '未知' if value is None or value == '' else str(value)


def preview(draft: ResumeDraft, candidate: Candidate | None) -> str:
    """短摘要说明差异，完整候选和每条原文依据位于同目录Markdown。"""
    lines = ['简历画像草稿：尚未保存为正式画像。']
    for item in draft.proposals:
        old = current_value(candidate, item.field) if candidate else None
        changed = old not in (None, '', []) and old != item.value
        lines.append(f'{item.id} {LABELS[item.field]}：{describe(item.value)}'
                     + (f'｜与现有值不同（原{describe(old)}），需选择保留或替换' if changed else ''))
    lines += ['待确认：' + text for text in draft.unknowns + draft.source.warnings]
    return '\n'.join(lines)


def edit_value(field, original):
    """由实际交互输入更正，保留为用户来源，不为新内容伪造PDF/Word引用。"""
    if field == 'expected_salary':
        lower = ask_number('更正月薪下界（千元）', original[0], positive=True)
        return [lower, ask_number('更正月薪上界（千元）', original[1], minimum=lower)]
    if field == 'work_experience_years':
        return ask_number('实际工作年限（年，不按日期推断）', original)
    if field in {'target_roles', 'preferred_cities', 'skills.strengths'}:
        return split_items(typer.prompt('更正内容（分号分隔，仅填写本人确认的事实）', default='；'.join(original)))
    if field == 'education.records':
        records = json.loads(json.dumps(original))
        for record in records:
            if confirm(f'修改教育记录：{record["school"]} {record["level"]}？', default=False):
                record['school'] = typer.prompt('学校', default=record['school'])
                record['level'] = typer.prompt('实际学历', default=record['level'])
                record['major'] = ask_optional('专业', record['major'])
                record['period'] = ask_optional('原文时间', record['period'])
        return records
    if field != 'projects':
        return typer.prompt('更正内容（仅填写本人确认的事实）', default=original)
    projects = json.loads(json.dumps(original))
    while projects and confirm('修改/删除某个项目？', default=False):
        index = ask_number('项目编号', minimum=1, maximum=len(projects), integer=True) - 1
        if confirm('删除这个项目？', default=False):
            projects.pop(index)
            continue
        project = projects[index]
        project['name'] = typer.prompt('项目名称/匿名代号', default=project['name'])
        project['period'] = ask_optional('项目时间，不推算', project['period'])
        project['business_scenario'] = typer.prompt('真实业务场景', default=project['business_scenario'])
        project['responsibilities'] = split_items(typer.prompt('本人职责（分号分隔）', default='；'.join(project['responsibilities'])))
        project['technologies'] = split_items(typer.prompt('实际技术（分号分隔）', default='；'.join(project['technologies'])))
    return projects


def save_candidate_file(path: Path, candidate: Candidate):
    """默认用户只原子更新受忽略的candidate.yaml；不修改检索/动作设置。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.candidate-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            yaml.safe_dump(candidate.model_dump(), stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def apply_review(draft: ResumeDraft, changes: dict, directory: Path, user_corrections=None):
    """确认前重验来源与基线，保存选中字段；调用者须已取得用户对具体结果的确认。"""
    if (directory / 'confirmed.json').exists():
        raise ConfigurationError('这份草稿已确认过，不重复覆盖画像')
    validate_sources(draft)
    preferences, current = load_target(draft.profile_name)
    if draft.base_candidate_hash is None or candidate_hash(current) != draft.base_candidate_hash:
        raise ConfigurationError('正式画像在导入后发生变化，请重新导入后核对差异')
    approved = {proposal.field: proposal.value for proposal in draft.proposals}
    corrections = user_corrections or {}
    for field, value in changes.items():
        if value != approved.get(field) and field not in corrections:
            raise ConfigurationError('新内容必须明确标注为用户更正，不能冒充简历提取')
    updated = patched_candidate(current, changes)
    if candidate_hash(updated) != candidate_hash(current):
        if draft.profile_name:
            save_profile(draft.profile_name, UserProfile(preferences=preferences, candidate=updated), overwrite=True)
        else:
            save_candidate_file(Path('config/candidate.yaml'), updated)
    private_write(directory / 'confirmed.json', json.dumps({
        'source_sha256': draft.source.sha256, 'profile_name': draft.profile_name,
        'before_hash': draft.base_candidate_hash, 'after_hash': candidate_hash(updated),
        'accepted_fields': list(changes), 'user_corrections': corrections,
        'provenance': '未更正内容来自draft.json原文引用；更正内容来自用户确认的交互输入',
    }, ensure_ascii=False, indent=2) + '\n')
    return updated


def review_command(path: Path, console, show=False, accept: str | None = None, save=False):
    """默认交互逐项核对；--show只读，--accept明确选择ID，--save才保存已选择内容。"""
    try:
        draft = ResumeDraft.model_validate_json(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValidationError):
        raise ConfigurationError('无法读取有效的待确认简历草稿') from None
    selected = selected_profile.get()
    if selected is not None and selected != draft.profile_name:
        raise ConfigurationError('所选配置与草稿绑定的配置不一致')
    preferences, candidate = load_target(draft.profile_name)
    console.print(preview(draft, candidate), markup=False)
    console.print(f'逐项候选与原文依据：{path.parent / "画像草稿待确认.md"}', markup=False)
    if show:
        return
    validate_sources(draft)
    if draft.base_candidate_hash is None:
        # 首次客户可先交简历再configure；确认时明确绑定已有新档案，不重跑模型。
        console.print('首次草稿现在绑定此配置；下列字段仍逐项选择，未选择的配置事实保留。', markup=False)
        draft = draft.model_copy(update={'base_candidate_hash': candidate_hash(candidate)})
    if draft.base_candidate_hash != candidate_hash(candidate):
        raise ConfigurationError('正式画像已变化，请重新导入')
    changes, corrections = {}, {}
    if accept is not None:
        wanted = {item.strip() for item in accept.split(',') if item.strip()}
        if wanted - {item.id for item in draft.proposals}:
            raise ConfigurationError('采纳编号不在草稿中；请使用明确的f编号，不接受all或隐式全选')
        changes = {item.field: item.value for item in draft.proposals if item.id in wanted}
    else:
        for item in draft.proposals:
            console.print(f'{item.id} {LABELS[item.field]}候选：', markup=False)
            console.print(json.dumps(item.value, ensure_ascii=False, indent=2), markup=False)
            if confirm('采纳这个候选字段？（否则保留现有值）', default=False):
                value = item.value
                if confirm('先修改这个候选字段？', default=False):
                    value = edit_value(item.field, value)
                    corrections[item.field] = {'origin': 'user_input', 'value': value}
                changes[item.field] = value
        if changes:
            console.print('将保存以下字段（未选择的字段保持现有值）：', markup=False)
            console.print(json.dumps(changes, ensure_ascii=False, indent=2), markup=False)
            save = confirm('确认这些事实并保存为正式画像？不启动投递', default=False)
    if not changes:
        console.print('未采纳任何字段，正式画像未修改。', markup=False)
        return
    patched_candidate(candidate, changes)
    if not save:
        console.print('仅预览所选字段，未修改正式画像。', markup=False)
        return
    apply_review(draft, changes, path.parent, corrections)
    console.print('已保存用户确认的画像字段，保留原文依据和更正来源；未启动浏览器、模型或投递。', markup=False)
