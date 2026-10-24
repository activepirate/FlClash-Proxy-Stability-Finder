#!/usr/bin/env python3
"""Record repeated proxy delay measurements from a local Clash-compatible API."""

from __future__ import annotations

import json
import math
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen


GROUP_TYPES = {'selector', 'urltest', 'loadbalance', 'relay'}
BUILT_IN_PROXIES = {'DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'COMPATIBLE'}


class Controller:
    def __init__(self, base_url: str, secret: str, timeout: float) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in {'http', 'https'} or not parts.netloc:
            raise ValueError('Controller URL must start with http:// or https://')
        if timeout <= 0 or not math.isfinite(timeout):
            raise ValueError('Controller timeout must be greater than zero')
        self.base_url = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip('/'), '', ''))
        self.secret = secret
        self.timeout = timeout

    def _request(self, url: str) -> Request:
        headers = {'Accept': 'application/json'}
        if self.secret:
            headers['Authorization'] = f'Bearer {self.secret}'
        return Request(url, headers=headers)

    def get_json(self, path: str, query: dict[str, str] | None = None) -> Any:
        url = f'{self.base_url}{path}'
        if query:
            url = f'{url}?{urlencode(query)}'
        request = self._request(url)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read())
        except HTTPError as error:
            detail = error.read().decode('utf-8', errors='replace').strip()
            raise RuntimeError(f'HTTP {error.code}: {detail or error.reason}') from error
        except (URLError, OSError, TimeoutError, json.JSONDecodeError) as error:
            raise RuntimeError(str(error)) from error

    def read_stream(self, path: str, on_value: Any, stop_event: threading.Event) -> None:
        request = self._request(f'{self.base_url}{path}')
        try:
            with urlopen(request, timeout=self.timeout) as response:
                while not stop_event.is_set():
                    line = response.readline()
                    if not line:
                        return
                    on_value(json.loads(line.decode('utf-8')))
        except HTTPError as error:
            detail = error.read().decode('utf-8', errors='replace').strip()
            raise RuntimeError(f'HTTP {error.code}: {detail or error.reason}') from error
        except (URLError, OSError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise RuntimeError(str(error)) from error

    def proxies(self) -> dict[str, dict[str, Any]]:
        payload = self.get_json('/proxies')
        proxies = payload.get('proxies') if isinstance(payload, dict) else None
        if not isinstance(proxies, dict):
            raise RuntimeError('Controller response does not contain a proxies object')
        return {
            name: details
            for name, details in proxies.items()
            if isinstance(details, dict)
        }

    def delay(self, name: str, test_url: str, timeout_ms: int) -> int:
        payload = self.get_json(
            f'/proxies/{quote(name, safe="")}/delay',
            {'url': test_url, 'timeout': str(timeout_ms)},
        )
        delay = payload.get('delay') if isinstance(payload, dict) else None
        if not isinstance(delay, int) or delay <= 0:
            raise RuntimeError('Controller response does not contain a valid delay')
        return delay


def summarize(samples: dict[str, list[int | None]]) -> list[dict[str, Any]]:
    result = []
    for name, values in samples.items():
        successful = [value for value in values if value is not None]
        adjacent_deltas = [
            abs(current - previous)
            for previous, current in zip(successful, successful[1:])
        ]
        result.append(
            {
                'proxy': name,
                'success': len(successful),
                'total': len(values),
                'rate': len(successful) / len(values) if values else 0,
                'average': statistics.fmean(successful) if successful else math.nan,
                'jitter': statistics.fmean(adjacent_deltas) if adjacent_deltas else math.nan,
            }
        )
    return sorted(
        result,
        key=lambda row: (
            -row['rate'],
            row['average'] if not math.isnan(row['average']) else math.inf,
            row['proxy'].casefold(),
        ),
    )


def select_proxies(
    proxies: dict[str, dict[str, Any]],
    filters: list[str] | None = None,
) -> list[tuple[str, str]]:
    selected = []
    for name, details in proxies.items():
        kind = str(details.get('type', 'unknown'))
        if name.upper() in BUILT_IN_PROXIES:
            continue
        if kind.casefold() in GROUP_TYPES:
            continue
        if filters and not any(pattern.casefold() in name.casefold() for pattern in filters):
            continue
        selected.append((name, kind))
    return selected


def run_session(
    controller: Controller,
    selected: list[tuple[str, str]],
    rounds: int,
    interval: float,
    concurrency: int,
    timeout_ms: int,
    test_url: str,
    on_sample: Any = None,
    stop_event: threading.Event | None = None,
) -> tuple[dict[str, list[int | None]], bool]:
    if rounds < 1 or interval < 0 or concurrency < 1 or timeout_ms < 1:
        raise ValueError('轮数、并发数和超时须为正数，间隔不可小于 0')
    if not selected:
        raise ValueError('至少需要选择一个代理')

    samples: dict[str, list[int | None]] = {name: [] for name, _ in selected}
    stopped = False
    worker_count = min(concurrency, len(selected))

    def measure(item: tuple[str, str]) -> tuple[str, str, int | None, str]:
        name, kind = item
        try:
            return name, kind, controller.delay(name, test_url, timeout_ms), ''
        except (RuntimeError, OSError, ValueError) as exception:
            return name, kind, None, str(exception)

    # Reuse the pool across batches and rounds. Batches still provide a
    # stop point between groups without creating a new pool each time.
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        for round_number in range(1, rounds + 1):
            if stop_event and stop_event.is_set():
                stopped = True
                break
            round_started = time.monotonic()
            for index in range(0, len(selected), concurrency):
                batch = selected[index:index + concurrency]
                if stop_event and stop_event.is_set():
                    stopped = True
                    break

                futures = {executor.submit(measure, item): item for item in batch}
                for future in as_completed(futures):
                    name, kind, delay, error = future.result()
                    samples[name].append(delay)
                    sample = {
                        'round': round_number,
                        'proxy': name,
                        'proxy_type': kind,
                        'delay_ms': delay if delay is not None else '',
                        'success': delay is not None,
                        'error': error,
                    }
                    if on_sample:
                        on_sample(sample)

            if stopped:
                break
            if round_number < rounds:
                elapsed = time.monotonic() - round_started
                wait = max(0.0, interval - elapsed)
                if wait and stop_event:
                    stopped = stop_event.wait(wait)
                elif wait:
                    time.sleep(wait)
    return samples, stopped
