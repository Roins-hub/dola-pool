"""Run local unit/transport tests; interactive browser cases are checked through CUA."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / 'tests'))
os.chdir(root)
with tempfile.TemporaryDirectory() as directory:
    os.environ['DOLA_PROXY_USAGE_DB'] = str(Path(directory) / 'proxy.db')
    suite = unittest.defaultTestLoader.discover(str(root / 'tests'))
    def mark_cases(group):
        for item in group:
            if isinstance(item, unittest.TestSuite):
                mark_cases(item)
            elif item.__class__.__module__ in ('test_page_readiness', 'test_birthday_repair'):
                item.__class__.__unittest_skip__ = True
                item.__class__.__unittest_skip_why__ = 'Interactive browser verification uses CUA'
    mark_cases(suite)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    import proxy_usage
    proxy_usage.usage._conn.close()
sys.exit(0 if result.wasSuccessful() else 1)
