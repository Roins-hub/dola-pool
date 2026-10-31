"""Account route names follow the assigned endpoint without guessing unknown regions."""
import ast
import json
from pathlib import Path
import unittest

import config


class ProxyRouteLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = Path(__file__).resolve().parents[1].joinpath('server.py').read_text(encoding='utf-8')
        node = next(n for n in ast.parse(source).body
                    if isinstance(n, ast.FunctionDef) and n.name == '_proxy_display')
        namespace = {'config': config}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                     'proxy-route-display', 'exec'), namespace)
        cls.display = staticmethod(namespace['_proxy_display'])

    def test_bound_endpoint_selects_country_and_line_without_exposing_credentials(self):
        for host, expected in (
            ('192.0.2.11', '英国线路 1'),
            ('192.0.2.12', '英国线路 2'),
            ('192.0.2.13', '英国线路 3'),
            ('192.0.2.14', '德国线路 1'),
            ('192.0.2.15', '德国线路 2'),
        ):
            with self.subTest(host=host):
                result = self.display(f'http://test-user:private-test-password@{host}:11000')
                self.assertEqual(result.get('route_label'), expected)
                self.assertEqual(result['host'], host)
                self.assertNotIn('private-test-password', json.dumps(result))

    def test_unknown_endpoint_is_not_mislabelled_from_an_ip_prefix(self):
        for raw in ('http://178.93.15.139:11000', 'http://127.0.0.1:7890', ''):
            with self.subTest(raw=raw):
                self.assertEqual(self.display(raw).get('route_label'), '')


if __name__ == '__main__':
    unittest.main()
