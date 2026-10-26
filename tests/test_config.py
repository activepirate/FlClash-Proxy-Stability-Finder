import unittest

from app.config import normalize_config


class ConfigTests(unittest.TestCase):
    def test_normalize_config_coerces_supported_values(self):
        values = normalize_config({
            'controller': ' http://127.0.0.1:9090/ ',
            'rounds': '3',
            'interval': '1.5',
            'concurrency': 4,
            'timeout_ms': '2000',
            'request_timeout': 2,
            'test_url': ' https://example.com/check ',
        })
        self.assertEqual(values['controller'], 'http://127.0.0.1:9090/')
        self.assertEqual(values['rounds'], 3)
        self.assertEqual(values['interval'], 1.5)
        self.assertEqual(values['concurrency'], 4)
        self.assertEqual(values['test_url'], 'https://example.com/check')
        self.assertEqual(
            values['services'],
            [
                {'name': 'Google', 'url': 'https://www.google.com/generate_204'},
                {'name': 'GitHub', 'url': 'https://github.com/'},
                {'name': '百度', 'url': 'https://www.baidu.com/'},
            ],
        )

    def test_normalize_config_accepts_custom_services(self):
        values = normalize_config({
            'services': [{'name': '内部健康检查', 'url': ' http://example.test/health ' }],
        })
        self.assertEqual(values['services'], [{'name': '内部健康检查', 'url': 'http://example.test/health'}])

    def test_normalize_config_rejects_invalid_urls_and_fractional_counts(self):
        with self.assertRaises(ValueError):
            normalize_config({'controller': 'ftp://127.0.0.1:9090'})
        with self.assertRaises(ValueError):
            normalize_config({'test_url': 'not-a-url'})
        with self.assertRaises(ValueError):
            normalize_config({'rounds': 1.5})
        with self.assertRaises(ValueError):
            normalize_config({'services': [{'name': 'bad', 'url': 'ftp://example.com'}]})
        with self.assertRaises(ValueError):
            normalize_config({'services': [{'name': 'same', 'url': 'https://example.com'}, {'name': 'SAME', 'url': 'https://other.example'}]})

    def test_normalize_config_rejects_malformed_service_hosts(self):
        for url in ('http://:80/', 'http://example.com:99999/', 'http://exa mple.com/', 'http://example.com/\nhealth'):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    normalize_config({'services': [{'name': 'invalid', 'url': url}]})

    def test_normalize_config_rejects_malformed_controller_and_test_urls(self):
        for field, value in (
            ('controller', 'http://:9090'),
            ('controller', 'http://user:pass@example.com:99999'),
            ('test_url', 'https://example.com:99999/health'),
            ('test_url', 'https://user@example.com/health'),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValueError):
                    normalize_config({field: value})

    def test_normalize_config_treats_null_secret_as_empty(self):
        self.assertEqual(normalize_config({'secret': None})['secret'], '')


if __name__ == '__main__':
    unittest.main()
