"""scripts/host_alert.py and the host cron scripts that call it.

The host checks run outside every container, when the app may be down, so they
carry their own sender. Before it, `ops_check.sh` read ALERT_WEBHOOK_URL from
cron's empty environment and `watchdog_prices.sh` only logged, so neither ever
reached anyone. These pin that the settings come from the env file, that the
channels are independent, and that a failure never breaks the calling script.
"""
import importlib.util
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load():
    spec = importlib.util.spec_from_file_location("host_alert", SCRIPTS / "host_alert.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host_alert = _load()


def test_env_file_values_keep_equals_signs_and_drop_quotes(tmp_path):
    env = tmp_path / "env"
    env.write_text(
        "# comment\n"
        "ALERT_WEBHOOK_URL=https://hooks.test/x?a=1&b=2\n"
        'ALERT_TELEGRAM_BOT_TOKEN="123:abc"\n'
        "ALERT_TELEGRAM_CHAT_ID='-100'\n"
    )
    config = host_alert.alert_config(str(env), environ={})
    assert config["ALERT_WEBHOOK_URL"] == "https://hooks.test/x?a=1&b=2"
    assert config["ALERT_TELEGRAM_BOT_TOKEN"] == "123:abc"
    assert config["ALERT_TELEGRAM_CHAT_ID"] == "-100"
    assert config["ALERT_TELEGRAM_API_BASE"] == ""


def test_exported_value_wins_and_missing_file_is_empty(tmp_path):
    config = host_alert.alert_config(str(tmp_path / "missing"), environ={"ALERT_WEBHOOK_URL": "https://x.test"})
    assert config["ALERT_WEBHOOK_URL"] == "https://x.test"
    assert config["ALERT_TELEGRAM_BOT_TOKEN"] == ""


def _config(**overrides):
    base = {key: "" for key in host_alert.KEYS}
    base.update(overrides)
    return base


def test_telegram_goes_to_bale_by_default_as_plain_text():
    with mock.patch.object(host_alert, "_post_json") as post:
        delivered, configured = host_alert.send(
            _config(ALERT_TELEGRAM_BOT_TOKEN="tok", ALERT_TELEGRAM_CHAT_ID="42"), "ev", {"detail": "d"}
        )
    assert (delivered, configured) == (True, True)
    url, body = post.call_args.args
    assert url == "https://tapi.bale.ai/bottok/sendMessage"
    assert body["chat_id"] == "42"
    assert body["text"] == "🔴 Ev\nd"
    assert "parse_mode" not in body


def test_channels_are_independent_and_errors_do_not_print_the_token(capsys):
    def post(url, body):
        if "hooks.test" in url:
            raise OSError("down")

    with mock.patch.object(host_alert, "_post_json", side_effect=post) as spy:
        delivered, _ = host_alert.send(
            _config(ALERT_WEBHOOK_URL="https://hooks.test", ALERT_TELEGRAM_BOT_TOKEN="sekret", ALERT_TELEGRAM_CHAT_ID="1"),
            "ev", {},
        )
    assert delivered is True
    assert spy.call_count == 2

    with mock.patch.object(host_alert, "_post_json", side_effect=OSError("https://x/botsekret/sendMessage")):
        delivered, configured = host_alert.send(_config(ALERT_TELEGRAM_BOT_TOKEN="sekret", ALERT_TELEGRAM_CHAT_ID="1"), "ev", {})
    assert (delivered, configured) == (False, True)
    assert "sekret" not in capsys.readouterr().err


def test_record_pushes_the_console_shape_and_trims():
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("input")))

    assert host_alert.record("c.yml", "env", "ev", {"detail": "d"}, True, run=run) is True
    push, trim = calls
    assert push[0][-4:] == ["1", "-x", "LPUSH", "ops:recent_alerts"]
    entry = json.loads(push[1])
    assert entry["event"] == "ev" and entry["source"] == "host" and entry["delivered"] is True
    assert trim[0][-4:] == ["LTRIM", "ops:recent_alerts", "0", "49"]


def test_main_exits_zero_when_everything_fails(tmp_path):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired("docker", 15)

    with mock.patch.object(host_alert.subprocess, "run", run):
        assert host_alert.main(["--env-file", str(tmp_path / "nope"), "--compose-file", "x", "ev", "d"]) == 0


class _Hook(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        _Hook.received.append(json.loads(self.rfile.read(length)))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.mark.skipif(not os.path.exists("/bin/bash"), reason="needs bash")
def test_ops_check_alerts_from_env_file_not_cron_environment(tmp_path):
    """The regression itself: the URL lives only in the env file, never exported."""
    server = HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Hook.received = []
    try:
        (tmp_path / "scripts").mkdir()
        for name in ("ops_check.sh", "host_alert.py"):
            (tmp_path / "scripts" / name).write_text((SCRIPTS / name).read_text())
        (tmp_path / "docker-compose.prod.yml").write_text("")
        env = tmp_path / "env"
        env.write_text(f"ALERT_WEBHOOK_URL=http://127.0.0.1:{server.server_port}/hook\nARCHIVE_WORKER_ENABLED=0\nCODAL_WORKER_ENABLED=0\n")
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        docker_log = tmp_path / "docker.log"
        (bin_dir / "docker").write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "$*" >> "{docker_log}"\n'
            'if [[ " $* " == *" ps "* ]]; then printf "db\\nredis\\nbroker\\nminio\\nbackend\\ncelery_worker_live\\nfrontend\\n"; fi\n'
        )
        (bin_dir / "docker").chmod(0o755)
        environ = {k: v for k, v in os.environ.items() if not k.startswith("ALERT_")}
        environ.update(
            PATH=f"{bin_dir}:{environ['PATH']}",
            PROJECT_DIR=str(tmp_path),
            ENV_FILE=str(env),
            DISK_USAGE_THRESHOLD_PERCENT="101",
            NO_PROXY="127.0.0.1",
            no_proxy="127.0.0.1",
        )
        result = subprocess.run(
            ["bash", str(tmp_path / "scripts" / "ops_check.sh")],
            env=environ, capture_output=True, text=True, timeout=60,
        )
    finally:
        server.shutdown()

    assert result.returncode == 1
    assert "service celery_beat is not running" in result.stderr
    assert [r["event"] for r in _Hook.received] == ["host-ops-check"]
    assert "celery_beat" in _Hook.received[0]["details"]["detail"]
    assert "LPUSH ops:recent_alerts" in docker_log.read_text()


def test_ops_check_failures_read_as_one_bullet_each():
    title, body = host_alert.format_text("host-ops-check", "disk usage 91%;service celery_beat is not running")
    assert title == "Server check failed"
    assert body == "• disk usage 91%\n• service celery_beat is not running"
