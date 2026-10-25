"""Local settings for the recorder UI and command-line tool."""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / 'config' / 'config.json'
DEFAULTS: dict[str, Any] = {
    'controller': 'http://127.0.0.1:9090',
    'secret': '',
    'rounds': 5,
    'interval': 5,
    'concurrency': 20,
    'timeout_ms': 5000,
    'request_timeout': 10,
    'test_url': 'https://www.gstatic.com/generate_204',
    'services': [
        {'name': 'Google', 'url': 'https://www.google.com/generate_204'},
        {'name': 'GitHub', 'url': 'https://github.com/'},
        {'name': '百度', 'url': 'https://www.baidu.com/'},
    ],
}


def _number(value: Any, name: str, *, integer: bool = False, minimum: float = 0) -> int | float:
    if isinstance(value, bool):
        raise ValueError(f'{name}必须是数字')
    try:
        converted = int(value) if integer else float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f'{name}必须是数字') from error
    if integer and isinstance(value, float) and not value.is_integer():
        raise ValueError(f'{name}必须是整数')
    if not math.isfinite(converted) or converted < minimum:
        suffix = '正数' if minimum > 0 else '非负数'
        raise ValueError(f'{name}须为{suffix}')
    return converted


def normalize_service_url(url: Any) -> str:
    value = str(url).strip()
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as error:
        raise ValueError('服务 URL 必须是有效的 http:// 或 https:// 地址') from error
    if (
        len(value) > 2048
        or any(character.isspace() for character in value)
        or parsed.scheme not in {'http', 'https'}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError('服务 URL 必须是有效的 http:// 或 https:// 地址')
    return value


def normalize_services(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value or len(value) > 50:
        raise ValueError('服务检测列表必须包含 1 到 50 项')
    services = []
    seen_names = set()
    seen_urls = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('服务检测项格式错误')
        name = str(item.get('name', '')).strip()
        if not name or len(name) > 80:
            raise ValueError('服务名称不能为空且不能超过 80 个字符')
        url = normalize_service_url(item.get('url', ''))
        name_key = name.casefold()
        if name_key in seen_names:
            raise ValueError(f'服务名称重复：{name}')
        if url in seen_urls:
            raise ValueError(f'服务 URL 重复：{url}')
        seen_names.add(name_key)
        seen_urls.add(url)
        services.append({'name': name, 'url': url})
    return services


def normalize_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a typed, validated configuration suitable for persistence/use."""
    values = {key: config.get(key, default) for key, default in DEFAULTS.items()}
    values['controller'] = str(values['controller']).strip()
    controller = urlsplit(values['controller'])
    if controller.scheme not in {'http', 'https'} or not controller.netloc:
        raise ValueError('控制器地址必须以 http:// 或 https:// 开头')

    values['secret'] = str(values['secret'])
    values['rounds'] = _number(values['rounds'], '轮数', integer=True, minimum=1)
    values['interval'] = _number(values['interval'], '间隔', minimum=0)
    values['concurrency'] = _number(values['concurrency'], '并发数', integer=True, minimum=1)
    values['timeout_ms'] = _number(values['timeout_ms'], '单次超时', integer=True, minimum=1)
    values['request_timeout'] = _number(values['request_timeout'], 'HTTP 请求超时', minimum=0.000001)

    values['test_url'] = str(values['test_url']).strip()
    test_url = urlsplit(values['test_url'])
    if test_url.scheme not in {'http', 'https'} or not test_url.netloc:
        raise ValueError('测试 URL 必须是有效的 http:// 或 https:// 地址')
    values['services'] = normalize_services(values['services'])
    return values


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        save_config(DEFAULTS)
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f'无法读取配置文件 {CONFIG_PATH}: {error}') from error
    if not isinstance(loaded, dict):
        raise ValueError('配置文件根节点必须是 JSON 对象')
    try:
        return normalize_config(loaded)
    except ValueError as error:
        raise ValueError(f'配置文件内容无效：{error}') from error


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w',
            encoding='utf-8',
            dir=CONFIG_PATH.parent,
            prefix=f'.{CONFIG_PATH.name}.',
            suffix='.tmp',
            delete=False,
        ) as output:
            temporary_path = Path(output.name)
            values = normalize_config(config)
            output.write(json.dumps(values, ensure_ascii=False, indent=2))
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        try:
            os.chmod(temporary_path, 0o600)
        except OSError:
            pass
        os.replace(temporary_path, CONFIG_PATH)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass
