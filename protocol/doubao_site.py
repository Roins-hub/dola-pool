"""豆包（doubao.com）站点：复用 dola_pure_api 的协议实现。

dola.com 是豆包的海外版，网页接口同一套（/chat/completion、/im/chain/single、ImageX 上传…），
差别只在域名 / aid / bot_id / 上传机房 / 签名器的页面环境。这里把 dola_pure_api.py **再加载一份
独立的模块实例**，改写它的站点常量 —— 模块全局量各归各的，dola 号和豆包号在同一进程里并发跑不串。
"""
from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

SITE = "doubao"
BASE = "https://www.doubao.com"
HOST = "www.doubao.com"
AID = "497858"
BOT_ID = "7338286299411103781"
IMAGEX_REGION = "cn-north-1"
REGION = "CN"
PC_VERSION = "3.41.4"
# 签名器页面环境：域名 / aid；/creativity（无水印下载接口）也要签
SIGNER_ENV = {"BDMS_HOST": HOST, "BDMS_AID": AID, "BDMS_EXTRA_PATHS": "/creativity"}

_LOCK = threading.Lock()
_MODULE = None


def load():
    """返回豆包站点的协议模块实例（进程内只加载一次）。"""
    global _MODULE
    with _LOCK:
        if _MODULE is not None:
            return _MODULE
        path = Path(__file__).with_name("dola_pure_api.py")
        spec = importlib.util.spec_from_file_location("protocol.doubao_pure_api", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module.SITE = SITE
        module.SITE_HOST = "doubao.com"
        module.COOKIE_DOMAIN = ".doubao.com"
        module.BASE = BASE
        module.AID = AID
        module.BOT_ID = BOT_ID
        module.IMAGEX_REGION = IMAGEX_REGION
        module.SIGNER_ENV = dict(SIGNER_ENV)
        _MODULE = module
        return module
