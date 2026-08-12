import argparse
import json

from fishagent.application.agent_service import FishAgentSystem


def main() -> None:
    parser = argparse.ArgumentParser(prog="fishagent")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo")
    demo.add_argument("mode", choices=["init", "success", "failure", "dedup"])
    sub.add_parser("doctor")
    args = parser.parse_args()

    if args.command == "doctor":
        print("fishagent doctor: ok")
        return
    if args.command == "demo":
        system = FishAgentSystem()
        state = system.initialize_demo() if args.mode == "init" else system.run_demo(args.mode)
        print(json.dumps(state, ensure_ascii=False, indent=2, default=str))
