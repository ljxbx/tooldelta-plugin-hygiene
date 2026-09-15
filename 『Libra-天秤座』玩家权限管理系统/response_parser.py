"""ToolDelta 命令响应的结构化解析。"""
from __future__ import annotations

import json
import re
from typing import Any

try:
    from .core import normalize_xuid
except ImportError:  # 兼容直接运行模块的测试环境
    from core import normalize_xuid

_BLOCK_RE = re.compile(r"###\*", re.S)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def normalize_response(response: Any, channel: str) -> dict[str, Any]:
    """将 Packet_CommandOutput、字典或字符串统一为可序列化结构。"""
    if isinstance(response, str):
        try:
            decoded = json.loads(response)
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, dict) and "OutputMessages" in decoded:
            return normalize_response(decoded, channel)
    messages = _field(response, "OutputMessages", []) or []
    if not isinstance(messages, list):
        messages = [messages]
    texts: list[str] = []
    success_values: list[bool] = []
    for item in messages:
        message = _field(item, "Message", None)
        if message is not None:
            texts.append(str(message))
        success = _field(item, "Success", None)
        if isinstance(success, bool):
            success_values.append(success)
    if isinstance(response, str):
        texts.append(response)
    dataset = _field(response, "DataSet", "") or ""
    if dataset:
        texts.append(str(dataset))
    text = "\n".join(texts)
    payloads: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    for match in _BLOCK_RE.finditer(text):
        start = text.find("{", match.end())
        if start < 0:
            continue
        try:
            payload, end = decoder.raw_decode(text[start:])
        except (TypeError, ValueError):
            continue
        marker_end = text.find("*###", start + end)
        if marker_end >= 0 and isinstance(payload, dict):
            payloads.append(payload)
    # 兼容没有 ###* 包装但直接返回 JSON 的版本。
    if not payloads:
        for candidate in texts:
            try:
                payload = json.loads(candidate.strip())
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                payloads.append(payload)
    success_count = _field(response, "SuccessCount", None)
    confirmed = bool(success_values) and all(success_values)
    if isinstance(success_count, int) and success_count <= 0:
        confirmed = False
    elif isinstance(success_count, int) and success_count > 0:
        confirmed = True
    success = confirmed or bool(payloads)
    error_code = None
    if "commands.generic.error.permissions" in text:
        error_code = "permission_denied"
        success = False
        confirmed = False
    elif not success:
        error_code = "command_failed"
    return {
        "success": success,
        "confirmed": confirmed,
        "messages": texts,
        "payloads": payloads,
        "text": text,
        "error_code": error_code,
        "channel": channel,
    }


def extract_admin_xuids(parsed: dict[str, Any]) -> list[str]:
    """从 permissions/ops 结构化结果提取 operator XUID。"""
    found: list[str] = []
    payloads = parsed.get("payloads", [])
    recognized = False
    for payload in payloads:
        command = str(payload.get("command", "")).lower()
        result = payload.get("result", [])
        candidates: list[Any] = []
        if command == "permissions":
            recognized = True
            if not isinstance(result, list):
                raise ValueError("permissions.result 必须是列表")
            candidates = [item.get("xuid") for item in result if isinstance(item, dict) and str(item.get("permission", "")).lower() == "operator"]
        elif command == "ops":
            recognized = True
            if not isinstance(result, list):
                raise ValueError("ops.result 必须是列表")
            candidates = result
        for value in candidates:
            try:
                xuid = normalize_xuid(str(value))
            except ValueError:
                raise ValueError(f"无效的管理员 XUID: {value}")
            if xuid not in found:
                found.append(xuid)
    if not recognized:
        raise ValueError("响应中没有 ops 或 permissions 数据")
    return found
