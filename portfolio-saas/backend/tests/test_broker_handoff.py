"""Exercise the queue handoff against two disposable Redis processes."""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
import redis


@pytest.mark.skipif(shutil.which("redis-server") is None, reason="redis-server unavailable")
def test_copy_preserves_source_and_refuses_duplicate_or_inflight_work():
    script = Path(__file__).resolve().parents[2] / "scripts" / "copy_celery_queues.py"
    processes = []
    with tempfile.TemporaryDirectory(prefix="broker-handoff-") as root:
        try:
            urls = []
            for name, db in (("old", 2), ("new", 0)):
                folder = Path(root, name)
                folder.mkdir()
                socket = folder / "r.sock"
                processes.append(subprocess.Popen([
                    "redis-server", "--port", "0", "--unixsocket", str(socket),
                    "--dir", str(folder), "--appendonly", "yes", "--save", "",
                    "--maxmemory-policy", "noeviction", "--loglevel", "warning",
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                url = f"unix://{socket}?db={db}"
                client = redis.Redis.from_url(url)
                for _ in range(50):
                    try:
                        if client.ping():
                            break
                    except redis.ConnectionError:
                        time.sleep(0.1)
                else:
                    pytest.fail("disposable Redis did not start")
                urls.append(url)

            source, target = map(redis.Redis.from_url, urls)
            source.rpush("archive", b"first\x00job", b"second-job")
            source.rpush("codal", b'{"headers":{"task":"extract"}}')
            source.rpush("temporary.reply.celery.pidbox", b"control-message")
            base = [sys.executable, str(script), "--source", urls[0], "--target", urls[1], "--copy"]
            receipt = Path(root, "receipt.json")
            subprocess.run([*base, "--receipt", str(receipt)], check=True, capture_output=True)

            assert target.lrange("archive", 0, -1) == source.lrange("archive", 0, -1)
            assert target.lrange("codal", 0, -1) == source.lrange("codal", 0, -1)
            assert source.exists("temporary.reply.celery.pidbox")
            assert not target.exists("temporary.reply.celery.pidbox")
            assert receipt.stat().st_mode & 0o777 == 0o600
            assert json.loads(receipt.read_text())["queues"]["archive"]["count"] == 2

            duplicate = subprocess.run(
                [*base, "--receipt", str(Path(root, "again.json"))], capture_output=True, text=True,
            )
            assert duplicate.returncode != 0
            assert "Target broker is not empty" in duplicate.stderr
            target.flushdb()  # Disposable target only.
            target.set("unexpected-result", "occupied")
            occupied = subprocess.run(
                [*base, "--receipt", str(Path(root, "occupied.json"))], capture_output=True, text=True,
            )
            assert occupied.returncode != 0
            assert "Target broker is not empty" in occupied.stderr
            target.flushdb()  # Disposable target only.
            source.hset("unacked", "x", "busy")
            inflight = subprocess.run(
                [*base, "--receipt", str(Path(root, "busy.json"))], capture_output=True, text=True,
            )
            assert inflight.returncode != 0
            assert "unacknowledged tasks" in inflight.stderr

            source.hdel("unacked", "x")
            target.flushdb()  # Disposable target only.
            spec = importlib.util.spec_from_file_location("broker_handoff", script)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            class ChangingSource:
                def __getattr__(self, name):
                    return getattr(source, name)

                def dump(self, key):
                    payload = source.dump(key)
                    if key == b"archive":
                        source.rpush(key, b"arrived-during-copy")
                    return payload

            with pytest.raises(ValueError, match="Queues changed"):
                module.copy_queues(ChangingSource(), target)
            assert source.lrange("archive", -1, -1) == [b"arrived-during-copy"]
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                process.wait(timeout=5)
