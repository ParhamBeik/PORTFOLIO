#!/usr/bin/env python3
"""Copy quiesced Celery Redis queues to a fresh durable broker.

This never deletes source keys or starts workers. Stop every old producer and
worker first, wait for unacked messages to reach zero, and keep the new workers
stopped until the receipt has been reviewed. A failed copy leaves the target
unusable for cutover; clear that isolated target and start over.
"""

import argparse
import hashlib
import json
import os
import struct
from datetime import datetime, timezone
from pathlib import Path

import redis

QUEUES = (b"live", b"archive", b"codal")
CONTROL_SUFFIX = b".reply.celery.pidbox"


def unacked(client):
    return client.hlen("unacked") + client.zcard("unacked_index")


def queue_snapshot(client, max_messages, max_bytes):
    other_lists = [
        key.decode("utf-8", "replace")
        for key in client.scan_iter()
        if client.type(key) == b"list" and key not in QUEUES
        and not key.endswith(CONTROL_SUFFIX)
    ]
    if other_lists:
        raise ValueError(f"Unexpected Redis lists; inspect before cutover: {other_lists}")
    result = {}
    total_count = total_bytes = 0
    for queue in QUEUES:
        if client.type(queue) not in (b"none", b"list"):
            raise ValueError(f"{queue.decode()} is not a Redis list")
        messages = client.lrange(queue, 0, -1)
        total_count += len(messages)
        total_bytes += sum(map(len, messages))
        if total_count > max_messages or total_bytes > max_bytes:
            raise ValueError("Queue snapshot exceeds the configured size ceiling")
        digest = hashlib.sha256()
        for message in messages:
            digest.update(struct.pack(">Q", len(message)))
            digest.update(message)
        result[queue.decode()] = {"count": len(messages), "bytes": sum(map(len, messages)), "sha256": digest.hexdigest()}
    return result


def copy_queues(source, target, *, max_messages=10000, max_bytes=64 * 1024 * 1024):
    source_id = (source.info("server")["run_id"], source.connection_pool.connection_kwargs["db"])
    target_id = (target.info("server")["run_id"], target.connection_pool.connection_kwargs["db"])
    if source_id == target_id:
        raise ValueError("Source and target are the same Redis database")
    if unacked(source):
        raise ValueError("Source has unacknowledged tasks; stop and drain workers first")
    if unacked(target):
        raise ValueError("Target has unacknowledged tasks")
    before = queue_snapshot(source, max_messages, max_bytes)
    if target.dbsize():
        raise ValueError("Target broker is not empty")
    if target.config_get("maxmemory-policy").get("maxmemory-policy") != "noeviction":
        raise ValueError("Target broker must use noeviction")
    if not target.info("persistence").get("aof_enabled"):
        raise ValueError("Target broker must have AOF enabled")

    for queue in QUEUES:
        if before[queue.decode()]["count"]:
            payload = source.dump(queue)
            if payload is None:
                raise ValueError("Source queue disappeared during copy")
            target.restore(queue, 0, payload)

    if unacked(source) or unacked(target):
        raise ValueError("Unacknowledged task appeared during copy; do not start target workers")
    after = queue_snapshot(source, max_messages, max_bytes)
    copied = queue_snapshot(target, max_messages, max_bytes)
    if before != after or before != copied:
        raise ValueError("Queues changed or copied bytes differ; do not start target workers")
    if not target.save():  # Verify a complete disk snapshot before issuing a receipt.
        raise ValueError("Target Redis did not confirm its disk snapshot")
    if (
        unacked(source) or unacked(target)
        or before != queue_snapshot(source, max_messages, max_bytes)
        or before != queue_snapshot(target, max_messages, max_bytes)
    ):
        raise ValueError("Queues changed after the disk snapshot; do not start target workers")
    return {
        "copied_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_redis_run_id": source_id[0],
        "target_redis_run_id": target_id[0],
        "queues": copied,
        "source_preserved": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=os.getenv("BROKER_SOURCE_URL"), help="Old Redis broker URL; prefer BROKER_SOURCE_URL for credentials")
    parser.add_argument("--target", default=os.getenv("BROKER_TARGET_URL"), help="Fresh Redis broker URL; prefer BROKER_TARGET_URL for credentials")
    parser.add_argument("--copy", action="store_true", help="Copy queue bytes after stopping producers and workers")
    parser.add_argument("--receipt", type=Path, help="New mode-0600 JSON receipt; required with --copy")
    args = parser.parse_args()
    if not args.source or not args.target:
        parser.error("source and target URLs are required")
    if args.copy and args.receipt is None:
        parser.error("--copy requires --receipt")
    if args.receipt is not None and args.receipt.exists():
        parser.error("receipt already exists; refusing to copy")
    source = redis.Redis.from_url(args.source, socket_connect_timeout=5, socket_timeout=30)
    target = redis.Redis.from_url(args.target, socket_connect_timeout=5, socket_timeout=30)
    if args.copy:
        receipt = copy_queues(source, target)
        output = json.dumps(receipt, sort_keys=True, indent=2) + "\n"
        fd = os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(output)
            file.flush()
            os.fsync(file.fileno())
        print(output, end="")
    else:
        print(json.dumps({
            "source_unacked": unacked(source),
            "target_unacked": unacked(target),
            "source_queues": queue_snapshot(source, 10000, 64 * 1024 * 1024),
            "target_queues": queue_snapshot(target, 10000, 64 * 1024 * 1024),
        }, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
