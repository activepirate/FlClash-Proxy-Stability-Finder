import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from app import web


class WebTests(unittest.TestCase):
    def setUp(self):
        handler = type(
            'TestRequestHandler',
            (web.RequestHandler,),
            {'service': web.RecorderService()},
        )
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, headers=None, body=None):
        connection = HTTPConnection('127.0.0.1', self.server.server_port)
        self.addCleanup(connection.close)
        connection.request(method, path, body=body, headers=headers or {})
        return connection.getresponse()

    def test_connection_traffic_is_split_by_proxy_chain(self):
        totals = {
            kind: {'up': 0, 'down': 0} for kind in ('proxy', 'direct')
        }
        first = [
            {'id': 'p1', 'chains': ['Selector', 'node-a'], 'upload': 100, 'download': 200},
            {'id': 'd1', 'chains': ['DIRECT'], 'upload': 50, 'download': 70},
            {'id': 'u1', 'chains': [], 'upload': 10, 'download': 20},
        ]
        previous, rates = web.connection_traffic_snapshot(first, {}, totals, elapsed=1)
        self.assertEqual(rates['proxy'], {'up': 0, 'down': 0})
        self.assertEqual(rates['direct'], {'up': 0, 'down': 0})

        second = [
            {'id': 'p1', 'chains': ['Selector', 'node-a'], 'upload': 300, 'download': 600},
            {'id': 'd1', 'chains': ['DIRECT'], 'upload': 90, 'download': 170},
            {'id': 'u1', 'chains': [], 'upload': 500, 'download': 500},
        ]
        _, rates = web.connection_traffic_snapshot(second, previous, totals, elapsed=2)
        self.assertEqual(rates['proxy'], {'up': 100, 'down': 200})
        self.assertEqual(rates['direct'], {'up': 20, 'down': 50})
        self.assertEqual(totals['proxy'], {'up': 200, 'down': 400})
        self.assertEqual(totals['direct'], {'up': 40, 'down': 100})

    def test_connection_chain_classification(self):
        self.assertEqual(web.classify_connection({'chains': ['DIRECT']}), 'direct')
        self.assertEqual(web.classify_connection({'chains': ['PASS']}), 'direct')
        self.assertEqual(web.classify_connection({'chains': ['relay', 'node-a']}), 'proxy')
        self.assertEqual(web.classify_connection({'chains': []}), 'unknown')
        self.assertEqual(web.classify_connection({}), 'unknown')

    def test_collection_logs_keep_only_latest_200_entries(self):
        service = web.RecorderService()
        with service.lock:
            for index in range(250):
                service._append_log_locked(f'记录 {index}')

        self.assertEqual(len(service.logs), web.MAX_LOG_ENTRIES)
        self.assertEqual(service.logs[0], '记录 50')
        self.assertEqual(service.state()['logs'], [f'记录 {index}' for index in range(50, 250)])

    def test_connections_monitor_records_sample(self):
        service = web.RecorderService()
        event = service.monitor_event

        class ControllerStub:
            def get_json(self, path):
                self.path = path
                payload = {
                    'connections': [
                        {'id': 'p1', 'chains': ['node-a'], 'upload': 100, 'download': 200},
                    ],
                    'uploadTotal': 100,
                    'downloadTotal': 200,
                }
                return payload

        controller = ControllerStub()
        monitor_thread = threading.Thread(
            target=service._connections_monitor,
            args=(controller, event),
            daemon=True,
        )
        monitor_thread.start()
        try:
            for _ in range(100):
                with service.lock:
                    if service.monitor['connections']:
                        break
                threading.Event().wait(0.01)
        finally:
            event.set()
            monitor_thread.join(timeout=1)

        self.assertEqual(controller.path, '/connections')
        self.assertEqual(service.monitor['connections'][0]['id'], 'p1')
        self.assertEqual(service.monitor['connection_totals'], {'uploadTotal': 100, 'downloadTotal': 200})

    def test_homepage_serves_web_interface(self):
        response = self.request('GET', '/')
        self.assertEqual(response.status, 200)
        self.assertIn('text/html', response.getheader('Content-Type'))
        page = response.read().decode()
        self.assertIn('FlClash 代理延迟记录器', page)
        self.assertNotIn('<div class="top-status">', page)
        for endpoint in ('控制器实时状态', '活动连接', '控制器日志', '/traffic', '/logs', '/connections'):
            self.assertIn(endpoint, page)
        for row_limit in ('collection-log-limit', 'connection-limit', 'controller-log-limit', 'value="10" selected'):
            self.assertIn(row_limit, page)
        for sort_header in ('data-sort-key="rate"', 'data-sort-key="average"', 'data-sort-key="jitter"', 'active-sorts', 'sort-order', 'sort-remove', '按成功率排序', '按平均延迟排序', '按抖动排序', 'aria-sort="none"'):
            self.assertIn(sort_header, page)
        self.assertNotIn('id="proxy-sort"', page)
        self.assertIn('grid-template-columns: minmax(0, 1.25fr) minmax(360px, 1fr)', page)
        self.assertIn('<div class="connection-column">', page)
        self.assertIn('@media (max-width: 900px) { .dashboard-columns { grid-template-columns: minmax(0, 1fr); } }', page)
        connection_column = page[page.index('<div class="connection-column">'):page.index('</div>\n      </div>', page.index('<div class="connection-column">'))]
        self.assertLess(connection_column.index('<h2>控制器实时状态</h2>'), connection_column.index('<h2>代理与直连流量</h2>'))
        self.assertLess(connection_column.index('<h2>代理与直连流量</h2>'), connection_column.index('<h2>活动连接</h2>'))
        self.assertLess(connection_column.index('<h2>活动连接</h2>'), connection_column.index('<h2>控制器日志</h2>'))
        primary_column = page[page.index('<div class="primary-column">'):page.index('<div class="connection-column">')]
        self.assertLess(primary_column.index('aria-label="采集概览"'), primary_column.index('<h2>代理测试结果</h2>'))
        results_head = primary_column[primary_column.index('class="section-head proxy-results-head"'):primary_column.index('<div class="table-wrap">')]
        self.assertLess(results_head.index('<h2>代理测试结果</h2>'), results_head.index('class="section-actions"'))
        self.assertLess(results_head.index('id="stop"'), results_head.index('id="open-settings"'))
        self.assertIn('.proxy-results-head .section-actions { justify-content: flex-end; }', page)
        for label in ('GET /proxies/{name}/delay', 'GET /connections', 'GET /traffic', 'GET /logs?level=info&amp;format=structured', '本地'):
            self.assertIn(label, page)
        self.assertNotIn('/configs', page)

    def test_config_api_never_returns_controller_secret(self):
        with patch.object(web, 'load_config', return_value={'secret': 'private-key'}):
            response = self.request('GET', '/api/config')
        payload = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertTrue(payload['secret_configured'])
        self.assertEqual(payload['secret'], '')
        self.assertNotIn('private-key', json.dumps(payload))

    def test_rejects_non_local_host(self):
        response = self.request('GET', '/api/state', headers={'Host': 'example.com'})
        self.assertEqual(response.status, 403)

    def test_accepts_ipv6_loopback_host_header(self):
        response = self.request('GET', '/api/config', headers={'Host': '[::1]:8765'})
        self.assertEqual(response.status, 200)

    def test_rejects_non_local_origin(self):
        response = self.request(
            'POST',
            '/api/stop',
            headers={'Origin': 'http://example.com'},
        )
        self.assertEqual(response.status, 403)


if __name__ == '__main__':
    unittest.main()
