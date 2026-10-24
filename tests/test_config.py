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

    def test_normalize_config_rejects_invalid_urls_and_fractional_counts(self):
        with self.assertRaises(ValueError):
            normalize_config({'controller': 'ftp://127.0.0.1:9090'})
        with self.assertRaises(ValueError):
            normalize_config({'test_url': 'not-a-url'})
        with self.assertRaises(ValueError):
            normalize_config({'rounds': 1.5})


if __name__ == '__main__':
    unittest.main()
