"""Owned agent-browser daemon and one guarded Chrome attachment.

Use the pinned native IPC protocol, never the CLI launcher which may respawn a
daemon. This process owns only its daemon; it never owns the user's Chrome.
"""

import asyncio
import json
import os
import tempfile
import uuid
from pathlib import Path

from job_hunter.browser.adapter_base import PageReadError
from job_hunter.browser.guard import ConnectionGuard

ROOT = Path(__file__).resolve().parents[3]
CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
DEFAULT_BINARY = (
    ROOT / ".local/tools/agent-browser-experimental/cli/target/ci/agent-browser"
)


class AgentSession:
    def __init__(
        self, endpoint: str, index: int | None = None, binary: Path = DEFAULT_BINARY
    ):
        self.binary = binary.resolve()
        self.guard = ConnectionGuard(endpoint, index)
        self.process = None
        self.directory = None
        self.socket = None
        self.counter = 0
        self.lock = asyncio.Lock()
        self.started = False

    def require_alive(self):
        self.guard.require_alive()
        if self.process is None or self.process.returncode is not None:
            self.guard.trip("AGENT_BROWSER_DAEMON_STOPPED")
            raise PageReadError(
                "AGENT_BROWSER_DAEMON_STOPPED：会话已停止，不会重启或重连"
            )

    async def __aenter__(self):
        if self.started:
            raise PageReadError("CHROME_SESSION_ALREADY_USED")
        self.started = True
        try:
            if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
                raise PageReadError(
                    "AGENT_BROWSER_NOT_INSTALLED：请配置修复版 AGENT_BROWSER_BINARY"
                )
            env = {
                k: v
                for k, v in os.environ.items()
                if k in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"}
            }
            probe = await asyncio.create_subprocess_exec(
                str(self.binary),
                "--help",
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                help_text, _ = await asyncio.wait_for(probe.communicate(), 5)
            except TimeoutError:
                probe.kill()
                await probe.wait()
                raise PageReadError("AGENT_BROWSER_PREFLIGHT_TIMEOUT") from None
            if b"AGENT_BROWSER_RUNTIME_EVENTS" not in help_text:
                raise PageReadError("AGENT_BROWSER_UNPATCHED：拒绝用原版连接 BOSS")
            await self.guard.start()  # Authorization finishes before daemon requests.
            self.directory = tempfile.TemporaryDirectory(prefix="zt-", dir="/tmp")
            name = "s" + uuid.uuid4().hex[:10]
            self.socket = Path(self.directory.name) / f"{name}.sock"
            env.update(
                AGENT_BROWSER_DAEMON="1",
                AGENT_BROWSER_SESSION=name,
                AGENT_BROWSER_SOCKET_DIR=self.directory.name,
                AGENT_BROWSER_RUNTIME_EVENTS="on-demand",
                AGENT_BROWSER_NO_AUTO_DIALOG="1",
            )
            self.process = await asyncio.create_subprocess_exec(
                str(self.binary),
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            async with asyncio.timeout(10):
                while not self.socket.exists():
                    self.require_alive()
                    await asyncio.sleep(0.02)
            await self._request(
                {"action": "launch", "cdpUrl": self.guard.local_endpoint}, timeout=15
            )
            return self
        except OSError, TimeoutError:
            await self.close()
            raise PageReadError(
                "AGENT_BROWSER_START_FAILED：会话已停止，不会重启或重连"
            ) from None
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *_):
        await self.close()

    async def _request(self, command: dict, timeout: float = 10) -> dict:
        async with self.lock:
            self.require_alive()
            self.counter += 1
            try:
                async with asyncio.timeout(timeout):
                    reader, writer = await asyncio.open_unix_connection(
                        self.socket, limit=8 * 1024 * 1024
                    )
                    try:
                        writer.write(
                            json.dumps({"id": str(self.counter), **command}).encode()
                            + b"\n"
                        )
                        await writer.drain()
                        response = json.loads(await reader.readline())
                    finally:
                        writer.close()
                        await writer.wait_closed()
                self.require_alive()
                if not response.get("success"):
                    raise PageReadError(
                        "AGENT_BROWSER_COMMAND_FAILED：动作未确认，会话已停止"
                    )
                return response.get("data", {})
            except OSError, ValueError, TimeoutError, PageReadError:
                self.guard.trip(self.guard.reason or "CHROME_SESSION_STOPPED")
                raise PageReadError(self.guard.reason) from None

    async def evaluate(self, script: str):
        return (await self._request({"action": "evaluate", "script": script})).get(
            "result"
        )

    async def bring_to_front(self):
        tabs = (await self._request({"action": "tab_list"})).get("tabs", [])
        matches = [
            tab for tab in tabs if tab.get("targetId") == self.guard.target["targetId"]
        ]
        if len(matches) != 1:
            self.guard.trip("BOSS_TARGET_NOT_CONFIRMED")
            raise PageReadError("BOSS_TARGET_NOT_CONFIRMED")
        await self._request({"action": "tab_switch", "tabId": matches[0]["tabId"]})

    async def close(self):
        # Cut the Chrome transport first. Teardown cannot close user pages or
        # reattach, even if the daemon's shutdown behavior changes later.
        await self.guard.close()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.directory:
            self.directory.cleanup()
