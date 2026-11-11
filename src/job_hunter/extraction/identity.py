"""公司与卡片岗位名称的等价身份，不保存名称原文。"""
import hashlib
import json
import unicodedata


def job_key(company, title):
    """返回公司＋岗位名称指纹；仅统一全半角和空白，不猜简称或相似职位。"""
    values = [''.join(unicodedata.normalize('NFKC', value).split()) for value in (company, title)]
    return hashlib.sha256(json.dumps(values,ensure_ascii=False).encode()).hexdigest()
