from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import AnyHttpUrl, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from job_hunter.models.preferences import Candidate, ConfigModel, Preferences


class ConfigurationError(Exception):
    pass


class Settings(BaseSettings):
    """从项目.env和进程环境读取设置；密钥使用SecretStr，禁止明文输出。"""
    model_config = SettingsConfigDict(
        env_file=".env", env_ignore_empty=True, extra="ignore"
    )

    llm_api_key: SecretStr | None = None  # 模型服务密钥，只能在发请求时取值。
    llm_base_url: AnyHttpUrl | None = None  # 模型服务的API基础地址。
    llm_model: str | None = None  # 服务商接受的模型标识，不是显示名称。
    llm_timeout_seconds: float = Field(default=45, gt=0, allow_inf_nan=False)  # 单次模型请求超时秒数。
    llm_max_retries: int = Field(default=2, ge=0, le=5)  # 模型请求重试上限，不适用于浏览器动作。
    app_env: str = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    agent_browser_binary: Path = (
        Path(__file__).resolve().parents[2]
        / ".local/tools/agent-browser-experimental/cli/target/ci/agent-browser"
    )
    dry_run: bool = True
    enable_external_actions: bool = False
    send_approval_required: bool = True

    @model_validator(mode="after")
    def check_review_only(self) -> Self:
        if (
            not self.dry_run
            or self.enable_external_actions
            or not self.send_approval_required
        ):
            raise ValueError(
                "DRY_RUN=true, ENABLE_EXTERNAL_ACTIONS=false, "
                "SEND_APPROVAL_REQUIRED=true are required"
            )
        return self

    def missing_model_settings(self) -> list[str]:
        """只返回缺失的配置字段名，不返回密钥、地址或其他配置值。"""
        return [
            key
            for key, value in (
                ("LLM_API_KEY", self.llm_api_key),
                ("LLM_BASE_URL", self.llm_base_url),
                ("LLM_MODEL", self.llm_model),
            )
            if value is None
        ]


def _validation_fields(error: ValidationError) -> str:
    return ", ".join(
        ".".join(map(str, item["loc"])) or item["msg"]
        for item in error.errors(include_input=False)
    )


def load_settings() -> Settings:
    """加载并校验环境设置；校验失败时隐藏输入值，避免泄露密钥。"""
    try:
        settings = Settings()
    except ValidationError as error:
        raise ConfigurationError(f"环境变量无效：{_validation_fields(error)}") from None
    return settings


def load_yaml[Model: ConfigModel](path: Path, model: type[Model]) -> Model:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError, UnicodeError, yaml.YAMLError:
        raise ConfigurationError(f"无法读取有效的 YAML：{path}") from None
    try:
        return model.model_validate(data)
    except ValidationError as error:
        raise ConfigurationError(
            f"{path} 配置无效：{_validation_fields(error)}"
        ) from None


def load_user_config() -> tuple[Preferences, Candidate]:
    preferences = load_yaml((Path("config/preferences.local.yaml") if Path("config/preferences.local.yaml").exists() else Path("config/preferences.example.yaml")), Preferences)
    candidate_path = Path("config/candidate.yaml")
    if not candidate_path.exists():
        candidate_path = Path("config/candidate.example.yaml")
    return preferences, load_yaml(candidate_path, Candidate)
