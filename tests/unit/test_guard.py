import asyncio
import json

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from job_hunter.browser.adapter_base import PageReadError
from job_hunter.browser.guard import ConnectionGuard


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["disconnect", "close", "origin", "runtime"])
async def test_sticky_stop_without_reconnect(failure):
    sockets = []
    commands = []

    async def chrome(ws):
        sockets.append(ws)
        async for raw in ws:
            m = json.loads(raw)
            commands.append(m["method"])
            result = (
                {
                    "targetInfos": [
                        {
                            "targetId": "boss",
                            "type": "page",
                            "url": "https://www.zhipin.com/shanghai/",
                        }
                    ]
                }
                if m["method"] == "Target.getTargets"
                else {}
            )
            await ws.send(json.dumps({"id": m["id"], "result": result}))

    async with serve(chrome, "127.0.0.1", 0) as server:
        guard = ConnectionGuard(
            f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/browser"
        )
        await guard.start()
        try:
            async with connect(guard.local_endpoint, proxy=None) as client:
                await client.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
                await client.recv()
                if failure == "disconnect":
                    await sockets[0].close()
                elif failure == "close":
                    await sockets[0].send(
                        json.dumps(
                            {
                                "method": "Target.targetDestroyed",
                                "params": {"targetId": "boss"},
                            }
                        )
                    )
                elif failure == "origin":
                    await sockets[0].send(
                        json.dumps(
                            {
                                "method": "Target.targetInfoChanged",
                                "params": {
                                    "targetInfo": {
                                        "targetId": "boss",
                                        "url": "https://example.com/",
                                    }
                                },
                            }
                        )
                    )
                else:
                    await client.send(json.dumps({"id": 2, "method": "Runtime.enable"}))
                await asyncio.wait_for(guard.stopped.wait(), 2)
            with pytest.raises(PageReadError):
                guard.require_alive()
            with pytest.raises(PageReadError):
                await guard.start()
            assert len(sockets) == 1
            assert "Runtime.enable" not in commands
            assert not set(commands) & {"Browser.close", "Target.closeTarget"}
        finally:
            await guard.close()


@pytest.mark.asyncio
async def test_rejected_handshake_is_sanitized_and_sticky(monkeypatch):
    from websockets.exceptions import WebSocketException

    async def denied(*args, **kwargs):
        raise WebSocketException("private-endpoint-token")

    monkeypatch.setattr("job_hunter.browser.guard.connect", denied)
    guard = ConnectionGuard("ws://127.0.0.1:1234/browser")
    with pytest.raises(PageReadError, match="CHROME_ATTACH_FAILED") as error:
        await guard.start()
    assert "private-endpoint-token" not in str(error.value)
    with pytest.raises(PageReadError):
        await guard.start()


@pytest.fixture(autouse=True)
def isolated_permission_monitor(monkeypatch):
    """模拟CDP/临时无头Chrome测试不接触日常Chrome的原生窗口。"""
    monkeypatch.setenv('JOB_AGENT_CHROME_AUTO_ALLOW', '0')
