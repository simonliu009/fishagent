import argparse
import json

from fishagent.agent_runtime.crewai_runtime import CrewAIOrchestrator
from fishagent.application.agent_service import FishAgentSystem
from fishagent.core import AppConfig
from fishagent.infrastructure.gateways import mqtt_gateway_from_config
from fishagent.infrastructure.mqtt import MqttTelemetryAdapter, MqttTelemetryPublisher
from fishagent.infrastructure.object_store import object_store_from_config
from fishagent.infrastructure.persistence import PersistenceError, repository_from_config
from fishagent.infrastructure.realtime import publisher_from_config


def main() -> None:
    parser = argparse.ArgumentParser(prog="fishagent")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo")
    demo.add_argument("mode", choices=["init", "success", "failure", "dedup"])
    sub.add_parser("doctor")
    args = parser.parse_args()

    if args.command == "doctor":
        config = AppConfig.from_env()
        checks = {}
        try:
            if config.database_url:
                postgres = repository_from_config(config.database_url)
                checks["postgres"] = postgres.health() if postgres else {"status": "disabled", "backend": "memory"}
            else:
                checks["postgres"] = {"status": "disabled", "backend": "memory"}
        except PersistenceError as exc:
            checks["postgres"] = {"status": "not_ready", "detail": str(exc)}
        if config.redis_url:
            redis = publisher_from_config(config.redis_url)
            checks["redis"] = redis.health() if redis else {"status": "disabled"}
        else:
            checks["redis"] = {"status": "disabled"}
        if config.minio_endpoint:
            minio = object_store_from_config(config.minio_endpoint, config.minio_access_key, config.minio_secret_key, config.minio_bucket)
            checks["minio"] = minio.health() if minio else {"status": "disabled"}
        else:
            checks["minio"] = {"status": "disabled"}
        checks["llm"] = {
            "status": "configured" if config.llm.has_api_key() else "not_configured",
            "model": config.llm.model,
        }
        print(json.dumps(checks, ensure_ascii=False, indent=2))
        raise SystemExit(0 if checks["postgres"]["status"] in {"ok", "disabled"} else 1)
    if args.command == "demo":
        config = AppConfig.from_env()
        device_gateway = mqtt_gateway_from_config(
            config.mqtt_enabled,
            config.mqtt_host,
            config.mqtt_port,
            config.mqtt_command_topic,
        )
        telemetry_publisher = MqttTelemetryPublisher(config.mqtt_host, config.mqtt_port) if config.mqtt_enabled else None
        system = FishAgentSystem(
            repository=repository_from_config(config.database_url),
            event_publisher=publisher_from_config(config.redis_url),
            device_gateway=device_gateway,
            telemetry_publisher=telemetry_publisher,
            agent_decision_timeout_seconds=config.agent_decision_timeout_seconds,
        )
        system.agent_orchestrator = CrewAIOrchestrator(system, config.llm)
        adapter = None
        if config.mqtt_enabled:
            adapter = MqttTelemetryAdapter(config.mqtt_host, config.mqtt_port, config.mqtt_topic, system.ingest_do)
            adapter.start()
        try:
            state = system.initialize_demo() if args.mode == "init" else system.run_demo(args.mode)
            print(json.dumps(state, ensure_ascii=False, indent=2, default=str))
        finally:
            if adapter:
                adapter.stop()
            if device_gateway:
                device_gateway.close()
            if telemetry_publisher:
                telemetry_publisher.close()


if __name__ == "__main__":
    main()
