#!/usr/bin/env python3
"""Dependency-light health probes used by the container image."""

from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from collections.abc import Callable
from typing import Protocol, cast


class _RedisHealthClient(Protocol):
    def ping(self) -> bool: ...


def check_api() -> None:
    port = os.environ.get("APP_PORT", "8000")
    url = f"http://127.0.0.1:{port}/api/v1/ready"
    with urllib.request.urlopen(url, timeout=3) as response:
        if response.status != 200:
            raise RuntimeError(f"API health returned HTTP {response.status}")


def check_redis() -> None:
    from redis import Redis

    url = os.environ.get("REDIS_URL")
    if not url:
        raise RuntimeError("REDIS_URL is not configured")
    factory = cast(
        Callable[..., _RedisHealthClient],
        Redis.from_url,  # pyright: ignore[reportUnknownMemberType]
    )
    client = factory(url, socket_connect_timeout=3, socket_timeout=3)
    if not client.ping():
        raise RuntimeError("Redis ping returned false")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", choices=("api", "redis"))
    target = parser.parse_args().target
    try:
        check_api() if target == "api" else check_redis()
    except Exception as exc:
        print(f"unhealthy: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
