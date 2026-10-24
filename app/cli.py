"""Command-line entry point."""

from __future__ import annotations

import argparse
import math
import os
import sys

from app.config import load_config
from app.recorder import Controller, run_session, select_proxies, summarize


def parse_args() -> argparse.Namespace:
    config = load_config()
    parser = argparse.ArgumentParser(
        description='通过 FlClash 本机控制器重复测试代理延迟。'
    )
    parser.add_argument('--controller', default=config['controller'])
    parser.add_argument(
        '--secret', default=os.environ.get('FLCLASH_SECRET', config['secret'])
    )
    parser.add_argument('--rounds', type=int, default=config['rounds'])
    parser.add_argument('--interval', type=float, default=config['interval'])
    parser.add_argument('--concurrency', type=int, default=config['concurrency'])
    parser.add_argument('--timeout-ms', type=int, default=config['timeout_ms'])
    parser.add_argument('--request-timeout', type=float, default=config['request_timeout'])
    parser.add_argument('--test-url', default=config['test_url'])
    parser.add_argument('--proxy', action='append')
    args = parser.parse_args()
    if args.rounds < 1 or args.interval < 0 or args.concurrency < 1 or args.timeout_ms < 1 or args.request_timeout <= 0:
        parser.error('轮数、并发数和超时须为正数，间隔不可小于 0')
    return args


def main() -> int:
    args = parse_args()
    try:
        controller = Controller(args.controller, args.secret, args.request_timeout)
        selected = select_proxies(controller.proxies(), args.proxy)
        if not selected:
            print('没有找到符合条件的代理。', file=sys.stderr)
            return 2
        print(f'代理数: {len(selected)} | 轮数: {args.rounds} | 并发数: {args.concurrency}')

        def show_sample(sample: dict[str, object]) -> None:
            value = sample['delay_ms']
            message = f"{value} ms" if value else f"失败 ({sample['error']})"
            print(f"[{sample['round']}/{args.rounds}] {sample['proxy']}: {message}")

        samples, _ = run_session(
            controller,
            selected,
            args.rounds,
            args.interval,
            args.concurrency,
            args.timeout_ms,
            args.test_url,
            on_sample=show_sample,
        )
        print('\n汇总（按成功率、平均延迟排序）')
        print(f'{"代理":<28} {"成功率":>9} {"平均(ms)":>11} {"抖动(ms)":>11}')
        for row in summarize(samples):
            average = f'{row["average"]:.1f}' if not math.isnan(row['average']) else '-'
            jitter = f'{row["jitter"]:.1f}' if not math.isnan(row['jitter']) else '-'
            print(
                f'{row["proxy"][:28]:<28} '
                f'{row["success"]}/{row["total"]} ({row["rate"]:.0%})'.rjust(9),
                f'{average:>11} {jitter:>11}',
            )
        return 0
    except KeyboardInterrupt:
        print('\n已中断；已完成的测量仅保留在本次运行内存中。', file=sys.stderr)
        return 130
    except (RuntimeError, ValueError, OSError) as error:
        print(f'错误: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
