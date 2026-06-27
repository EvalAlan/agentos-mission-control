#!/usr/bin/env python3
"""AgentOS task worker.

Polls AgentOS board.db for tasks explicitly moved to in_progress, claims one,
runs Hermes in the task's workspace, then marks done or returns to pending
with failure notes.

Stdlib only. One worker at a time via flock.
"""

from __future__ import annotations

import fcntl
import os
import re
import sqlite3
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOARD_DB = ROOT / "board.db"
LOG_PATH = ROOT / "agentos-task-worker.log"
LOCK_PATH = ROOT / ".agentos-task-worker.lock"
PID_PATH = ROOT / ".agentos-task-worker.pid"
POLL_SECONDS = int(os.environ.get("AGENTOS_TASK_WORKER_POLL_SECONDS", "10"))
MAX_OUTPUT_CHARS = int(os.environ.get("AGENTOS_TASK_WORKER_MAX_OUTPUT_CHARS", "6000"))
TASK_TIMEOUT_SECONDS = int(os.environ.get("AGENTOS_TASK_WORKER_TIMEOUT_SECONDS", "7200"))

CLAIM_RE = re.compile(r"AGENTOS_WORKER_CLAIM=([0-9TZ:._+-]+)")
WORKSPACE_RE = re.compile(r"Workspace:\s*([^\n(]+)?\s*\((/[^)]+)\)", re.IGNORECASE)

DEFAULT_WORKSPACES = {
    "sideband": Path.home() / "repos" / "sideband",
    "evilsdr": Path.home() / "repos" / "evilSDR",
    "sdr": Path.home() / "repos" / "evilSDR",
    "elemta": Path.home() / "repos" / "elemta",
    "mta": Path.home() / "repos" / "elemta",
    "agentos": Path.home() / "repos" / "agentos-mission-control",
    "infra": Path.home() / "repos" / "agentos-mission-control",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(msg: str) -> None:
    line = f"{utc_now()} {msg}\n"
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line)
    if sys.stdout.isatty():
        print(line, end="", flush=True)


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(BOARD_DB), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema() -> None:
    conn = connect()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                priority TEXT DEFAULT 'medium',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT
            )"""
        )
        conn.commit()
    finally:
        conn.close()


def workspace_for(task: Mapping[str, object]) -> Path:
    title = str(task["title"] or "")
    notes = str(task["notes"] or "")
    match = WORKSPACE_RE.search(notes)
    if match:
        return Path(match.group(2)).expanduser()

    haystack = f"{title}\n{notes}".lower()
    for key, path in DEFAULT_WORKSPACES.items():
        if key.lower() in haystack:
            return path
    return ROOT


def is_claimed(notes: str) -> bool:
    return bool(CLAIM_RE.search(notes or ""))


def _fetch_order_clause() -> str:
    return "ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, COALESCE(updated_at, created_at) ASC"


def _auto_pull_pending() -> dict[str, object] | None:
    """Pull the next unblocked pending task into in_progress."""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"""SELECT * FROM tasks
                WHERE status = 'pending'
                {_fetch_order_clause()}"""
        ).fetchall()
        task = None
        for row in rows:
            tid = row["id"]
            dep_rows = conn.execute(
                "SELECT depends_on FROM task_deps WHERE task_id = ?", (tid,)
            ).fetchall()
            deps = [d["depends_on"] for d in dep_rows]
            blocked = False
            for dep_id in deps:
                dep_status = conn.execute(
                    "SELECT status FROM tasks WHERE id = ?", (dep_id,)
                ).fetchone()
                if not dep_status or dep_status["status"] not in ("review", "done"):
                    blocked = True
                    break
            if not blocked:
                task = row
                break
        if task is None:
            conn.rollback()
            return None

        claim = f"AGENTOS_WORKER_CLAIM={utc_now()}"
        notes = (task["notes"] or "").rstrip()
        notes = f"{notes}\n\n{claim}\nWorker auto-pulled pending task.".strip()
        conn.execute(
            "UPDATE tasks SET status='in_progress', notes=?, updated_at=? WHERE id=?",
            (notes, utc_now(), task["id"]),
        )
        conn.commit()
        log(f"auto-pulled {task['id']}: {task['title']}")
        return dict(task)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_next() -> dict[str, object] | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            f"""SELECT * FROM tasks
               WHERE status = 'in_progress'
               {_fetch_order_clause()}"""
        ).fetchall()
        task = None
        for row in rows:
            if not is_claimed(row["notes"] or ""):
                task = row
                break
        if task is None:
            conn.rollback()
            # No unclaimed in_progress tasks — try to auto-pull from pending
            return _auto_pull_pending()

        claim = f"AGENTOS_WORKER_CLAIM={utc_now()}"
        notes = (task["notes"] or "").rstrip()
        notes = f"{notes}\n\n{claim}\nWorker picked up this task.".strip()
        conn.execute(
            "UPDATE tasks SET notes=?, updated_at=? WHERE id=?",
            (notes, utc_now(), task["id"]),
        )
        conn.commit()
        log(f"claimed {task['id']}: {task['title']}")
        return dict(task)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def append_result(task_id: str, status: str, heading: str, output: str) -> None:
    output = (output or "").strip()
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[-MAX_OUTPUT_CHARS:]
        output = "[truncated to tail]\n" + output

    stamp = utc_now()
    conn = connect()
    try:
        row = conn.execute("SELECT notes FROM tasks WHERE id=?", (task_id,)).fetchone()
        notes = row["notes"] if row else ""
        notes = f"{notes.rstrip()}\n\n{heading} at {stamp}:\n{output}".strip()
        conn.execute(
            "UPDATE tasks SET status=?, notes=?, updated_at=? WHERE id=?",
            (status, notes, stamp, task_id),
        )
        conn.commit()
    finally:
        conn.close()


def build_prompt(task: Mapping[str, object], workspace: Path) -> str:
    return f"""
You are an autonomous AgentOS worker processing a task Alan explicitly moved to in_progress.

Task ID: {task['id']}
Title: {task['title']}
Priority: {task.get('priority') or 'medium'}
Workspace: {workspace}

Notes:
{task.get('notes') or ''}

Rules:
- Work inside the workspace/repo above unless the task explicitly says otherwise.
- Inspect before editing. Do not guess architecture.
- Make the smallest useful change that advances the task.
- Run appropriate tests/checks. If the environment blocks validation, say exactly what blocked it.
- If code changed, leave the repo in a reviewable state. Commit only if the repo/user conventions clearly expect it.
- Do not modify AgentOS task automation or board.db from inside this worker run.
- Final response must include: changed files, tests/checks run, result, and any blockers.
""".strip()


def run_task(task: Mapping[str, object]) -> None:
    task_id = str(task["id"])
    workspace = workspace_for(task)
    if not workspace.exists():
        msg = f"Workspace does not exist: {workspace}"
        log(f"blocked {task_id}: {msg}")
        append_result(task_id, "pending", "WORKER BLOCKED", msg)
        return

    prompt = build_prompt(task, workspace)
    log(f"running {task_id} in {workspace}")
    env = os.environ.copy()
    env["HERMES_AGENTOS_WORKER"] = "1"
    env["HERMES_AGENTOS_TASK_ID"] = task_id

    try:
        proc = subprocess.run(
            ["hermes", "chat", "-q", prompt],
            cwd=str(workspace),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=TASK_TIMEOUT_SECONDS,
        )
        output = proc.stdout or ""
    except subprocess.TimeoutExpired as exc:
        raw_output = exc.stdout or ""
        if isinstance(raw_output, bytes):
            output = raw_output.decode(errors="replace")
        else:
            output = str(raw_output)
        output += f"\nTimed out after {TASK_TIMEOUT_SECONDS}s"
        log(f"timeout {task_id}")
        append_result(task_id, "pending", "WORKER FAILED", output)
        return
    except Exception as exc:
        output = f"Worker crashed before Hermes completed: {exc!r}"
        log(f"crash {task_id}: {exc!r}")
        append_result(task_id, "pending", "WORKER FAILED", output)
        return

    if proc.returncode == 0:
        log(f"completed {task_id}")
        append_result(task_id, "review", "WORKER COMPLETED", output)
    else:
        log(f"failed {task_id} rc={proc.returncode}")
        append_result(task_id, "pending", f"WORKER FAILED rc={proc.returncode}", output)


def main() -> int:
    ensure_schema()
    with LOCK_PATH.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another worker already running; exiting")
            return 0

        PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
        try:
            log(f"worker started; polling {BOARD_DB} every {POLL_SECONDS}s")
            while True:
                task = claim_next()
                if task:
                    run_task(task)
                else:
                    time.sleep(POLL_SECONDS)
        finally:
            try:
                if PID_PATH.exists() and PID_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
                    PID_PATH.unlink()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
