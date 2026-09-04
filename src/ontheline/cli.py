"""On the Line command-line entry points."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from ontheline.config import Settings
from ontheline.database import Store


def main() -> int:
    parser = argparse.ArgumentParser(prog="ontheline", description="On the Line community directory")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="initialize and seed the configured database")
    backup = subparsers.add_parser("backup", help="create an integrity-checked online backup")
    backup.add_argument("target", type=Path)
    subparsers.add_parser("audit-verify", help="verify the complete append-only audit chain")
    serve = subparsers.add_parser("serve", help="run the web application")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    args = parser.parse_args()
    settings = Settings()
    if args.command == "init":
        store = Store(settings.database_path)
        store.initialize()
        store.seed_demo(settings)
        print(f"On the Line database ready at {settings.database_path}")
        return 0
    if args.command == "backup":
        store = Store(settings.database_path)
        completed = store.backup_to(args.target)
        print(f"Verified backup written to {completed}")
        return 0
    if args.command == "audit-verify":
        valid = Store(settings.database_path).verify_audit_chain()
        print("Audit chain valid" if valid else "Audit chain verification failed")
        return 0 if valid else 1
    elif args.command == "serve":
        uvicorn.run("ontheline.api:app", host=args.host or settings.host, port=args.port or settings.port,
                    proxy_headers=True, forwarded_allow_ips=settings.forwarded_allow_ips)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
