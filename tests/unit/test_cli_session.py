import asyncio
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from job_hunter import cli
from job_hunter.browser.adapter_base import PageReadError
from job_hunter.models.preferences import Search


def test_login_is_instructions_only():
    result = CliRunner().invoke(cli.app, ["login"])
    assert result.exit_code == 0
    assert "登录由你" in result.output


@pytest.mark.asyncio
async def test_configured_unsupported_filters_fail_before_connection(monkeypatch):
    preferences = SimpleNamespace(
        search=Search(
            keywords=["大模型应用开发"], cities=["上海"], salary_filter="25-30K"
        )
    )
    monkeypatch.setattr(cli, "load_user_config", lambda: (preferences, None))
    monkeypatch.setattr(cli, "load_settings", lambda: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("Must not connect")

    monkeypatch.setattr(cli, "AgentSession", forbidden)
    with pytest.raises(PageReadError, match="M3_FILTER_NOT_READY"):
        await cli._search(None, None, None, None)


@pytest.mark.asyncio
async def test_idle_session_exits_on_disconnect(monkeypatch):
    from job_hunter.browser.adapter_base import LoginStatus

    stopped = asyncio.Event()
    entered = asyncio.Event()
    closed = []

    class FakeSession:
        guard = SimpleNamespace(stopped=stopped)

        def __init__(self, *args):
            pass

        async def __aenter__(self):
            entered.set()
            return self

        async def __aexit__(self, *args):
            closed.append(True)

        def require_alive(self):
            raise PageReadError("CHROME_DISCONNECTED")

    class FakeAdapter:
        def __init__(self, *args):
            pass

        async def check_login(self):
            return LoginStatus.SIGNED_IN

    async def waiting():
        await asyncio.Future()

    monkeypatch.setattr(cli, "AgentSession", FakeSession)
    monkeypatch.setattr(cli, "BossAdapter", FakeAdapter)
    monkeypatch.setattr(
        cli, "load_settings", lambda: SimpleNamespace(agent_browser_binary=None)
    )
    monkeypatch.setattr(cli, "_read_command", waiting)
    task = asyncio.create_task(cli._session("ws://127.0.0.1:1234/browser", None, True))
    await entered.wait()
    stopped.set()
    with pytest.raises(PageReadError, match="CHROME_DISCONNECTED"):
        await asyncio.wait_for(task, 1)
    assert closed
