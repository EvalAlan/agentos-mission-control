import http.client
import importlib.util
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("agentos_server", REPO / "server.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class AgentUpdateStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.old_db = server.AGENT_UPDATES_DB
        server.AGENT_UPDATES_DB = str(Path(self.tmp.name) / "agent-updates.db")
        server.init_agent_updates_db()

    def tearDown(self):
        server.AGENT_UPDATES_DB = self.old_db
        self.tmp.cleanup()

    def test_rejects_update_without_agent_id(self):
        with self.assertRaisesRegex(ValueError, "agent_id"):
            server.record_agent_update({"status": "running"})

    def test_records_usage_and_deduplicates_event_id(self):
        payload = {
            "event_id": "claude-session-1-final",
            "agent_id": "claude-code:mercury",
            "agent_name": "Claude Code on mercury",
            "agent_type": "claude-code",
            "event_type": "usage",
            "status": "completed",
            "task": "Add authenticated ingestion",
            "model": "claude-sonnet-4-6",
            "session_id": "session-1",
            "usage": {
                "input_tokens": 120,
                "output_tokens": 30,
                "cache_read_tokens": 50,
                "cost_usd": 0.012,
                "turns": 4,
                "duration_ms": 9000,
            },
            "metadata": {"repo": "agentos-mission-control"},
        }

        first = server.record_agent_update(payload)
        second = server.record_agent_update(payload)
        data = server.external_agents_data()

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(data["usage"]["input_tokens"], 120)
        self.assertEqual(data["usage"]["output_tokens"], 30)
        self.assertEqual(data["usage"]["events"], 1)
        self.assertEqual(data["agents"][0]["agent_id"], "claude-code:mercury")
        self.assertEqual(data["agents"][0]["metadata"]["repo"], "agentos-mission-control")

    def test_known_agents_are_not_lost_when_recent_event_limit_is_saturated(self):
        server.record_agent_update({
            "event_id": "mercury-old",
            "agent_id": "hermes:mercury",
            "status": "completed",
        })
        for index in range(101):
            server.record_agent_update({
                "event_id": f"docker01-{index}",
                "agent_id": "hermes:docker01",
                "status": "completed",
            })

        data = server.external_agents_data(limit=100)

        self.assertEqual(
            {agent["agent_id"] for agent in data["agents"]},
            {"hermes:docker01", "hermes:mercury"},
        )
        self.assertEqual(len(data["recent"]), 50)


class AgentUpdateHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.old_db = server.AGENT_UPDATES_DB
        self.old_token = server.AGENTOS_INGEST_TOKEN
        server.AGENT_UPDATES_DB = str(Path(self.tmp.name) / "agent-updates.db")
        server.AGENTOS_INGEST_TOKEN = "test-secret"
        server.init_agent_updates_db()
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.DashboardHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        server.AGENT_UPDATES_DB = self.old_db
        server.AGENTOS_INGEST_TOKEN = self.old_token
        self.tmp.cleanup()

    def request(self, method, path, payload=None, headers=None):
        body = json.dumps(payload).encode() if payload is not None else None
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=3)
        conn.request(method, path, body=body, headers=request_headers)
        response = conn.getresponse()
        data = json.loads(response.read().decode())
        conn.close()
        return response.status, data

    def test_ingest_requires_bearer_token_and_appears_in_snapshot(self):
        payload = {"agent_id": "hermes:docker01", "status": "running", "task": "Test ingestion"}

        status, data = self.request("POST", "/api/agents/update", payload)
        self.assertEqual(status, 401)
        self.assertIn("error", data)

        status, data = self.request(
            "POST",
            "/api/agents/update",
            payload,
            {"Authorization": "Bearer test-secret"},
        )
        self.assertEqual(status, 201)
        self.assertTrue(data["ok"])

        status, snapshot = self.request("GET", "/api/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["external_agents"]["agents"][0]["agent_id"], "hermes:docker01")


if __name__ == "__main__":
    unittest.main()
