from pathlib import Path

import pytest

from job_hunter.browser.adapter_base import PageReadError
from job_hunter.browser.attached import (
    discover_endpoint,
    select_boss_page,
    validate_endpoint,
)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com:9222",
        "ws://127.0.0.1.evil.test:9222/x",
        "ws://user:secret@127.0.0.1:9222/x",
        "file:///tmp/x",
        "http://127.0.0.1:bad",
        "http://127.0.0.1:9222/?secret=x",
        "ws://127.0.0.1:0/x",
    ],
)
def test_reject_bad_endpoint(endpoint):
    with pytest.raises(PageReadError, match="CDP_ENDPOINT_INVALID"):
        validate_endpoint(endpoint)


def test_selection():
    pages = [
        {"type": "page", "targetId": str(i), "url": url}
        for i, url in enumerate(
            [
                "https://www.zhipin.com/shanghai/",
                "https://example.com/",
                "https://zhipin.com/web/geek/jobs",
            ]
        )
    ]
    with pytest.raises(PageReadError, match="AMBIGUOUS"):
        select_boss_page(pages, None)
    assert select_boss_page(pages, 2)["targetId"] == "2"
    with pytest.raises(PageReadError, match="INDEX_INVALID"):
        select_boss_page(pages, 3)
    with pytest.raises(PageReadError, match="NOT_FOUND"):
        select_boss_page([], None)


def test_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr("job_hunter.browser.attached.sys.platform", "darwin")
    with pytest.raises(PageReadError, match="DISCOVERY_FAILED"):
        discover_endpoint()
    p = tmp_path / "Library/Application Support/Google/Chrome/DevToolsActivePort"
    p.parent.mkdir(parents=True)
    p.write_text("9222\n/devtools/browser/test\n")
    assert discover_endpoint() == "ws://127.0.0.1:9222/devtools/browser/test"
