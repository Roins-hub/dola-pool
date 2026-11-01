"""Dola「记忆」开关的结果判定（不依赖浏览器，便于测试）。读写本身见 video_worker.MEMORY_JS 与 video_worker_ui.set_dola_memory。"""
def memory_outcome(res: dict, want: bool = False) -> tuple[bool, str]:
    """MEMORY_JS 的结果 → (是否已是想要的状态, 说明)。开关值 dola 可能给 bool 或 0/1/2，按真假判断。"""
    res = res or {}
    after = (res.get("after") or {})
    mem = after.get("memory")
    upd = res.get("update") or {}
    body = upd.get("body") or {}
    if upd and (upd.get("status") != 200 or body.get("code") not in (0, None)):
        return False, f"关闭失败：HTTP {upd.get('status')} code={body.get('code')} {str(body.get('msg') or '')[:60]}"
    if after.get("status") != 200 or after.get("code") not in (0, None):
        return False, f"读取设置失败：HTTP {after.get('status')} code={after.get('code')}"
    if not isinstance(mem, dict) or "switch_status" not in mem:
        if upd and (body.get("data") or {}).get("res") is True:
            return True, "已关闭（接口确认成功，设置里未返回开关值）"
        return False, "读取不到记忆开关状态"
    value = mem.get("switch_status")
    on = value is True or value in (1, "1", "true", "on")
    if on == want:
        return True, "记忆已关闭" if not want else "记忆已开启"
    if upd and (body.get("data") or {}).get("res") is True:
        return False, f"Dola 回复成功，但回读仍是{'开启' if on else '关闭'}（未生效）"
    return False, f"回读仍是{'开启' if on else '关闭'}（switch_status={value!r}）"

