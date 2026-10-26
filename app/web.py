"""Loopback-only web interface for the proxy delay recorder."""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import socket
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from app.config import DEFAULTS, load_config, normalize_config, normalize_service_url, save_config
from app.recorder import Controller, run_session, select_proxies, summarize


PAGE = Path(__file__).parent / 'web' / 'index.html'
DIRECT_CHAINS = {'DIRECT', 'PASS', 'COMPATIBLE'}
MAX_LOG_ENTRIES = 200
EXIT_IP_PROVIDERS = {
    'ipify': 'https://api.ipify.org/',
    'ipapi': 'https://ipapi.co/json/',
    'ipwho': 'https://ipwho.is/',
    'ipwhois': 'https://ipwhois.app/json/',
}
EXIT_IP_LABELS = {
    'ipify': 'ipify',
    'ipapi': 'ipapi.co',
    'ipwho': 'ipwho.is',
    'ipwhois': 'ipwhois.app',
}
SERVICE_TARGETS = {
    'google': ('Google', 'https://www.google.com/generate_204'),
    'github': ('GitHub', 'https://github.com/'),
    'baidu': ('百度', 'https://www.baidu.com/'),
}
MAX_SERVICE_CHECKS = 50


def _empty_monitor() -> dict[str, Any]:
    return {
        'traffic': {},
        'connections': [],
        'controller_logs': [],
        'errors': {},
        'connection_traffic': {},
        'connection_totals': {},
    }


@lru_cache(maxsize=1)
def _page_body() -> bytes:
    return PAGE.read_bytes()


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _read_exit_ip_payload(opener: Any, url: str, timeout: float) -> Any:
    request = Request(
        url,
        headers={
            'Accept': 'application/json',
            'User-Agent': 'FlClashNetworkObserver/1.0',
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(4096).decode('utf-8', errors='replace').strip()
    except HTTPError as error:
        if error.code == 429:
            raise RuntimeError('请求过于频繁（HTTP 429），请稍后再试') from error
        raise RuntimeError(f'HTTP {error.code}: {error.reason}') from error
    except (OSError, TimeoutError, URLError) as error:
        raise RuntimeError(str(error) or error.__class__.__name__) from error
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def _normalize_exit_ip_payload(payload: Any, provider: str) -> dict[str, Any]:
    value = payload.get('ip') if isinstance(payload, dict) else payload
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError('IP 查询服务返回了无效结果')
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError as error:
        raise RuntimeError('IP 查询服务返回了无效地址') from error

    timezone_data = payload.get('timezone') if isinstance(payload, dict) else None
    if isinstance(timezone_data, dict):
        timezone_name = timezone_data.get('id') or timezone_data.get('time_zone')
        utc_offset = (
            timezone_data.get('utc')
            or timezone_data.get('utc_offset')
            or timezone_data.get('gmt')
        )
    else:
        timezone_name = timezone_data
        utc_offset = (
            payload.get('utc_offset') or payload.get('timezone_gmt')
            if isinstance(payload, dict)
            else None
        )
    return {
        'ip': str(address),
        'version': address.version,
        'country': (
            payload.get('country_name') or payload.get('country')
            if isinstance(payload, dict)
            else None
        ),
        'city': (
            payload.get('city') or payload.get('city_name')
            if isinstance(payload, dict)
            else None
        ),
        'timezone': timezone_name,
        'utc_offset': utc_offset,
        'checked_at': _utc_timestamp(),
        'provider': provider,
        'error': None,
    }


def check_exit_ip(timeout: float, provider: str = 'ipapi') -> dict[str, Any]:
    """Return public IP and geolocation from the selected provider only."""
    if provider not in EXIT_IP_PROVIDERS:
        raise RuntimeError('不支持的 IP 查询服务')
    opener = build_opener(ProxyHandler({}))
    try:
        payload = _read_exit_ip_payload(opener, EXIT_IP_PROVIDERS[provider], timeout)
        return _normalize_exit_ip_payload(payload, provider)
    except RuntimeError as error:
        raise RuntimeError(f'{EXIT_IP_LABELS[provider]}：{error}') from error


def check_service(name: str, timeout: float) -> dict[str, Any]:
    """Check reachability with the host's normal URL opener configuration."""
    target = SERVICE_TARGETS.get(name)
    if target is None:
        raise ValueError('不支持的探测目标')
    label, url = target
    return _check_service_url(url, timeout, name=name, label=label)


def _check_service_url(url: str, timeout: float, *, name: str, label: str) -> dict[str, Any]:
    request = Request(url, headers={'User-Agent': 'FlClashNetworkObserver/1.0'})
    started = time.monotonic()
    status_code = None
    error = None
    timed_out = False
    try:
        with build_opener().open(request, timeout=timeout) as response:
            status_code = response.status
    except HTTPError as exception:
        # An HTTP response, including 4xx/5xx, proves the service was reached.
        status_code = exception.code
    except (OSError, TimeoutError, URLError) as exception:
        error = str(exception) or exception.__class__.__name__
        timed_out = isinstance(exception, (TimeoutError, socket.timeout)) or (
            isinstance(exception, URLError)
            and isinstance(exception.reason, (TimeoutError, socket.timeout))
        )
    elapsed_ms = round((time.monotonic() - started) * 1000)
    return {
        'name': name,
        'label': label,
        'url': url,
        'status': 'available' if status_code is not None else ('timeout' if timed_out else 'failed'),
        'status_code': status_code,
        'delay_ms': elapsed_ms if status_code is not None else None,
        'checked_at': _utc_timestamp(),
        'error': error,
    }


def check_services(
    timeout: float,
    names: list[Any] | None = None,
    urls: list[str] | None = None,
) -> list[dict[str, Any]]:
    selected = list(SERVICE_TARGETS) if names is None and urls is None else (names or [])
    if selected and all(isinstance(item, dict) for item in selected):
        targets = []
        for item in selected:
            name = str(item.get('name', '')).strip()
            url = normalize_service_url(item.get('url', ''))
            if not name:
                raise ValueError('可达性探测目标格式错误')
            targets.append((name, url, name))
    else:
        if not all(isinstance(name, str) for name in selected):
            raise ValueError('可达性探测目标列表格式错误')
        unknown = [name for name in selected if name not in SERVICE_TARGETS]
        if unknown:
            raise ValueError(f'不支持的探测目标: {", ".join(unknown)}')
        targets = [(name, SERVICE_TARGETS[name][1], SERVICE_TARGETS[name][0]) for name in selected]
    for url in urls or []:
        normalized_url = normalize_service_url(url)
        targets.append((normalized_url, normalized_url, normalized_url))
    if not targets:
        return []
    if len(targets) > MAX_SERVICE_CHECKS:
        raise ValueError(f'单次最多检测 {MAX_SERVICE_CHECKS} 个服务')
    with ThreadPoolExecutor(max_workers=min(8, len(targets))) as executor:
        return list(executor.map(
            lambda target: _check_service_url(target[1], timeout, name=target[0], label=target[2]),
            targets,
        ))


def classify_connection(connection: dict[str, Any]) -> str:
    chains = connection.get('chains')
    if not isinstance(chains, list):
        return 'unknown'
    names = [str(name).strip().upper() for name in chains if name is not None]
    if not names:
        return 'unknown'
    if any(name in {'REJECT', 'REJECT-DROP'} for name in names):
        return 'unknown'
    return 'direct' if all(name in DIRECT_CHAINS for name in names) else 'proxy'


def connection_traffic_snapshot(
    connections: list[dict[str, Any]],
    previous: dict[str, tuple[str, int, int]],
    totals: dict[str, dict[str, int]],
    elapsed: float = 1.0,
) -> tuple[dict[str, tuple[str, int, int]], dict[str, dict[str, int]]]:
    current: dict[str, tuple[str, int, int]] = {}
    rates = {kind: {'up': 0, 'down': 0} for kind in ('proxy', 'direct')}
    for index, connection in enumerate(connections):
        if not isinstance(connection, dict):
            continue
        connection_id = str(connection.get('id') or f'connection-{index}')
        kind = classify_connection(connection)
        if kind == 'unknown':
            continue
        try:
            upload = max(0, int(connection.get('upload') or 0))
            download = max(0, int(connection.get('download') or 0))
        except (TypeError, ValueError):
            continue
        current[connection_id] = (kind, upload, download)
        old = previous.get(connection_id)
        if old and old[0] == kind:
            up_delta = max(0, upload - old[1])
            down_delta = max(0, download - old[2])
            rates[kind]['up'] += up_delta
            rates[kind]['down'] += down_delta
            totals[kind]['up'] += up_delta
            totals[kind]['down'] += down_delta
    if elapsed > 0:
        for values in rates.values():
            values['up'] = round(values['up'] / elapsed)
            values['down'] = round(values['down'] / elapsed)
    return current, rates


class RecorderService:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.stopping = False
        self.status = '就绪'
        self.samples: dict[str, list[int | None]] = {}
        self.proxy_types: dict[str, str] = {}
        self.logs: list[str] = []
        self.stop_event = threading.Event()
        self.monitor_key: tuple[str, str, float] | None = None
        self.monitor_event = threading.Event()
        self.monitor: dict[str, Any] = _empty_monitor()
        self.connection_counters: dict[str, tuple[str, int, int]] = {}
        self.connection_sampled_at: float | None = None
        self.connection_totals = {
            kind: {'up': 0, 'down': 0} for kind in ('proxy', 'direct')
        }

    def _append_log_locked(self, line: str) -> None:
        self.logs.append(line)
        del self.logs[:-MAX_LOG_ENTRIES]

    def ensure_monitor(self) -> None:
        config = load_config()
        key = (str(config['controller']), str(config['secret']), float(config['request_timeout']))
        with self.lock:
            if key == self.monitor_key:
                return
            self.monitor_event.set()
            event = threading.Event()
            self.monitor_event = event
            self.monitor_key = key
            self.monitor = _empty_monitor()
            self.connection_counters = {}
            self.connection_sampled_at = None
            self.connection_totals = {
                kind: {'up': 0, 'down': 0} for kind in ('proxy', 'direct')
            }

        try:
            controller = Controller(key[0], key[1], key[2])
        except (ValueError, TypeError) as error:
            with self.lock:
                self.monitor['errors'] = {name: str(error) for name in ('traffic', 'logs', 'connections')}
            return

        for name, path, field in (
            ('traffic', '/traffic', 'traffic'),
            ('logs', '/logs?level=info&format=structured', 'controller_logs'),
        ):
            threading.Thread(
                target=self._stream_monitor,
                args=(controller, name, path, field, event),
                daemon=True,
            ).start()
        threading.Thread(
            target=self._connections_monitor, args=(controller, event), daemon=True
        ).start()

    def _set_monitor_value(self, event: threading.Event, key: str, value: Any) -> None:
        with self.lock:
            if event is self.monitor_event and not event.is_set():
                self.monitor[key] = value
                endpoint = 'logs' if key == 'controller_logs' else key
                self.monitor['errors'].pop(endpoint, None)

    def _set_monitor_error(self, event: threading.Event, endpoint: str, error: str) -> None:
        with self.lock:
            if event is self.monitor_event and not event.is_set():
                self.monitor['errors'][endpoint] = error

    def _stream_monitor(
        self, controller: Controller, endpoint: str, path: str, field: str,
        event: threading.Event,
    ) -> None:
        def receive(value: Any) -> None:
            if endpoint == 'logs':
                with self.lock:
                    if event is self.monitor_event and not event.is_set() and isinstance(value, dict):
                        self.monitor[field].append(value)
                        del self.monitor[field][:-MAX_LOG_ENTRIES]
                        self.monitor['errors'].pop(endpoint, None)
            elif isinstance(value, dict):
                self._set_monitor_value(event, field, value)

        while not event.is_set():
            try:
                controller.read_stream(path, receive, event)
                if not event.is_set():
                    raise RuntimeError('控制器关闭了数据流')
            except RuntimeError as error:
                self._set_monitor_error(event, endpoint, str(error))
                event.wait(2)

    def _connections_monitor(self, controller: Controller, event: threading.Event) -> None:
        while not event.is_set():
            try:
                payload = controller.get_json('/connections')
                connections = payload.get('connections', []) if isinstance(payload, dict) else []
                if not isinstance(connections, list):
                    raise RuntimeError('控制器返回的连接列表格式无效')
                with self.lock:
                    if event is self.monitor_event and not event.is_set():
                        sampled_at = time.monotonic()
                        elapsed = (
                            sampled_at - self.connection_sampled_at
                            if self.connection_sampled_at is not None else 0
                        )
                        current, rates = connection_traffic_snapshot(
                            connections, self.connection_counters, self.connection_totals, elapsed
                        )
                        self.connection_counters = current
                        self.connection_sampled_at = sampled_at
                        self.monitor['connections'] = connections[:300]
                        self.monitor['connection_traffic'] = {
                            kind: {
                                'up': rates[kind]['up'],
                                'down': rates[kind]['down'],
                                'upTotal': self.connection_totals[kind]['up'],
                                'downTotal': self.connection_totals[kind]['down'],
                            }
                            for kind in ('proxy', 'direct')
                        }
                        self.monitor['connection_totals'] = {
                            key: payload.get(key, 0) for key in ('uploadTotal', 'downloadTotal')
                        }
                        self.monitor['errors'].pop('connections', None)
            except RuntimeError as error:
                self._set_monitor_error(event, 'connections', str(error))
            event.wait(1)

    def state(self) -> dict[str, Any]:
        with self.lock:
            rows = summarize(self.samples) if self.samples else []
            for row in rows:
                for key in ('average', 'jitter'):
                    if math.isnan(row[key]):
                        row[key] = None
            monitor = {
                'traffic': dict(self.monitor.get('traffic', {})),
                'connections': list(self.monitor.get('connections', [])),
                'connection_totals': dict(self.monitor.get('connection_totals', {})),
                'connection_traffic': {
                    kind: dict(values)
                    for kind, values in self.monitor.get('connection_traffic', {}).items()
                },
                'controller_logs': list(self.monitor.get('controller_logs', [])),
                'errors': dict(self.monitor.get('errors', {})),
            }
            return {
                'running': self.running,
                'stopping': self.stopping,
                'status': self.status,
                'rows': rows,
                'logs': list(self.logs),
                'proxy_types': dict(self.proxy_types),
                **monitor,
            }

    def start(self, values: dict[str, Any], names: list[str]) -> None:
        with self.lock:
            if self.running:
                raise RuntimeError('已有延迟采样任务正在运行')

        controller = Controller(
            str(values['controller']),
            str(values['secret']),
            float(values['request_timeout']),
        )
        available = controller.proxies()
        selected = select_proxies(available)
        requested = set(names)
        selected = [item for item in selected if item[0] in requested]
        if not selected:
            raise ValueError('请先刷新并选择至少一个可探测代理节点')

        with self.lock:
            if self.running:
                raise RuntimeError('已有延迟采样任务正在运行')
            self.running = True
            self.stopping = False
            self.status = f'准备采样 {len(selected)} 个代理节点'
            self.samples = {name: [] for name, _ in selected}
            self.proxy_types = dict(selected)
            self.logs = []
            self.stop_event = threading.Event()

        worker = threading.Thread(
            target=self._run,
            args=(controller, selected, values, self.stop_event),
            daemon=True,
        )
        worker.start()

    def _run(
        self,
        controller: Controller,
        selected: list[tuple[str, str]],
        values: dict[str, Any],
        stop_event: threading.Event,
    ) -> None:
        with self.lock:
            self.status = '正在采样'

        def record_sample(sample: dict[str, Any]) -> None:
            value = f"{sample['delay_ms']} ms" if sample['success'] else f"失败：{sample['error']}"
            line = f"第 {sample['round']} 轮 | {sample['proxy']} | {value}"
            with self.lock:
                self.samples[sample['proxy']].append(
                    int(sample['delay_ms']) if sample['success'] else None
                )
                self._append_log_locked(line)

        try:
            _samples, stopped = run_session(
                controller,
                selected,
                int(values['rounds']),
                float(values['interval']),
                int(values['concurrency']),
                int(values['timeout_ms']),
                str(values['test_url']),
                on_sample=record_sample,
                stop_event=stop_event,
            )
            with self.lock:
                self.status = '已停止' if stopped else '采样完成'
        except (RuntimeError, ValueError, OSError) as error:
            with self.lock:
                self.status = f'采样失败：{error}'
                self._append_log_locked(f'错误：{error}')
        finally:
            with self.lock:
                self.running = False
                self.stopping = False

    def stop(self) -> None:
        with self.lock:
            if self.running:
                self.stopping = True
                self.status = '等待当前请求结束后停止'
                self.stop_event.set()


SERVICE = RecorderService()


def public_config() -> dict[str, Any]:
    values = load_config()
    values['secret_configured'] = bool(values.get('secret'))
    values['secret'] = ''
    return values


def update_config(payload: dict[str, Any]) -> dict[str, Any]:
    values = load_config()
    clear_secret = payload.get('clear_secret', False)
    if not isinstance(clear_secret, bool):
        raise ValueError('clear_secret 必须是布尔值')
    if clear_secret:
        values['secret'] = ''
    elif 'secret' in payload:
        secret = payload['secret']
        if not isinstance(secret, str):
            raise ValueError('密钥必须是字符串')
        if secret:
            values['secret'] = secret

    values.update({key: payload[key] for key in DEFAULTS if key in payload and key != 'secret'})
    values = normalize_config(values)
    save_config(values)
    return values


class RequestHandler(BaseHTTPRequestHandler):
    server_version = 'FlClashNetworkObserver/1.0'
    service: RecorderService

    def log_message(self, _format: str, *_args: Any) -> None:
        pass

    def _host_is_local(self) -> bool:
        try:
            hostname = urlsplit(f"//{self.headers.get('Host', '')}").hostname
        except ValueError:
            hostname = None
        return (hostname or '').casefold() in {'127.0.0.1', 'localhost', '::1'}

    def _origin_is_local(self) -> bool:
        origin = self.headers.get('Origin')
        if not origin:
            return True
        parts = urlsplit(origin)
        return parts.scheme in {'http', 'https'} and (parts.hostname or '').casefold() in {
            '127.0.0.1', 'localhost', '::1'
        }

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        size = int(self.headers.get('Content-Length', '0'))
        if size < 1 or size > 1_000_000:
            raise ValueError('请求内容为空或超过 1 MB')
        payload = json.loads(self.rfile.read(size))
        if not isinstance(payload, dict):
            raise ValueError('请求内容必须是 JSON 对象')
        return payload

    def do_GET(self) -> None:
        if not self._host_is_local():
            self._json(403, {'error': '仅允许本机访问'})
            return
        path = urlsplit(self.path).path
        try:
            if path == '/':
                body = _page_body()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.end_headers()
                self.wfile.write(body)
            elif path == '/api/config':
                self._json(200, public_config())
            elif path == '/api/exit-ip':
                config = load_config()
                provider = parse_qs(urlsplit(self.path).query).get('provider', ['ipapi'])[0]
                try:
                    result = check_exit_ip(float(config['request_timeout']), provider)
                except RuntimeError as error:
                    result = {
                        'ip': None,
                        'version': None,
                        'country': None,
                        'city': None,
                        'timezone': None,
                        'utc_offset': None,
                        'checked_at': _utc_timestamp(),
                        'error': str(error),
                    }
                self._json(200, result)
            elif path == '/api/services':
                config = load_config()
                query = parse_qs(urlsplit(self.path).query)
                names = query.get('name')
                urls = query.get('url')
                if urls:
                    services = check_services(float(config['request_timeout']), names, urls)
                elif names:
                    services = check_services(float(config['request_timeout']), names)
                else:
                    services = check_services(float(config['request_timeout']), config['services'])
                self._json(200, {'services': services})
            elif path == '/api/state':
                self.service.ensure_monitor()
                self._json(200, self.service.state())
            elif path == '/api/proxies':
                config = load_config()
                controller = Controller(
                    config['controller'], config['secret'], float(config['request_timeout'])
                )
                proxies = select_proxies(controller.proxies())
                self._json(200, {'proxies': [{'name': name, 'type': kind} for name, kind in proxies]})
            else:
                self._json(404, {'error': 'Not found'})
        except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as error:
            self._json(400, {'error': str(error)})

    def do_POST(self) -> None:
        if not self._host_is_local() or not self._origin_is_local():
            self._json(403, {'error': '仅允许本机页面访问'})
            return
        try:
            payload = self._body()
            path = urlsplit(self.path).path
            if path == '/api/config':
                values = update_config(payload)
                public = {**values, 'secret': '', 'secret_configured': bool(values['secret'])}
                self._json(200, {'ok': True, 'config': public})
            elif path == '/api/run':
                values = update_config(payload)
                names = payload.get('proxies', [])
                if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
                    raise ValueError('代理选择格式错误')
                self.service.start(values, names)
                self._json(202, {'ok': True})
            elif path == '/api/stop':
                self.service.stop()
                self._json(200, {'ok': True})
            else:
                self._json(404, {'error': 'Not found'})
        except (RuntimeError, ValueError, OSError, json.JSONDecodeError) as error:
            self._json(400, {'error': str(error)})


def main() -> None:
    parser = argparse.ArgumentParser(description='启动 FlClash 代理稳定性筛选器')
    parser.add_argument('--host', default='127.0.0.1', help='只能使用本机回环地址')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true', help='不自动打开浏览器')
    args = parser.parse_args()
    if args.host not in {'127.0.0.1', 'localhost', '::1'}:
        parser.error('Web 界面只允许绑定到本机回环地址')
    if not 1 <= args.port <= 65535:
        parser.error('端口号必须在 1 到 65535 之间')

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    handler = type('RecorderRequestHandler', (RequestHandler,), {'service': SERVICE})
    server = Server((args.host, args.port), handler)
    host = '127.0.0.1' if args.host == 'localhost' else args.host
    address = f'http://{host}:{args.port}/'
    print(f'FlClash 代理稳定性筛选器已启动：{address}')
    print('按 Ctrl+C 停止服务。')
    if not args.no_browser:
        threading.Timer(0.4, webbrowser.open, args=(address,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n正在关闭 Web 服务…')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
