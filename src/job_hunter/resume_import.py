"""本地简历提取与有据草稿；不调用模型、不把草稿自动写成已确认画像。"""

import hashlib
import json
import re
import uuid
from copy import deepcopy
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Literal
from zipfile import ZipFile
from zoneinfo import ZoneInfo

from docx.oxml import parse_xml
from docx.text.paragraph import Paragraph
from pydantic import Field
from pypdf import PdfReader

from job_hunter.models.preferences import Candidate, ConfigModel
from job_hunter.settings import ConfigurationError

MAX_FILE_BYTES = 20 * 1024 * 1024  # 本地简历文件默认上限20MiB，不加载整套OCR模型。
MAX_TEXT_CHARS = 200_000  # 提取正文上限，防止异常文件形成无限草稿。
MAX_PAGES = 30  # 文本PDF最多30页；扫描/空白页记录为部分读取。
W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
SECTIONS = {'个人优势', '专业技能', '技能特长', '工作经历', '工作经验', '项目经历', '项目经验',
            '教育经历', '教育背景', '资格证书', '证书', '自我评价'}
PERIOD = re.compile(r'\d{4}[./-]\d{1,2}\s*[-–—~至]\s*(?:\d{4}[./-]\d{1,2}|至今|现在|Present)', re.I)
NUMBERED = re.compile(r'^\s*\d{1,2}[.．、)）]\s*')


class SourceLine(ConfigModel):
    """保留原始页码/OOXML段落位置，编号不使用模型生成。"""
    id: str
    location: str
    text: str


class DocumentText(ConfigModel):
    """源文件只保留本机路径与哈希，不复制原PDF/DOCX；正文仅保存在私有草稿内。"""
    path: str
    sha256: str
    format: Literal['pdf', 'docx']
    pages: int | None
    lines: list[SourceLine]
    partial: bool
    warnings: list[str]


class SourceEvidence(ConfigModel):
    """画像叶子字段对应的原文行和引文，不把改写文字冒充原文。"""
    field: str
    source_ids: list[str] = Field(min_length=1)
    quote: str


class Proposal(ConfigModel):
    """一个可选择采纳的候选字段；projects作为整体，防止混合两份项目的索引。"""
    id: str
    field: str
    value: Any
    evidence: list[SourceEvidence] = Field(min_length=1)


class ResumeDraft(ConfigModel):
    """待用户确认的草稿；不携带任何业务动作授权。"""
    version: Literal[1] = 1
    state: Literal['pending_review'] = 'pending_review'
    created_at: str
    profile_name: str | None
    base_candidate_hash: str | None
    source: DocumentText
    proposals: list[Proposal]
    unknowns: list[str]
    unmapped_source_ids: list[str]
    model_analysis: dict = Field(default_factory=dict)  # 仅存校验后的DeepSeek原文选择及用量，不作为新事实。


def candidate_hash(candidate: Candidate) -> str:
    """对已确认画像计算语义快照，用于阻止确认时覆盖期间发生的新修改。"""
    return hashlib.sha256(json.dumps(candidate.model_dump(), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def redact_contacts(text: str) -> str:
    """电话号码与邮箱不进入草稿引用或终端；其他个人经历仍只保存在本机私有目录。"""
    text = re.sub(r'(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)', '[电话已省略]', text)
    return re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[邮箱已省略]', text)


def xml_root(data: bytes):
    """仅解析普通OOXML；不解析DTD/实体，不提取外链或运行宏。"""
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ConfigurationError('DOCX包含不支持的XML声明')
    return parse_xml(data)


def visible_paragraph(node) -> str:
    """读取可见段落文字，略过删除/直接隐藏内容，保留表格段落及换行。

    参考Reactive Resume docx-text的可见文本与页眉页脚边界，使用标准库独立实现。
    不解析样式继承或绘图阅读顺序，因此不会把文字提取称为版面完整性证明。
    """
    visible = deepcopy(node)
    for element in list(visible.iter()):
        if element.tag in {W + 'del', W + 'delText', W + 'instrText'} and element.getparent() is not None:
            element.getparent().remove(element)
        elif element.tag == W + 'r':
            hidden = element.find(W + 'rPr/' + W + 'vanish')
            if hidden is not None and hidden.get(W + 'val', 'true') not in {'0', 'false', 'off'}:
                element.getparent().remove(element)
    return Paragraph(visible, None).text.strip()


def docx_lines(data: bytes) -> list[SourceLine]:
    """本地读取正文及实际引用的页眉页脚；不按PDF页码伪造Word分页。"""
    with ZipFile(BytesIO(data)) as archive:
        parts = [info for info in archive.infolist() if info.filename in {
            'word/document.xml', 'word/_rels/document.xml.rels',
        } or re.fullmatch(r'word/(?:header|footer)\d*\.xml', info.filename)]
        if any(info.file_size > MAX_FILE_BYTES for info in parts) or sum(info.file_size for info in parts) > 2 * MAX_FILE_BYTES:
            raise ConfigurationError('DOCX解压后的正文过大')
        names = [part.filename for part in parts]
        if len(names) != len(set(names)):
            raise ConfigurationError('DOCX存在重复正文部件，无法确认读取来源')
        if 'word/document.xml' not in names:
            raise ConfigurationError('DOCX缺少正文')
        roots = {part.filename: xml_root(archive.read(part)) for part in parts}
    body = roots['word/document.xml']
    used = {node.get(REL) for node in body.iter() if node.tag in {W + 'headerReference', W + 'footerReference'}}
    rels = roots.get('word/_rels/document.xml.rels')
    selected = set()
    if rels is not None:
        for relationship in rels:
            if relationship.get('Id') in used and relationship.get('TargetMode') != 'External':
                target = relationship.get('Target', '')
                selected.add(target.lstrip('/') if target.startswith('/') else 'word/' + target.removeprefix('./'))
    def side(kind):
        return sorted(name for name in roots if re.fullmatch(rf'word/{kind}\d*\.xml', name)
                      and (rels is None or name in selected))
    result = []
    for name in side('header') + ['word/document.xml'] + side('footer'):
        for index, paragraph in enumerate(roots[name].iter(W + 'p'), 1):
            if text := visible_paragraph(paragraph):
                result.append(SourceLine(id=f'{name}#p{index}', location=f'{name} 第{index}段', text=redact_contacts(text)))
    return result


def extract_local(path: Path) -> DocumentText:
    """支持.docx和文本.pdf；空页/乱码标明部分读取，旧.doc或加密文件明确拒绝。"""
    path = path.expanduser().resolve()
    if path.suffix.lower() not in {'.pdf', '.docx'}:
        raise ConfigurationError('只支持.docx和文本PDF；旧.doc请先本地另存为.docx')
    try:
        if not path.is_file():
            raise ConfigurationError('简历文件不存在或不是普通文件')
        with path.open('rb') as stream:
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ConfigurationError('简历文件超过20MiB上限')
        digest = hashlib.sha256(data).hexdigest()
        lines, warnings, pages = [], [], None
        if path.suffix.lower() == '.pdf':
            reader = PdfReader(BytesIO(data))
            if reader.is_encrypted:
                raise ConfigurationError('加密PDF请先在本机解密后导入')
            pages = len(reader.pages)
            if not 0 < pages <= MAX_PAGES:
                raise ConfigurationError('PDF页数须为1–30页')
            for page_number, page in enumerate(reader.pages, 1):
                contents = page.get_contents()
                if contents is not None and len(contents.get_data()) > 4 * MAX_FILE_BYTES:
                    raise ConfigurationError('PDF页面解压内容过大')
                text = page.extract_text(extraction_mode='layout') or ''
                if not text.strip():
                    warnings.append(f'第{page_number}页无可提取文字，可能为扫描/图片页，未执行OCR')
                for number, text_line in enumerate(text.splitlines(), 1):
                    if text_line.strip():
                        lines.append(SourceLine(id=f'p{page_number}.l{number}', location=f'第{page_number}页第{number}行', text=redact_contacts(text_line.rstrip())))
        else:
            lines = docx_lines(data)
            warnings.append('DOCX为段落定位；样式继承隐藏、复杂绘图及分页未作视觉验收')
    except ConfigurationError:
        raise
    except Exception as error:
        # 不回显异常中的文件内容、XML或个人信息；只报告异常类型。
        raise ConfigurationError(f'本地简历读取失败：{type(error).__name__}') from None
    if not lines:
        raise ConfigurationError('未提取到文字：扫描PDF需另行OCR，不生成空画像冒充成功')
    if sum(len(line.text) for line in lines) > MAX_TEXT_CHARS:
        raise ConfigurationError('提取文字超过20万字符上限')
    suspicious = any('\ufffd' in line.text or re.search(r'[\uE000-\uF8FF]', line.text) for line in lines)
    if suspicious:
        warnings.append('存在替换字符或私有字体编码，可能有文字损失，须人工核对')
    return DocumentText(path=str(path), sha256=digest, format=path.suffix.lower()[1:], pages=pages,
                        lines=lines, partial=suspicious or any('无可提取文字' in item for item in warnings), warnings=warnings)


def joined(lines: list[SourceLine]) -> str:
    """仅按阅读行连接文字，不修补单词、不推断省略内容。"""
    return ' '.join(line.text.strip() for line in lines).strip()


def evidence(field: str, lines: list[SourceLine]) -> SourceEvidence:
    return SourceEvidence(field=field, source_ids=[line.id for line in lines], quote=joined(lines))


def paragraphs(lines: list[SourceLine]) -> list[list[SourceLine]]:
    """将编号条目与续行分组；仅用于明确的个人优势/职责区，不把关键词当技能证据。"""
    groups = []
    for line in lines:
        if NUMBERED.match(line.text) or not groups:
            groups.append([])
        groups[-1].append(line)
    return groups


def parse_projects(lines: list[SourceLine]):
    """按明确标题/日期、技术栈、介绍、本人职责提取项目，不将团队描述改成本人职责。"""
    projects, citations, pending = [], [], []
    current = None
    mode = None
    def finish():
        if current is None:
            return
        if not current['name'] or not current['scenario'] or not current['responsibilities']:
            pending.extend(line.id for line in current['all'])
            return
        index = len(projects)
        tech_text = re.sub(r'^(?:技术栈|使用技术)\s*[:：]\s*', '', joined(current['tech']))
        technology = list(dict.fromkeys(item.strip() for item in re.split(r'[、,，;；]', tech_text) if item.strip()))
        duties = paragraphs(current['responsibilities'])
        project = {'name': current['name'], 'period': current['period'],
                   'business_scenario': joined(current['scenario']),
                   'responsibilities': [NUMBERED.sub('', joined(group)) for group in duties],
                   'technologies': technology}
        projects.append(project)
        citations.append(evidence(f'projects.{index}.name', [current['header']]))
        if project['period']:
            citations.append(evidence(f'projects.{index}.period', [current['header']]))
        citations.append(evidence(f'projects.{index}.business_scenario', current['scenario']))
        citations.extend(evidence(f'projects.{index}.responsibilities.{n}', group) for n, group in enumerate(duties))
        citations.extend(evidence(f'projects.{index}.technologies.{n}', current['tech']) for n in range(len(technology)))
    for line in lines:
        text = line.text.strip()
        period = PERIOD.search(text)
        if period and period.end() == len(text) and not NUMBERED.match(text):
            finish()
            title = re.split(r'\s{2,}|\t+', text[:period.start()].strip())[0]
            current = {'name': title, 'period': period.group(), 'header': line,
                       'scenario': [], 'responsibilities': [], 'tech': [], 'all': [line]}
            mode = None
            continue
        if text.startswith('项目名称：') or text.startswith('项目名称:'):
            finish()
            current = {'name': text.split(text[4], 1)[1].strip(), 'period': None, 'header': line,
                       'scenario': [], 'responsibilities': [], 'tech': [], 'all': [line]}
            mode = None
            continue
        if current is None:
            pending.append(line.id)
            continue
        current['all'].append(line)
        label = re.match(r'^(技术栈|使用技术|项目介绍|项目描述|项目背景|业务场景|个人职责|本人职责|项目职责)\s*[:：]\s*(.*)', text)
        if label:
            mode = ('tech' if label[1] in {'技术栈', '使用技术'} else
                    'responsibilities' if label[1] in {'个人职责', '本人职责', '项目职责'} else 'scenario')
            if mode == 'tech':
                current[mode].append(line)
            elif label[2]:
                # 原文引用仍指向整行，只在候选值中去掉固定字段标签。
                current[mode].append(SourceLine(id=line.id, location=line.location, text=label[2]))
        elif mode:
            current[mode].append(line)
    finish()
    return projects, citations, pending


def propose(document: DocumentText) -> list[Proposal]:
    """确定性抽取明确事实，不从时间线/院校名称/技术词反推能力、年限或英语。"""
    proposals = []
    def add(field, value, citations):
        proposals.append(Proposal(id=f'f{len(proposals)+1:02}', field=field, value=value, evidence=citations))
    sections = {}
    section = '基本信息'
    for line in document.lines:
        heading = re.sub(r'\s+', '', line.text).strip('：:')
        if heading in SECTIONS:
            section = heading
        else:
            sections.setdefault(section, []).append(line)
    header = sections.get('基本信息', [])
    patterns = [
        ('work_experience_years', r'(\d+(?:\.\d+)?)\s*年工作经验', lambda match: float(match[1])),
        ('target_roles', r'(?:求职意向|目标岗位)\s*[:：]\s*([^|｜\n]+)', lambda match: [match[1].strip()]),
        ('preferred_cities', r'(?:期望城市|意向城市)\s*[:：]\s*([^|｜\n]+)', lambda match: [match[1].strip()]),
        ('expected_salary', r'期望薪资\s*[:：]\s*(\d+(?:\.\d+)?)\s*[-–—~]\s*(\d+(?:\.\d+)?)\s*[kK]', lambda match: [float(match[1]), float(match[2])]),
    ]
    for field, pattern, convert in patterns:
        matches = []
        for line in header:
            for match in re.finditer(pattern, line.text):
                if field == 'work_experience_years':
                    before = line.text[max(0, match.start()-8):match.start()]
                    after = line.text[match.end():match.end()+4]
                    if (re.search(r'约|近|大概|至少|超过|\d+\s*[-–—~至]\s*$', before)
                            or re.match(r'以上|左右|多', after)):
                        continue  # 范围/约数不能改成精确年限，保留原文待用户确认。
                matches.append((convert(match), line))
        if matches and all(value == matches[0][0] for value, _ in matches):
            add(field, matches[0][0], [evidence(field, [line]) for _, line in matches])
    education = sections.get('教育经历', []) + sections.get('教育背景', [])
    for degree in ('博士', '硕士', '本科', '大专', '高中', '中专'):
        found = [line for line in education if degree in line.text
                 and not re.search(r'在读|未毕业|肄业|备考|计划|准备|拟就读', line.text)]
        if found:
            add('education.level', degree, [evidence('education.level', found)])
            break
    records, citations = [], []
    for line in education:
        degree = re.search(r'博士|硕士|本科|大专|高中|中专', line.text)
        if not degree or re.search(r'在读|未毕业|肄业|备考|计划|准备|拟就读', line.text):
            continue
        school = line.text[:degree.start()].strip()
        tail = line.text[degree.end():].strip()
        period = re.search(r'\d{4}\s*[-–—~至]\s*\d{4}', tail)
        if school:
            records.append({'school': school, 'level': degree.group(),
                            'major': tail[:period.start()].strip() or None if period else tail or None,
                            'period': period.group() if period else None})
            citations.append(evidence(f'education.records.{len(records)-1}', [line]))
    if records:
        add('education.records', records, citations)
    skills = sections.get('个人优势', []) + sections.get('专业技能', []) + sections.get('技能特长', [])
    groups = paragraphs([line for line in skills if re.sub(r'\s+', '', line.text) not in {'Agent技术栈', 'Java技术栈'}])
    if groups:
        add('skills.strengths', [NUMBERED.sub('', joined(group)) for group in groups],
            [evidence(f'skills.strengths.{n}', group) for n, group in enumerate(groups)])
    work = sections.get('工作经历', []) + sections.get('工作经验', [])
    if work:
        add('resume_summary', '\n'.join(line.text.strip() for line in work), [evidence('resume_summary', work)])
    projects, citations, _ = parse_projects(sections.get('项目经历', []) + sections.get('项目经验', []))
    if projects:
        add('projects', projects, citations)
    return proposals


def make_draft(path: Path, candidate: Candidate | None, profile_name: str | None) -> ResumeDraft:
    """生成带来源的待确认草稿，已有画像仅用于版本比较；不继承其事实冒充简历提取。"""
    document = extract_local(path)
    proposals = propose(document)
    used = {identifier for item in proposals for citation in item.evidence for identifier in citation.source_ids}
    fields = {item.field for item in proposals}
    unknowns = [label for field, label in (
        ('work_experience_years', '未发现明确工作年限，不按任职日期推算'),
        ('expected_salary', '未发现明确月薪期望，不使用页面筛选代替'),
        ('education.level', '未确认实际学历，不按院校名称推断'),
        ('projects', '未识别出同时含业务场景和本人职责的完整项目，原文保留待核对'),
    ) if field not in fields]
    english_known = candidate is not None and all(value is not None for value in (
        candidate.languages.english_reading, candidate.languages.english_spoken, candidate.languages.english_fluent))
    unknowns.append('英语沿用本机已确认值，简历未注明不会重新询问或覆盖' if english_known else
                    '英语未明确的字段保留已有确认值或未知，不从证书推断')
    unknowns.append('院校层次和证书不根据校名/技术词推断；已有用户确认的学历取得方式与全日制状态不覆盖')
    if candidate is None or candidate.education.is_full_time is None:
        unknowns.append('是否全日制未确认，待用户补充，不按学校名称推断')
    unknowns.append('项目介绍不等于本人职责；未识别/未映射的原文仍保留供复核')
    return ResumeDraft(created_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(), profile_name=profile_name,
                       base_candidate_hash=candidate_hash(candidate) if candidate else None, source=document,
                       proposals=proposals, unknowns=unknowns,
                       unmapped_source_ids=[line.id for line in document.lines if line.id not in used])


def private_write(path: Path, text: str):
    """私有草稿权限0600、排他创建；原文件不修改，已有文件不覆盖。"""
    import os

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(text)


def render_review(draft: ResumeDraft) -> str:
    """生成可读的原文依据清单；映射量不冒充视觉完整性或岗位匹配准确性。"""
    locations = {line.id: line.location for line in draft.source.lines}
    labels = {'work_experience_years': '工作年限', 'target_roles': '求职方向', 'preferred_cities': '期望城市',
              'expected_salary': '期望月薪（千元）', 'education.level': '候选最高学历', 'education.records': '教育经历原文记录',
              'skills.strengths': '明确自述的技能/优势', 'resume_summary': '工作经历原文', 'projects': '项目经历'}
    text = ['# 简历画像草稿 待用户确认', '', '只做本地提取；尚未保存为正式画像，不启动投递。',
            f'格式：{draft.source.format}；页数：{draft.source.pages or "DOCX按段落定位"}；非空原文行：{len(draft.source.lines)}。',
            f'状态：{"部分文字读取，必须核对缺失页/乱码" if draft.source.partial else "已提取文字层，未证明与视觉版面绝对一致"}。', '']
    if draft.model_analysis:
        text += ['## DeepSeek有据优势摘录', '',
                 f"模型：{draft.model_analysis.get('model')}；只分析脱敏文字，不读取简历图片。", '']
        for highlight in draft.model_analysis.get('highlights', []):
            text += [f"- {highlight['field']}（{'、'.join(locations[key] for key in highlight['source_ids'])}）", f"  > {highlight['quote']}", '']
        text += ['项目时效权重（策略，不是新增履历事实）：', '',
                 '| 项目编号 | 原文时间跨度（月） | 是否重点 | 场景/职责权重 |', '| --- | --- | --- | --- |']
        for row in draft.model_analysis.get('project_recency', []):
            text.append(f"| {row['project_index']+1} | {row['duration_months'] if row['duration_months'] is not None else '未知'} | {'是' if row['focus'] else '否/未知'} | {row['scenario_weight']:.3f} |")
        text += ['', '技能和技术栈累积，不因以上场景权重降低而删除；时间跨度不能叠加为总工作年限。', '']
    for proposal in draft.proposals:
        text += [f'## {proposal.id} {labels[proposal.field]}', '', '候选内容：', '',
                 '```json', json.dumps(proposal.value, ensure_ascii=False, indent=2), '```', '', '原文依据：', '']
        grouped = {}
        for citation in proposal.evidence:
            key = (tuple(citation.source_ids), citation.quote)
            grouped.setdefault(key, []).append(citation.field)
        for (source_ids, quote), fields in grouped.items():
            refs = '、'.join(locations[identifier] for identifier in source_ids)
            text += [f'- {"、".join(fields)}：{refs}', f'  > {quote}', '']
    text += ['## 未自动确认', ''] + [f'- {line}' for line in draft.unknowns + draft.source.warnings]
    text += ['', '## 尚未映射到候选字段的原文', '', '以下内容没有丢弃，也不自动当作个人能力：', '']
    text += [f'- {line.location}：{line.text}' for line in draft.source.lines if line.id in set(draft.unmapped_source_ids)]
    return '\n'.join(text) + '\n'


def write_draft(draft: ResumeDraft, directory=Path('.local/resume-drafts')) -> tuple[Path, Path]:
    """只持久化私有待确认材料；保存草稿不改变任何正式配置。"""
    name = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
    folder = directory / name
    folder.mkdir(parents=True, mode=0o700)
    path, review = folder / 'draft.json', folder / '画像草稿待确认.md'
    private_write(path, draft.model_dump_json(indent=2) + '\n')
    private_write(review, render_review(draft))
    return path, review


def validate_sources(draft: ResumeDraft):
    """确认前重读同一源文件，核对哈希、事实值及来源；禁止编辑草稿伪造原文依据。"""
    source = extract_local(Path(draft.source.path))
    if source.sha256 != draft.source.sha256 or source.lines != draft.source.lines:
        raise ConfigurationError('简历源文件或提取原文已变化，请重新导入')
    if propose(source) != draft.proposals:
        raise ConfigurationError('草稿事实或引文被修改；请在正式复核入口记录用户更正，不能伪造原文')
