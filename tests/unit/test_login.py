import pytest

from job_hunter.browser.adapter_base import LoginStatus
from job_hunter.browser.boss_adapter import classify_login


@pytest.mark.parametrize(
    "evidence,status",
    [
        ({"messages": 1, "resume": 1}, LoginStatus.SIGNED_IN),
        ({"login": 1}, LoginStatus.SIGNED_OUT),
        ({}, LoginStatus.UNKNOWN),
        ({"messages": 1, "resume": 1, "verification": 1}, LoginStatus.UNKNOWN),
        ({"messages": 1, "resume": 1, "login": 1}, LoginStatus.UNKNOWN),
        ({"messages": 2, "resume": 1}, LoginStatus.UNKNOWN),
    ],
)
def test_login_evidence(evidence, status):
    assert classify_login(evidence) == status
