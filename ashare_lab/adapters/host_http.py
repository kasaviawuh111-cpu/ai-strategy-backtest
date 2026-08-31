"""Process-local HTTPX connection reuse and per-host request spacing.

The reservation pattern is adapted from HKUDS/Vibe-Trading
``agent/backtest/loaders/_http.py`` at commit
``e90b6c6cd9fea23067a85667e7fbf74f9d73ea48`` (MIT).  This implementation is
deliberately transport-only: callers retain retry classification, status
handling, raw-response hashing, decoding, and provider schema validation.
"""

from __future__ import annotations

import atexit
import math
import random
import threading
import time
from collections.abc import Callable, Mapping

import httpx

VIBE_TRADING_SOURCE_COMMIT = "e90b6c6cd9fea23067a85667e7fbf74f9d73ea48"
VIBE_TRADING_SOURCE_PATH = "agent/backtest/loaders/_http.py"
_MAX_JITTER_SECONDS = 0.4
_STALE_SWEEP_INTERVAL_SECONDS = 60.0


def _default_jitter() -> float:
    return random.uniform(0.0, _MAX_JITTER_SECONDS)


class HostThrottle:
    """Reserve process-local request slots independently for each HTTP host."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._jitter = jitter or _default_jitter
        self._reservations: dict[str, tuple[float, float]] = {}
        self._last_sweep: float | None = None
        self._lock = threading.Lock()

    def wait(self, host: str, min_interval: float) -> None:
        """Wait for a reserved slot, chaining concurrent calls for one host."""

        normalized_host = _normalized_host(host)
        interval = _non_negative_finite(min_interval, field_name="min_interval")
        if interval == 0:
            return

        with self._lock:
            now = _monotonic_value(self._monotonic)
            if self._last_sweep is None or now - self._last_sweep >= _STALE_SWEEP_INTERVAL_SECONDS:
                self._sweep_stale_locked(now)
                self._last_sweep = now

            previous = self._reservations.get(normalized_host)
            if previous is None or now >= previous[0] + interval:
                fire_at = now
            else:
                jitter = _non_negative_finite(
                    self._jitter(),
                    field_name="jitter",
                )
                fire_at = previous[0] + interval + jitter
            self._reservations[normalized_host] = (fire_at, interval)

        sleep_for = fire_at - _monotonic_value(self._monotonic)
        if sleep_for > 0:
            self._sleeper(sleep_for)

    def _sweep_stale_locked(self, now: float) -> None:
        stale_hosts = [
            host
            for host, (fire_at, interval) in self._reservations.items()
            if fire_at + interval < now
        ]
        for host in stale_hosts:
            del self._reservations[host]


_PROCESS_HOST_THROTTLE = HostThrottle()
_process_client: httpx.Client | None = None
_PROCESS_CLIENT_LOCK = threading.Lock()


class HostThrottledHttpClient:
    """Issue one throttled GET while preserving caller-owned HTTP semantics.

    The default path shares one process-level :class:`httpx.Client`, whose
    connection pool reuses TCP/TLS connections by origin.  An explicitly
    injected client is borrowed, while a client created for an injected
    transport is owned and closed by this wrapper.  Injected paths do not use
    the real process throttle unless a throttle is supplied explicitly, so
    mock transports and caller-managed clients never acquire an accidental
    wall-clock sleep.
    """

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float | httpx.Timeout,
        throttle: HostThrottle | None = None,
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("inject client or transport, not both")

        if client is not None:
            self._client = client
            self._owns_client = False
            self._throttle = throttle
        elif transport is not None:
            self._client = httpx.Client(
                transport=transport,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            )
            self._owns_client = True
            self._throttle = throttle
        else:
            self._client = process_httpx_client()
            self._owns_client = False
            self._throttle = throttle or _PROCESS_HOST_THROTTLE

    @property
    def client(self) -> httpx.Client:
        """Return the underlying client for lifecycle and reuse inspection."""

        return self._client

    def get(
        self,
        url: str | httpx.URL,
        *,
        min_interval: float,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | httpx.Timeout,
    ) -> httpx.Response:
        """Perform exactly one GET after reserving the URL host's slot."""

        parsed_url = httpx.URL(url)
        if not parsed_url.is_absolute_url or parsed_url.scheme not in {"http", "https"}:
            raise ValueError("url must be an absolute HTTP or HTTPS URL")
        host = parsed_url.host
        if not host:
            raise ValueError("url must include a host")
        interval = _non_negative_finite(min_interval, field_name="min_interval")
        if self._throttle is not None:
            self._throttle.wait(host, interval)
        return self._client.get(
            parsed_url,
            params=params,
            headers=headers,
            timeout=timeout,
        )

    def close(self) -> None:
        """Close only a client created for this wrapper's injected transport."""

        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HostThrottledHttpClient:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def process_httpx_client() -> httpx.Client:
    """Return the live process-level HTTPX client, creating it once."""

    global _process_client
    with _PROCESS_CLIENT_LOCK:
        if _process_client is None or _process_client.is_closed:
            _process_client = httpx.Client(
                follow_redirects=False,
                trust_env=False,
            )
        return _process_client


def _close_process_httpx_client() -> None:
    global _process_client
    with _PROCESS_CLIENT_LOCK:
        client = _process_client
        _process_client = None
    if client is not None:
        client.close()


def _normalized_host(host: object) -> str:
    if not isinstance(host, str) or not host.strip():
        raise ValueError("host must be non-empty text")
    normalized = host.strip().rstrip(".").lower()
    if not normalized:
        raise ValueError("host must be non-empty text")
    return normalized


def _non_negative_finite(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")
    return normalized


def _monotonic_value(monotonic: Callable[[], object]) -> float:
    value = monotonic()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("monotonic clock must return a number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError("monotonic clock must return a finite number")
    return normalized


atexit.register(_close_process_httpx_client)
