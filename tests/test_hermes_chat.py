import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("agentos_server", ROOT / "server.py")
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


def test_dashboard_chat_defaults_to_openrouter_nemotron():
    assert server.HERMES_CHAT_PROVIDER == "openrouter"
    assert server.HERMES_CHAT_MODEL == "nvidia/nemotron-3-ultra-550b-a55b:free"


class FakeProcess:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.returncode = returncode
        self.terminated = False

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return None if not self.terminated else self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


def test_hermes_chat_command_uses_absolute_cli_and_dashboard_model(monkeypatch):
    monkeypatch.setattr(server, "HERMES_CLI", "/home/rocky/.local/bin/hermes")
    monkeypatch.setattr(server, "HERMES_CHAT_PROVIDER", "openai-codex")
    monkeypatch.setattr(server, "HERMES_CHAT_MODEL", "gpt-5.6-sol")

    command = server.hermes_chat_command("hello")

    assert command == [
        "/home/rocky/.local/bin/hermes",
        "chat",
        "--provider",
        "openai-codex",
        "-m",
        "gpt-5.6-sol",
        "--source",
        "agentos-dashboard",
        "-q",
        "hello",
    ]


def test_iter_hermes_chat_streams_tool_progress_and_clean_final_response(monkeypatch):
    process = FakeProcess([
        "Query: inspect the host\n",
        "Initializing agent...\n",
        "  ┊ 💻 $         uptime  0.2s\n",
        "╭─ ⚕ Hermes ─────────────────────╮\n",
        "    Host is healthy.\n",
        "    Load is low.\n",
        "╰────────────────────────────────╯\n",
        "Session:        20260717_123456_abcdef\n",
    ])
    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(server, "HERMES_CLI", "/home/rocky/.local/bin/hermes")
    events = list(server.iter_hermes_chat("inspect the host", 30, popen_factory=fake_popen))

    assert events[0]["type"] == "start"
    assert any(
        event.get("type") == "progress" and event.get("message") == "💻 $ uptime 0.2s"
        for event in events
    )
    assert events[-1]["type"] == "done"
    assert events[-1]["ok"] is True
    assert events[-1]["response"] == "Host is healthy.\nLoad is low."
    assert events[-1]["session_id"] == "20260717_123456_abcdef"
    assert captured["command"][0] == "/home/rocky/.local/bin/hermes"
    assert captured["kwargs"]["stderr"] == server.subprocess.STDOUT


def test_extract_hermes_result_preserves_response_indentation():
    response, _ = server._extract_hermes_result([
        "╭─ ⚕ Hermes ─────────╮\n",
        "    Example:\n",
        "        def hello():\n",
        "            return 'hi'\n",
        "╰────────────────────╯\n",
    ])

    assert response == "Example:\n    def hello():\n        return 'hi'"


def test_ndjson_event_is_single_line_utf8_json():
    payload = server.encode_ndjson_event({"type": "progress", "message": "💻 uptime"})

    assert payload.endswith(b"\n")
    assert payload.count(b"\n") == 1
    assert server.json.loads(payload) == {"type": "progress", "message": "💻 uptime"}


def test_iter_hermes_chat_reports_missing_cli(monkeypatch):
    def missing_popen(*args, **kwargs):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(server, "HERMES_CLI", "/missing/hermes")
    events = list(server.iter_hermes_chat("hello", 30, popen_factory=missing_popen))

    assert events[-1] == {
        "type": "done",
        "ok": False,
        "error": "hermes CLI not found at /missing/hermes",
        "exit_code": 127,
    }
