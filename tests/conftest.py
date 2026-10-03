"""No test may import the app against operational stores or profiles."""
import os
import tempfile
from pathlib import Path

_repo = Path(__file__).resolve().parents[1]
_isolated = tempfile.TemporaryDirectory(prefix="dola-pytest-")
_root = Path(_isolated.name)
os.environ.update({"DOLA_SKIP_LOCAL_ENV": "1", "DOLA_DB_PATH": str(_root / "tasks.db"),
    "DOLA_POOL_DB_PATH": str(_root / "pool.db"), "DOLA_DOWNLOAD_DIR": str(_root / "downloads"),
    "DOLA_AI_ENC_FILE": str(_root / "ai.enc"), "DOLA_AI_KEY_FILE": str(_root / "ai.key"),
    "DOLA_CRED_FILE": str(_root / "creds.enc"), "DOLA_CRED_KEY_FILE": str(_root / "cred.key"),
    "DOLA_PROXY": "", "DOLA_ADMIN_KEY": "", "DOLA_API_KEYS": ""})
(_root / "web").symlink_to(_repo / "web", target_is_directory=True)
(_root / "downloads").mkdir()
os.chdir(_root)
# Existing fixtures override these attributes; keep their semantics safely.
import ai_settings
ai_settings._ENCFILE = ai_settings._DEFAULT_ENCFILE = _root / "ai.enc"
ai_settings._KEYFILE = ai_settings._DEFAULT_KEYFILE = _root / "ai.key"
os.environ.pop("DOLA_AI_ENC_FILE")
os.environ.pop("DOLA_AI_KEY_FILE")

import asyncio
import pytest

@pytest.fixture(autouse=True)
def legacy_sync_event_loop():
    """Legacy synchronous tests use get_event_loop after asyncio.run closed it."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
        created = None
    except RuntimeError:
        created = asyncio.new_event_loop()
        asyncio.set_event_loop(created)
    yield
    if created and not created.is_closed():
        created.close()
