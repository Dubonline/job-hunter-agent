from enum import StrEnum
from typing import Protocol


class LoginStatus(StrEnum):
    SIGNED_IN = "已登录"
    SIGNED_OUT = "未登录"
    UNKNOWN = "无法识别"


class PageReadError(Exception):
    pass


class BrowserAdapter(Protocol):
    async def check_login(self) -> LoginStatus: ...

    async def read_current_page(self) -> str: ...
