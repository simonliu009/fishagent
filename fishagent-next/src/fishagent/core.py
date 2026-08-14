import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from uuid import uuid4


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def normalize_llm_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    suffix = "/chat/completions"
    if base_url.endswith(suffix):
        base_url = base_url[: -len(suffix)]
    return base_url


@dataclass
class LLMConfig:
    profile_id: str = ""
    name: str = ""
    provider: str = "zai"
    base_url: str = "https://api.z.ai/api/paas/v4"
    model: str = "glm-4.5"
    api_key: str = ""
    enabled: bool = False

    def has_api_key(self) -> bool:
        value = self.api_key.strip()
        return bool(value and "REPLACE_WITH_YOUR_KEY" not in value)

    @classmethod
    def from_env(cls) -> "LLMConfig":
        return cls(
            profile_id=os.environ.get("FISHAGENT_LLM_PROFILE_ID", ""),
            name=os.environ.get("FISHAGENT_LLM_NAME", ""),
            provider=os.environ.get("FISHAGENT_LLM_PROVIDER", cls.provider),
            base_url=normalize_llm_base_url(os.environ.get("FISHAGENT_LLM_BASE_URL", cls.base_url)),
            model=os.environ.get("FISHAGENT_LLM_MODEL", cls.model),
            api_key=os.environ.get("FISHAGENT_LLM_API_KEY", ""),
            enabled=_bool_env("FISHAGENT_LLM_ENABLED", False),
        )

    def public_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "enabled": self.enabled,
            "api_key_configured": self.has_api_key(),
            "api_key_preview": ("%s..." % self.api_key[:6]) if self.has_api_key() else "",
        }

    def update_from_payload(self, payload: dict) -> None:
        for key in ("profile_id", "name", "provider", "base_url", "model", "api_key"):
            if key in payload:
                value = str(payload.get(key) or "").strip()
                setattr(self, key, normalize_llm_base_url(value) if key == "base_url" else value)
        if "enabled" in payload:
            value = payload["enabled"]
            self.enabled = value if isinstance(value, bool) else str(value).lower() in {"1", "true", "yes", "on"}

    def private_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "api_key": self.api_key,
            "enabled": self.enabled,
        }


@dataclass
class AppConfig:
    host: str = "0.0.0.0"
    port: int = 3000
    public_port: int = 3001
    database_url: str = ""
    redis_url: str = ""
    minio_endpoint: str = ""
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "fishagent-evidence"
    celery_enabled: bool = False
    celery_broker_url: str = ""
    celery_result_backend: str = ""
    mqtt_enabled: bool = False
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_topic: str = "farms/+/ponds/+/sensors/+"
    mqtt_command_topic: str = "fishagent/ponds/{pond_id}/devices/{device_id}/commands"
    auth_enabled: bool = False
    auth_username: str = "admin"
    auth_password: str = ""
    llm: LLMConfig = field(default_factory=LLMConfig)

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            host=os.environ.get("FISHAGENT_HOST", "0.0.0.0"),
            port=int(os.environ.get("FISHAGENT_PORT", "3000")),
            public_port=int(os.environ.get("FISHAGENT_PUBLIC_PORT", "3001")),
            database_url=os.environ.get("FISHAGENT_DATABASE_URL", ""),
            redis_url=os.environ.get("FISHAGENT_REDIS_URL", ""),
            minio_endpoint=os.environ.get("FISHAGENT_MINIO_ENDPOINT", ""),
            minio_access_key=os.environ.get("FISHAGENT_MINIO_ACCESS_KEY", ""),
            minio_secret_key=os.environ.get("FISHAGENT_MINIO_SECRET_KEY", ""),
            minio_bucket=os.environ.get("FISHAGENT_MINIO_BUCKET", "fishagent-evidence"),
            celery_enabled=_bool_env("FISHAGENT_CELERY_ENABLED", False),
            celery_broker_url=os.environ.get("FISHAGENT_CELERY_BROKER_URL", ""),
            celery_result_backend=os.environ.get("FISHAGENT_CELERY_RESULT_BACKEND", ""),
            mqtt_enabled=_bool_env("FISHAGENT_MQTT_ENABLED", False),
            mqtt_host=os.environ.get("FISHAGENT_MQTT_HOST", "127.0.0.1"),
            mqtt_port=int(os.environ.get("FISHAGENT_MQTT_PORT", "1883")),
            mqtt_topic=os.environ.get("FISHAGENT_MQTT_TOPIC", cls.mqtt_topic),
            mqtt_command_topic=os.environ.get("FISHAGENT_MQTT_COMMAND_TOPIC", cls.mqtt_command_topic),
            auth_enabled=_bool_env("FISHAGENT_AUTH_ENABLED", False),
            auth_username=os.environ.get("FISHAGENT_ADMIN_USERNAME", "admin"),
            auth_password=os.environ.get("FISHAGENT_ADMIN_PASSWORD", ""),
            llm=LLMConfig.from_env(),
        )


class RuntimeConfigStore:
    def __init__(self, data_dir: str | None = None) -> None:
        self.data_dir = Path(data_dir or os.environ.get("FISHAGENT_DATA_DIR", "data"))
        self.path = self.data_dir / "runtime_config.json"

    def load_llm(self, fallback: LLMConfig) -> LLMConfig:
        if not self.path.exists():
            return fallback
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return self._config_from_payload(fallback, data.get("llm", {}))

    @staticmethod
    def _config_from_payload(fallback: LLMConfig, payload: object) -> LLMConfig:
        merged = fallback.private_dict()
        if isinstance(payload, dict):
            merged.update({key: value for key, value in payload.items() if key in merged})
        result = LLMConfig()
        result.update_from_payload(merged)
        return result

    def load_llm_bundle(self, fallback: LLMConfig) -> tuple[LLMConfig, list[LLMConfig]]:
        if not self.path.exists():
            return fallback, []
        with self.path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        active = self._config_from_payload(fallback, data.get("llm", {}))
        profiles = [
            self._config_from_payload(LLMConfig(), item)
            for item in data.get("llm_profiles", [])
            if isinstance(item, dict) and str(item.get("profile_id") or "").strip()
        ]
        return active, profiles

    def save_llm(self, config: LLMConfig, profiles: Optional[list[LLMConfig]] = None) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload: dict[str, object] = {"llm": config.private_dict()}
        if profiles is not None:
            payload["llm_profiles"] = [item.private_dict() for item in profiles]
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        tmp.replace(self.path)


def new_llm_profile_id() -> str:
    return "custom-%s" % uuid4().hex[:12]
