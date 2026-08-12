import os
import json
from dataclasses import dataclass
from pathlib import Path


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class LLMConfig:
    provider: str = "zai"
    base_url: str = "https://api.z.ai/api/paas/v4"
    model: str = "glm-4.5"
    api_key: str = ""
    enabled: bool = False

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            provider=os.environ.get("FISHAGENT_LLM_PROVIDER", cls.provider),
            base_url=os.environ.get("FISHAGENT_LLM_BASE_URL", cls.base_url),
            model=os.environ.get("FISHAGENT_LLM_MODEL", cls.model),
            api_key=os.environ.get("FISHAGENT_LLM_API_KEY", ""),
            enabled=_bool_env("FISHAGENT_LLM_ENABLED", False),
        )

    def public_dict(self) -> dict:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "enabled": self.enabled,
            "api_key_configured": bool(self.api_key),
            "api_key_preview": ("%s..." % self.api_key[:6]) if self.api_key else "",
        }

    def update_from_payload(self, payload: dict) -> None:
        for key in ("provider", "base_url", "model", "api_key"):
            if key in payload:
                setattr(self, key, str(payload.get(key) or "").strip())
        if "enabled" in payload:
            value = payload["enabled"]
            self.enabled = value if isinstance(value, bool) else str(value).lower() in {"1", "true", "yes", "on"}

    def private_dict(self) -> dict:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "api_key": self.api_key,
            "enabled": self.enabled,
        }


@dataclass
class AppConfig:
    host: str = "0.0.0.0"
    port: int = 3008
    database_url: str = ""
    redis_url: str = ""
    minio_endpoint: str = ""
    auth_enabled: bool = False
    auth_username: str = "admin"
    auth_password: str = ""
    llm: LLMConfig = None

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            host=os.environ.get("FISHAGENT_HOST", "0.0.0.0"),
            port=int(os.environ.get("FISHAGENT_PORT", "3008")),
            database_url=os.environ.get("FISHAGENT_DATABASE_URL", ""),
            redis_url=os.environ.get("FISHAGENT_REDIS_URL", ""),
            minio_endpoint=os.environ.get("FISHAGENT_MINIO_ENDPOINT", ""),
            auth_enabled=_bool_env("FISHAGENT_AUTH_ENABLED", False),
            auth_username=os.environ.get("FISHAGENT_ADMIN_USERNAME", "admin"),
            auth_password=os.environ.get("FISHAGENT_ADMIN_PASSWORD", ""),
            llm=LLMConfig.from_env(),
        )


class RuntimeConfigStore:
    def __init__(self, data_dir: str = None) -> None:
        self.data_dir = Path(data_dir or os.environ.get("FISHAGENT_DATA_DIR", "data"))
        self.path = self.data_dir / "runtime_config.json"

    def load_llm(self, fallback: LLMConfig) -> LLMConfig:
        if not self.path.exists():
            return fallback
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        llm = data.get("llm", {})
        merged = fallback.private_dict()
        merged.update({key: value for key, value in llm.items() if key in merged})
        result = LLMConfig()
        result.update_from_payload(merged)
        return result

    def save_llm(self, config: LLMConfig) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {"llm": config.private_dict()}
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        tmp.replace(self.path)
