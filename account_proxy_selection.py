"""Validate explicit proxy selections and bind them before account initialization."""
import proxy_store


def validate_proxy_ids(ids):
    selected = list(dict.fromkeys(ids or []))
    for proxy_id in selected:
        info = proxy_store.get_proxy(proxy_id)
        if not info or not info.get("enabled"):
            raise ValueError("所选代理不存在或已停用，请刷新代理列表")
    return selected


def bind_selected_proxy(name, proxy_id):
    if not proxy_id:
        return
    validate_proxy_ids([proxy_id])
    proxy_store.set_account_proxy(name, proxy_id)
    # Allocate dynamic/extract sessions before the first browser or verification request.
    url = proxy_store.proxy_url_for(name)
    info = proxy_store.get_account_proxy(name)
    if not info or not url:
        raise ValueError("所选代理当前不可用，未开始登录或验证")
    if info.get("mode") == "extract":
        session = proxy_store.get_session(name) or {}
        if not session.get("node_id"):
            raise ValueError("所选节点池没有可用节点，未开始登录或验证")
