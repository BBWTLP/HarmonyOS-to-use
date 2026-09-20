import argparse
import json
import os
import sys
from .service import default_state

def main():
    argv = sys.argv[1:]
    if argv and argv[0] == "agent":
        # Read-only task/evidence tooling. It never constructs a device driver,
        # so it has no --execute gate.
        from harmony_agent.cli import main as agent_main
        rest = argv[1:]
        state = os.environ.get("HARMONY_STATE_DIR") or str(default_state())
        if "--state-dir" in rest:
            index = rest.index("--state-dir")
            if index + 1 >= len(rest):
                print(json.dumps({"status": "invalid_arguments",
                                  "message": "--state-dir needs a path"},
                                 ensure_ascii=False))
                return 2
            state = rest[index + 1]
            rest = rest[:index] + rest[index + 2:]
        return agent_main(rest, state)
    parser = argparse.ArgumentParser(description="HarmonyOS local agent runtime")
    parser.add_argument("command", choices=["serve", "mcp", "doctor", "probe", "benchmark", "baseline", "protocol"])
    parser.add_argument("--state-dir", default=str(default_state()))
    parser.add_argument("--samples", type=int, default=10, help="Benchmark observations (1-500)")
    parser.add_argument("--mode", choices=["FAST", "FULL"], default="FAST")
    parser.add_argument("--include-image", action="store_true")
    parser.add_argument("--execute", action="store_true",
                        help="Acknowledge device access (baseline is read-only; benchmark may wake/unlock)")
    args = parser.parse_args()
    if args.command in {"baseline", "protocol"} and not args.execute:
        parser.error(f"{args.command} requires --execute")
    if args.command == "benchmark":
        if not args.execute:
            parser.error("benchmark requires --execute; observations may wake/unlock the phone")
        if not 1 <= args.samples <= 500:
            parser.error("--samples must be between 1 and 500")
    if args.command == "serve":
        from .service import serve
        serve(args.state_dir)
    elif args.command == "mcp":
        from .mcp_server import run
        run(args.state_dir)
    elif args.command == "benchmark":
        from .benchmark import run
        result = run(args.state_dir, args.samples, args.mode, args.include_image)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0 if result["status"] == "ok" else 1
    elif args.command == "baseline":
        from .baseline import report
        result = report()
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0 if result["status"] == "ok" else 1
    elif args.command == "protocol":
        from .protocol import report
        result = report()
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0 if result["status"] == "ok" else 1
    elif args.command == "probe":
        from .probe import run
        result = run(args.state_dir)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "ok" else 1
    else:
        from .diagnostics import report
        result = report()
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "ok" else 1

if __name__ == "__main__": raise SystemExit(main())
