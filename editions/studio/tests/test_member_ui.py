"""Exercise the actual member endpoint functions without starting browser workers."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest

from video_capabilities import SUPPORTED_DURATIONS_BY_MODEL


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        self.detail = detail


class StoreFixture:
    def __init__(self):
        self.calls = []
        self.keys = {
            1: [self.key("own-a"), self.key("own-b")],
            2: [self.key("other-user")],
        }

    @staticmethod
    def key(value):
        return dict(key=value, name=value, enabled=True, daily_limit=10,
                    concurrency_limit=2, allowed_durations=[10, 15],
                    expires_at=0, created_at=1, last_used_at=0)

    def keys_for_user(self, user_id):
        return self.keys.get(user_id, [])

    def hash_api_key(self, key):
        return "hash:" + key

    def usage_for_hashes(self, hashes):
        self.calls.append(("usage", hashes[:]))
        counts = {"hash:own-a": 2, "hash:own-b": 5, "hash:other-user": 99}
        return dict(total=sum(counts.get(h, 0) for h in hashes), completed=1,
                    failed=0, active=0, queued=0)

    def recent_tasks_for_hashes(self, hashes, limit):
        self.calls.append(("tasks", hashes[:], limit))
        return [dict(id="video_owned", prompt="本人提示词", model="seedance-2.0",
                     ratio="16:9", duration=10, status="failed", created_at=1,
                     account="private-account", api_key_hash=hashes[0],
                     api_key_name="private-key-name", error="503 proxy 192.0.2.9 private-account")]

    def count_users(self):
        return 2


class MemberUITests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8"))
        names = {"me", "me_tasks", "_member_task", "_admin_auth", "_require_admin"}
        functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        for node in functions:
            node.decorator_list = []
        self.store = StoreFixture()
        self.user = {"id": 1, "email": "member@example.test", "role": "member"}
        self.ns = dict(store=self.store, config=SimpleNamespace(PUBLIC_BASE="https://example.test", ADMIN_KEY="admin-secret"),
                       SUPPORTED_DURATIONS=(5, 10, 15, 30),
                       SUPPORTED_DURATIONS_BY_MODEL=SUPPORTED_DURATIONS_BY_MODEL,
                       HTTPException=HTTPException,
                       Header=lambda **kwargs: None,
                       _require_user=lambda authorization: self.user,
                       _session_user=lambda authorization: self.user,
                       _public_user=lambda user: dict(user))
        module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
        exec(compile(module, "server-member-endpoints", "exec"), self.ns)

    def test_each_key_has_its_own_usage_and_personal_total(self):
        result = asyncio.run(self.ns["me"]("session"))
        self.assertEqual([k["key"] for k in result["keys"]], ["own-a", "own-b"])
        self.assertEqual([k["usage"]["total"] for k in result["keys"]], [2, 5])
        self.assertEqual(result["usage"]["total"], 7)
        self.assertEqual(result["supported_durations"], [5, 10, 15, 30])
        self.assertNotIn("other-user", str(result))

    def test_task_lookup_uses_only_current_user_keys_and_caps_range(self):
        result = asyncio.run(self.ns["me_tasks"](9999, "session"))
        self.assertIn(("tasks", ["hash:own-a", "hash:own-b"], 200), self.store.calls)
        task = result["tasks"][0]
        self.assertEqual(task["id"], "video_owned")
        self.assertEqual(task["prompt"], "本人提示词")
        for private_field in ("account", "api_key_hash", "api_key_name"):
            self.assertNotIn(private_field, task)
        self.assertNotIn("192.0.2.9", task["error"])
        self.assertNotIn("private-account", task["error"])

    def test_another_member_has_a_separate_scope(self):
        self.user = {"id": 2, "role": "member", "email": "other@example.test"}
        result = asyncio.run(self.ns["me_tasks"](-5, "session"))
        self.assertIn(("tasks", ["hash:other-user"], 1), self.store.calls)
        self.assertNotIn("hash:own-a", str(result))

    def test_empty_keys_return_zero_personal_usage(self):
        self.user = {"id": 3, "role": "member", "email": "empty@example.test"}
        result = asyncio.run(self.ns["me"]("session"))
        self.assertEqual(result["keys"], [])
        self.assertEqual(result["usage"]["total"], 0)

    def test_member_errors_keep_useful_category_without_internals(self):
        source = dict(id="video_test", prompt="test", error="679s 未出片 conversation_id=3841 account=secret")
        public = self.ns["_member_task"](source)
        self.assertIn("超时", public["error"])
        self.assertNotIn("conversation_id", public["error"])
        self.assertIn("secret", source["error"])
        self.assertIsNone(self.ns["_member_task"]({"id": "ok", "error": None})["error"])

    def test_member_session_cannot_use_admin_authentication(self):
        with self.assertRaises(HTTPException) as denied:
            self.ns["_admin_auth"](None, "member-session")
        self.assertEqual(denied.exception.status_code, 401)
        self.user = {"id": 1, "role": "admin"}
        self.assertEqual(self.ns["_admin_auth"](None, "admin-session")["role"], "admin")


if __name__ == "__main__":
    unittest.main()
