"""只记录岗位分类、短证据及运行状态，不保存整份JD或个人画像。"""

import fcntl
import hashlib
import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from job_hunter.settings import ConfigurationError

READING_VERSION = 'response_dom_v1'  # 读取来源变更后，旧评审不能绕过新的双源核对。


class RunLog:
    """最小运行日志；同版本续跑通过配置指纹和规则版本双重校验。"""

    def __init__(self, preferences, candidate, directory=Path('.local/runs'), compact_apply=False):
        """preferences为运行配置，candidate为画像，directory为受忽略保护的日志目录。"""
        now = datetime.now(ZoneInfo('Asia/Shanghai'))
        self.run_id = now.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / (self.run_id + '.jsonl')
        self.compact_apply = compact_apply  # 正式投递只保留计数、防重、动作事实；名单由成功事件显式提供。
        # 快照仅在内存中计算指纹，磁盘日志不包含这些完整配置/画像。
        snapshot = json.dumps([preferences.model_dump(), candidate.model_dump()], ensure_ascii=False, sort_keys=True)
        self.fingerprint = hashlib.sha256(snapshot.encode()).hexdigest()[:16]

    def append(self, event):
        """追加一条最小事件；event不得携带整份JD、密钥或完整画像。"""
        if self.compact_apply:
            if event['event'] in {'detail_read', 'evaluating', 'sent_dialog_observed'}:
                return
            if event['event'] in {'decision', 'job_result'}:
                event = {key: value for key, value in event.items() if key in {
                    'event', 'ordinal', 'job_id', 'identity_key', 'status', 'actions', 'timing',
                }}
        record = {
            'at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
            'run_id': self.run_id, 'config_version': self.fingerprint,
            'reading_version': READING_VERSION, **event,
        }
        # 跨进程共用锁；检查额度和持久化意图必须在同一个临界区，避免两个会话超额。
        with (self.path.parent / '.daily.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            record['at'] = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()
            if event['event'] == 'communicate_intent':
                from job_hunter.storage.daily import events, snapshot, validate_target
                if not event.get('job_id'):
                    raise ConfigurationError('COMMUNICATE_JOB_ID_MISSING')
                if any(row.get('event') == 'communicate_intent' and row.get('job_id') == event['job_id'] for row in events(self.path.parent)):
                    raise ConfigurationError('COMMUNICATE_PREVIOUS_ATTEMPT：禁止重复提交')
                if event.get('identity_key') and any(row.get('event') == 'communicate_intent' and row.get('identity_key') == event['identity_key'] for row in events(self.path.parent)):
                    raise ConfigurationError('COMMUNICATE_EQUIVALENT_PREVIOUS_ATTEMPT：同公司同岗位已有尝试')
                validate_target(1, snapshot(self.path.parent, locked=True))
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                if event['event'].endswith('_intent'):
                    stream.flush()
                    os.fsync(stream.fileno())

    def resume(self, run_id, engine):
        """读取run_id对应的旧判断；engine必须与旧日志的规则版本一致。

        配置或画像指纹变化时拒绝续用，不混合不同条件下的判断。
        此方法只读本地日志，不建立或恢复浏览器连接。
        """
        if not re.fullmatch(r'\d{8}-\d{6}-[a-f0-9]{8}', run_id):
            raise ConfigurationError('RESUME_RUN_ID_INVALID')
        try:
            rows = [json.loads(line) for line in (self.path.parent / f'{run_id}.jsonl').read_text().splitlines()]
        except OSError, ValueError:
            raise ConfigurationError('RESUME_LOG_UNREADABLE') from None
        if not rows or any(row.get('config_version') != self.fingerprint for row in rows):
            raise ConfigurationError('RESUME_CONFIG_OR_PROFILE_CHANGED：不能混用两套条件/画像')
        if any(row.get('reading_version') != READING_VERSION for row in rows):
            raise ConfigurationError('RESUME_READING_CHANGED：旧结果未通过当前详情来源核对，不能续用')
        if rows[0].get('engine') != engine:
            raise ConfigurationError('RESUME_ENGINE_CHANGED：规则版本不同，需要重新判断')
        decisions = {}
        for row in rows:
            if row['event'] == 'decision':
                if row.get('engine') != engine:
                    raise ConfigurationError('RESUME_ENGINE_CHANGED')
                decisions[row['job_id']] = {k: v for k, v in row.items() if k not in {'at', 'run_id', 'config_version'}}
        return list(decisions.values())
