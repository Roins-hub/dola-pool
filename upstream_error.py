"""Bounded, allowlisted video diagnostics; never infer a task state from a code."""
import json
import re


def _json(value, default):
    if isinstance(value, str):
        if len(value) > 16384:
            return default
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            return default
    return value


def _number(value):
    if type(value) not in (str, int):
        return None
    value = str(value)
    return value if re.fullmatch(r"[0-9]{1,32}", value) else None


def normalize(value):
    """Keep numeric codes/status and symbolic reasons, never raw ext/URLs/IDs."""
    value = _json(value, {})
    if not isinstance(value, dict):
        return {}
    out = {}
    code = _number(value.get("ai_creation_res_code"))
    if code is not None:
        out["ai_creation_res_code"] = code
    tools = _json(value.get("ai_creation_tool_list"), [])
    if isinstance(tools, list):
        records = []
        for tool in tools[:16]:
            if not isinstance(tool, dict):
                continue
            if tool.get("file_type") not in (None, "video"):
                continue
            if tool.get("tool_name") and "video" not in str(tool["tool_name"]).lower():
                continue
            item = {}
            for key in ("status", "fail_code"):
                number = _number(tool.get(key))
                if number is not None:
                    item[key] = number
            reason = tool.get("failure_reason")
            # Keep protocol enums such as cta_limit, not free-form text that
            # may embed credentials, signed URLs or personal information.
            if isinstance(reason, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason):
                item["failure_reason"] = reason
            if item:
                records.append(item)
        if records:
            out["ai_creation_tool_list"] = records
    return out


def from_message(message):
    return normalize(message.get("ext")) if isinstance(message, dict) else {}


def attach(error, diagnostic):
    """Enrich an existing exception without changing type or retry semantics."""
    diagnostic = normalize(diagnostic)
    if diagnostic:
        error.upstream_error = diagnostic
    return error


def _failure(source):
    """(code, diagnostic) of one exception/result; a nonzero preflight code is not a failed tool."""
    get = source.get if isinstance(source, dict) else lambda key: getattr(source, key, None)
    diagnostic = normalize(get("upstream_error"))
    records = diagnostic.get("ai_creation_tool_list", [])
    last = records[-1] if records else {}
    # A typed final failure (UpstreamGenerationFailed) carries its own confirmed code.
    code = _number(get("failure_code")) or (last.get("fail_code") if last.get("status") == "5" else None)
    return (code if code is not None and int(code) else None), diagnostic


def failure_fields(source):
    """DB/test-result fields. Follows `raise ... from` so the scheduler's summary keeps the code."""
    code, diagnostic = _failure(source)
    depth = 0
    while not code and not diagnostic and isinstance(source, BaseException) and source.__cause__ and depth < 8:
        source, depth = source.__cause__, depth + 1
        code, diagnostic = _failure(source)
    return {"failure_code": code,
            "upstream_error": json.dumps(diagnostic, ensure_ascii=False) if diagnostic else None}


def known_fields(source, row):
    """For later read-backs: never erase or weaken a failure that is already recorded."""
    fields = failure_fields(source)
    if fields["failure_code"]:
        return {key: value for key, value in fields.items() if value is not None}
    if fields["upstream_error"] and not row.get("failure_code"):
        return {"upstream_error": fields["upstream_error"]}
    return {}


def public_fields(row):
    if row.get("status") not in ("failed", "风控"):
        return {"failure_code": None, "upstream_error": None}
    return {"failure_code": _number(row.get("failure_code")),
            "upstream_error": normalize(row.get("upstream_error")) or None}
