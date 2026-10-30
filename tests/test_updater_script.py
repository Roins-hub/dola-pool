"""真实运行 deploy/updater.sh（源码部署的一键更新程序）：临时 git 仓库 + 替身 systemctl / health。

覆盖：git 方式更新成功、新版本起不来自动回滚、有本地改动拒绝、非 git 部署用源码包更新且不碰 dola.env。
需要 Linux 的 bash / git / tar / curl；Windows 上跳过（CI 是 Linux）。
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name == "nt" or not shutil.which("bash") or not shutil.which("git"),
                                reason="needs a POSIX shell with git")


def sh(cmd, cwd=None, env=None):
    return subprocess.run(cmd, cwd=cwd, env=env, shell=True, check=True, capture_output=True, text=True)


def make_origin(tmp):
    origin = tmp / "origin"
    origin.mkdir()
    sh("git init -q && git config user.email t@t && git config user.name t", cwd=origin)
    (origin / "requirements.txt").write_text("fastapi\n")
    (origin / "deploy").mkdir()
    shutil.copy(ROOT / "deploy" / "updater.sh", origin / "deploy" / "updater.sh")
    for version in ("1.0.0", "1.1.0"):
        (origin / "server.py").write_text(f'APP_VERSION = "{version}"\n')
        sh(f"git add -A && git commit -qm v{version} && git tag v{version}", cwd=origin)
    return origin


def make_env(tmp, app, origin, **extra):
    stub = tmp / "stub"
    stub.mkdir(exist_ok=True)
    health = tmp / "health.json"
    # 替身 systemctl：restart 时按 server.py 的版本写 health；FAIL_VERSION 版本「起不来」
    (stub / "systemctl").write_text(f"""#!/bin/bash
echo "$@" >> {tmp}/systemctl.log
v=$(grep -oE '[0-9]+\\.[0-9]+\\.[0-9]+' {app}/server.py)
if [ -n "${{FAIL_VERSION:-}}" ] && [ "$v" = "$FAIL_VERSION" ]; then v=broken; fi
printf '{{"version":"%s"}}' "$v" > {health}
""")
    (stub / "systemctl").chmod(0o755)
    (tmp / "update").mkdir(exist_ok=True)
    (tmp / "update" / "request.json").write_text(json.dumps({"id": "req1", "version": "1.1.0"}))
    (tmp / "update" / "ready").write_text("{}")
    env = {**os.environ, "DOLA_UPDATER_CONF": "/nonexistent", "APP_DIR": str(app), "SERVICE": "fake",
           "SVC_USER": subprocess.run("id -un", shell=True, capture_output=True, text=True).stdout.strip(),
           "UPDATE_DIR": str(tmp / "update"), "HEALTH_URL": f"file://{health}", "GIT_URL": str(origin),
           "BACKUP_DIR": str(tmp / "backups"), "SYSTEMCTL": str(stub / "systemctl"), "VERIFY_SECONDS": "3",
           "PY": sys.executable, **extra}
    return env


def run_updater(env):
    return subprocess.run(["bash", str(ROOT / "deploy" / "updater.sh")], env=env, capture_output=True, text=True)


def agent_status(tmp):
    return json.loads((tmp / "update" / "agent-status.json").read_text())


def version_of(app):
    return (app / "server.py").read_text().split('"')[1]


def git_app(tmp, origin):
    app = tmp / "app"
    sh(f"git clone -q {origin} {app} && git -C {app} checkout -q -B main v1.0.0")
    (app / "tasks.db").write_bytes(b"")
    (app / "dola.env").write_text("DOLA_SECRET=keep\n")
    return app


def test_git_update_succeeds_and_clears_the_request(tmp_path):
    origin = make_origin(tmp_path)
    app = git_app(tmp_path, origin)
    result = run_updater(make_env(tmp_path, app, origin))
    status = agent_status(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert status["state"] == "done" and status["id"] == "req1" and "git" in status["message"]
    assert version_of(app) == "1.1.0" and (app / "dola.env").read_text() == "DOLA_SECRET=keep\n"
    assert not (tmp_path / "update" / "request.json").exists() and not (tmp_path / "update" / "ready").exists()
    backups = list((tmp_path / "backups").iterdir())
    assert len(backups) == 1 and (backups[0] / "code.tar").exists() and (backups[0] / "tasks.db").exists()
    assert "restart fake" in (tmp_path / "systemctl.log").read_text()


def test_new_version_that_does_not_come_up_is_rolled_back(tmp_path):
    origin = make_origin(tmp_path)
    app = git_app(tmp_path, origin)
    result = run_updater(make_env(tmp_path, app, origin, FAIL_VERSION="1.1.0"))
    status = agent_status(tmp_path)
    assert result.returncode == 1
    assert status["state"] == "rolled_back" and "1.0.0" in status["message"]
    assert version_of(app) == "1.0.0"
    assert (tmp_path / "systemctl.log").read_text().count("restart fake") == 2
    assert not (tmp_path / "update" / "request.json").exists()


def test_local_changes_block_the_update_without_touching_files(tmp_path):
    origin = make_origin(tmp_path)
    app = git_app(tmp_path, origin)
    (app / "requirements.txt").write_text("fastapi\nhotfix\n")
    result = run_updater(make_env(tmp_path, app, origin))
    status = agent_status(tmp_path)
    assert result.returncode == 1 and status["state"] == "failed"
    assert "未提交的改动" in status["message"] and "未改动任何文件" in status["message"]
    assert version_of(app) == "1.0.0" and not (tmp_path / "systemctl.log").exists()


def test_non_git_install_updates_from_the_source_tarball(tmp_path):
    origin = make_origin(tmp_path)
    app = tmp_path / "app"
    sh(f"mkdir -p {app} && git -C {origin} archive v1.0.0 | tar -x -C {app}")
    (app / "dola.env").write_text("DOLA_SECRET=keep\n")
    sh(f"git -C {origin} archive --prefix=dola-pool-1.1.0/ v1.1.0 | gzip > {tmp_path}/v1.1.0.tgz")
    env = make_env(tmp_path, app, origin, TARBALL_URL=f"file://{tmp_path}/%s.tgz")
    result = run_updater(env)
    status = agent_status(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert status["state"] == "done" and "tarball" in status["message"]
    assert version_of(app) == "1.1.0" and (app / "dola.env").read_text() == "DOLA_SECRET=keep\n"


def test_nothing_to_do_without_a_ready_request(tmp_path):
    origin = make_origin(tmp_path)
    app = git_app(tmp_path, origin)
    env = make_env(tmp_path, app, origin)
    (tmp_path / "update" / "ready").unlink()
    result = run_updater(env)
    assert result.returncode == 0 and version_of(app) == "1.0.0"
    assert not (tmp_path / "update" / "agent-status.json").exists()
