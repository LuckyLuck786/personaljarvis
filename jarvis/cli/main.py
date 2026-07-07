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


def cmd_chat(args) -> None:
    with _client() as c:
        r = c.post("/chat", json={"text": args.text, "session": "cli"},
                   timeout=300)  # local models on modest hardware can be slow
        r.raise_for_status()
        d = r.json()
    print(d["reply"])
    print(f"  ── tier={d['tier']} model={d['model']} {d['latency_ms']}ms "
          f"memories_used={d['memories_used']}", file=sys.stderr)


def cmd_remember(args) -> None:
    with _client() as c:
        r = c.post("/memory/ingest",
                   json={"text": args.text, "kind": "note", "source": "cli"},
                   timeout=120)
        r.raise_for_status()
        print(f"noted (doc {r.json()['doc_id']})")


def cmd_recall(args) -> None:
    with _client() as c:
        r = c.get("/memory/search", params={"q": args.query, "k": args.k}, timeout=120)
        r.raise_for_status()
        results = r.json()["results"]
    if not results:
        print("nothing in memory for that")
    for res in results:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(res["ts"]))
        print(f"[{res['score']:.4f}] ({res['kind']}/{res['source']} {ts}) {res['text'][:200]}")


def cmd_capture(args) -> None:
    from jarvis.core.config import Secrets

    key = Secrets().jarvis_api_key
    if not key:
        sys.exit("JARVIS_API_KEY not set")
    from jarvis.capture.runner import run_forever
    from jarvis.core.logging import setup_logging

    setup_logging()
    run_forever(_hub_url(), key, capture_config=args.config)


def cmd_timeline(args) -> None:
    with _client() as c:
        params = {"hours": args.hours, "limit": args.limit}
        if args.kind:
            params["kind"] = args.kind
        r = c.get("/timeline", params=params)
        r.raise_for_status()
        events = r.json()["events"]
    if not events:
        print("nothing captured in that window")
    for e in events:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["ts"]))
        preview = e["preview"].replace("\n", " ")[:120]
        print(f"{ts}  [{e['kind']}/{e['source']}]  {preview}")


def cmd_voice(args) -> None:
    from jarvis.core.config import Secrets
    from jarvis.interfaces.voice.loop import run_voice
    from jarvis.core.logging import setup_logging

    setup_logging()
    key = Secrets().jarvis_api_key
    run_voice(_hub_url(), key, tts_voice=args.tts_voice)


def cmd_voice_check(args) -> None:
    from jarvis.core.config import Secrets
    from jarvis.interfaces.voice.loop import VoiceLoop

    caps = VoiceLoop(_hub_url(), Secrets().jarvis_api_key,
                     tts_voice=args.tts_voice).capabilities()
    print("Voice capabilities (install '.[voice]' + Piper to enable all):")
    print(f"  {caps.summary()}")
    if not (caps.mic and caps.wakeword and caps.stt and caps.tts):
        print("  Fallbacks are active for anything 'off' — voice still usable "
              "in push-to-talk / text mode.")


def cmd_digest(args) -> None:
    with _client() as c:
        r = c.post(f"/proactive/run/{args.which}_digest", timeout=300)
        r.raise_for_status()
        print(r.json()["result"])


def cmd_jobs(args) -> None:
    with _client() as c:
        r = c.get("/proactive/jobs")
        r.raise_for_status()
        for j in r.json()["jobs"]:
            nxt = time.strftime("%Y-%m-%d %H:%M", time.localtime(j["next_run"]))
            last = j["last_status"] or "—"
            en = "on" if j["enabled"] else "off"
            print(f"{j['name']:24} {en:3} next={nxt}  last={last}")


def cmd_followups(args) -> None:
    with _client() as c:
        r = c.get("/followups")
        r.raise_for_status()
        fu = r.json()["followups"]
    if not fu:
        print("no open follow-ups")
    for f in fu:
        by = time.strftime("%Y-%m-%d %H:%M", time.localtime(f["by_ts"])) if f["by_ts"] else "—"
        print(f"#{f['id']} [{f['status']}] by {by}: {f['text']}")


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

    ch = sub.add_parser("chat", help="talk to JARVIS")
    ch.add_argument("text")
    ch.set_defaults(fn=cmd_chat)

    rm = sub.add_parser("remember", help="store a note in memory")
    rm.add_argument("text")
    rm.set_defaults(fn=cmd_remember)

    rc = sub.add_parser("recall", help="search memory")
    rc.add_argument("query")
    rc.add_argument("-k", type=int, default=6)
    rc.set_defaults(fn=cmd_recall)

    cap = sub.add_parser("capture", help="run capture collectors (foreground)")
    capsub = cap.add_subparsers(dest="capture_cmd", required=True)
    cr = capsub.add_parser("run")
    cr.add_argument("--config", default=None, help="capture.yaml path")
    cap.set_defaults(fn=cmd_capture)

    tl = sub.add_parser("timeline", help="what happened in the last N hours")
    tl.add_argument("--hours", type=float, default=24)
    tl.add_argument("--kind", default=None)
    tl.add_argument("--limit", type=int, default=100)
    tl.set_defaults(fn=cmd_timeline)

    vc = sub.add_parser("voice", help="run the voice loop (wake word/STT/TTS or push-to-talk)")
    vc.add_argument("--tts-voice", default=None, help="path to a Piper .onnx voice model")
    vc.set_defaults(fn=cmd_voice)

    vck = sub.add_parser("voice-check", help="report voice capabilities honestly")
    vck.add_argument("--tts-voice", default=None)
    vck.set_defaults(fn=cmd_voice_check)

    dg = sub.add_parser("digest", help="generate a digest now (morning|evening)")
    dg.add_argument("which", choices=["morning", "evening"], nargs="?", default="morning")
    dg.set_defaults(fn=cmd_digest)

    sub.add_parser("jobs", help="list scheduled proactive jobs").set_defaults(fn=cmd_jobs)
    sub.add_parser("followups", help="list open follow-ups").set_defaults(fn=cmd_followups)

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
