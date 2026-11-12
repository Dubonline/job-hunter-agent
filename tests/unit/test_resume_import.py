"""本地文档提取与有据画像草稿；全部使用合成文档，不包含真实简历。"""

import re
from pathlib import Path
from zipfile import ZipFile

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from job_hunter.models.preferences import Candidate
from job_hunter.resume_import import (
    DocumentText,
    SourceLine,
    extract_local,
    make_draft,
    propose,
    validate_sources,
    write_draft,
)
from job_hunter.settings import ConfigurationError

SAMPLE = [
    '5年工作经验 | 求职意向：应用开发工程师 | 期望薪资：25-30K | 期望城市：上海',
    '个人优势', '1. 熟悉Python，了解某工具。', '2. 掌握接口设计。',
    '工作经历', '测试单位  开发  2021.01-2026.01', '负责接口实现。',
    '项目经历', '示例项目  开发  2024.01-2025.01', '技术栈：Python、FastAPI',
    '项目介绍：面向内部知识查询。', '个人职责：', '1. 负责接口实现。', '2. 参与测试。',
    '教育经历', '示例学院 本科 2020-2022', '示例学校 大专 2017-2020',
]


def docx(path: Path, paragraphs=SAMPLE, extra=''):
    from xml.sax.saxutils import escape

    body = ''.join(f'<w:p><w:r><w:t>{escape(text)}</w:t></w:r></w:p>' for text in paragraphs)
    with ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' + body + extra + '</w:body></w:document>')
    return path


def pdf(path: Path, blank=False, extra_blank=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    if not blank:
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 20 250 Td (Resume local text) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
    if extra_blank:
        writer.add_blank_page(width=300, height=300)
    writer.write(path)
    return path


def test_docx_facts_have_exact_source_and_no_invented_skills(tmp_path):
    path = docx(tmp_path / 'resume.docx')
    draft = make_draft(path, Candidate(), None)
    facts = {item.field: item for item in draft.proposals}
    assert facts['work_experience_years'].value == 5
    assert facts['expected_salary'].value == [25, 30]
    assert facts['education.level'].value == '本科'
    assert facts['skills.strengths'].value == ['熟悉Python，了解某工具。', '掌握接口设计。']
    project = facts['projects'].value[0]
    assert project['business_scenario'] == '面向内部知识查询。'
    assert project['responsibilities'] == ['负责接口实现。', '参与测试。']
    assert project['technologies'] == ['Python', 'FastAPI']
    assert not any(item.field.startswith('languages.') for item in draft.proposals)
    lookup = {line.id: line.text for line in draft.source.lines}
    for fact in draft.proposals:
        for citation in fact.evidence:
            source = ' '.join(lookup[identifier].strip() for identifier in citation.source_ids)
            assert re.sub(r'\s+', '', citation.quote) in re.sub(r'\s+', '', source)
    validate_sources(draft)


def test_no_years_from_dates_or_english_from_certificate(tmp_path):
    path = docx(tmp_path / 'resume.docx', ['工作经历', '某单位 2020.01-2026.01',
                                          '教育经历', '某学院 本科', '资格证书', '大学英语六级'])
    draft = make_draft(path, Candidate(), None)
    assert 'work_experience_years' not in {item.field for item in draft.proposals}
    assert not any(item.field.startswith('languages.') for item in draft.proposals)


@pytest.mark.parametrize('years', ['约5年工作经验', '3-5年工作经验', '5年工作经验左右'])
def test_approximate_years_are_not_promoted_to_exact_facts(tmp_path, years):
    draft = make_draft(docx(tmp_path / 'resume.docx', [years]), Candidate(), None)
    assert not any(item.field == 'work_experience_years' for item in draft.proposals)


def test_planned_degree_does_not_replace_completed_degree(tmp_path):
    path = docx(tmp_path / 'resume.docx', ['教育经历', '准备攻读硕士', '某学院 本科'])
    draft = make_draft(path, Candidate(), None)
    assert next(item for item in draft.proposals if item.field == 'education.level').value == '本科'


def test_project_continues_across_page_boundary():
    rows = [('p1.l1', '项目经历'), ('p1.l2', '跨页项目  开发  2024.01-2025.01'),
            ('p1.l3', '项目介绍：业务场景'), ('p1.l4', '个人职责：'),
            ('p2.l1', '1. 本人完成跨页接口。'), ('p2.l2', '技术栈：Python')]
    document = DocumentText(path='local.pdf', sha256='test', format='pdf', pages=2, partial=False,
                            warnings=[], lines=[SourceLine(id=key, location=key, text=text) for key, text in rows])
    project = next(item for item in propose(document) if item.field == 'projects')
    assert project.value[0]['responsibilities'] == ['本人完成跨页接口。']
    assert {identifier.split('.')[0] for item in project.evidence for identifier in item.source_ids} == {'p1', 'p2'}


def test_team_project_description_is_not_personal_responsibility(tmp_path):
    path = docx(tmp_path / 'resume.docx', ['项目经历', '示例项目  开发  2024.01-2025.01',
                                          '项目介绍：团队构建大型平台。', '技术栈：Python'])
    draft = make_draft(path, Candidate(), None)
    assert 'projects' not in {item.field for item in draft.proposals}
    assert any('团队' in line.text for line in draft.source.lines if line.id in draft.unmapped_source_ids)


def test_hidden_and_deleted_docx_text_is_not_read(tmp_path):
    extra = '<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>虚假的高级技能</w:t></w:r><w:del><w:r><w:delText>删除内容</w:delText></w:r></w:del><w:r><w:t>可见内容</w:t></w:r></w:p>'
    document = extract_local(docx(tmp_path / 'resume.docx', [], extra))
    assert [line.text for line in document.lines] == ['可见内容']


def test_only_referenced_headers_and_footers_are_imported(tmp_path):
    path = tmp_path / 'headers.docx'
    with ZipFile(path, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><w:body><w:p><w:r><w:t>正文</w:t></w:r></w:p><w:sectPr><w:headerReference r:id="h1"/></w:sectPr></w:body></w:document>')
        archive.writestr('word/_rels/document.xml.rels', '<Relationships><Relationship Id="h1" Target="header1.xml"/></Relationships>')
        for n in (1, 2):
            archive.writestr(f'word/header{n}.xml', f'<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>页眉{n}</w:t></w:r></w:p></w:hdr>')
    document = extract_local(path)
    assert [line.text for line in document.lines] == ['页眉1', '正文']


def test_pdf_text_pages_and_partial_scans(tmp_path):
    document = extract_local(pdf(tmp_path / 'resume.pdf', extra_blank=True))
    assert document.pages == 2
    assert document.partial is True
    assert document.lines[0].location.startswith('第1页')
    assert 'Resume local text' in document.lines[0].text
    assert '第2页' in document.warnings[0]
    with pytest.raises(ConfigurationError, match='未提取到文字'):
        extract_local(pdf(tmp_path / 'scan.pdf', blank=True))


def test_invalid_files_fail_without_source_leakage(tmp_path):
    path = tmp_path / 'bad.docx'
    path.write_text('sensitive-payload-do-not-print')
    with pytest.raises(ConfigurationError) as error:
        extract_local(path)
    assert 'sensitive-payload' not in str(error.value)
    with pytest.raises(ConfigurationError, match='旧.doc'):
        extract_local(tmp_path / 'old.doc')


def test_draft_is_private_and_contacts_are_redacted(tmp_path):
    path = docx(tmp_path / 'resume.docx', ['电话：13800138000 | 邮箱：private@example.test'] + SAMPLE)
    draft = make_draft(path, Candidate(), None)
    draft_path, review = write_draft(draft, tmp_path / 'drafts')
    assert draft_path.stat().st_mode & 0o777 == 0o600
    assert review.stat().st_mode & 0o777 == 0o600
    assert '13800138000' not in review.read_text()
    assert 'private@example.test' not in draft_path.read_text()
    assert '待用户确认' in review.read_text()


def test_changed_source_or_fabricated_proposal_is_rejected(tmp_path):
    path = docx(tmp_path / 'resume.docx')
    draft = make_draft(path, Candidate(), None)
    draft.proposals[0].value = 10
    with pytest.raises(ConfigurationError, match='草稿事实或引文被修改'):
        validate_sources(draft)
    draft = make_draft(path, Candidate(), None)
    docx(path, ['这是已经改变的源文件'])
    with pytest.raises(ConfigurationError, match='源文件或提取原文已变化'):
        validate_sources(draft)
