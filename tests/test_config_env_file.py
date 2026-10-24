"""DOLA_ENV_FILE：start-local.ps1 把 dola.env 交给 config.py 再读一遍。

Windows 上 SetEnvironmentVariable 传空值等于删除，`DOLA_PROXY=`（显式不走代理）会退回默认的
http://127.0.0.1:7890；config.py 从文件里补回空串。用子进程导入，免得改动本进程里已加载的 config。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_config(env_file, extra_env):
    env = {k: v for k, v in os.environ.items() if not k.startswith("DOLA_")}
    env.update(extra_env)
    if env_file:
        env["DOLA_ENV_FILE"] = str(env_file)
    code = "import json, config; print(json.dumps({'proxy': config.PROXY, 'port': config.PORT}))"
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_empty_value_in_env_file_is_kept(tmp_path):
    f = tmp_path / "dola.env"
    f.write_text("# 本机就在日本，不走代理\nDOLA_PROXY=\n", encoding="utf-8")
    assert _load_config(f, {})["proxy"] == ""


def test_real_environment_wins(tmp_path):
    f = tmp_path / "dola.env"
    f.write_text("DOLA_PROXY=\nDOLA_PORT=9000\n", encoding="utf-8")
    cfg = _load_config(f, {"DOLA_PROXY": "http://10.0.0.1:8080", "DOLA_PORT": "8100"})
    assert cfg["proxy"] == "http://10.0.0.1:8080"
    assert str(cfg["port"]) == "8100"


def test_without_env_file_default_unchanged():
    assert _load_config(None, {})["proxy"] == "http://127.0.0.1:7890"


def test_explicit_env_file_overrides_local_defaults(tmp_path):
    # An isolated config copy avoids touching the developer's own .env.local.
    (tmp_path / "config.py").write_bytes((ROOT / "config.py").read_bytes())
    (tmp_path / ".env.local").write_text(
        "DOLA_PROXY=http://127.0.0.1:7890\nDOLA_PORT=8100\nDOLA_MAX_PENDING_TASKS=17\n",
        encoding="utf-8")
    explicit = tmp_path / "dola.env"
    explicit.write_text("DOLA_PROXY=\nDOLA_PORT=9200\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("DOLA_")}
    env["DOLA_ENV_FILE"] = str(explicit)
    code = "import config,json; print(json.dumps([config.PROXY, config.PORT, config.MAX_PENDING_TASKS]))"
    def read():
        r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env,
                           capture_output=True, text=True, check=True)
        return json.loads(r.stdout)
    assert read() == ["", 9200, 17]
    env["DOLA_PROXY"] = "http://10.0.0.1:8080"
    env["DOLA_PORT"] = "9300"
    assert read() == ["http://10.0.0.1:8080", 9300, 17]
