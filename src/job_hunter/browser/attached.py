"""Connection metadata discovery only; never open a daily browser Profile."""

import sys
from pathlib import Path
from urllib.parse import urlsplit

from job_hunter.browser.adapter_base import PageReadError


def validate_endpoint(endpoint: str) -> str:
    try:
        url = urlsplit(endpoint)
        valid = (
            url.scheme in {"http", "ws"}
            and url.hostname in {"127.0.0.1", "::1"}
            and url.port is not None
            and 1 <= url.port <= 65535
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise PageReadError("CDP_ENDPOINT_INVALID：仅接受本机回环 HTTP/WS 端点")
    return endpoint


def discover_endpoint() -> str:
    if sys.platform != "darwin":
        raise PageReadError("CDP_DISCOVERY_UNSUPPORTED：请指定 --endpoint")
    metadata = (
        Path.home() / "Library/Application Support/Google/Chrome/DevToolsActivePort"
    )
    try:
        port, path = metadata.read_text().splitlines()[:2]
        if not path.startswith("/devtools/browser/"):
            raise ValueError
        return validate_endpoint(f"ws://127.0.0.1:{int(port)}{path}")
    except OSError, ValueError:
        raise PageReadError(
            "CDP_DISCOVERY_FAILED：请手动开启 Chrome 远程调试"
        ) from None


def is_boss_url(value: str) -> bool:
    url = urlsplit(value)
    return url.scheme == "https" and url.hostname in {"www.zhipin.com", "zhipin.com"}


def select_boss_page(targets: list[dict], index: int | None) -> dict:
    pages = [
        t for t in targets if t.get("type") == "page" and is_boss_url(t.get("url", ""))
    ]
    if not pages:
        raise PageReadError("BOSS_PAGE_NOT_FOUND：请先打开并登录 BOSS")
    if index is None and len(pages) != 1:
        raise PageReadError("BOSS_PAGE_AMBIGUOUS：多个 BOSS 页面，请用 --page 指定")
    selected = index or 1
    if not 1 <= selected <= len(pages):
        raise PageReadError("BOSS_PAGE_INDEX_INVALID")
    return pages[selected - 1]
