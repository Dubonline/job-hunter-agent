#!/usr/bin/env python3
"""公开仓库提交检查：只允许清单文件，拒绝私人路径、真实岗位ID和凭据。"""
import argparse
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)


def validate(files, allowed):
    """files为相对路径到内容的映射；异常只报路径，不回显疑似秘密。"""
    errors = []
    for name, data in files.items():
        if name not in allowed:
            errors.append(f'不在公开白名单：{name}')
            continue
        text = data.decode('utf-8', errors='replace')
        forbidden = [r'/'+'Users'+r'/[^/\s]+/', r'/'+'home'+r'/[^/\s]+/',
                     r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
                     r'gh[pousr]_[A-Za-z0-9]{20,}', r'github_pat_[A-Za-z0-9_]{20,}',
                     r'\bsk-[A-Za-z0-9_-]{24,}', r'\b[a-f0-9]{16}0n[A-Za-z0-9_-]{6,}\b', r'/job_detail/[A-Za-z0-9_-]{20,}\.html']
        if any(re.search(pattern, text) for pattern in forbidden):
            errors.append(f'疑似私人信息或凭据：{name}')
        if name == '.env.example' and any(line.startswith('LLM_API_KEY=') and line.split('=',1)[1].strip() for line in text.splitlines()):
            errors.append('.env.example不得包含密钥')
    if errors:
        raise SystemExit('\n'.join(errors))


def check(ref=None):
    """校验暂存区或指定提交树；清单自身必须在树中。"""
    prefix = f'{ref}:' if ref else ':'
    allowed = set(json.loads(git('show', prefix+'PUBLIC_FILES.json')))
    names = git('ls-tree','-r','--name-only',ref).decode().splitlines() if ref else git('ls-files','--cached').decode().splitlines()
    files = {name: git('show', prefix+name) for name in names}
    validate(files, allowed)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--staged',action='store_true')
    parser.add_argument('--pre-push',action='store_true')
    args=parser.parse_args()
    if args.pre_push:
        import sys
        for line in sys.stdin:
            _, sha, _, old = line.split()
            if set(sha)=={'0'}:
                continue
            revisions=[sha] if set(old)=={'0'} else [sha,'^'+old]
            for ref in git('rev-list',*revisions).decode().splitlines():
                check(ref)
    else:
        check()
    print('公开文件检查通过')


if __name__ == '__main__':
    main()
