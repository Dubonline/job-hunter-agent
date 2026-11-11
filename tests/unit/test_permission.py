"""授权辅助进程生命周期与失败边界；不访问真实GUI。"""
import asyncio
import sys

import pytest

from job_hunter.browser.adapter_base import PageReadError
from job_hunter.browser.permission import ChromePermissionMonitor


def helper(tmp_path, body):
    path = tmp_path / 'permission-helper'
    path.write_text(f'#!{sys.executable}\nimport time\n{body}\n')
    path.chmod(0o700)
    return path


@pytest.fixture(autouse=True)
def enable_monitor(monkeypatch):
    monkeypatch.setattr('job_hunter.browser.permission.sys.platform', 'darwin')
    monkeypatch.setenv('JOB_AGENT_CHROME_AUTO_ALLOW', '1')


@pytest.mark.asyncio
async def test_missing_permission_prevents_connection(tmp_path):
    binary = helper(tmp_path, 'print(\'{"event":"ACCESSIBILITY_REQUIRED"}\', flush=True)')
    monitor = ChromePermissionMonitor(lambda _: pytest.fail('unexpected failure'), binary)
    with pytest.raises(PageReadError, match='CHROME_ACCESSIBILITY_REQUIRED'):
        await monitor.start()
    assert monitor.process.returncode is not None


@pytest.mark.asyncio
async def test_monitor_accepts_late_dialog_events_and_stops(tmp_path):
    binary = helper(tmp_path, '''print('{"event":"READY"}', flush=True)
time.sleep(.05)
print('{"event":"DIALOG_DISMISSED","milliseconds":25}', flush=True)
time.sleep(.1)
print('{"event":"DIALOG_DISMISSED","milliseconds":30}', flush=True)
time.sleep(60)''')
    failures = []
    monitor = ChromePermissionMonitor(failures.append, binary)
    await monitor.start()
    async with asyncio.timeout(2):
        while len(monitor.events) < 2:
            await asyncio.sleep(.01)
    await monitor.close()
    assert monitor.process.returncode is not None
    assert monitor.reader.done()
    assert not failures


@pytest.mark.asyncio
async def test_unexpected_helper_exit_stops_session(tmp_path):
    binary = helper(tmp_path, 'print(\'{"event":"READY"}\', flush=True)')
    stopped = asyncio.Event()
    monitor = ChromePermissionMonitor(lambda _: stopped.set(), binary)
    await monitor.start()
    await asyncio.wait_for(stopped.wait(), 2)
    await monitor.close()


@pytest.mark.asyncio
async def test_guard_permission_failure_never_connects(monkeypatch):
    from job_hunter.browser.guard import ConnectionGuard

    async def denied(_):
        raise PageReadError('CHROME_ACCESSIBILITY_REQUIRED')

    async def forbidden(*_, **__):
        pytest.fail('must not connect without GUI permission')

    monkeypatch.setattr(ChromePermissionMonitor, 'start', denied)
    monkeypatch.setattr('job_hunter.browser.guard.connect', forbidden)
    guard = ConnectionGuard('ws://127.0.0.1:1234/browser')
    with pytest.raises(PageReadError, match='CHROME_ACCESSIBILITY_REQUIRED'):
        await guard.start()
    assert guard.stopped.is_set()


@pytest.mark.asyncio
async def test_helper_failure_aborts_pending_handshake(monkeypatch):
    from job_hunter.browser.guard import ConnectionGuard

    cancelled = asyncio.Event()
    started = asyncio.Event()

    async def ready(_):
        pass

    async def pending(*_, **__):
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    monkeypatch.setattr(ChromePermissionMonitor, 'start', ready)
    monkeypatch.setattr('job_hunter.browser.guard.connect', pending)
    guard = ConnectionGuard('ws://127.0.0.1:1234/browser')
    task = asyncio.create_task(guard.start())
    await asyncio.wait_for(started.wait(), 1)
    guard.trip('CHROME_PERMISSION_MONITOR_STOPPED')
    with pytest.raises(PageReadError, match='CHROME_PERMISSION_MONITOR_STOPPED'):
        await asyncio.wait_for(task, 1)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_guard_stop_reaps_owned_helper(tmp_path):
    """Agent守卫停止后，自己启动的辅助进程也必须退出且不再接收事件。"""
    from job_hunter.browser.guard import ConnectionGuard

    binary = helper(tmp_path, 'print(\'{"event":"READY"}\', flush=True)\ntime.sleep(60)')
    guard = ConnectionGuard('ws://127.0.0.1:1234/browser')
    guard.permission_monitor = ChromePermissionMonitor(guard.trip, binary)
    await guard.permission_monitor.start()
    process = guard.permission_monitor.process
    try:
        guard.trip('USER_STOPPED')
        await asyncio.wait_for(process.wait(), 1)
        assert guard.stopped.is_set()
        assert guard.permission_monitor.closing
    finally:
        await guard.close()
    assert guard.permission_monitor.reader.done()


@pytest.mark.asyncio
async def test_explicit_disable_does_not_start_helper(monkeypatch, tmp_path):
    """显式禁用时不启动辅助进程，保留手动处理授权的兼容入口。"""
    monkeypatch.setenv('JOB_AGENT_CHROME_AUTO_ALLOW', '0')
    monitor = ChromePermissionMonitor(lambda _: pytest.fail('unexpected failure'), tmp_path / 'missing')
    await monitor.start()
    assert monitor.process is None
    await monitor.close()


@pytest.mark.asyncio
async def test_macos_default_starts_owned_helper(monkeypatch, tmp_path):
    """无需环境开关，独立macOS命令默认启动并回收本机助手。"""
    monkeypatch.delenv('JOB_AGENT_CHROME_AUTO_ALLOW', raising=False)
    binary = helper(tmp_path, 'print(\'{"event":"READY"}\', flush=True)\ntime.sleep(60)')
    monitor = ChromePermissionMonitor(lambda _: pytest.fail('unexpected failure'), binary)
    await monitor.start()
    assert monitor.process.returncode is None
    await monitor.close()
    assert monitor.process.returncode is not None


@pytest.mark.asyncio
async def test_wait_idle_requires_dialog_confirmation(tmp_path):
    """点击已开始时，调用方必须等到弹框消失事件才能继续退出连接。"""
    binary = helper(tmp_path, '''print('{"event":"READY"}', flush=True)
print('{"event":"ALLOW_INTENT"}', flush=True)
time.sleep(.15)
print('{"event":"DIALOG_DISMISSED","milliseconds":150}', flush=True)
time.sleep(60)''')
    monitor = ChromePermissionMonitor(lambda _: pytest.fail('unexpected failure'), binary)
    await monitor.start()
    try:
        async with asyncio.timeout(1):
            while not monitor.events:
                await asyncio.sleep(.001)
        assert not monitor.idle.is_set()
        await monitor.wait_idle()
        assert monitor.events[-1]['event'] == 'DIALOG_DISMISSED'
    finally:
        await monitor.close()
