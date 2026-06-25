#!/usr/bin/env python3
"""Hermes AgentOS Mission Control Dashboard — Backend Server
Python stdlib only. Read-only connections to Hermes databases.
"""

import json
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
DB_LOG = os.path.join(HERMES_HOME, "agent-logs.db")
DB_STATE = os.path.join(HERMES_HOME, "state.db")
DB_KANBAN = os.path.join(HERMES_HOME, "kanban.db")
GATEWAY_JSON = os.path.join(HERMES_HOME, "gateway_state.json")
BOARD_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "board.db")


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
        return {
            "state": data.get("state", "unknown"),
            "platforms": data.get("platforms", []),
            "active_agents": len(data.get("active_agents", [])),
            "uptime": data.get("uptime", "N/A"),
        }
    except Exception:
        return {"state": "error", "platforms": [], "active_agents": 0, "uptime": "N/A"}


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


def sessions_data():
    conn = safe_read_db(DB_STATE)
    if not conn:
        return {"count": 0, "messages": 0, "tokens": {"input": 0, "output": 0, "cache": 0}, "recent": []}
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

        count = 0
        messages = 0
        tokens = {"input": 0, "output": 0, "cache": 0}
        recent = []

        if "sessions" in tables:
            count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
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

        if "messages" in tables:
            messages = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

        if "token_usage" in tables:
            row = conn.execute("SELECT SUM(input_tokens), SUM(output_tokens), SUM(cache_tokens) FROM token_usage").fetchone()
            if row:
                tokens = {"input": row[0] or 0, "output": row[1] or 0, "cache": row[2] or 0}

        conn.close()
        return {"count": count, "messages": messages, "tokens": tokens, "recent": recent}
    except Exception:
        conn.close()
        return {"count": 0, "messages": 0, "tokens": {"input": 0, "output": 0, "cache": 0}, "recent": []}


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


def cron_jobs():
    jobs = []
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


def _session_matches_workspace(row, terms):
    text = " ".join(str(row.get(k) or "") for k in ("title", "id", "source", "model")).lower()
    return any(term.lower() in text for term in terms)


def workspace_data():
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workspaces.json")
    try:
        with open(config_path) as f:
            config = json.load(f)
    except Exception:
        config = {"workspaces": []}

    sessions = []
    conn = safe_read_db(DB_STATE)
    if conn:
        try:
            sessions = [dict(r) for r in conn.execute("""
                SELECT id, source, model, started_at, ended_at, message_count, tool_call_count,
                       input_tokens, output_tokens, cache_read_tokens, title
                FROM sessions
                WHERE source = 'discord'
                ORDER BY started_at DESC
                LIMIT 250
            """).fetchall()]
        except Exception:
            sessions = []
        finally:
            conn.close()

    result = []
    now = time.time()
    for ws in config.get("workspaces", []):
        terms = ws.get("terms", []) + [ws.get("key", ""), ws.get("name", "")]
        matched = [s for s in sessions if _session_matches_workspace(s, terms)]
        latest = matched[0] if matched else None
        repo = _repo_info(ws.get("repo", ""))
        last_activity = latest.get("started_at") if latest else repo.get("last_commit_ts")
        stale_days = None
        if last_activity:
            stale_days = round((now - float(last_activity)) / 86400, 1)
        result.append({
            **ws,
            "repo_info": repo,
            "sessions": len(matched),
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

def get_snapshot():
    return {
        "gateway": gateway_data(),
        "activity": activity_data(),
        "sessions": sessions_data(),
        "workspaces": workspace_data(),
        "vps": vps_health(),
        "crons": cron_jobs(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default logging

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
        path = parsed.path
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

        elif path == "/api/snapshot":
            self.send_json(get_snapshot())

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
            docs = []
            if os.path.isdir(CONTENT_DIR):
                for agent_dir in os.listdir(CONTENT_DIR):
                    agent_path = os.path.join(CONTENT_DIR, agent_dir)
                    if os.path.isdir(agent_path):
                        for fname in os.listdir(agent_path):
                            if fname.endswith(".md"):
                                fpath = os.path.join(agent_path, fname)
                                try:
                                    with open(fpath) as f:
                                        first_line = f.readline().strip()
                                    title = first_line.lstrip("# ").strip() if first_line.startswith("#") else fname
                                    stat = os.stat(fpath)
                                    docs.append({
                                        "agent": agent_dir,
                                        "filename": fname,
                                        "title": title,
                                        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                                        "size": stat.st_size,
                                    })
                                except Exception:
                                    pass
            self.send_json(docs)

        elif path == "/api/content/get":
            filename = params.get("path", [""])[0]
            if not filename:
                self.send_json({"error": "Missing path parameter"}, 400)
                return
            # Validate: no traversal
            if ".." in filename or "/" in filename:
                self.send_json({"error": "Invalid path"}, 403)
                return
            # Search all agent dirs
            found = False
            for agent_dir in os.listdir(CONTENT_DIR) if os.path.isdir(CONTENT_DIR) else []:
                fpath = os.path.join(CONTENT_DIR, agent_dir, filename)
                if os.path.isfile(fpath):
                    with open(fpath) as f:
                        content = f.read()
                    self.send_json({"content": content, "agent": agent_dir, "filename": filename})
                    found = True
                    break
            if not found:
                self.send_json({"error": "File not found"}, 404)

        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
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
            filename = body.get("path", "")
            content = body.get("content", "")
            if not filename or ".." in filename or "/" in filename:
                self.send_json({"error": "Invalid path"}, 403)
                return
            # Save to first agent dir found, or create new
            saved = False
            if os.path.isdir(CONTENT_DIR):
                for agent_dir in os.listdir(CONTENT_DIR):
                    fpath = os.path.join(CONTENT_DIR, agent_dir, filename)
                    if os.path.isfile(fpath):
                        with open(fpath, "w") as f:
                            f.write(content)
                        saved = True
                        break
            if not saved:
                # Default to writer
                fpath = os.path.join(CONTENT_DIR, "writer", filename)
                os.makedirs(os.path.dirname(fpath), exist_ok=True)
                with open(fpath, "w") as f:
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
