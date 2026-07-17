import importlib.util
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("agent_update_client", REPO / "scripts" / "report_agent_update.py")
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)


class ClaudeResultPayloadTests(unittest.TestCase):
    def test_maps_claude_print_json_to_agentos_contract(self):
        result = {
            "subtype": "success",
            "session_id": "abc-123",
            "num_turns": 6,
            "total_cost_usd": 0.123,
            "duration_ms": 4200,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 25,
                "cache_read_input_tokens": 70,
                "cache_creation_input_tokens": 9,
            },
            "modelUsage": {"claude-sonnet-4-6": {"costUSD": 0.123}},
        }

        payload = client.payload_from_claude_result(
            result,
            agent_id="claude-code:mercury",
            agent_name="Claude Code",
            task="Implement feature",
        )

        self.assertEqual(payload["event_id"], "abc-123:final")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["model"], "claude-sonnet-4-6")
        self.assertEqual(payload["usage"]["input_tokens"], 100)
        self.assertEqual(payload["usage"]["cache_read_tokens"], 70)
        self.assertEqual(payload["usage"]["turns"], 6)


if __name__ == "__main__":
    unittest.main()
