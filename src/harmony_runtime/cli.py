import argparse
import json
from .service import default_state

def main():
    parser = argparse.ArgumentParser(description="HarmonyOS local agent runtime")
    parser.add_argument("command", choices=["serve", "mcp", "doctor", "probe"])
    parser.add_argument("--state-dir", default=str(default_state()))
    args = parser.parse_args()
    if args.command == "serve":
        from .service import serve
        serve(args.state_dir)
    elif args.command == "mcp":
        from .mcp_server import run
        run(args.state_dir)
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
