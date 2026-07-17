#!/usr/bin/env python3
"""Report Hermes/Claude Code status and usage to AgentOS Mission Control."""

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
import uuid


DEFAULT_URL = "https://docker01.tailfd29f7.ts.net/api/agents/update"


def payload_from_claude_result(result, agent_id, agent_name="", task=""):
    usage = result.get("usage") or {}
    models = result.get("modelUsage") or {}
    model = next(iter(models), "")
    session_id = str(result.get("session_id") or "")
    subtype = str(result.get("subtype") or "")
    successful = subtype == "success"
    return {
        "event_id": f"{session_id}:final" if session_id else str(uuid.uuid4()),
        "agent_id": agent_id,
        "agent_name": agent_name or agent_id,
        "agent_type": "claude-code",
        "event_type": "usage",
        "status": "completed" if successful else "failed",
        "task": task,
        "model": model,
        "session_id": session_id,
        "usage": {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
            "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
            "cost_usd": result.get("total_cost_usd", 0),
            "turns": result.get("num_turns", 0),
            "duration_ms": result.get("duration_ms", 0),
        },
        "metadata": {
            "result_subtype": subtype,
            "stop_reason": result.get("stop_reason"),
            "terminal_reason": result.get("terminal_reason"),
        },
    }


def post_update(url, token, payload, timeout=15):
    if not token:
        raise ValueError("AGENTOS_INGEST_TOKEN is not set")
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "agentos-update-client/1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"AgentOS returned HTTP {exc.code}: {detail}") from exc


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=os.environ.get("AGENTOS_UPDATE_URL", DEFAULT_URL))
    parser.add_argument("--token", default=os.environ.get("AGENTOS_INGEST_TOKEN", ""))
    parser.add_argument("--agent-id", default=f"agent:{socket.gethostname()}")
    parser.add_argument("--agent-name", default="")
    parser.add_argument("--agent-type", default="hermes")
    parser.add_argument("--event-id", default="")
    parser.add_argument("--event-type", default="status")
    parser.add_argument("--status", default="running")
    parser.add_argument("--task", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--input-tokens", type=int, default=0)
    parser.add_argument("--output-tokens", type=int, default=0)
    parser.add_argument("--cache-read-tokens", type=int, default=0)
    parser.add_argument("--cache-creation-tokens", type=int, default=0)
    parser.add_argument("--cost-usd", type=float, default=0)
    parser.add_argument("--turns", type=int, default=0)
    parser.add_argument("--duration-ms", type=int, default=0)
    parser.add_argument("--claude-result", help="Claude Code --output-format json result file, or - for stdin")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.claude_result:
        stream = sys.stdin if args.claude_result == "-" else open(args.claude_result, encoding="utf-8")
        try:
            payload = payload_from_claude_result(json.load(stream), args.agent_id, args.agent_name, args.task)
        finally:
            if stream is not sys.stdin:
                stream.close()
    else:
        payload = {
            "event_id": args.event_id or str(uuid.uuid4()),
            "agent_id": args.agent_id,
            "agent_name": args.agent_name or args.agent_id,
            "agent_type": args.agent_type,
            "event_type": args.event_type,
            "status": args.status,
            "task": args.task,
            "model": args.model,
            "session_id": args.session_id,
            "usage": {
                "input_tokens": args.input_tokens,
                "output_tokens": args.output_tokens,
                "cache_read_tokens": args.cache_read_tokens,
                "cache_creation_tokens": args.cache_creation_tokens,
                "cost_usd": args.cost_usd,
                "turns": args.turns,
                "duration_ms": args.duration_ms,
            },
            "metadata": {"hostname": socket.gethostname()},
        }
    status, response = post_update(args.url, args.token, payload)
    print(json.dumps({"http_status": status, **response}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError, urllib.error.URLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
