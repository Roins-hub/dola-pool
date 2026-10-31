"""Exercise the standalone CLI without opening a socket or loading live accounts."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


LAUNCHER = Path(__file__).resolve().parents[1] / 'run.py'


class StudioLauncherTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(LAUNCHER.is_file(), 'The standalone Studio launcher is missing')
        directory = tempfile.TemporaryDirectory(prefix='studio launcher ')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.studio = self.root / 'editions' / 'studio'
        self.studio.mkdir(parents=True)
        shutil.copyfile(LAUNCHER, self.studio / 'run.py')
        (self.studio / 'server.py').write_text('app = object()\n', encoding='utf-8')
        self.caller = self.root / 'caller'
        self.caller.mkdir()
        (self.caller / 'server.py').write_text('raise RuntimeError("wrong edition")\n', encoding='utf-8')
        self.dependencies = self.root / 'dependencies'
        self.dependencies.mkdir()
        (self.dependencies / 'uvicorn.py').write_text('''
import importlib.util
import json
import os

def run(app, *, host, port):
    print(json.dumps({
        'app': app, 'host': host, 'port': port, 'cwd': os.getcwd(),
        'env_host': os.environ.get('DOLA_HOST'),
        'env_port': os.environ.get('DOLA_PORT'),
        'server': importlib.util.find_spec(app.split(':')[0]).origin,
    }))
''', encoding='utf-8')
        self.env = dict(os.environ, PYTHONPATH=str(self.dependencies),
                        DOLA_HOST='0.0.0.0', DOLA_PORT='8000')

    def launch(self, *arguments):
        result = subprocess.run(
            [sys.executable, str(self.studio / 'run.py'), *arguments],
            cwd=self.caller, env=self.env, capture_output=True,
            encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_defaults_use_studio_directory_and_separate_loopback_port(self):
        report = self.launch()
        self.assertEqual(report['app'], 'server:app')
        self.assertEqual(Path(report['cwd']), self.studio)
        self.assertEqual(Path(report['server']), self.studio / 'server.py')
        self.assertEqual((report['host'], report['port']), ('127.0.0.1', 8001))
        self.assertEqual((report['env_host'], report['env_port']), ('127.0.0.1', '8001'))

    def test_cli_host_and_port_match_internal_configuration(self):
        report = self.launch('--host', '0.0.0.0', '--port', '8765')
        self.assertEqual((report['host'], report['port']), ('0.0.0.0', 8765))
        self.assertEqual((report['env_host'], report['env_port']), ('0.0.0.0', '8765'))

    def test_import_does_not_start_service_or_change_caller_state(self):
        script = '''
import json, os, runpy, sys
before = (os.getcwd(), dict(os.environ), list(sys.path))
runpy.run_path(sys.argv[1], run_name='studio_launcher')
print(json.dumps({'unchanged': before == (os.getcwd(), dict(os.environ), list(sys.path))}))
'''
        result = subprocess.run(
            [sys.executable, '-c', script, str(self.studio / 'run.py')],
            cwd=self.caller, env=self.env, capture_output=True,
            encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'unchanged': True})


if __name__ == '__main__':
    unittest.main()
