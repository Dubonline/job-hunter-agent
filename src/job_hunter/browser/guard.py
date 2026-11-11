"""A single-use CDP transport. No retries, browser defaults, or event subscription."""

import asyncio
import json
import uuid
from urllib.request import ProxyHandler, build_opener

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import WebSocketException

from job_hunter.browser.adapter_base import PageReadError
from job_hunter.browser.attached import is_boss_url, select_boss_page, validate_endpoint
from job_hunter.browser.permission import ChromePermissionMonitor


async def websocket_endpoint(endpoint: str) -> str:
    validate_endpoint(endpoint)
    if endpoint.startswith("ws:"):
        return endpoint

    def resolve():
        with build_opener(ProxyHandler({})).open(
            endpoint.rstrip("/") + "/json/version", timeout=3
        ) as response:
            return json.load(response)["webSocketDebuggerUrl"]

    try:
        return validate_endpoint(await asyncio.to_thread(resolve))
    except OSError, ValueError, KeyError:
        raise PageReadError(
            "CDP_DISCOVERY_FAILED：HTTP 发现失败，请使用 WebSocket 端点"
        ) from None


class ConnectionGuard:
    def __init__(self, endpoint: str, index: int | None = None):
        self.endpoint = endpoint
        self.index = index
        self.reason: str | None = None
        self.stopped = asyncio.Event()
        self.upstream = self.server = self.client = self.pump = None
        self.accepted = False
        self.closing = False
        self.page_session = None
        self.requests: dict[int, dict] = {}
        self.target: dict = {}
        self.targets: list[dict] = []
        self.token = uuid.uuid4().hex
        self.permission_monitor = ChromePermissionMonitor(self.trip)

    def require_alive(self):
        if self.stopped.is_set():
            raise PageReadError(self.reason or "CHROME_SESSION_STOPPED")

    async def start(self):
        if self.upstream is not None or self.stopped.is_set():
            raise PageReadError("CHROME_SESSION_ALREADY_USED")
        try:
            # 授权监视器先就绪；保持同一进程覆盖握手及会话中途的新弹框。
            await self.permission_monitor.start()
            self.require_alive()
            # 监视器故障时中止尚在等待授权的握手，不能空等180秒或另开连接。
            connecting = asyncio.ensure_future(connect(
                await websocket_endpoint(self.endpoint),
                proxy=None,
                open_timeout=180,
                close_timeout=1,
                max_size=16 * 1024 * 1024,
            ))
            stopping = asyncio.create_task(self.stopped.wait())
            try:
                await asyncio.wait({connecting, stopping}, return_when=asyncio.FIRST_COMPLETED)
                if connecting.done() and not connecting.cancelled():
                    self.upstream = connecting.result()
                self.require_alive()
            finally:
                connecting.cancel()
                stopping.cancel()
                await asyncio.gather(connecting, stopping, return_exceptions=True)
            self.require_alive()
            await self.upstream.send(
                json.dumps({"id": 1_000_000_000, "method": "Target.getTargets"})
            )
            async with asyncio.timeout(5):
                while True:
                    message = json.loads(await self.upstream.recv())
                    if message.get("id") == 1_000_000_000:
                        self.targets = message.get("result", {}).get("targetInfos", [])
                        break
            self.target = select_boss_page(self.targets, self.index)
            await self.permission_monitor.wait_idle()
            self.require_alive()
            self.server = await serve(
                self._client,
                "127.0.0.1",
                0,
                origins=[None],
                close_timeout=1,
                max_size=16 * 1024 * 1024,
            )
            self.local_endpoint = (
                f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/{self.token}"
            )
        except PageReadError:
            await self.close()
            raise
        except OSError, ValueError, TimeoutError, WebSocketException:
            await self.close()
            raise PageReadError(
                "CHROME_ATTACH_FAILED：请确认调试开启；本机授权助手不自动重连"
            ) from None

        except BaseException:
            await self.close()
            raise

    def trip(self, reason: str):
        if not self.stopped.is_set():
            self.reason = reason
            self.stopped.set()
        self.permission_monitor.stop_now()
        if self.client:
            asyncio.create_task(self.client.close())
        if self.upstream:
            asyncio.create_task(self.upstream.close())

    async def _client(self, client):
        if (
            self.accepted
            or self.stopped.is_set()
            or client.request.path != f"/{self.token}"
        ):
            await client.close(code=1008)
            return
        self.accepted = True
        self.client = client
        self.pump = asyncio.create_task(self._receive())
        try:
            async for raw in client:
                self.require_alive()
                message = json.loads(raw)
                method, params = message.get("method"), message.get("params", {})
                if method in {
                    "Runtime.enable",
                    "Debugger.enable",
                    "Browser.close",
                    "Target.closeTarget",
                    "Target.disposeBrowserContext",
                    "Target.createTarget",
                }:
                    self.trip("CHROME_COMMAND_OUT_OF_SCOPE")
                    break
                if (
                    method == "Target.attachToTarget"
                    and params.get("targetId") != self.target["targetId"]
                ):
                    self.trip("CHROME_TARGET_OUT_OF_SCOPE")
                    break
                self.requests[message["id"]] = message
                await self.upstream.send(raw if isinstance(raw, str) else raw.decode())
        except Exception:
            if not self.closing:
                self.trip("CHROME_CLIENT_DISCONNECTED")
        finally:
            if not self.closing:
                self.trip("CHROME_CLIENT_DISCONNECTED")

    async def _receive(self):
        try:
            async for raw in self.upstream:
                message = json.loads(raw)
                method, params = message.get("method"), message.get("params", {})
                request = self.requests.pop(message.get("id"), None)
                if request and request.get("method") == "Target.getTargets":
                    if "result" in message:
                        message["result"]["targetInfos"] = [
                            t
                            for t in message["result"].get("targetInfos", [])
                            if t["targetId"] == self.target["targetId"]
                        ]
                if request and request.get("method") == "Target.attachToTarget":
                    self.page_session = message.get("result", {}).get("sessionId")
                if (
                    method == "Target.targetDestroyed"
                    and params.get("targetId") == self.target["targetId"]
                ):
                    self.trip("BOSS_TARGET_CLOSED")
                    return
                info = params.get("targetInfo", {})
                if info.get("targetId") == self.target["targetId"] and not is_boss_url(
                    info.get("url", "")
                ):
                    self.trip("BOSS_PAGE_ORIGIN_CHANGED")
                    return
                if (
                    method == "Page.frameNavigated"
                    and message.get("sessionId") == self.page_session
                ):
                    frame = params.get("frame", {})
                    if not frame.get("parentId") and not is_boss_url(
                        frame.get("url", "")
                    ):
                        self.trip("BOSS_PAGE_ORIGIN_CHANGED")
                        return
                if (
                    info.get("type") == "page"
                    and info.get("targetId") != self.target["targetId"]
                ):
                    continue
                await self.client.send(json.dumps(message))
        except Exception:
            if not self.closing:
                self.trip("CHROME_DISCONNECTED")
        finally:
            if not self.closing:
                self.trip("CHROME_DISCONNECTED")

    async def close(self):
        self.closing = True
        self.stopped.set()
        await self.permission_monitor.close()
        if self.upstream:
            await self.upstream.close()
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if self.pump:
            await asyncio.gather(self.pump, return_exceptions=True)
