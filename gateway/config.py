import os
import re
from pathlib import Path
from typing import Annotated, Literal
import yaml
from pydantic import BaseModel, Field, model_validator


def _interpolate_env(text: str) -> str:
    """Replace ${VAR_NAME} with environment variable values."""
    def replace(match):
        var_name = match.group(1)
        value = os.environ.get(var_name)
        if value is None:
            raise ValueError(f"Environment variable '{var_name}' not set")
        return value
    return re.sub(r"\$\{([^}]+)\}", replace, text)


class GatewayConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080


class AuthConfig(BaseModel):
    module: str
    config: dict = Field(default_factory=dict)


class AdapterAuthConfig(BaseModel):
    type: Literal["bearer"] = "bearer"
    token_env: str


class OpenAICompatibleAdapterConfig(BaseModel):
    name: str
    type: Literal["openai_compatible"]
    base_url: str
    auth: AdapterAuthConfig
    default: bool = False


class PluginAdapterConfig(BaseModel):
    name: str
    type: Literal["plugin"]
    module: str
    default: bool = False


class SanitizerItemConfig(BaseModel):
    module: str
    config: dict = Field(default_factory=dict)


class SanitizersConfig(BaseModel):
    input: list[SanitizerItemConfig] = []
    output: list[SanitizerItemConfig] = []


class StdoutAuditConfig(BaseModel):
    type: Literal["stdout"] = "stdout"


class FileAuditConfig(BaseModel):
    type: Literal["file"]
    path: str = Field(min_length=1)


class PluginAuditConfig(BaseModel):
    type: Literal["plugin"]
    module: str
    config: dict = Field(default_factory=dict)


AuditConfig = Annotated[
    StdoutAuditConfig | FileAuditConfig | PluginAuditConfig,
    Field(discriminator="type")
]


class Config(BaseModel):
    gateway: GatewayConfig = GatewayConfig()
    auth: AuthConfig
    adapters: list[OpenAICompatibleAdapterConfig | PluginAdapterConfig] = []
    sanitizers: SanitizersConfig = SanitizersConfig()
    audit: AuditConfig = StdoutAuditConfig()

    @model_validator(mode="after")
    def check_single_default_adapter(self) -> "Config":
        if not self.adapters:
            return self
        defaults = [a for a in self.adapters if a.default]
        if len(defaults) != 1:
            raise ValueError(
                f"Exactly one adapter must have default=true, found {len(defaults)}"
            )
        return self


def load_config(path: str) -> Config:
    raw = Path(path).read_text()
    interpolated = _interpolate_env(raw)
    data = yaml.safe_load(interpolated)
    return Config.model_validate(data)
