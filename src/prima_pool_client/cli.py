"""Client CLI: bootstrap an account/key, or run the worker agent."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys

from .agent import Agent
from .config import ClientConfig
from .rest import PoolClient
from .wireguard import generate_keypair


def _bootstrap(args: argparse.Namespace) -> int:
    """Register an account and create a worker key (one-time setup)."""
    client = PoolClient(args.pool_url)
    username = args.username or input("Username: ").strip()
    password = args.password or getpass.getpass("Password: ")

    try:
        account = client.register_account(username, password)
    except Exception as exc:  # noqa: BLE001
        # Account may already exist; try login.
        try:
            account = client.login(username, password)
        except Exception:  # noqa: BLE001
            print(f"error: {exc}", file=sys.stderr)
            return 1

    session = client.login(username, password)
    key = client.create_key(account["account_id"], args.name, "worker", session["session_token"])
    print(f"account_id: {account['account_id']}")
    print(f"worker key: {key['api_key']}")
    print("Save this key — it is shown only once.")
    client.close()
    return 0


def _run(args: argparse.Namespace) -> int:
    config = ClientConfig.load(args.config)
    if args.pool_url:
        config.pool_url = args.pool_url
    if args.api_key:
        config.api_key = args.api_key
    if not config.api_key:
        print("error: no API key. Run 'prima-pool-client bootstrap' first, or set PRIMA_POOL_API_KEY.", file=sys.stderr)
        return 1

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))
    agent = Agent(config)

    async def _main() -> None:
        try:
            await agent.run()
        except asyncio.CancelledError:
            pass
        finally:
            await agent.stop()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    return 0


def _genkey(args: argparse.Namespace) -> int:
    priv, pub = generate_keypair()
    print(f"private: {priv}")
    print(f"public:  {pub}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="prima-pool-client", description="prima-pool worker agent")
    sub = parser.add_subparsers(dest="command")

    b = sub.add_parser("bootstrap", help="Register an account and create a worker key")
    b.add_argument("--pool-url", default="http://127.0.0.1:8000")
    b.add_argument("--username")
    b.add_argument("--password")
    b.add_argument("--name", default="worker")
    b.set_defaults(func=_bootstrap)

    r = sub.add_parser("run", help="Run the worker agent")
    r.add_argument("--config", default=None)
    r.add_argument("--pool-url")
    r.add_argument("--api-key")
    r.add_argument("--log-level", default="info")
    r.set_defaults(func=_run)

    g = sub.add_parser("genkey", help="Generate a WireGuard keypair")
    g.set_defaults(func=_genkey)

    args = parser.parse_args()
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
