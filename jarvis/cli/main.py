"""Operator CLI. Talks to the hub over HTTP with the API key from .env —
same authenticated path as every other client, no local backdoors.

    jarvis keygen          generate JARVIS_API_KEY / JARVIS_MASTER_KEY into .env
    jarvis migrate         apply DB migrations
    jarvis serve           run the hub daemon (foreground; systemd calls this)
    jarvis status          hub + node health
    jarvis pause / resume  kill switch
    jarvis bus tail|publish
    jarvis audit tail|verify
"""

from __future__ import annotations

import argparse
import json
import os
import secrets as pysecrets
import stat
import sys
import time
from pathlib import Path

import httpx


def _hub_url() -> str:
    if url := os.environ.get("JARVIS_HUB_URL"):
        return url.rstrip("/")
    from jarvis.core.config import load_config

    cfg = load_config()
    return f"http://{cfg.hub.bind_host}:{cfg.hub.port}"


def _client() -> httpx.Client:
    from jarvis.core.config import Secrets

    key = Secrets().jarvis_api_key
    if not key:
        sys.exit("JARVIS_API_KEY not set (run `jarvis keygen`, or export it)")
    return httpx.Client(
        base_url=_hub_url(), headers={"x-api-key": key}, timeout=10
    )


def _print(data) -> None:
    print(json.dumps(data, indent=2, default=str))


# -- commands -----------------------------------------------------------------


def cmd_keygen(args) -> None:
    from jarvis.core.crypto import generate_master_key

    env_path = Path(args.env_file)
    example = Path(".env.example")
    if not env_path.exists():
        env_path.write_text(example.read_text() if example.exists() else "")
    content = env_path.read_text()

    def fill(content: str, key: str, value: str) -> tuple[str, bool]:
        for line in content.splitlines():
            if line.startswith(f"{key}=") and line.split("=", 1)[1].strip():
                if not args.force:
                    return content, False  # keep existing unless --force
        lines, replaced = [], False
        for line in content.splitlines():
            if line.startswith(f"{key}="):
                lines.append(f"{key}={value}")
                replaced = True
            else:
                lines.append(line)
        if not replaced:
            lines.append(f"{key}={value}")
        return "\n".join(lines) + "\n", True

    content, wrote_api = fill(content, "JARVIS_API_KEY", pysecrets.token_urlsafe(32))
    content, wrote_master = fill(content, "JARVIS_MASTER_KEY", generate_master_key())
    env_path.write_text(content)
    env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    print(f"JARVIS_API_KEY:    {'generated' if wrote_api else 'kept existing'}")
    print(f"JARVIS_MASTER_KEY: {'generated' if wrote_master else 'kept existing'}")
    print(f"written to {env_path} (mode 600)")
    if wrote_master:
        print("BACK UP JARVIS_MASTER_KEY off this machine — encrypted memories "
              "are unrecoverable without it.")


def cmd_migrate(args) -> None:
    from jarvis.core.config import load_config
    from jarvis.core.db import migrate

    cfg = load_config()
    applied = migrate(cfg.db_path)
    print(f"db: {cfg.db_path}")
    print(f"applied: {applied or 'nothing new'}")


def cmd_serve(args) -> None:
    from jarvis.hub.app import serve

    serve()


def cmd_status(args) -> None:
    with _client() as c:
        r = c.get("/health")
        r.raise_for_status()
        h = r.json()
    ks = h["killswitch"]["state"]
    print(f"JARVIS hub v{h['version']}  status={h['status']}  killswitch={ks}")
    hub = h["hub"]
    print(f"  hub: db={hub['db']} rss={hub['rss_mb']}MB "
          f"disk_free={hub['disk_free_gb']}GB bus_backlog={hub['bus_backlog']} "
          f"uptime={hub['uptime_s']}s")
    for name, s in h["nodes"].items():
        extra = f" latency={s.get('latency_ms')}ms models={len(s.get('models', []))}" \
            if s.get("status") == "up" else f" ({s.get('error', 'unprobed')})"
        print(f"  node {name}: {s.get('status')}{extra}")
    cloud = ", ".join(f"{k}={v}" for k, v in h["cloud"].items())
    print(f"  cloud: {cloud}")


def cmd_pause(args) -> None:
    with _client() as c:
        r = c.post("/control/pause", json={"reason": args.reason})
        r.raise_for_status()
        _print(r.json())


def cmd_resume(args) -> None:
    with _client() as c:
        r = c.post("/control/resume")
        r.raise_for_status()
        _print(r.json())


def cmd_bus(args) -> None:
    with _client() as c:
        if args.bus_cmd == "tail":
            r = c.get("/bus/tail", params={"topic": args.topic, "limit": args.limit}
                      if args.topic else {"limit": args.limit})
            r.raise_for_status()
            for m in r.json()["messages"]:
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m["ts"]))
                print(f"[{m['id']}] {ts} {m['topic']} {json.dumps(m['payload'])}")
        elif args.bus_cmd == "publish":
            r = c.post("/bus/publish",
                       json={"topic": args.topic, "payload": json.loads(args.payload)})
            r.raise_for_status()
            _print(r.json())


def cmd_audit(args) -> None:
    with _client() as c:
        if args.audit_cmd == "tail":
            r = c.get("/audit/tail", params={"limit": args.limit})
            r.raise_for_status()
            for e in r.json()["entries"]:
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e["ts"]))
                print(f"[{e['id']}] {ts} {e['actor']} {e['action']} -> {e['outcome']}")
        elif args.audit_cmd == "verify":
            r = c.get("/audit/verify")
            r.raise_for_status()
            v = r.json()
            if v["ok"]:
                print(f"audit chain OK ({v['entries']} entries)")
            else:
                sys.exit(f"AUDIT CHAIN BROKEN at entry {v['first_bad_id']} "
                         f"(verified {v['entries']} before break)")


def main() -> None:
    p = argparse.ArgumentParser(prog="jarvis", description="JARVIS operator CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    kg = sub.add_parser("keygen", help="generate secrets into .env")
    kg.add_argument("--env-file", default=".env")
    kg.add_argument("--force", action="store_true", help="overwrite existing keys")
    kg.set_defaults(fn=cmd_keygen)

    sub.add_parser("migrate", help="apply DB migrations").set_defaults(fn=cmd_migrate)
    sub.add_parser("serve", help="run the hub daemon").set_defaults(fn=cmd_serve)
    sub.add_parser("status", help="hub + node health").set_defaults(fn=cmd_status)

    pz = sub.add_parser("pause", help="engage kill switch")
    pz.add_argument("--reason", default="operator request")
    pz.set_defaults(fn=cmd_pause)
    sub.add_parser("resume", help="release kill switch").set_defaults(fn=cmd_resume)

    b = sub.add_parser("bus", help="message bus")
    bsub = b.add_subparsers(dest="bus_cmd", required=True)
    bt = bsub.add_parser("tail")
    bt.add_argument("topic", nargs="?", default=None)
    bt.add_argument("--limit", type=int, default=20)
    bp = bsub.add_parser("publish")
    bp.add_argument("topic")
    bp.add_argument("payload", help='JSON, e.g. \'{"k": "v"}\'')
    b.set_defaults(fn=cmd_bus)

    a = sub.add_parser("audit", help="audit trail")
    asub = a.add_subparsers(dest="audit_cmd", required=True)
    at = asub.add_parser("tail")
    at.add_argument("--limit", type=int, default=20)
    asub.add_parser("verify")
    a.set_defaults(fn=cmd_audit)

    args = p.parse_args()
    try:
        args.fn(args)
    except httpx.ConnectError:
        sys.exit(f"cannot reach hub at {_hub_url()} — is `jarvis serve` running?")
    except httpx.HTTPStatusError as exc:
        sys.exit(f"hub returned {exc.response.status_code}: {exc.response.text}")


if __name__ == "__main__":
    main()
