"""Chrome点击助手的会话内监视器：独立CLI默认启用，不通过CDP/模型识别弹框。"""

import asyncio
import json
import os
import sys
from pathlib import Path

from job_hunter.browser.adapter_base import PageReadError

BINARY = Path(__file__).resolve().parents[3] / '.local/tools/chrome-permission'


class ChromePermissionMonitor:
    """连接前启动、全程监视、停止即结束；不负责重连或重试业务动作。"""

    def __init__(self, on_failure, binary=BINARY):
        # on_failure将监视器意外退出升级为守卫停止；binary仅供安装与隔离测试覆盖。
        self.on_failure = on_failure
        self.binary = binary
        self.process = None
        self.reader = None
        self.closing = False
        self.events = []  # 仅存事件和毫秒数，不含窗口正文或个人数据。
        self.idle = asyncio.Event()  # 点击到弹框消失之间禁止连接流程提前结束助手。
        self.idle.set()

    async def start(self):
        """收到READY后才允许CDP连接；权限/安装缺失明确报错，不静默降级。"""
        # macOS默认随会话启用；显式设为0关闭，非macOS不启动本机辅助进程。
        if sys.platform != 'darwin' or os.environ.get('JOB_AGENT_CHROME_AUTO_ALLOW') == '0':
            return
        try:
            self.process = await asyncio.create_subprocess_exec(
                str(self.binary), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            line = await asyncio.wait_for(self.process.stdout.readline(), 5)
            event = json.loads(line)
            if event.get('event') == 'ACCESSIBILITY_REQUIRED':
                raise PageReadError(
                    'CHROME_ACCESSIBILITY_REQUIRED：Chrome点击助手现有辅助功能权限不可用，'
                    '已停止且未连接Chrome；程序不会请求新权限'
                )
            if event.get('event') != 'READY':
                raise ValueError('not ready')
            self.reader = asyncio.create_task(self._read_events())
        except (OSError, ValueError, TimeoutError):
            await self.close()
            raise PageReadError(
                'CHROME_PERMISSION_HELPER_NOT_READY：Chrome点击助手未安装或启动失败；'
                '见docs/INSTALL.md，尚未连接Chrome'
            ) from None
        except BaseException:
            await self.close()
            raise

    async def _read_events(self):
        """接收脱敏事件，异常立即停止原会话，不重启辅助进程。"""
        try:
            async for line in self.process.stdout:
                event = json.loads(line)
                self.events.append(event)
                if event.get('event') == 'ALLOW_INTENT':
                    self.idle.clear()
                elif event.get('event') == 'DIALOG_DISMISSED':
                    self.idle.set()
                print('Chrome点击助手：' + json.dumps(event, ensure_ascii=False), file=sys.stderr, flush=True)
            await self.process.wait()
        except (ValueError, OSError):
            pass
        finally:
            if not self.closing:
                self.on_failure('CHROME_PERMISSION_MONITOR_STOPPED')
                self.idle.set()

    async def wait_idle(self):
        """最多等2秒让已开始的授权点击完成核验；失败仍由守卫停止处理。"""
        try:
            await asyncio.wait_for(self.idle.wait(), 2)
        except TimeoutError:
            self.on_failure('CHROME_PERMISSION_DIALOG_UNVERIFIED')

    def stop_now(self):
        """守卫断线时同步发终止信号，避免后台继续点击授权。"""
        self.closing = True
        if self.process and self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass

    async def close(self):
        """回收自己拥有的进程与事件任务，最多等待1秒后强制结束。"""
        self.stop_now()
        if self.process:
            try:
                await asyncio.wait_for(self.process.wait(), 1)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader and self.reader is not asyncio.current_task():
            await asyncio.gather(self.reader, return_exceptions=True)
