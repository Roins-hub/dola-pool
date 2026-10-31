"""Exercise public health output without opening live stores or browser workers."""
import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest


class PublicHealthTests(unittest.TestCase):
    def test_public_health_reports_counts_without_account_or_proxy_details(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8"))
        node = next(node for node in tree.body
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "health")
        node.decorator_list = []
        namespace = dict(
            app=SimpleNamespace(version='0.5.0'),
            pool=SimpleNamespace(
                cookie_count=2, available=True,
                account_status=lambda: [
                    {"account": "private-account-a", "proxy_server": "http://192.0.2.9:8080"},
                    {"account": "private-account-b", "proxy_used": 123},
                ],
            ),
            store=SimpleNamespace(pending_task_count=lambda: 3),
            config=SimpleNamespace(MAX_PENDING_TASKS=50, MAX_CONCURRENCY=2,
                                   MAX_INFLIGHT=4, POLL_INTERVAL=15, MAX_DOWNLOADS=2,
                                   PROXY_REQUEST_LIMIT_ENABLED=False),
        )
        module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
        exec(compile(module, "server-health-endpoint", "exec"), namespace)
        result = asyncio.run(namespace["health"]())
        serialized = json.dumps(result)
        for sensitive in ("private-account", "192.0.2.9", "proxy_server", "proxy_used"):
            self.assertNotIn(sensitive, serialized)
        self.assertEqual(result["accounts"], 2)
        self.assertTrue(result["ok"])
        self.assertFalse(result["isp_request_limit_enabled"])
        self.assertEqual(result['version'], '0.5.0')
        self.assertTrue(result["available"])
        self.assertEqual(result["pending_tasks"], 3)
        self.assertEqual(result["submission_concurrency"], 2)


if __name__ == "__main__":
    unittest.main()
