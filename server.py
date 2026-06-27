#!/usr/bin/env python3
"""Hermes AgentOS Mission Control Dashboard — Backend Server
Python stdlib only. Read-only connections to Hermes databases.
"""

import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
CONTENT_DIR = os.path.join(HERMES_HOME, "content")
PROJECT_DOC_SKIP_DIRS = {
    ".git", ".github", ".venv", "__pycache__", "node_modules", "dist",
    "build", "target", "graphify-out", "coverage", "backups",
}
DB_LOG = os.path.join(HERMES_HOME, "agent-logs.db")
DB_STATE = os.path.join(HERMES_HOME, "state.db")
DB_KANBAN = os.path.join(HERMES_HOME, "kanban.db")
GATEWAY_JSON = os.path.join(HERMES_HOME, "gateway_state.json")
BOARD_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "board.db")
SYDNEY_AVATAR = os.path.expanduser("~/workspace/avatars/sydney.jpg")


def init_board_db():
    conn = sqlite3.connect(BOARD_DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        priority TEXT DEFAULT 'medium',
        notes TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT
    )""")
    count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    if count == 0:
        now = datetime.now(timezone.utc).isoformat()
        seeds = [
            ("infra-agentos", "Finish AgentOS dashboard wiring", "in_progress", "high", "Backed by Hermes sessions, ~/repos, and Tailscale Serve", now),
            ("evilhotkeys-gw2", "evilhotkeys GW2 spec maintenance", "pending", "medium", "Pixel-state debugging, fishing/manual pool workflow, mechanist/untamed specs", now),
            ("elemta-work", "Elemta MTA backlog", "pending", "medium", "Go MTA, observability, LDAP, queue/security work", now),
            ("evilsdr-work", "evilSDR/Skywarn backlog", "pending", "medium", "SDR tooling, scan hits, GUI polish", now),
        ]
        conn.executemany("INSERT INTO tasks (id, title, status, priority, notes, created_at) VALUES (?,?,?,?,?,?)", seeds)
    conn.commit()
    conn.close()


def safe_read_db(db_path):
    """Read-only SQLite connection with mode=ro."""
    if not os.path.exists(db_path):
        return None
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.execute("PRAGMA query_only=1")
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def gateway_data():
    if not os.path.exists(GATEWAY_JSON):
        return {"state": "unknown", "platforms": [], "active_agents": 0, "uptime": "N/A"}
    try:
        with open(GATEWAY_JSON) as f:
            data = json.load(f)
        pid = data.get("pid")
        uptime = "N/A"
        if pid:
            try:
                elapsed = _process_elapsed_seconds(int(pid))
                if elapsed is not None:
                    uptime = _format_duration(elapsed)
            except Exception:
                pass
        return {
            "state": data.get("gateway_state", "unknown"),
            "platforms": data.get("platforms", []),
            "active_agents": data.get("active_agents", 0) if isinstance(data.get("active_agents"), int) else len(data.get("active_agents", [])),
            "uptime": uptime,
        }
    except Exception:
        return {"state": "error", "platforms": [], "active_agents": 0, "uptime": "N/A"}


def _process_elapsed_seconds(pid: int):
    """Return elapsed seconds since the given PID started, or None."""
    try:
        stat_path = Path(f"/proc/{pid}/stat")
        if not stat_path.exists():
            return None
        start_ticks = int(stat_path.read_text(encoding="utf-8").split()[21])
        clk_tck = os.sysconf("SC_CLK_TCK")
        uptime_seconds = _uptime_seconds()
        if uptime_seconds is None:
            return None
        process_start_seconds = start_ticks / clk_tck
        elapsed = max(0, uptime_seconds - process_start_seconds)
        return elapsed
    except Exception:
        return None


def _uptime_seconds():
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return None


def _format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h {m}m"
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    return f"{d}d {h}h"


def activity_data():
    conn = safe_read_db(DB_LOG)
    if not conn:
        return {"entries": [], "agents": {}, "totals": {"total": 0, "completed": 0, "failed": 0}, "by_day": []}
    try:
        rows = conn.execute("SELECT * FROM agent_logs ORDER BY created_at DESC, id DESC LIMIT 50").fetchall()
        entries = [dict(r) for r in rows]

        agent_stats = {}
        for r in entries:
            name = r["agent_name"]
            if name not in agent_stats:
                agent_stats[name] = {"total": 0, "completed": 0, "failed": 0, "last_task": "", "last_seen": "", "model": ""}
            agent_stats[name]["total"] += 1
            if r["status"] == "completed":
                agent_stats[name]["completed"] += 1
            elif r["status"] == "failed":
                agent_stats[name]["failed"] += 1
            if not agent_stats[name]["last_task"]:
                agent_stats[name]["last_task"] = r["task_description"]
                agent_stats[name]["last_seen"] = r["created_at"]
                agent_stats[name]["model"] = r.get("model_used", "")

        totals = {"total": len(entries), "completed": sum(1 for e in entries if e["status"] == "completed"), "failed": sum(1 for e in entries if e["status"] == "failed")}

        # 7-day breakdown
        day_counts = {}
        for e in entries:
            try:
                day = e["created_at"][:10]
                day_counts[day] = day_counts.get(day, 0) + 1
            except Exception:
                pass
        by_day = [{"date": d, "total": c} for d, c in sorted(day_counts.items())[-7:]]

        conn.close()
        return {"entries": entries, "agents": agent_stats, "totals": totals, "by_day": by_day}
    except Exception:
        conn.close()
        return {"entries": [], "agents": {}, "totals": {"total": 0, "completed": 0, "failed": 0}, "by_day": []}


def sessions_data(days=None):
    conn = safe_read_db(DB_STATE)
    if not conn:
        return {"count": 0, "messages": 0, "tokens": {"input": 0, "output": 0, "cache": 0}, "recent": [], "model_breakdown": {}}
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

        count = 0
        messages = 0
        tokens = {"input": 0, "output": 0, "cache": 0}
        recent = []

        if "sessions" in tables:
            where = ""
            params = []
            if days:
                cutoff = time.time() - (days * 86400)
                where = "WHERE started_at >= ?"
                params = [cutoff]
            count = conn.execute(f"SELECT COUNT(*) FROM sessions {where}", params).fetchone()[0]
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
            has_input = "input_tokens" in cols
            has_output = "output_tokens" in cols
            if has_input or has_output:
                row = conn.execute(
                    f"SELECT SUM({('input_tokens' if has_input else '0')}), SUM({('output_tokens' if has_output else '0')}) FROM sessions {where}",
                    params
                ).fetchone()
                tokens = {
                    "input": row[0] or 0 if has_input else 0,
                    "output": row[1] or 0 if has_output else 0,
                    "cache": 0,
                }
            wanted = [
                "id", "source", "model", "started_at", "ended_at", "end_reason",
                "message_count", "tool_call_count", "input_tokens", "output_tokens",
                "cache_read_tokens", "reasoning_tokens", "title", "api_call_count",
            ]
            safe_cols = [c for c in wanted if c in cols]
            if safe_cols:
                col_sql = ", ".join(safe_cols)
                recent = [dict(r) for r in conn.execute(f"SELECT {col_sql} FROM sessions ORDER BY rowid DESC LIMIT 25").fetchall()]
            else:
                recent = []

            model_breakdown = {}
            if "model" in cols:
                for row in conn.execute("SELECT model, COUNT(*) FROM sessions GROUP BY model ORDER BY COUNT(*) DESC").fetchall():
                    model_breakdown[row[0] or "unknown"] = row[1]

        if "messages" in tables:
            messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

        if "token_usage" in tables:
            row = conn.execute("SELECT SUM(input_tokens), SUM(output_tokens), SUM(cache_tokens) FROM token_usage").fetchone()
            if row:
                tokens = {"input": row[0] or 0, "output": row[1] or 0, "cache": row[2] or 0}

        conn.close()
        return {"count": count, "messages": messages, "tokens": tokens, "recent": recent, "model_breakdown": model_breakdown}
    except Exception:
        conn.close()
        return {"count": 0, "messages": 0, "tokens": {"input": 0, "output": 0, "cache": 0}, "recent": [], "model_breakdown": {}}


def vps_health():
    result = {"cpu": 0, "ram_used": 0, "ram_total": 0, "ram_pct": 0, "disk_used": 0, "disk_total": 0, "disk_pct": 0}
    try:
        # CPU
        def read_stat():
            with open("/proc/stat") as f:
                parts = f.readline().split()
            return sum(int(p) for p in parts[1:5])

        t1 = read_stat()
        idle1 = int(open("/proc/stat").readline().split()[4])
        time.sleep(0.1)
        t2 = read_stat()
        idle2 = int(open("/proc/stat").readline().split()[4])
        total = t2 - t1
        idle = idle2 - idle1
        result["cpu"] = round((1 - idle / max(total, 1)) * 100, 1)

        # RAM
        with open("/proc/meminfo") as f:
            mem = {}
            for line in f:
                parts = line.split()
                if parts[0] in ("MemTotal:", "MemAvailable:"):
                    mem[parts[0]] = int(parts[1]) * 1024
        total_ram = mem.get("MemTotal:", 1)
        avail_ram = mem.get("MemAvailable:", total_ram)
        result["ram_total"] = total_ram
        result["ram_used"] = total_ram - avail_ram
        result["ram_pct"] = round((1 - avail_ram / total_ram) * 100, 1)

        # Disk
        st = os.statvfs("/")
        result["disk_total"] = st.f_blocks * st.f_frsize
        result["disk_used"] = (st.f_blocks - st.f_bfree) * st.f_frsize
        result["disk_pct"] = round(result["disk_used"] / max(result["disk_total"], 1) * 100, 1)
    except Exception:
        pass
    return result


def hermes_cron_jobs():
    jobs = []
    jobs_file = os.path.join(HERMES_HOME, "cron", "jobs.json")
    if not os.path.isfile(jobs_file):
        return jobs
    try:
        with open(jobs_file) as f:
            data = json.load(f)
    except Exception:
        return jobs

    for job in data.get("jobs", []):
        name = job.get("name") or job.get("id") or "Hermes job"
        schedule = job.get("schedule_display") or ""
        if not schedule:
            raw_schedule = job.get("schedule") or {}
            if isinstance(raw_schedule, dict):
                schedule = raw_schedule.get("display") or raw_schedule.get("expr") or raw_schedule.get("run_at") or ""
            else:
                schedule = str(raw_schedule)
        mode = "script-only" if job.get("no_agent") else "agent"
        script = job.get("script")
        command = name
        if script:
            command = f"{name} — {script}"
        status = "active" if job.get("enabled", True) else "paused"
        if job.get("state") and job.get("state") != "scheduled":
            status = str(job.get("state"))
        next_run = job.get("next_run_at") or "not scheduled"
        repeat = job.get("repeat") or {}
        repeat_text = "forever" if repeat.get("times") is None else f"{repeat.get('completed', 0)}/{repeat.get('times')}"
        description = f"{status}; next {next_run}; {mode}; repeat {repeat_text}"
        if job.get("last_status"):
            description += f"; last {job.get('last_status')}"

        jobs.append({
            "schedule": schedule,
            "command": command,
            "owner": "hermes",
            "description": description,
            "source": f"{jobs_file}#{job.get('id', '')}",
            "job_id": job.get("id"),
            "enabled": job.get("enabled", True),
            "state": job.get("state"),
            "next_run_at": next_run,
        })
    return jobs


def cron_jobs():
    jobs = hermes_cron_jobs()
    crontab_files = [
        "/var/spool/cron/crontabs/root",
        "/etc/crontab",
    ]
    cron_d = "/etc/cron.d"

    def parse_line(line, source, is_system=False):
        line = line.strip()
        if not line or line.startswith("#"):
            return None
        parts = line.split()
        if len(parts) < 6:
            return None
        if is_system:
            schedule = " ".join(parts[:5])
            owner = parts[5] if len(parts) > 5 else "unknown"
            command = " ".join(parts[6:])
        else:
            schedule = " ".join(parts[:5])
            owner = "hermes"
            command = " ".join(parts[5:])

        # Plain English schedule
        desc = schedule
        try:
            fields = schedule.split()
            if len(fields) == 5:
                min_f, hr_f, dom_f, mon_f, dow_f = fields
                if hr_f.isdigit() and min_f.isdigit():
                    desc = f"Every day at {hr_f.zfill(2)}:{min_f.zfill(2)}"
                elif min_f == "0" and hr_f == "*/1":
                    desc = "Every hour"
                elif min_f.startswith("*/"):
                    desc = f"Every {min_f[2:]} minutes"
                else:
                    desc = f"Cron: {schedule}"
        except Exception:
            pass

        return {"schedule": schedule, "command": command, "owner": owner, "description": desc, "source": source}

    for fpath in crontab_files:
        if os.path.isfile(fpath):
            try:
                with open(fpath) as f:
                    for line in f:
                        job = parse_line(line, fpath, is_system=(fpath != "/var/spool/cron/crontabs/root"))
                        if job:
                            jobs.append(job)
            except Exception:
                pass

    if os.path.isdir(cron_d):
        for fname in os.listdir(cron_d):
            fpath = os.path.join(cron_d, fname)
            if os.path.isfile(fpath):
                try:
                    with open(fpath) as f:
                        for line in f:
                            job = parse_line(line, fpath, is_system=True)
                            if job:
                                jobs.append(job)
                except Exception:
                    pass

    return jobs



def _run_git(repo, args):
    if not repo or not os.path.isdir(repo):
        return ""
    try:
        return subprocess.check_output(["git", "-C", repo, *args], text=True, stderr=subprocess.DEVNULL, timeout=2).strip()
    except Exception:
        return ""


def _repo_info(repo):
    exists = bool(repo and os.path.isdir(repo))
    info = {
        "path": repo,
        "exists": exists,
        "branch": "",
        "dirty": False,
        "last_commit": "",
        "last_commit_ts": None,
        "language": "",
    }
    if not exists:
        return info

    info["branch"] = _run_git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    info["dirty"] = bool(_run_git(repo, ["status", "--porcelain"]))
    info["last_commit"] = _run_git(repo, ["log", "-1", "--pretty=%h %s"])
    ts = _run_git(repo, ["log", "-1", "--pretty=%ct"])
    if ts.isdigit():
        info["last_commit_ts"] = int(ts)

    markers = [
        ("go.mod", "Go"),
        ("Cargo.toml", "Rust"),
        ("package.json", "Node"),
        ("pyproject.toml", "Python"),
        ("pubspec.yaml", "Flutter/Dart"),
    ]
    for marker, lang in markers:
        if os.path.exists(os.path.join(repo, marker)):
            info["language"] = lang
            break
    return info


def _markdown_title(fpath):
    try:
        with open(fpath, encoding="utf-8", errors="replace") as f:
            for _ in range(16):
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if line.startswith("#"):
                    title = line.lstrip("#").strip()
                    if title:
                        return title
    except Exception:
        pass
    name = os.path.splitext(os.path.basename(fpath))[0]
    return "README" if name.lower() == "readme" else name


def _markdown_docs_in_root(root, group, group_key, prefix, editable):
    docs = []
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        return docs

    for current_root, dirs, files in os.walk(root):
        rel_root = os.path.relpath(current_root, root)
        depth = 0 if rel_root == "." else rel_root.count(os.sep) + 1
        dirs[:] = [d for d in dirs if d not in PROJECT_DOC_SKIP_DIRS and not d.startswith(".")]
        if depth >= 4:
            dirs[:] = []

        for fname in files:
            if not fname.lower().endswith(".md"):
                continue
            fpath = os.path.join(current_root, fname)
            rel_path = os.path.relpath(fpath, root)
            try:
                stat = os.stat(fpath)
            except Exception:
                continue
            docs.append({
                "group": group,
                "group_key": group_key,
                "path": f"{prefix}/{rel_path}".replace("\\", "/"),
                "filename": rel_path.replace("\\", "/"),
                "title": _markdown_title(fpath),
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                "size": stat.st_size,
                "editable": editable,
            })
    return docs


def _content_docs():
    docs = []
    if os.path.isdir(CONTENT_DIR):
        for agent_dir in sorted(os.listdir(CONTENT_DIR)):
            agent_path = os.path.join(CONTENT_DIR, agent_dir)
            if not os.path.isdir(agent_path):
                continue
            docs.extend(_markdown_docs_in_root(
                agent_path,
                group=agent_dir,
                group_key=agent_dir,
                prefix=f"content/{agent_dir}",
                editable=True,
            ))

    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspaces.json")
    try:
        with open(config_path) as f:
            config = json.load(f)
    except Exception:
        config = {"workspaces": []}

    for ws in config.get("workspaces", []):
        repo = ws.get("repo", "")
        docs.extend(_markdown_docs_in_root(
            repo,
            group=ws.get("name") or ws.get("label") or ws.get("key") or "workspace",
            group_key=ws.get("key") or ws.get("name") or ws.get("label") or "workspace",
            prefix=f"workspace/{ws.get('key') or ws.get('name') or 'workspace'}",
            editable=False,
        ))

    docs.sort(key=lambda d: ((d.get("group") or "").lower(), (d.get("title") or "").lower(), d.get("path") or ""))
    return docs


def _session_matches_workspace(row, terms):
    text = " ".join(str(row.get(k) or "") for k in ("title", "id", "source", "model")).lower()
    return any(term.lower() in text for term in terms)


def _task_matches_workspace(row, terms, repo_path=""):
    repo_name = os.path.basename(repo_path.rstrip("/")) if repo_path else ""
    text = " ".join(str(row.get(k) or "") for k in ("title", "notes", "status")).lower()
    if repo_path:
        text += f" {repo_path.lower()} {repo_name.lower()}"
    return any(term and term.lower() in text for term in terms)


def _board_tasks():
    conn = safe_read_db(BOARD_DB)
    if not conn:
        return []
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, title, status, priority, notes, created_at, updated_at FROM tasks ORDER BY COALESCE(updated_at, created_at) DESC"
        ).fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def _resolve_content_request(requested):
    requested = (requested or "").replace("\\", "/")
    if requested.startswith("content/"):
        rel = requested[len("content/"):]
        agent_dir, _, rel_path = rel.partition("/")
        if not agent_dir or not rel_path:
            return None
        return {
            "base_dir": os.path.join(CONTENT_DIR, agent_dir),
            "rel_path": rel_path,
            "editable": True,
        }

    if requested.startswith("workspace/"):
        rel = requested[len("workspace/"):]
        ws_key, _, rel_path = rel.partition("/")
        if not ws_key or not rel_path:
            return None
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspaces.json")
        try:
            with open(config_path) as f:
                config = json.load(f)
        except Exception:
            config = {"workspaces": []}
        for ws in config.get("workspaces", []):
            if ws.get("key") == ws_key:
                return {
                    "base_dir": ws.get("repo", ""),
                    "rel_path": rel_path,
                    "editable": False,
                }
    return None


def _safe_path_under(base_dir, rel_path):
    base_dir = os.path.abspath(base_dir)
    candidate = os.path.abspath(os.path.normpath(os.path.join(base_dir, rel_path)))
    if candidate != base_dir and not candidate.startswith(base_dir + os.sep):
        return None
    return candidate


def _safe_read_text(base_dir, rel_path):
    candidate = _safe_path_under(base_dir, rel_path)
    if not candidate or not os.path.isfile(candidate):
        return None
    with open(candidate, encoding="utf-8", errors="replace") as f:
        return f.read()


def _serve_content_asset(handler, requested, src):
    resolved = _resolve_content_request(requested)
    if not resolved:
        handler.send_json({"error": "Invalid document path"}, 403)
        return
    src = (src or "").strip().replace("\\", "/")
    if not src or src.startswith(("http://", "https://", "data:", "blob:")):
        handler.send_json({"error": "Unsupported asset path"}, 400)
        return
    doc_dir = os.path.dirname(resolved["rel_path"])
    asset_rel = os.path.normpath(os.path.join(doc_dir, src)).replace("\\", "/")
    asset_path = _safe_path_under(resolved["base_dir"], asset_rel)
    if not asset_path or not os.path.isfile(asset_path):
        handler.send_json({"error": "Asset not found"}, 404)
        return
    mime, _ = mimetypes.guess_type(asset_path)
    handler._serve_static(asset_path, mime or "application/octet-stream")


def _looks_like_session_id(value):
    text = str(value or "").strip()
    return bool(re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]+", text))


def workspace_data():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspaces.json")
    try:
        with open(config_path) as f:
            config = json.load(f)
    except Exception:
        config = {"workspaces": []}

    sessions = []
    tasks = _board_tasks()
    conn = safe_read_db(DB_STATE)
    if conn:
        try:
            sessions = [dict(r) for r in conn.execute("""
                SELECT id, source, model, started_at, ended_at, message_count, tool_call_count,
                       input_tokens, output_tokens, cache_read_tokens, title
                FROM sessions
                WHERE source IN ('discord', 'cli', 'cron')
                ORDER BY started_at DESC
                LIMIT 400
            """).fetchall()]
        except Exception:
            sessions = []
        finally:
            conn.close()

    result = []
    now = time.time()
    for ws in config.get("workspaces", []):
        repo_path = ws.get("repo", "")
        repo_name = os.path.basename(str(repo_path).rstrip("/")) if repo_path else ""
        terms = ws.get("terms", []) + [ws.get("key", ""), ws.get("name", ""), repo_name]
        matched = [s for s in sessions if _session_matches_workspace(s, terms)]
        active_tasks = [t for t in tasks if (t.get("status") == "in_progress") and _task_matches_workspace(t, terms, repo_path)]
        latest = matched[0] if matched else None
        repo = _repo_info(repo_path)
        active_task_titles = [str(t.get("title") or "").strip() for t in active_tasks if str(t.get("title") or "").strip()]
        active_task_label = f"{ws.get('name')}: {active_task_titles[0]}" if active_task_titles else ""
        latest_title = str((latest or {}).get("title") or "").strip()
        latest_display_title = latest_title
        if _looks_like_session_id(latest_title) and active_task_label:
            latest_display_title = active_task_label
        activity_label = active_task_label or latest_display_title or ws.get("name") or ws.get("key") or ""
        task_activity = 0.0
        for t in active_tasks:
            stamp = t.get("updated_at") or t.get("created_at")
            if not stamp:
                continue
            try:
                task_activity = max(task_activity, datetime.fromisoformat(str(stamp)).timestamp())
            except Exception:
                pass
        last_activity = max([v for v in [latest.get("started_at") if latest else None, task_activity or None, repo.get("last_commit_ts")] if v is not None], default=None)
        stale_days = None
        if last_activity:
            stale_days = round((now - float(last_activity)) / 86400, 1)
        result.append({
            **ws,
            "repo_info": repo,
            "sessions": len(matched),
            "active_tasks": len(active_tasks),
            "active_task_titles": active_task_titles,
            "active_task_label": active_task_label,
            "activity_label": activity_label,
            "messages": sum(int(s.get("message_count") or 0) for s in matched),
            "tools": sum(int(s.get("tool_call_count") or 0) for s in matched),
            "tokens": {
                "input": sum(int(s.get("input_tokens") or 0) for s in matched),
                "output": sum(int(s.get("output_tokens") or 0) for s in matched),
                "cache": sum(int(s.get("cache_read_tokens") or 0) for s in matched),
            },
            "latest_session": latest,
            "last_activity": last_activity,
            "stale_days": stale_days,
        })
    return result

def get_snapshot(days=None):
    return {
        "gateway": gateway_data(),
        "activity": activity_data(),
        "sessions": sessions_data(days=days),
        "workspaces": workspace_data(),
        "vps": vps_health(),
        "crons": cron_jobs(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default logging

    def _normalize_path(self, path: str) -> str:
        if path == "/agentos":
            return "/"
        if path.startswith("/agentos/"):
            stripped = path[len("/agentos"):]
            return stripped or "/"
        return path

    def send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve_static(self, path, content_type):
        if os.path.exists(path):
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        else:
            self.send_error(404, "Not Found")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = self._normalize_path(parsed.path)
        params = parse_qs(parsed.query)

        if path == "/":
            index = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
            self._serve_static(index, "text/html; charset=utf-8")

        elif path == "/tokens.css":
            css = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokens.css")
            self._serve_static(css, "text/css; charset=utf-8")

        elif path == "/components.js":
            js = os.path.join(os.path.dirname(os.path.abspath(__file__)), "components.js")
            self._serve_static(js, "application/javascript; charset=utf-8")

        elif path == "/avatar/sydney.jpg":
            self._serve_static(SYDNEY_AVATAR, "image/jpeg")

        elif path == "/api/snapshot":
            params = parse_qs(urlparse(self.path).query)
            days = None
            if "tf" in params:
                try:
                    days = int(params["tf"][0])
                except (ValueError, IndexError):
                    days = None
            self.send_json(get_snapshot(days=days))

        elif path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    data = json.dumps(get_snapshot(), default=str)
                    self.wfile.write(f"data: {data}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(5)
            except Exception:
                pass

        elif path == "/api/board":
            conn = sqlite3.connect(BOARD_DB)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
            conn.close()
            self.send_json([dict(r) for r in rows])

        elif path == "/api/content":
            self.send_json(_content_docs())

        elif path == "/api/content/get":
            requested = params.get("path", [""])[0]
            if not requested:
                self.send_json({"error": "Missing path parameter"}, 400)
                return
            resolved = _resolve_content_request(requested)
            if not resolved:
                self.send_json({"error": "File not found"}, 404)
                return
            content = _safe_read_text(resolved["base_dir"], resolved["rel_path"])
            if content is None:
                self.send_json({"error": "File not found"}, 404)
                return
            self.send_json({"content": content, "path": requested, "editable": bool(resolved["editable"])})
            return

        elif path == "/api/content/asset":
            requested = params.get("path", [""])[0]
            src = params.get("src", [""])[0]
            _serve_content_asset(self, requested, src)
            return

        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = self._normalize_path(parsed.path)
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length)) if content_length > 0 else {}

        if path == "/api/board":
            task = {
                "id": str(uuid.uuid4()),
                "title": body.get("title", ""),
                "status": body.get("status", "pending"),
                "priority": body.get("priority", "medium"),
                "notes": body.get("notes", ""),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": None,
            }
            conn = sqlite3.connect(BOARD_DB)
            conn.execute("INSERT INTO tasks (id, title, status, priority, notes, created_at) VALUES (?,?,?,?,?,?)",
                (task["id"], task["title"], task["status"], task["priority"], task["notes"], task["created_at"]))
            conn.commit()
            conn.close()
            self.send_json(task, 201)

        elif path == "/api/board/update":
            task_id = parse_qs(parsed.query).get("id", [""])[0]
            if not task_id:
                self.send_json({"error": "Missing id"}, 400)
                return
            conn = sqlite3.connect(BOARD_DB)
            fields = []
            values = []
            for key in ("title", "status", "priority", "notes"):
                if key in body:
                    fields.append(f"{key} = ?")
                    values.append(body[key])
            if fields:
                fields.append("updated_at = ?")
                values.append(datetime.now(timezone.utc).isoformat())
                values.append(task_id)
                conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", values)
                conn.commit()
            conn.close()
            self.send_json({"ok": True})

        elif path == "/api/board/delete":
            task_id = parse_qs(parsed.query).get("id", [""])[0]
            if not task_id:
                self.send_json({"error": "Missing id"}, 400)
                return
            conn = sqlite3.connect(BOARD_DB)
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.commit()
            conn.close()
            self.send_json({"ok": True})

        elif path == "/api/content/save":
            requested = body.get("path", "")
            content = body.get("content", "")
            if not requested or not requested.startswith("content/"):
                self.send_json({"error": "Read-only document"}, 403)
                return
            requested = requested.replace("\\", "/")
            rel = requested[len("content/"):]
            agent_dir, _, rel_path = rel.partition("/")
            if not agent_dir or not rel_path:
                self.send_json({"error": "Invalid path"}, 403)
                return
            base_dir = os.path.abspath(os.path.join(CONTENT_DIR, agent_dir))
            os.makedirs(base_dir, exist_ok=True)
            candidate = os.path.abspath(os.path.normpath(os.path.join(base_dir, rel_path)))
            if candidate != base_dir and not candidate.startswith(base_dir + os.sep):
                self.send_json({"error": "Invalid path"}, 403)
                return
            os.makedirs(os.path.dirname(candidate), exist_ok=True)
            with open(candidate, "w", encoding="utf-8") as f:
                f.write(content)
            self.send_json({"ok": True})

        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


if __name__ == "__main__":
    init_board_db()
    os.makedirs(CONTENT_DIR, exist_ok=True)
    for agent in ["orchestrator", "analyst", "writer", "marketer", "coder"]:
        os.makedirs(os.path.join(CONTENT_DIR, agent), exist_ok=True)

    server = ThreadingHTTPServer(("0.0.0.0", 8888), DashboardHandler)
    print("Dashboard server running on http://0.0.0.0:8888")
    server.serve_forever()
