import math
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from app.recorder import Controller, run_session, select_proxies, summarize


class RecorderTests(unittest.TestCase):
    def test_summary_uses_successful_samples(self):
        rows = summarize({'node-a': [100, None, 130], 'node-b': [None, None]})
        self.assertEqual(rows[0]['proxy'], 'node-a')
        self.assertEqual(rows[0]['success'], 2)
        self.assertEqual(rows[0]['rate'], 2 / 3)
        self.assertEqual(rows[0]['average'], 115)
        self.assertEqual(rows[0]['jitter'], 30)
        self.assertTrue(math.isnan(rows[1]['average']))

    def test_select_proxies_skips_builtin_and_strategy_groups(self):
        proxies = {
            'DIRECT': {'type': 'Direct'},
            'direct': {'type': 'Direct'},
            'Group': {'type': 'Selector'},
            'Japan 01': {'type': 'ss'},
        }
        self.assertEqual(select_proxies(proxies), [('Japan 01', 'ss')])

    def test_run_session_rejects_empty_selection(self):
        with self.assertRaisesRegex(ValueError, '至少需要选择一个代理'):
            run_session(
                object(),
                [],
                rounds=1,
                interval=0,
                concurrency=1,
                timeout_ms=1000,
                test_url='https://example.com',
            )

    def test_controller_normalizes_base_path(self):
        controller = Controller('http://127.0.0.1:9090/', 'secret', 2)
        self.assertEqual(controller.base_url, 'http://127.0.0.1:9090')

    def test_controller_rejects_invalid_timeout(self):
        with self.assertRaises(ValueError):
            Controller('http://127.0.0.1:9090', '', 0)

    def test_controller_rejects_malformed_url(self):
        for url in ('http://:9090', 'http://user:pass@example.com:99999'):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    Controller(url, '', 2)

    def test_controller_reads_proxies_and_tests_escaped_name(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get('Authorization')))
                if urlsplit(self.path).path == '/proxies':
                    payload = {'proxies': {'node / one': {'type': 'ss'}}}
                else:
                    payload = {'delay': 123}
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        controller = Controller(f'http://127.0.0.1:{server.server_port}', 'secret', 2)
        self.assertIn('node / one', controller.proxies())
        self.assertEqual(
            controller.delay('node / one', 'https://example.com/check', 3000),
            123,
        )
        self.assertEqual(requests[0], ('/proxies', 'Bearer secret'))
        self.assertTrue(requests[1][0].startswith('/proxies/node%20%2F%20one/delay?'))
        self.assertEqual(requests[1][1], 'Bearer secret')

    def test_controller_reads_authenticated_json_stream(self):
        headers = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                headers.append(self.headers.get('Authorization'))
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"up":12}\n{"up":34}\n')
                self.wfile.flush()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)

        values = []
        controller = Controller(f'http://127.0.0.1:{server.server_port}', 'stream-key', 2)
        controller.read_stream('/traffic', values.append, threading.Event())
        self.assertEqual(values, [{'up': 12}, {'up': 34}])
        self.assertEqual(headers, ['Bearer stream-key'])

    def test_run_session_limits_parallel_proxy_tests(self):
        state = {'active': 0, 'maximum': 0}
        lock = threading.Lock()

        class FakeController:
            def delay(self, name, _test_url, _timeout_ms):
                with lock:
                    state['active'] += 1
                    state['maximum'] = max(state['maximum'], state['active'])
                time.sleep(0.03)
                with lock:
                    state['active'] -= 1
                return len(name)

        samples, stopped = run_session(
            FakeController(),
            [('node-a', 'ss'), ('node-b', 'ss'), ('node-c', 'ss'), ('node-d', 'ss')],
            rounds=1,
            interval=0,
            concurrency=2,
            timeout_ms=1000,
            test_url='https://example.com',
        )

        self.assertFalse(stopped)
        self.assertEqual(state['maximum'], 2)
        self.assertEqual([len(values) for values in samples.values()], [1, 1, 1, 1])

    def test_run_session_reports_fast_results_without_waiting_for_slow_ones(self):
        completed = []

        class FakeController:
            def delay(self, name, _test_url, _timeout_ms):
                if name == 'slow':
                    time.sleep(0.05)
                return 10

        run_session(
            FakeController(),
            [('slow', 'ss'), ('fast', 'ss')],
            rounds=1,
            interval=0,
            concurrency=2,
            timeout_ms=1000,
            test_url='https://example.com',
            on_sample=lambda sample: completed.append(sample['proxy']),
        )
        self.assertEqual(completed, ['fast', 'slow'])


if __name__ == '__main__':
    unittest.main()
