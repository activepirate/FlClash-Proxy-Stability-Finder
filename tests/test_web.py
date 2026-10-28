import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError, URLError
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

    def test_check_exit_ip_normalizes_ipapi_json(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                return b'{"ip":"2001:db8::1","country_name":"United Arab Emirates","city":"Al Fujairah City","timezone":"Asia/Dubai","utc_offset":"+0400"}'

        class Opener:
            def open(self, _request, timeout):
                self.timeout = timeout
                return Response()

        opener = Opener()
        with patch.object(web, 'build_opener', return_value=opener):
            result = web.check_exit_ip(3, 'ipapi')
        self.assertEqual(result['ip'], '2001:db8::1')
        self.assertEqual(result['version'], 6)
        self.assertEqual(result['country'], 'United Arab Emirates')
        self.assertEqual(result['city'], 'Al Fujairah City')
        self.assertEqual(result['timezone'], 'Asia/Dubai')
        self.assertEqual(result['utc_offset'], '+0400')
        self.assertIsNone(result['error'])
        self.assertEqual(opener.timeout, 3)

    def test_check_service_measures_normal_request_and_accepts_http_error_response(self):
        class Response:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Opener:
            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                return Response()

        opener = Opener()
        with patch.object(web, 'build_opener', return_value=opener):
            result = web.check_service('google', 4)
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['status_code'], 204)
        self.assertIsInstance(result['delay_ms'], int)
        self.assertEqual(opener.request.full_url, web.SERVICE_TARGETS['google'][1])
        self.assertEqual(opener.timeout, 4)

    def test_check_service_reports_timeout_without_delay(self):
        class Opener:
            def open(self, _request, timeout):
                raise TimeoutError('timed out')

        with patch.object(web, 'build_opener', return_value=Opener()):
            result = web.check_service('baidu', 1)
        self.assertEqual(result['status'], 'timeout')
        self.assertIsNone(result['delay_ms'])
        self.assertEqual(result['error'], 'timed out')

    def test_check_services_rejects_unknown_names(self):
        with self.assertRaises(ValueError):
            web.check_services(1, ['unknown'])

    def test_check_services_preserves_configured_name_and_url(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class Opener:
            def open(self, request, timeout):
                self.request = request
                return Response()

        opener = Opener()
        with patch.object(web, 'build_opener', return_value=opener):
            result = web.check_services(1, [{'name': '内网健康检查', 'url': 'https://example.com/health'}])
        self.assertEqual(result[0]['name'], '内网健康检查')
        self.assertEqual(result[0]['url'], 'https://example.com/health')

    def test_check_services_limits_batch_size(self):
        services = [
            {'name': f'service-{index}', 'url': f'https://example-{index}.test/' }
            for index in range(web.MAX_SERVICE_CHECKS + 1)
        ]
        with self.assertRaises(ValueError):
            web.check_services(1, services)

    def test_check_exit_ip_reads_ipify_plain_text(self):
        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                return self.body

        class Opener:
            def open(self, request, timeout):
                self.url = request.full_url
                self.timeout = timeout
                return Response(b'2001:db8::2')

        opener = Opener()
        with patch.object(web, 'build_opener', return_value=opener):
            result = web.check_exit_ip(3, 'ipify')
        self.assertEqual(result['ip'], '2001:db8::2')
        self.assertIsNone(result['country'])
        self.assertIsNone(result['city'])
        self.assertIsNone(result['timezone'])
        self.assertIsNone(result['utc_offset'])
        self.assertEqual(opener.url, web.EXIT_IP_PROVIDERS['ipify'])
        self.assertEqual(opener.timeout, 3)

    def test_check_exit_ip_normalizes_ipwho_timezone(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                return b'{"ip":"2001:db8::2","country":"United Arab Emirates",' \
                    b'"city":"Al Fujairah City","timezone":{"id":"Asia/Dubai",' \
                    b'"utc":"+04:00"}}'

        class Opener:
            def open(self, request, timeout):
                self.url = request.full_url
                self.timeout = timeout
                return Response()

        opener = Opener()
        with patch.object(web, 'build_opener', return_value=opener):
            result = web.check_exit_ip(3, 'ipwho')
        self.assertEqual(result['country'], 'United Arab Emirates')
        self.assertEqual(result['city'], 'Al Fujairah City')
        self.assertEqual(result['timezone'], 'Asia/Dubai')
        self.assertEqual(result['utc_offset'], '+04:00')
        self.assertEqual(opener.url, web.EXIT_IP_PROVIDERS['ipwho'])

    def test_check_exit_ip_uses_only_selected_provider(self):
        class Opener:
            def __init__(self):
                self.urls = []

            def open(self, request, timeout):
                self.urls.append(request.full_url)
                raise HTTPError(request.full_url, 429, 'Too Many Requests', {}, None)

        opener = Opener()
        with patch.object(web, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(RuntimeError, r'ipapi\.co：请求过于频繁（HTTP 429）'):
                web.check_exit_ip(3, 'ipapi')
        self.assertEqual(opener.urls, [web.EXIT_IP_PROVIDERS['ipapi']])

    def test_check_exit_ip_preserves_provider_label_for_url_errors(self):
        class Opener:
            def open(self, _request, timeout):
                raise URLError('TLS handshake failed')

        with patch.object(web, 'build_opener', return_value=Opener()):
            with self.assertRaises(RuntimeError) as context:
                web.check_exit_ip(3, 'ipwho')

        self.assertIn('ipwho.is', str(context.exception))
        self.assertIn('TLS handshake failed', str(context.exception))

    def test_check_exit_ip_supports_ipwhois_app_as_separate_provider(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size):
                return b'{"ip":"203.0.113.10","country":"Example",' \
                    b'"city":"Test City","timezone":"UTC","timezone_gmt":"+00:00"}'

        class Opener:
            def open(self, request, timeout):
                self.url = request.full_url
                return Response()

        opener = Opener()
        with patch.object(web, 'build_opener', return_value=opener):
            result = web.check_exit_ip(3, 'ipwhois')
        self.assertEqual(result['provider'], 'ipwhois')
        self.assertEqual(result['ip'], '203.0.113.10')
        self.assertEqual(opener.url, web.EXIT_IP_PROVIDERS['ipwhois'])

    def test_check_exit_ip_rejects_unknown_provider(self):
        with self.assertRaisesRegex(RuntimeError, '不支持的 IP 查询服务'):
            web.check_exit_ip(3, 'unknown')

    def test_collection_logs_keep_only_latest_200_entries(self):
        service = web.RecorderService()
        with service.lock:
            for index in range(250):
                service._append_log_locked(f'记录 {index}')

        self.assertEqual(len(service.logs), web.MAX_LOG_ENTRIES)
        self.assertEqual(service.logs[0], '记录 50')
        self.assertEqual(service.state()['logs'], [f'记录 {index}' for index in range(50, 250)])

    def test_start_tracks_total_samples_for_selected_proxies_and_rounds(self):
        class ControllerStub:
            def __init__(self, *_args):
                pass

            def proxies(self):
                return {
                    'node-a': {'type': 'ss'},
                    'node-b': {'type': 'vmess'},
                    'node-c': {'type': 'trojan'},
                }

            def delay(self, *_args):
                return 20

        service = web.RecorderService()
        values = {
            'controller': 'http://127.0.0.1:9090',
            'secret': '',
            'request_timeout': 1,
            'rounds': 3,
            'interval': 0,
            'concurrency': 2,
            'timeout_ms': 1000,
            'test_url': 'https://example.com',
        }
        with patch.object(web, 'Controller', ControllerStub):
            service.start(values, ['node-a', 'node-c'])

        self.assertEqual(service.state()['total_samples'], 6)

    def test_homepage_displays_completed_and_total_sample_counts(self):
        response = self.request('GET', '/')
        page = response.read().decode()
        self.assertIn("$('metric-samples').textContent = `${samples}/${state.total_samples}`;", page)

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
        self.assertIn('FlClash 代理稳定性筛选器', page)
        self.assertNotIn('<div class="top-status">', page)
        for endpoint in ('控制器遥测', '活动连接', '控制器运行日志', '/traffic', '/logs', '/connections'):
            self.assertIn(endpoint, page)
        for element_id in (
            'exit-ip-ipify-ip', 'exit-ip-ipify-country', 'exit-ip-ipify-city',
            'exit-ip-ipify-timezone', 'exit-ip-ipify-utc_offset', 'exit-ip-ipify-meta',
            'exit-ip-ipapi-ip', 'exit-ip-ipapi-country', 'exit-ip-ipapi-city',
            'exit-ip-ipapi-timezone', 'exit-ip-ipapi-utc_offset', 'exit-ip-ipapi-meta',
            'exit-ip-ipwho-ip', 'exit-ip-ipwho-country', 'exit-ip-ipwho-city',
            'exit-ip-ipwho-timezone', 'exit-ip-ipwho-utc_offset', 'exit-ip-ipwho-meta',
        ):
            self.assertIn(f'id="{element_id}"', page)
        self.assertNotIn('<span class="exit-ip-cell">服务</span>', page)
        self.assertIn('<h2>出口 IP 探测</h2>', page)
        self.assertIn('.exit-ip-table-wrap { min-width: 0; }', page)
        self.assertIn('.exit-ip-table { width: 100%; min-width: 0; }', page)
        self.assertNotIn('overflow-x: auto;', page[page.index('.exit-ip-panel'):page.index('.proxy-summary-inline')])
        self.assertNotIn('@container (max-width: 760px)', page)
        self.assertNotIn('.exit-ip-header { display: none; }', page)
        self.assertNotIn('.exit-ip-row:not(.exit-ip-header)', page)
        self.assertIn('.exit-ip-row { display: grid;', page)
        self.assertIn('grid-template-columns: minmax(0, 1.2fr)', page)
        self.assertIn('探测信息', page)
        self.assertIn('<span class="exit-ip-cell">探测服务</span>', page)
        self.assertNotIn('<h2>延迟采样概览</h2>', page)
        self.assertNotIn('aria-label="观测概览"', page)
        self.assertIn('<h2>服务可达性</h2>', page)
        self.assertIn('本机网络探测</span>', page)
        connection_header = '<thead><tr><th>网络</th><th>进程</th><th>开始时间</th><th>来源</th>'
        self.assertIn(connection_header, page)
        self.assertIn('formatChinaTime(connection.start)', page)
        self.assertNotIn('不显示代理节点', page)
        self.assertNotIn('id="service-status-time"', page)
        self.assertIn("const CHINA_TIME_ZONE = 'Asia/Shanghai'", page)
        self.assertIn("const CHINA_TIME_OFFSET = '+08:00'", page)
        self.assertIn('formatChinaTime(entry.time)', page)
        self.assertIn('? formatChinaTime(result.checked_at)', page)
        self.assertNotIn('探测时间：中国时间 ${formatChinaTime(result.checked_at)}', page)
        self.assertIn('探测目标</span><span>探测 URL</span><span>探测结果</span><span>最近探测</span><span>操作', page)
        self.assertIn('id="service-status-list" role="list"', page)
        self.assertIn("card.setAttribute('role', 'listitem')", page)
        self.assertIn('class="button service-edit" type="button">修改</button>', page)
        self.assertIn("function editService(url)", page)
        self.assertIn("let editingServiceUrl = null", page)
        self.assertIn("function openServiceDialog(service = null)", page)
        self.assertIn("service.url !== originalUrl", page)
        self.assertIn('id="proxy-search"', page)
        self.assertIn('placeholder="搜索代理名称"', page)
        self.assertIn("proxy.name.toLocaleLowerCase().includes(query)", page)
        self.assertIn('没有找到匹配的代理节点', page)
        self.assertIn('function replaceLogContents(body, children)', page)
        self.assertIn('const wasAtBottom = body.scrollHeight - body.clientHeight - scrollTop <= 2', page)
        self.assertIn('body.scrollTop = wasAtBottom ? body.scrollHeight : scrollTop', page)
        self.assertIn('replaceLogContents(body, lines)', page)
        self.assertIn("replaceLogContents($('log-body'), logLines)", page)
        self.assertNotIn('body.scrollTop = body.scrollHeight;', page)
        self.assertIn('id="open-service-add"', page)
        self.assertIn('id="service-add-dialog"', page)
        self.assertIn('id="service-add-form"', page)
        self.assertIn('id="submit-service-add" class="button primary" type="submit"', page)
        self.assertIn("$('service-add-form').addEventListener('submit'", page)
        self.assertIn('id="service-name"', page)
        self.assertIn('id="service-url"', page)
        self.assertIn('id="service-name-error"', page)
        self.assertIn('id="service-url-error"', page)
        self.assertIn('data.services.forEach((result) => setServiceCard(result.url, result));', page)
        self.assertIn('目标 URL 已存在', page)
        for provider, label in (('ipify', 'ipify'), ('ipapi', 'ipapi.co'), ('ipwho', 'ipwho.is'), ('ipwhois', 'ipwhois.app')):
            self.assertIn(f'data-provider="{provider}"', page)
            self.assertIn(f'>{label}</button>', page)
        self.assertNotIn('>探测服务</button>', page)
        self.assertNotIn('fallback', page)
        self.assertNotIn('await refreshExitIp()', page)
        for removed_control in ('id="select-all"', 'id="select-none"', 'id="select-toggle"', 'class="proxy-check'):
            self.assertNotIn(removed_control, page)
        self.assertNotIn('selection-count', page)
        for row_limit in ('collection-log-limit', 'connection-limit', 'controller-log-limit', 'value="10" selected'):
            self.assertIn(row_limit, page)
        for sort_header in ('data-sort-key="rate"', 'data-sort-key="minimum"', 'data-sort-key="maximum"', 'data-sort-key="average"', 'data-sort-key="jitter"', 'active-sorts', 'sort-order', 'sort-remove', '按成功率排序', '按最小延迟排序', '按最大延迟排序', '按平均延迟排序', '按抖动排序', 'aria-sort="none"'):
            self.assertIn(sort_header, page)
        self.assertLess(page.index('data-sort-label="最小延迟"'), page.index('data-sort-label="平均延迟"'))
        self.assertLess(page.index('data-sort-label="最大延迟"'), page.index('data-sort-label="平均延迟"'))
        self.assertNotIn('id="proxy-sort"', page)
        self.assertIn('grid-template-columns: minmax(0, 1.25fr) minmax(360px, 1fr)', page)
        self.assertIn('<div class="connection-column">', page)
        self.assertIn('.dashboard-columns { grid-template-columns: minmax(0, 1fr); }', page)
        connection_column = page[page.index('<div class="connection-column">'):page.index('</div>\n      </div>', page.index('<div class="connection-column">'))]
        self.assertLess(connection_column.index('<h2>控制器遥测</h2>'), connection_column.index('<h2>代理/直连流量</h2>'))
        self.assertLess(connection_column.index('<h2>代理/直连流量</h2>'), connection_column.index('<h2>活动连接</h2>'))
        self.assertLess(connection_column.index('<h2>活动连接</h2>'), connection_column.index('<h2>控制器运行日志</h2>'))
        primary_column = page[page.index('<div class="primary-column">'):page.index('<div class="connection-column">')]
        self.assertNotIn('class="panel summary"', primary_column)
        results_panel = primary_column[primary_column.index('<h2>代理延迟采样</h2>'):primary_column.index('<section class="panel logs">')]
        self.assertLess(results_panel.index('<h2>代理延迟采样</h2>'), results_panel.index('aria-label="延迟采样概览"'))
        self.assertLess(results_panel.index('aria-label="延迟采样概览"'), results_panel.index('proxy-results-actions'))
        self.assertLess(results_panel.index('proxy-results-actions'), results_panel.index('id="active-sorts"'))
        results_panel_markup = primary_column[primary_column.index('class="section-head proxy-results-head"'):primary_column.index('<div class="table-wrap">')]
        self.assertLess(results_panel_markup.index('<h2>代理延迟采样</h2>'), results_panel_markup.index('aria-label="延迟采样概览"'))
        self.assertLess(results_panel_markup.index('aria-label="延迟采样概览"'), results_panel_markup.index('proxy-results-actions'))
        self.assertLess(results_panel_markup.index('id="stop"'), results_panel_markup.index('id="open-settings"'))
        self.assertIn('.proxy-results-actions { justify-content: flex-end;', page)
        for label in ('GET /proxies/{name}/delay', 'GET /connections', 'GET /traffic', 'GET /logs?level=info&amp;format=structured', '本地运行日志'):
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

    def test_update_config_rejects_non_boolean_secret_reset(self):
        with patch.object(web, 'load_config', return_value={'secret': 'private-key'}):
            with self.assertRaises(ValueError):
                web.update_config({'clear_secret': 'false'})

    def test_exit_ip_api_returns_probe_result(self):
        with patch.object(
            web,
            'load_config',
            return_value={'request_timeout': 2},
        ), patch.object(
            web,
            'check_exit_ip',
            return_value={
                'ip': '203.0.113.10',
                'version': 4,
                'country': 'United States',
                'city': 'New York',
                'timezone': 'America/New_York',
                'utc_offset': '-0400',
                'checked_at': '2026-10-25T00:00:00Z',
                'error': None,
            },
        ):
            response = self.request('GET', '/api/exit-ip?provider=ipwho')
        payload = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(payload['ip'], '203.0.113.10')

    def test_services_api_checks_requested_services(self):
        expected = [{'name': 'github', 'status': 'available', 'delay_ms': 42}]
        with patch.object(
            web,
            'load_config',
            return_value={'request_timeout': 2},
        ), patch.object(web, 'check_services', return_value=expected) as check:
            response = self.request('GET', '/api/services?name=github')
        payload = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(payload['services'], expected)
        check.assert_called_once_with(2.0, ['github'])

    def test_services_api_uses_configured_services_by_default(self):
        configured = [
            {'name': 'Google', 'url': 'https://www.google.com/generate_204'},
            {'name': '自定义', 'url': 'https://example.com/health'},
        ]
        expected = [{'name': '自定义', 'url': 'https://example.com/health', 'status': 'available'}]
        with patch.object(
            web,
            'load_config',
            return_value={'request_timeout': 2, 'services': configured},
        ), patch.object(web, 'check_services', return_value=expected) as check:
            response = self.request('GET', '/api/services')
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())['services'], expected)
        check.assert_called_once_with(2.0, configured)

    def test_services_api_accepts_custom_url(self):
        expected = [{'name': 'https://example.com/health', 'status': 'failed'}]
        with patch.object(
            web,
            'load_config',
            return_value={'request_timeout': 2},
        ), patch.object(web, 'check_services', return_value=expected) as check:
            response = self.request('GET', '/api/services?url=https%3A%2F%2Fexample.com%2Fhealth')
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())['services'], expected)
        check.assert_called_once_with(2.0, None, ['https://example.com/health'])

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
