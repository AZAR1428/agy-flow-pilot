#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ultra-lightweight multi-task stream telemetry & on-demand error diagnosis for Flow Pilot v3.9.1."""

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import ctypes
import datetime
import gc
import heapq
import http.client
import json
import math
import os
import re
import socket
import sqlite3
import struct
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Optional

SAFE_CID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
INTENTIONAL_WAIT_TOOLS = frozenset({
    "swarm_wait", "ask_question", "invoke_subagent", "schedule", "run_workflow",
    "ask_permission", "ask_custom_permission", "wait", "notify_user", "browser_subagent",
    "command_status", "call_mcp_tool", "search_web", "read_url_content",
    "generate_image", "subagent", "swarm", "browser",
})


def _normalize_tool_key(raw: str) -> str:
    s = (raw or "").strip()
    if s.upper().startswith("CORTEX_STEP_TYPE_"):
        s = s[17:]
    return s.lower()


def _is_intentional_wait_step(tool_name: str, step_type: str = "") -> bool:
    if tn := _normalize_tool_key(tool_name):
        if tn in INTENTIONAL_WAIT_TOOLS or tn.startswith("mcp_") or tn.startswith("call_mcp"):
            return True
    if st := _normalize_tool_key(step_type):
        if st in INTENTIONAL_WAIT_TOOLS or st.startswith("mcp_") or st.startswith("call_mcp"):
            return True
    return False


ACTIVE_ROOT_STATUSES = frozenset({"RUNNING", "WAITING_FOR_USER", "QUEUED"})
ACTIVE_SUBAGENT_STATUSES = frozenset({"RUNNING", "WAITING_FOR_USER", "QUEUED", "CANCELLING"})
ACTIVE_PLANNER_STEP_STATUSES = frozenset({
    "CORTEX_STEP_STATUS_GENERATING", "CORTEX_STEP_STATUS_RUNNING", "CORTEX_STEP_STATUS_PENDING",
    "GENERATING", "RUNNING", "PENDING",
})

SPEED_PHASE_LABELS: dict[str, str] = {
    "waiting_ttft": "等待首字", "thinking": "思考中", "generating": "输出中",
    "tool_calls": "工具调用", "idle": "最近一轮",
}

DEFAULT_HUD_CONFIG: dict[str, Any] = {
    "enabled": False, "viewMode": "pill", "alwaysOnTop": True,
    "opacity": 0.96, "theme": "light", "width": 388, "customHeight": None,
    "x": None, "y": None, "physX": None, "physY": None, "anchorCorner": "top-right", "pinnedIds": [],
}

DEFAULT_FAST_IO_CONFIG: dict[str, Any] = {"mcpEnabled": True}
DEFAULT_FAST_IO_STATS: dict[str, Any] = {
    "batchReadCalls": 0, "batchReadFiles": 0, "grepCalls": 0, "globCalls": 0,
    "outlineCalls": 0, "savedSecEstimate": 0.0,
}

_CJK_RE = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef]")
_PAREN_NAME_RE = re.compile(r"\(([^()]+)\)\s*$")
_ISO_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?([+-]\d{2}:\d{2})?$")
_WIN_DRIVE_URI_RE = re.compile(r"^/[A-Za-z]:")
_UUID_LIKE_RE = re.compile(r"[0-9a-fA-F-]{32,40}")
_ATTEMPT_RE = re.compile(r"attempt\s+(\d+)", re.IGNORECASE)
_LOCALHOST_RE = re.compile(r"(?i)^localhost(?=:|$)")
_DAEMON_JSON_RE = re.compile(r'"[Ii]sDaemon"\s*:\s*true\b')
_CLAUDE_VER_RE = re.compile(r"\b(?:sonnet|opus)[-_ ]+(\d+(?:[.-]\d+)?)\b", re.IGNORECASE)
_GEMINI_FLASH_VER_RE = re.compile(r"\b(\d+(?:[.-]\d+)?)[-_ ]*flash\b", re.IGNORECASE)
_TOKEN_SPEC_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([km])?$", re.IGNORECASE)
_TOKEN_UNIT_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*([km])\b", re.IGNORECASE)


def _is_daemon_step_dict(st: dict[str, Any], tc: dict[str, Any]) -> bool:
    rc = st.get("runCommand")
    if isinstance(rc, dict) and bool(rc.get("isDaemon")):
        return True
    for key in ("argumentsJson", "arguments"):
        raw = tc.get(key)
        if isinstance(raw, str) and _DAEMON_JSON_RE.search(raw):
            return True
    return False


def _stat_mtime_ns(path_str: str) -> int:
    try:
        return os.stat(path_str).st_mtime_ns
    except OSError:
        return 0


def _stat_mtime_and_size(path_str: str) -> tuple[int, int]:
    try:
        st = os.stat(path_str)
        return int(st.st_mtime_ns), int(st.st_size)
    except OSError:
        return 0, 0


def _query_parent_ls_start_ts() -> float:
    """Best-effort discovery of parent language_server process start timestamp (Unix epoch)."""
    if os.name != "nt":
        return 0.0
    ppid = os.getppid()
    if ppid <= 0:
        return 0.0
    try:
        from ctypes import wintypes

        class _FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.GetProcessTimes.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
            ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
        ]
        k32.GetProcessTimes.restype = wintypes.BOOL
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        ]
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL

        h = k32.OpenProcess(0x1000, False, ppid)
        if not h:
            return 0.0
        try:
            buf_len = wintypes.DWORD(512)
            name_buf = ctypes.create_unicode_buffer(512)
            if not k32.QueryFullProcessImageNameW(h, 0, name_buf, ctypes.byref(buf_len)):
                return 0.0
            if "language_server" not in name_buf.value.lower():
                return 0.0
            ct, et, kt, ut = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
            if not k32.GetProcessTimes(h, ctypes.byref(ct), ctypes.byref(et), ctypes.byref(kt), ctypes.byref(ut)):
                return 0.0
            ft_val = (int(ct.dwHighDateTime) << 32) | int(ct.dwLowDateTime)
            unix_ts = (ft_val - 116_444_736_000_000_000) / 10_000_000.0
            return unix_ts if (1_600_000_000.0 < unix_ts <= time.time() + 60.0) else 0.0
        finally:
            k32.CloseHandle(h)
    except Exception:
        return 0.0


def _scandir_files_sig(dir_str: str, limit: int = 96) -> int:
    sig = _stat_mtime_ns(dir_str)
    if sig <= 0:
        return 0
    try:
        with os.scandir(dir_str) as it:
            for idx, e in enumerate(it):
                if idx >= limit:
                    break
                if not e.name.startswith("."):
                    st = e.stat(follow_symlinks=False)
                    sig ^= (hash(e.name) & 0x7FFFFFFF) ^ st.st_mtime_ns ^ (st.st_size << 16)
    except OSError:
        pass
    return sig


def _needs_model_label_refresh(label: str) -> bool:
    s = (label or "").strip()
    up = s.upper()
    return (not s) or up.startswith("MODEL_PLACEHOLDER_") or up.startswith("MODEL (M")


DEFAULT_CONTEXT_MAX_TOKENS = 512000

_CONTEXT_TOKEN_KEYS = (
    "maxContextTokens", "contextWindowTokens", "contextWindow", "maxTokens",
    "maxContextLength", "contextLength", "contextWindowSize", "defaultContextWindow",
)


def _parse_token_count(val: Any) -> int:
    if val is None or isinstance(val, bool):
        return 0
    if isinstance(val, (int, float)):
        if not math.isfinite(float(val)):
            return 0
        iv = int(round(float(val)))
        return iv if iv >= 1024 else 0
    if isinstance(val, str):
        s = val.strip().replace(",", "")
        if not s:
            return 0
        if m := _TOKEN_SPEC_RE.match(s):
            num = float(m.group(1))
            unit = (m.group(2) or "").lower()
            mult = 1_000_000 if unit == "m" else (1_000 if unit == "k" else 1)
            iv = int(round(num * mult))
            return iv if iv >= 1024 else 0
    return 0


def _extract_model_max_tokens_from_config(item: dict[str, Any]) -> int:
    """Extract live per-model max context tokens from a ClientModelConfig dict if present."""
    if not isinstance(item, dict):
        return 0
    for k in _CONTEXT_TOKEN_KEYS:
        if (v := item.get(k)) is not None:
            if isinstance(v, dict):
                for nk in _CONTEXT_TOKEN_KEYS:
                    if (nv := _parse_token_count(v.get(nk))) > 0:
                        return nv
            elif (pv := _parse_token_count(v)) > 0:
                return pv
    for sub_k in ("modelInfo", "capabilities", "limits", "contextWindowConfig"):
        if isinstance(sub := item.get(sub_k), dict):
            for k in _CONTEXT_TOKEN_KEYS:
                if (pv := _parse_token_count(sub.get(k))) > 0:
                    return pv
    opts = item.get("contextWindowOptions")
    if isinstance(opts, dict) and isinstance(opts.get("options"), list):
        opts = opts["options"]
    if isinstance(opts, list) and opts:
        ordered = [o for o in opts if isinstance(o, dict) and not bool(o.get("expensive"))] + [
            o for o in opts if isinstance(o, dict) and bool(o.get("expensive"))
        ]
        for opt in ordered:
            for k in _CONTEXT_TOKEN_KEYS:
                if (pv := _parse_token_count(opt.get(k))) > 0:
                    return pv
            for tk in ("name", "description"):
                txt = str(opt.get(tk) or "").strip().replace(",", "")
                if txt and (m := _TOKEN_UNIT_RE.search(txt)):
                    num = float(m.group(1))
                    mult = 1_000_000 if m.group(2).lower() == "m" else 1_000
                    iv = int(round(num * mult))
                    if iv >= 1024:
                        return iv
    return 0


def _infer_model_max_tokens(model_str: str, live_max_tokens: Optional[dict[str, int]] = None) -> int:
    s = (model_str or "").strip()
    if s and live_max_tokens:
        for cand in (s, s.upper()):
            if (live_val := live_max_tokens.get(cand)) and live_val > 0:
                return int(live_val)
    low = s.lower()
    if "claude" in low:
        return 200000
    if "gpt" in low:
        return 128000
    if "flash" in low:
        return 256000
    if "gemini" in low:
        return DEFAULT_CONTEXT_MAX_TOKENS
    return DEFAULT_CONTEXT_MAX_TOKENS


def _live_tps(ts: Any) -> float:
    if not isinstance(ts, dict) or not bool(ts.get("active")) or str(ts.get("phase") or "") == "waiting_ttft":
        return 0.0
    try:
        val = float(ts.get("displayTokensPerSec") or 0.0)
        return val if (math.isfinite(val) and val > 0.0) else 0.0
    except (ValueError, TypeError):
        return 0.0


def _to_int_safe(val: Any) -> int:
    if val is None or isinstance(val, bool):
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        try:
            return int(float(str(val).strip()))
        except Exception:
            return 0


def _finite_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (ValueError, TypeError):
        return None


def _sanitize_fast_io_config(raw: Any, base: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    out = dict(DEFAULT_FAST_IO_CONFIG)
    for src in (base, raw):
        if isinstance(src, dict) and src.get("mcpEnabled") is not None:
            out["mcpEnabled"] = bool(src["mcpEnabled"])
    return out


def _sanitize_hud_config(raw: Any, base: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    out = dict(DEFAULT_HUD_CONFIG)
    if isinstance(base, dict):
        for k in DEFAULT_HUD_CONFIG:
            if k in base:
                out[k] = base[k]
        out["pinnedIds"] = list(base.get("pinnedIds") or [])
    out["theme"] = "light"
    if isinstance(raw, dict):
        for bk in ("enabled", "alwaysOnTop"):
            if raw.get(bk) is not None:
                out[bk] = bool(raw[bk])
        vm = raw.get("viewMode")
        if isinstance(vm, str) and vm.strip().lower() in ("pill", "stack"):
            out["viewMode"] = vm.strip().lower()
        op = _finite_float(raw.get("opacity"))
        if op is not None:
            if op > 1.05:
                op /= 100.0
            out["opacity"] = round(max(0.50, min(1.0, op)), 2)
        wf = _finite_float(raw.get("width"))
        if wf is not None:
            out["width"] = int(round(max(320.0, min(960.0, wf))))
        if "customHeight" in raw:
            ch = raw["customHeight"]
            hf = None if (ch is None or ch == "" or ch is False) else _finite_float(ch)
            if ch is None or ch == "" or ch is False:
                out["customHeight"] = None
            elif hf is not None:
                out["customHeight"] = int(round(max(68.0, min(4096.0, hf)))) if hf > 0.0 else None
        for ck in ("x", "y"):
            if ck in raw:
                if raw[ck] is None:
                    out[ck] = None
                else:
                    cf = _finite_float(raw[ck])
                    if cf is not None and -10000.0 < cf < 30000.0:
                        out[ck] = int(round(cf))
        for pk in ("physX", "physY"):
            if pk in raw:
                if raw[pk] is None:
                    out[pk] = None
                else:
                    pf = _finite_float(raw[pk])
                    if pf is not None and -16000.0 < pf < 32768.0:
                        out[pk] = int(round(pf))
        ac = raw.get("anchorCorner")
        if isinstance(ac, str) and ac.strip().lower() in ("top-left", "top-right", "bottom-left", "bottom-right"):
            out["anchorCorner"] = ac.strip().lower()
        if isinstance(raw.get("pinnedIds"), list):
            seen_p: set[str] = set()
            cleaned_p: list[str] = []
            for item in raw["pinnedIds"]:
                if isinstance(item, str) and (s := item.strip()) and s not in seen_p:
                    seen_p.add(s)
                    cleaned_p.append(s)
                    if len(cleaned_p) >= 32:
                        break
            out["pinnedIds"] = cleaned_p

    if out.get("viewMode") not in ("pill", "stack"):
        out["viewMode"] = "pill"
    op0 = _finite_float(out.get("opacity"))
    out["opacity"] = round(max(0.50, min(1.0, op0 / 100.0 if (op0 is not None and op0 > 1.05) else (op0 if op0 is not None else 0.96))), 2)
    wf0 = _finite_float(out.get("width"))
    out["width"] = int(round(max(320.0, min(960.0, wf0)))) if wf0 is not None else 388
    if out.get("customHeight") is not None:
        hf0 = _finite_float(out["customHeight"])
        out["customHeight"] = int(round(max(68.0, min(4096.0, hf0)))) if (hf0 is not None and hf0 > 0.0) else None
    for ck in ("x", "y"):
        if out.get(ck) is not None:
            cf0 = _finite_float(out[ck])
            out[ck] = int(round(cf0)) if (cf0 is not None and -10000.0 < cf0 < 30000.0) else None
    for pk in ("physX", "physY"):
        if out.get(pk) is not None:
            pf0 = _finite_float(out[pk])
            out[pk] = int(round(pf0)) if (pf0 is not None and -16000.0 < pf0 < 32768.0) else None
    if out.get("anchorCorner") not in ("top-left", "top-right", "bottom-left", "bottom-right"):
        out["anchorCorner"] = "top-right"
    out["theme"] = "light"
    return out


def _extract_workspace_info(summary: dict[str, Any]) -> tuple[str, str, str]:
    meta = summary.get("trajectoryMetadata") if isinstance(summary.get("trajectoryMetadata"), dict) else {}
    ws_list = summary.get("workspaces")
    if not isinstance(ws_list, list) or not ws_list:
        ws_list = meta.get("workspaces") if isinstance(meta.get("workspaces"), list) else []
    raw_uri = branch = ""
    for w in ws_list:
        if isinstance(w, dict):
            u = str(w.get("workspaceFolderAbsoluteUri") or w.get("gitRootAbsoluteUri") or "").strip()
            if u:
                raw_uri = u
                branch = str(w.get("branchName") or "").strip()
                break
    if not raw_uri and isinstance(meta.get("workspaceUris"), list) and meta["workspaceUris"]:
        raw_uri = str(meta["workspaceUris"][0] or "").strip()
    if not raw_uri:
        return "", "", branch
    path_str = urllib.parse.unquote(urllib.parse.urlsplit(raw_uri).path or "") if "://" in raw_uri else urllib.parse.unquote(raw_uri)
    path_str = path_str.replace("\\", "/").rstrip("/")
    if _WIN_DRIVE_URI_RE.match(path_str):
        path_str = path_str[1:]
    name = path_str.rsplit("/", 1)[-1] if "/" in path_str else path_str
    return name, path_str, branch


def safe_cid(cid: str) -> str:
    return "".join(ch if ch in SAFE_CID_CHARS else "_" for ch in (cid or "").strip())


def normalize_ls_address(raw_addr: str) -> str:
    addr = (raw_addr or "").strip()
    for prefix in ("http://", "https://"):
        if addr.startswith(prefix):
            addr = addr[len(prefix) :]
            break
    return _LOCALHOST_RE.sub("127.0.0.1", addr.rstrip("/"))


def parse_iso_ts(iso_str: str) -> float:
    if not iso_str or not isinstance(iso_str, str) or not (s := iso_str.strip()):
        return 0.0
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        if m := _ISO_TS_RE.match(s):
            base, frac, tz = m.group(1), m.group(2) or "", m.group(3) or "+00:00"
            return datetime.datetime.fromisoformat(f"{base}.{(frac + '000000')[:6]}{tz}").timestamp()
        return datetime.datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def short_run_status(raw: str) -> str:
    if not raw:
        return "UNKNOWN"
    s = str(raw).upper()
    for prefix in ("CASCADE_RUN_STATUS_", "CORTEX_STEP_STATUS_"):
        if s.startswith(prefix):
            return s[len(prefix) :]
    return s


def extract_subagent_name(*candidates: Any, fallback_cid: str = "") -> str:
    cleaned: list[str] = []
    for raw in candidates:
        if isinstance(raw, str) and (s := raw.strip()):
            if (m := _PAREN_NAME_RE.search(s)) and m.group(1).strip():
                return m.group(1).strip()
            cleaned.append(s)
    for s in cleaned:
        if s.lower() not in ("swarm_worker", "swarm worker", "subagent"):
            return s
    return cleaned[0] if cleaned else (f"子代理-{fallback_cid[:6]}" if fallback_cid else "子代理")


def _parse_proto_duration_sec(val: Any) -> float:
    if val is None or isinstance(val, bool):
        return 0.0
    if isinstance(val, (int, float)):
        return max(0.0, float(val))
    if not (s := str(val).strip()):
        return 0.0
    if s.endswith("ms"):
        f = _finite_float(s[:-2])
        return max(0.0, f / 1000.0) if f is not None else 0.0
    f = _finite_float(s[:-1] if s.endswith("s") else s)
    return max(0.0, f) if f is not None else 0.0


def _estimate_raw_text_tokens(text: str) -> float:
    if not text:
        return 0.0
    if text.isascii():
        return len(text) * 0.265
    cjk_count = len(_CJK_RE.findall(text))
    return (cjk_count * 0.62) + (max(0, len(text) - cjk_count) * 0.265)


def _extract_tool_calls_text(tool_calls: Any) -> str:
    if not isinstance(tool_calls, list) or not tool_calls:
        return ""
    parts: list[str] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        if isinstance(tc.get("name"), str) and tc["name"]:
            parts.append(tc["name"])
        for ak in ("argumentsJson", "arguments", "argsJson", "input"):
            av = tc.get(ak)
            if isinstance(av, str) and av:
                parts.append(av)
                break
            if isinstance(av, (dict, list)) and av:
                parts.append(json.dumps(av, ensure_ascii=False, separators=(",", ":")))
                break
    return "\n".join(parts)


def _empty_token_speed() -> dict[str, Any]:
    return {
        "active": False, "phase": "idle", "phaseLabel": "空闲",
        "displayTokensPerSec": 0.0, "liveTokensPerSec": 0.0, "avgTokensPerSec": 0.0, "peakTokensPerSec": 0.0,
        "outputTokens": 0, "thinkingTokens": 0, "responseTokens": 0, "isExact": False,
        "ttftMs": 0, "elapsedGenSec": 0.0, "stepIndex": -1,
    }


def _build_subagent_token_usage(
    in_tok: int,
    cache_tok: int,
    out_tok: int,
    last_turn: Optional[tuple[int, int, int, int, int]] = None,
    cache_miss_streak: int = 0,
) -> dict[str, Any]:
    in_v, cache_v, out_v = max(0, int(in_tok)), max(0, int(cache_tok)), max(0, int(out_tok))
    denom = in_v + cache_v
    has_cache = denom > 0
    l_in = max(0, int(last_turn[0])) if last_turn is not None else 0
    l_cache = max(0, int(last_turn[1])) if last_turn is not None else 0
    l_denom = l_in + l_cache
    l_pct = round((l_cache / l_denom) * 100.0, 1) if l_denom > 0 else None
    return {
        "netTokens": in_v + out_v,
        "inputTokens": in_v,
        "cacheReadTokens": cache_v,
        "outputTokens": out_v,
        "cacheHitPct": round((cache_v / denom) * 100.0, 1) if has_cache else 0.0,
        "hasCacheData": has_cache,
        "lastTurnCacheHitPct": l_pct,
        "lastTurnCacheTokens": l_cache,
        "lastTurnInputTokens": l_in,
        "lastTurnTotalTokens": l_denom,
        "cacheMissStreak": max(0, int(cache_miss_streak)),
    }


def _pick_tok_int(usage: dict[str, Any], *keys: str) -> int:
    for k in keys:
        if (val := usage.get(k)) is not None:
            iv = max(0, _to_int_safe(val))
            if iv > 0:
                return iv
    return 0


def _extract_usage_dict_5(usage: Any) -> Optional[tuple[int, int, int, int, int]]:
    if not isinstance(usage, dict) or not usage:
        return None
    in_tok = _pick_tok_int(usage, "inputTokens", "promptTokenCount", "promptTokens")
    cache_tok = _pick_tok_int(usage, "cacheReadTokens", "cachedContentTokenCount", "cacheReadInputTokens")
    think_tok = _pick_tok_int(usage, "thinkingOutputTokens", "thinkingTokens")
    resp_tok = _pick_tok_int(usage, "responseOutputTokens", "responseTokens")
    out_tok = _pick_tok_int(usage, "outputTokens", "candidatesTokenCount", "completionTokens")
    if out_tok == 0:
        out_tok = think_tok + resp_tok
    if in_tok <= 0 and cache_tok <= 0 and out_tok <= 0 and think_tok <= 0 and resp_tok <= 0:
        return None
    return (in_tok, cache_tok, out_tok, think_tok, resp_tok)


def _extract_full_usage(chat_m: dict[str, Any]) -> Optional[tuple[int, int, int, int, int]]:
    """Extract (in, cache, out, think, resp) from chatModel with retry deduplication."""
    if not isinstance(chat_m, dict):
        return None
    retry_infos = chat_m.get("retryInfos")
    if isinstance(retry_infos, list) and len(retry_infos) > 1:
        seen_attempts: dict[tuple[str, Any], tuple[int, int, int, int, int]] = {}
        for idx_r, ri in enumerate(retry_infos):
            if not isinstance(ri, dict):
                continue
            ri_usage = ri.get("usage") if isinstance(ri.get("usage"), dict) else (
                ri["chatModel"].get("usage") if isinstance(ri.get("chatModel"), dict) else None
            )
            u5 = _extract_usage_dict_5(ri_usage)
            if u5 is None:
                continue
            dedup_id = str(ri.get("responseId") or "").strip() or str(ri.get("messageId") or "").strip()
            dedup_key: tuple[str, Any] = ("id", dedup_id) if dedup_id else ("idx", idx_r)
            if dedup_key not in seen_attempts:
                seen_attempts[dedup_key] = u5
        if seen_attempts:
            return (
                sum(u[0] for u in seen_attempts.values()),
                sum(u[1] for u in seen_attempts.values()),
                sum(u[2] for u in seen_attempts.values()),
                sum(u[3] for u in seen_attempts.values()),
                sum(u[4] for u in seen_attempts.values()),
            )
    return _extract_usage_dict_5(chat_m.get("usage"))


def _sum_usage_by_gm(usage_by_gm: dict[str, tuple[int, int, int, int, int]]) -> tuple[int, int, int, int, int, int]:
    s_in = s_cache = s_out = s_think = s_resp = 0
    for u in usage_by_gm.values():
        s_in += u[0]
        s_cache += u[1]
        s_out += u[2]
        s_think += u[3]
        s_resp += u[4]
    return (s_in, s_cache, s_out, s_think, s_resp, len(usage_by_gm))


def _build_root_token_usage(
    self_usage_by_gm: dict[str, tuple[int, int, int, int, int]],
    last_turn: Optional[tuple[int, int, int, int, int]],
    children_usage: dict[str, tuple[int, int, int, int, int, int]],
    cache_miss_streak: int = 0,
) -> dict[str, Any]:
    s_in, s_cache, s_out, s_think, s_resp, s_turns = _sum_usage_by_gm(self_usage_by_gm)
    sub_in = sub_cache = sub_out = sub_think = sub_resp = sub_turns = sub_cnt = 0
    for cu in children_usage.values():
        sub_in += cu[0]
        sub_cache += cu[1]
        sub_out += cu[2]
        sub_think += cu[3]
        sub_resp += cu[4]
        sub_turns += cu[5]
        if cu[5] > 0 or (cu[0] + cu[1] + cu[2]) > 0:
            sub_cnt += 1
    tot_in, tot_cache, tot_out = s_in + sub_in, s_cache + sub_cache, s_out + sub_out
    tot_think, tot_resp = s_think + sub_think, s_resp + sub_resp
    denom = tot_in + tot_cache
    has_cache = denom > 0
    hit_pct = round((tot_cache / denom) * 100.0, 1) if has_cache else 0.0
    l_in = max(0, int(last_turn[0])) if last_turn is not None else 0
    l_cache = max(0, int(last_turn[1])) if last_turn is not None else 0
    l_denom = l_in + l_cache
    l_pct = round((l_cache / l_denom) * 100.0, 1) if l_denom > 0 else None
    return {
        "netTokens": tot_in + tot_out,
        "grossTokens": tot_in + tot_cache + tot_out,
        "inputTokens": tot_in,
        "cacheReadTokens": tot_cache,
        "outputTokens": tot_out,
        "thinkingTokens": tot_think,
        "responseTokens": tot_resp,
        "cacheHitPct": hit_pct,
        "hasCacheData": has_cache,
        "lastTurnCacheHitPct": l_pct,
        "lastTurnCacheTokens": l_cache,
        "lastTurnInputTokens": l_in,
        "lastTurnTotalTokens": l_denom,
        "cacheMissStreak": max(0, int(cache_miss_streak)),
        "subNetTokens": sub_in + sub_out,
        "subCacheReadTokens": sub_cache,
        "subCount": sub_cnt,
        "turnCount": s_turns + sub_turns,
    }


def _extract_usage_tokens(usage: Any) -> Optional[tuple[int, int, int]]:
    if not isinstance(usage, dict) or not usage:
        return None
    keys = ("outputTokens", "thinkingOutputTokens", "responseOutputTokens", "thinkingTokens", "responseTokens", "candidatesTokenCount", "completionTokens")
    if not any(k in usage for k in keys):
        return None
    out_raw = usage.get("outputTokens")
    if out_raw is None:
        out_raw = usage.get("candidatesTokenCount") or usage.get("completionTokens")
    out_tok = max(0, _to_int_safe(out_raw))
    think_tok = max(0, _to_int_safe(usage.get("thinkingOutputTokens") if usage.get("thinkingOutputTokens") is not None else usage.get("thinkingTokens")))
    resp_tok = max(0, _to_int_safe(usage.get("responseOutputTokens") if usage.get("responseOutputTokens") is not None else usage.get("responseTokens")))
    if out_tok <= 0 and think_tok <= 0 and resp_tok <= 0:
        return None
    if think_tok > 0 and resp_tok == 0 and out_tok > 0:
        return (out_tok, think_tok, out_tok - think_tok) if out_tok >= think_tok else (think_tok + out_tok, think_tok, out_tok)
    total_out = max(out_tok, think_tok + resp_tok)
    if think_tok == 0:
        resp_tok = total_out
    elif total_out > think_tok + resp_tok:
        resp_tok = total_out - think_tok
    return (total_out, think_tok, resp_tok)


# --- Minimal pure-Python protobuf wire helpers for on-demand read-only SQLite diagnosis ---


def _decode_varint(buf: bytes, pos: int) -> tuple[int, int]:
    val = shift = 0
    n = len(buf)
    while pos < n:
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7
        if shift > 70:
            break
    return val, pos


def _iter_proto_fields(buf: bytes):
    pos, n = 0, len(buf)
    while pos < n:
        tag, next_pos = _decode_varint(buf, pos)
        if next_pos <= pos or tag == 0:
            break
        pos = next_pos
        field_no, wire = tag >> 3, tag & 0x07
        if wire == 0:
            v, pos = _decode_varint(buf, pos)
            yield field_no, wire, v
        elif wire == 2:
            ln, pos = _decode_varint(buf, pos)
            if ln < 0 or pos + ln > n:
                break
            yield field_no, wire, buf[pos : pos + ln]
            pos += ln
        elif wire == 1 and pos + 8 <= n:
            yield field_no, wire, buf[pos : pos + 8]
            pos += 8
        elif wire == 5 and pos + 4 <= n:
            yield field_no, wire, buf[pos : pos + 4]
            pos += 4
        else:
            break


def _parse_proto_timestamp(ts_bytes: bytes) -> float:
    sec = nanos = 0
    for fno, wire, val in _iter_proto_fields(ts_bytes):
        if fno == 1 and wire == 0:
            sec = int(val)
        elif fno == 2 and wire == 0:
            nanos = int(val)
    return (float(sec) + float(nanos) / 1e9) if sec > 0 else 0.0


def _extract_proto_strings(buf: bytes, max_depth: int = 3) -> list[str]:
    if not buf or max_depth < 0:
        return []
    found: list[str] = []
    for _fno, wire, val in _iter_proto_fields(buf):
        if wire != 2 or not isinstance(val, (bytes, bytearray)) or not val:
            continue
        try:
            text = val.decode("utf-8")
            if len(text) >= 4 and all(ch.isprintable() or ch in "\r\n\t" for ch in text):
                found.append(text.strip())
                continue
        except UnicodeDecodeError:
            pass
        if max_depth > 0:
            found.extend(_extract_proto_strings(bytes(val), max_depth - 1))
    return found


def _parse_step_metadata_blob(meta_blob: Optional[bytes]) -> dict[str, Any]:
    res: dict[str, Any] = {"createdAt": 0.0, "startedAt": 0.0, "completedAt": 0.0, "toolName": ""}
    if not meta_blob:
        return res
    timestamps: list[float] = []
    for fno, wire, val in _iter_proto_fields(meta_blob):
        if wire == 2 and isinstance(val, (bytes, bytearray)):
            if fno in (1, 6, 7, 8):
                ts = _parse_proto_timestamp(bytes(val))
                if ts > 1_600_000_000:
                    timestamps.append(ts)
                    if fno == 1 and res["createdAt"] <= 0:
                        res["createdAt"] = ts
            elif fno == 4:
                for tfno, twire, tval in _iter_proto_fields(bytes(val)):
                    if twire == 2 and tfno == 2 and isinstance(tval, (bytes, bytearray)):
                        res["toolName"] = bytes(tval).decode("utf-8", errors="ignore").strip()
    if timestamps:
        if res["createdAt"] <= 0:
            res["createdAt"] = min(timestamps)
        res["startedAt"] = timestamps[0]
        if len(timestamps) >= 2:
            res["completedAt"] = max(timestamps)
    return res


LARGE_FRAME_BYTES = 262_144

_WIN32_MEM_LOCK = threading.Lock()
_UCRT_HEAPMIN: Optional[Any] = None
_K32_SET_WS: Optional[Any] = None
_PSAPI_GET_MEM: Optional[Any] = None
_CUR_PROC_HANDLE: Optional[Any] = None


class _ProcMemCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _reclaim_native_heap(trim_ws_above_bytes: int = 0, run_gc: bool = True) -> None:
    """Release cyclic GC objects, compact Windows UCRT heap, and optionally trim excess Working Set."""
    global _UCRT_HEAPMIN, _K32_SET_WS, _PSAPI_GET_MEM, _CUR_PROC_HANDLE
    if run_gc:
        gc.collect()
    if os.name != "nt":
        return
    try:
        with _WIN32_MEM_LOCK:
            if _UCRT_HEAPMIN is None:
                _UCRT_HEAPMIN = ctypes.CDLL("ucrtbase")._heapmin
                k32 = ctypes.WinDLL("kernel32")
                k32.GetCurrentProcess.restype = ctypes.c_void_p
                k32.SetProcessWorkingSetSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t]
                k32.SetProcessWorkingSetSize.restype = ctypes.c_int
                psapi = ctypes.WinDLL("psapi")
                psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
                psapi.GetProcessMemoryInfo.restype = ctypes.c_int
                _CUR_PROC_HANDLE = k32.GetCurrentProcess()
                _K32_SET_WS = k32.SetProcessWorkingSetSize
                _PSAPI_GET_MEM = psapi.GetProcessMemoryInfo
        _UCRT_HEAPMIN()
        if trim_ws_above_bytes > 0 and _PSAPI_GET_MEM is not None and _K32_SET_WS is not None:
            pmc = _ProcMemCounters(cb=ctypes.sizeof(_ProcMemCounters))
            if _PSAPI_GET_MEM(_CUR_PROC_HANDLE, ctypes.byref(pmc), pmc.cb) and int(pmc.WorkingSetSize) > trim_ws_above_bytes:
                _K32_SET_WS(_CUR_PROC_HANDLE, ctypes.c_size_t(-1), ctypes.c_size_t(-1))
    except Exception:
        pass


def _read_exact(stream: Any, n: int) -> Optional[bytearray]:
    if n <= 0:
        return bytearray()
    buf = bytearray(n)
    mv = memoryview(buf)
    pos = 0
    try:
        if hasattr(stream, "readinto"):
            while pos < n:
                got = stream.readinto(mv[pos:])
                if not got:
                    return None
                pos += got
        else:
            while pos < n:
                chunk = stream.read(n - pos)
                if not chunk:
                    return None
                mv[pos : pos + len(chunk)] = chunk
                pos += len(chunk)
    finally:
        mv.release()
    return buf


def _loads_json_buf(buf: bytes | bytearray) -> Any:
    if not buf:
        return {}
    try:
        return json.loads(buf)
    except UnicodeDecodeError:
        return json.loads(bytes(buf).decode("utf-8", errors="replace"))


def _file_uri_to_local_path(uri: str) -> Optional[Path]:
    raw = (uri or "").strip()
    if not raw:
        return None
    if "://" in raw:
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme.lower() != "file":
            return None
        if parsed.netloc and parsed.netloc.lower() not in ("", "localhost", "127.0.0.1"):
            return None
        path_str = urllib.parse.unquote(parsed.path or "")
        if _WIN_DRIVE_URI_RE.match(path_str):
            path_str = path_str[1:]
        if os.name == "nt":
            path_str = path_str.replace("/", "\\")
        return Path(path_str) if path_str else None
    return Path(raw)


def _extract_bg_task_step_indices(idx_key: int, task: dict[str, Any]) -> tuple[int, ...]:
    found: set[int] = set()
    if isinstance(snap := task.get("taskSnapshot"), dict):
        for sk in ("stepIndex", "originStepIndex", "sourceStepIndex"):
            if isinstance(v := snap.get(sk), int) and not isinstance(v, bool):
                found.add(v)
    if isinstance(step := task.get("step"), dict):
        meta = step.get("metadata") if isinstance(step.get("metadata"), dict) else {}
        src_info = meta.get("sourceTrajectoryStepInfo") if isinstance(meta.get("sourceTrajectoryStepInfo"), dict) else {}
        if isinstance(sv := src_info.get("stepIndex"), int) and not isinstance(sv, bool):
            found.add(sv)
    for tk in ("stepIndex", "idx"):
        if isinstance(v := task.get(tk), int) and not isinstance(v, bool):
            found.add(v)
    if not found:
        found.add(idx_key)
    return tuple(sorted(found))


class ConversationRuntime:
    """Slotted in-memory telemetry state for a single conversation."""

    __slots__ = (
        "cid", "title", "workspace_name", "workspace_path", "branch_name",
        "last_modified_at", "sort_ts", "latest_termination_reason", "composite_running",
        "run_batch_started_at", "just_finished_at", "dismissed_recent_at",
        "snapshot_probed_steps", "raw_status", "raw_executor_loop_status",
        "stream_connected", "last_stream_frame_at", "model_id", "model_name", "model_display_name", "model_label", "routed_model_label",
        "tokens_used", "max_tokens", "max_tokens_exact", "last_step_index", "total_steps", "groups", "compaction_count",
        "estimated_after_compaction", "context_top_children", "context_top_children_from_breakdown",
        "bg_tasks_by_idx", "steps_by_idx", "had_db_on_disk", "has_error_step_hint",
        "db_sig", "db_latest_error", "db_recent_steps", "db_bg_sig", "db_bg_script", "last_user_input_idx",
        "disk_hydrated", "dirty_state", "last_state_persist_at", "last_requested_at",
        "stream_initial_seen", "tok_calibration", "tok_cal_think", "tok_cal_resp",
        "ts_calibrated_once", "ts_cal_think_once", "ts_cal_resp_once", "ts_seen_step_indices",
        "ts_calibrated_step_indices",
        "ts_waiting_turn_since", "ts_active_step_idx", "ts_step_created_at", "ts_first_token_at",
        "ts_first_chunk_tokens", "ts_last_growth_at", "ts_baseline_at", "ts_baseline_tokens", "ts_baseline_raw", "ts_thinking_ended_at",
        "ts_joined_midstream", "ts_phase", "ts_raw_think_chars", "ts_raw_resp_chars", "ts_raw_tc_chars",
        "ts_est_think_tokens", "ts_est_resp_tokens", "ts_est_output_tokens", "ts_cum_points",
        "ts_window_ready", "ts_live_tps", "ts_peak_tps", "ts_ttft_ms",
        "ts_step_raw_chars_by_idx", "ts_step_peak_by_idx", "ts_last_completed",
        "usage_by_gm", "last_turn_usage", "archived_children_usage", "usage_hydrated",
        "cache_miss_streak", "last_cache_gm_key",
    )

    def __init__(self, cid: str) -> None:
        self.cid = cid
        self.title = self.workspace_name = self.workspace_path = self.branch_name = ""
        self.last_modified_at = self.sort_ts = 0.0
        self.latest_termination_reason = ""
        self.composite_running = False
        self.run_batch_started_at = self.just_finished_at = self.dismissed_recent_at = 0.0
        self.snapshot_probed_steps = -1
        self.raw_status = self.raw_executor_loop_status = "CASCADE_RUN_STATUS_IDLE"
        self.stream_connected = False
        self.last_stream_frame_at = 0.0
        self.model_id = self.model_name = self.model_display_name = self.model_label = self.routed_model_label = ""
        self.tokens_used, self.max_tokens = 0, DEFAULT_CONTEXT_MAX_TOKENS
        self.max_tokens_exact = False
        self.last_step_index, self.total_steps = -1, 0
        self.groups = {"systemPrompt": 0, "extensions": 0, "tools": 0, "chatMessages": 0}
        self.compaction_count: int = 0
        self.estimated_after_compaction: bool = False
        self.context_top_children: list[dict[str, Any]] = []
        self.context_top_children_from_breakdown: bool = False
        self.bg_tasks_by_idx: dict[int, tuple[int, ...]] = {}
        self.steps_by_idx: dict[int, dict[str, Any]] = {}
        self.had_db_on_disk = self.has_error_step_hint = False
        self.db_sig: tuple[int, int, int] = (-1, -1, -1)
        self.db_latest_error: Optional[dict[str, Any]] = None
        self.db_recent_steps: list[dict[str, Any]] = []
        self.db_bg_sig: tuple[int, int, int] = (-1, -1, -1)
        self.db_bg_script: Optional[dict[str, Any]] = None
        self.last_user_input_idx = -1
        self.disk_hydrated = self.dirty_state = False
        self.last_state_persist_at = 0.0
        self.last_requested_at = time.time()
        self.stream_initial_seen = self.ts_calibrated_once = False
        self.ts_cal_think_once = self.ts_cal_resp_once = False
        self.tok_calibration = self.tok_cal_think = self.tok_cal_resp = 1.0
        self.ts_seen_step_indices: set[int] = set()
        self.ts_calibrated_step_indices: set[int] = set()
        self.ts_waiting_turn_since = 0.0
        self.ts_active_step_idx = -1
        self.ts_step_created_at = self.ts_first_token_at = self.ts_first_chunk_tokens = 0.0
        self.ts_last_growth_at = self.ts_baseline_at = self.ts_baseline_tokens = self.ts_thinking_ended_at = 0.0
        self.ts_baseline_raw: tuple[float, float] = (0.0, 0.0)
        self.ts_joined_midstream = self.ts_window_ready = False
        self.ts_phase = "idle"
        self.ts_raw_think_chars = self.ts_raw_resp_chars = self.ts_raw_tc_chars = 0
        self.ts_est_think_tokens = self.ts_est_resp_tokens = self.ts_est_output_tokens = 0
        self.ts_cum_points: list[tuple[float, float, float]] = []
        self.ts_live_tps = self.ts_peak_tps = 0.0
        self.ts_ttft_ms = 0
        self.ts_step_raw_chars_by_idx: dict[int, tuple[float, float]] = {}
        self.ts_step_peak_by_idx: dict[int, float] = {}
        self.ts_last_completed: Optional[dict[str, Any]] = None
        self.usage_by_gm: dict[str, tuple[int, int, int, int, int]] = {}
        self.last_turn_usage: Optional[tuple[int, int, int, int, int]] = None
        self.archived_children_usage: dict[str, tuple[int, int, int, int, int, int]] = {}
        self.usage_hydrated: bool = False
        self.cache_miss_streak: int = 0
        self.last_cache_gm_key: str = ""


class _StreamWorkerHandle:
    __slots__ = ("cid", "stop_event", "first_frame_event", "thread", "_conn_lock", "_conn")

    def __init__(self, cid: str) -> None:
        self.cid = cid
        self.stop_event = threading.Event()
        self.first_frame_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self._conn_lock = threading.Lock()
        self._conn: Optional[http.client.HTTPConnection] = None

    def bind_conn(self, conn: Optional[http.client.HTTPConnection]) -> None:
        with self._conn_lock:
            self._conn = conn

    def stop(self) -> None:
        self.stop_event.set()
        with self._conn_lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                sock = getattr(conn, "sock", None)
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                conn.close()
            except Exception:
                pass


class FlowPilotEngine:
    """Lightweight Sidecar Engine with monotonic sinceRev short-circuiting and on-demand anomaly diagnosis."""

    MAX_ACTIVE_STREAMS = 24
    MAX_SNAPSHOT_ROOTS = 24
    MAX_TRACKED_ROOTS = 128
    MAX_BG_SCAN_ROOTS = 16
    MAX_SUB_BG_SCAN = 12
    MAX_WARM_SUB_STREAMS = 4
    WARM_SUB_WINDOW_SEC = 300.0
    BG_SCAN_RECENT_WINDOW_SEC = 86400.0
    RECENT_HOLD_SEC = 180.0
    MAX_RECENT_CARDS = 2
    DISK_STATE_TTL_SEC = 7 * 86400.0
    LS_ABSENT_STATE_TTL_SEC = 86400.0
    MAX_DISK_STATE_FILES = 64
    MAX_MODEL_CALIBRATIONS = 32
    MODEL_CAL_PERSIST_INTERVAL_SEC = 15.0
    SYNC_ACTIVE_INTERVAL_SEC = 1.2
    SYNC_IDLE_INTERVAL_SEC = 4.0
    SYNC_FS_MIN_INTERVAL_SEC = 0.8
    NEW_CONV_DB_GRACE_SEC = 15.0
    NEW_ZERO_STEP_DB_GRACE_SEC = 30.0
    STREAM_READ_TIMEOUT_SEC = 660.0
    PLANNER_CREATED_TS_MAX_AGE_SEC = 780.0
    SQLITE_READ_TIMEOUT_SEC = 0.15
    _ANNOTATION_REMOVED_RE = re.compile(r"\b(?:archived|deleted)\s*:\s*true\b", re.IGNORECASE)
    _NORMAL_TERM_KEYWORDS = ("TERMINAL_CUSTOM_HOOK", "NO_TOOL_CALL", "USER_CANCELED", "USER_CANCELLED", "COMPLETED")
    _ANOMALY_TERM_KEYWORDS = ("ERROR", "INTERRUPT", "DEADLINE", "TIMEOUT", "QUOTA", "EXCEED", "PANIC", "ABORT", "FAIL", "EOF")
    _SERVER_RESTART_EXEMPT_KEYWORDS = ("interrupted due to server shutdown", "server shutdown", "server restart")
    _GENUINE_STREAM_DROP_KEYWORDS = (
        "stream was interrupted", "unexpected eof", "connection reset",
        "wsarecv", "broken pipe", "stream closed unexpectedly",
    )

    def __init__(self, ls_instance_id: str = "") -> None:
        self._lock = threading.RLock()
        self._rev_cond = threading.Condition(self._lock)
        self._stop_event = threading.Event()
        self._sync_wake_event = threading.Event()
        self._poller_started = False
        self._on_config_change: Optional[Callable[[], None]] = None
        self._rpc_local = threading.local()
        self._rpc_conns_lock = threading.Lock()
        self._rpc_conns: set[http.client.HTTPConnection] = set()

        self.ls_address = normalize_ls_address(os.environ.get("ANTIGRAVITY_LS_ADDRESS", ""))
        self.csrf_token = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "").strip()
        self.default_env_cid = os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "").strip()
        self.ls_instance_id = ls_instance_id.strip() or self._derive_ls_instance_id(self.ls_address)
        self._engine_started_at = time.time()
        self._ls_start_ts = self._discover_ls_start_ts()

        app_data_env = os.environ.get("ANTIGRAVITY_APP_DATA_DIR", "").strip()
        self.app_data_dir = Path(app_data_env) if app_data_env else (Path.home() / ".gemini" / "antigravity")
        plugin_data_env = os.environ.get("PLUGIN_DATA", "").strip()
        self.plugin_data_dir = Path(plugin_data_env) if plugin_data_env else (self.app_data_dir / "plugin_data" / "agy-flow-pilot")
        self.state_dir = self.plugin_data_dir / "state"
        self.nodes_dir = self.plugin_data_dir / "nodes"
        self.config_path = self.plugin_data_dir / "config.json"
        self.fast_io_stats_path = self.plugin_data_dir / "fast_io_stats.json"
        self.model_cal_path = self.plugin_data_dir / "model_calibration.json"

        self._config_mtime_ns = 0
        self._fast_io_stats_sig: tuple[int, int] = (0, -1)
        self._fast_io_stats_cache: dict[str, Any] = dict(DEFAULT_FAST_IO_STATS)
        self.config: dict[str, Any] = {
            "stallThresholdSec": 90,
            "hud": dict(DEFAULT_HUD_CONFIG),
            "fastIo": dict(DEFAULT_FAST_IO_CONFIG),
        }

        self._model_labels: dict[str, str] = {}
        self._model_max_tokens: dict[str, int] = {}
        self._default_model_id = ""
        self._last_model_sync_at = 0.0

        self._trajectories: dict[str, dict[str, Any]] = {}
        self._all_ls_cids: set[str] = set()
        self._last_trajectories_sync_at = 0.0
        self._last_trajectories_attempt_at = 0.0
        self._traj_sync_in_flight = False
        self._traj_sync_done_event = threading.Event()
        self._traj_sync_done_event.set()
        self._last_traj_fp: Any = None
        self._active_main_cid = ""
        self._cold_boot_hydrated = False
        self._last_gc_at = 0.0
        self._probing_cids: set[str] = set()
        self._probe_worker_busy = False
        self._raw_parent_map: dict[str, tuple[str, int]] = {}
        self._raw_all_children_by_root: dict[str, list[tuple[str, int]]] = {}
        self._probed_historical_subs: set[str] = set()

        self._db_seen_cids: dict[str, float] = {}
        self._ls_seen_cids: dict[str, float] = {}
        self._first_seen_at: dict[str, float] = {}
        self._known_cids: set[str] = set()
        self._checked_hints: set[str] = set()
        self._removed_cids: dict[str, float] = {}
        self._annotation_archive_cache: dict[str, tuple[int, int, bool]] = {}
        self._dir_watch_sig: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._pending_fs_mutation = False

        self._runtimes: dict[str, ConversationRuntime] = {}
        self._streams: dict[str, _StreamWorkerHandle] = {}
        self._poller_thread: Optional[threading.Thread] = None
        self._large_frame_lock = threading.Lock()
        self._init_conn_sem = threading.Semaphore(1)
        self._last_gc_collect_at = 0.0
        self._last_ws_trim_at = 0.0

        self._state_rev = 0
        self._last_built_rev = 0
        self._has_built_once = False
        self._last_built_at = 0.0
        self._last_running_count = 0
        self._last_stalled_count = 0
        self._last_total_live_tps = 0.0
        self._next_recent_expiry_at = 0.0
        self._last_calm_fp: Any = None
        self._last_bg_active_tick_fp: Any = None
        self._watched_fs_sig: tuple[int, int, int, int, int, int] = (0, 0, 0, 0, 0, 0)
        self._watched_conv_entry_sig = 0
        self._ann_dir_ns = -1
        self._ann_sig_cache_at = 0.0
        self._ann_sig_cache_val = 0
        self._conv_dir_ns = -1
        self._conv_entry_sig_cache = 0
        self._log_fresh_cache: dict[tuple[str, int, str, int], tuple[float, float, bool]] = {}
        self._model_calibrations: dict[str, dict[str, float]] = {}
        self._model_cal_dirty = False
        self._model_cal_mtime_ns = 0
        self._last_model_cal_persist_at = 0.0
        self._load_model_calibrations(force=True)

    def set_config_change_callback(self, cb: Optional[Callable[[], None]]) -> None:
        self._on_config_change = cb

    def _bump_rev_locked(self) -> int:
        self._state_rev += 1
        self._rev_cond.notify_all()
        return self._state_rev

    def bump_rev(self) -> int:
        with self._lock:
            return self._bump_rev_locked()

    def get_rev(self) -> int:
        with self._lock:
            return self._state_rev

    def wake_sse_waiters(self) -> None:
        with self._rev_cond:
            self._rev_cond.notify_all()

    def wait_for_rev_change(
        self,
        last_rev: int,
        timeout: float = 20.0,
        stop_check: Optional[Callable[[], bool]] = None,
    ) -> tuple[int, bool]:
        """Block on _rev_cond until _state_rev != last_rev, stop is signaled, or timeout elapses."""
        deadline = time.monotonic() + max(0.05, float(timeout))
        with self._rev_cond:
            while (
                not self._stop_event.is_set()
                and not (stop_check is not None and stop_check())
                and self._state_rev == last_rev
            ):
                rem = deadline - time.monotonic()
                if rem <= 0.0:
                    break
                self._rev_cond.wait(timeout=rem)
            stopped = self._stop_event.is_set() or bool(stop_check is not None and stop_check())
            return self._state_rev, stopped

    @staticmethod
    def _derive_ls_instance_id(ls_addr: str) -> str:
        if ls_addr and ":" in ls_addr and (tail := ls_addr.rsplit(":", 1)[1].strip()).isdigit():
            return f"ls_{tail}"
        return f"ls_{ls_addr}" if (ls_addr and ls_addr.isdigit()) else f"ls_pid_{os.getpid()}"

    def _discover_ls_start_ts(self) -> float:
        if env_ts := _finite_float(os.environ.get("ANTIGRAVITY_LS_START_TS")):
            if 1_600_000_000.0 < env_ts <= time.time() + 60.0:
                return float(env_ts)
        if self.ls_address:
            if p_ts := _query_parent_ls_start_ts():
                return p_ts
        return self._engine_started_at

    def start(self) -> None:
        self._load_config_safe(force=True)
        with self._lock:
            if self._poller_started:
                return
            self._poller_started = True
            self._engine_started_at = time.time()
            self._ls_start_ts = self._discover_ls_start_ts()
        self._poller_thread = threading.Thread(target=self._background_loop, name="flow-pilot-watchdog", daemon=True)
        self._poller_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._sync_wake_event.set()
        with self._rev_cond:
            self._rev_cond.notify_all()
        with self._lock:
            self._poller_started = False
            handles = list(self._streams.values())
            self._streams.clear()
            dirty_cids = [
                cid for cid, rt in self._runtimes.items()
                if rt.dirty_state and cid not in self._raw_parent_map and not self._is_subagent_summary(cid, self._trajectories.get(cid) or {})
            ]
        for h in handles:
            h.stop()
        with self._rpc_conns_lock:
            rpc_conns = list(self._rpc_conns)
            self._rpc_conns.clear()
        for conn in rpc_conns:
            try:
                conn.close()
            except Exception:
                pass
        for cid in dirty_cids:
            try:
                self._persist_runtime_state(cid)
            except Exception:
                pass
        try:
            self._persist_model_calibrations(force=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Fast Filesystem Watch & Monotonic Short-Circuit Protocol
    # ------------------------------------------------------------------

    def _compute_fast_fs_sig(self) -> tuple[int, int, int, int, int, int]:
        conv_dir = self.app_data_dir / "conversations"
        conv_ns = _stat_mtime_ns(str(conv_dir))
        sum_db_str = str(self.app_data_dir / "conversation_summaries.db")
        sum_ns = _stat_mtime_ns(sum_db_str) ^ (_stat_mtime_ns(sum_db_str + "-wal") << 1)
        ann_dir_str = str(self.app_data_dir / "annotations")
        ann_dir_ns = _stat_mtime_ns(ann_dir_str)
        now_m = time.monotonic()
        with self._lock:
            need_ann_scan = (ann_dir_ns != self._ann_dir_ns) or ((now_m - self._ann_sig_cache_at) >= 2.5)
            cached_ann = self._ann_sig_cache_val
            wal_cids: list[str] = []
            seen_w: set[str] = set()
            for scid in self._streams.keys():
                if scid and scid not in seen_w:
                    seen_w.add(scid)
                    wal_cids.append(scid)
                    if len(wal_cids) >= 16:
                        break
            if len(wal_cids) < 16 and self._active_main_cid and self._active_main_cid not in seen_w:
                seen_w.add(self._active_main_cid)
                wal_cids.append(self._active_main_cid)
            if len(wal_cids) < 16 and self._runtimes:
                ranked_rt = sorted(
                    (
                        (1 if rt.composite_running else 0, rt.sort_ts or rt.last_modified_at, rcid)
                        for rcid, rt in self._runtimes.items()
                        if rcid not in seen_w
                    ),
                    key=lambda x: (x[0], x[1]),
                    reverse=True,
                )
                for _, _, rcid in ranked_rt[: max(0, 16 - len(wal_cids))]:
                    seen_w.add(rcid)
                    wal_cids.append(rcid)
            if len(wal_cids) < 16 and self._trajectories:
                ranked_tr = sorted(
                    (
                        (self._summary_sort_ts(s), tcid)
                        for tcid, s in self._trajectories.items()
                        if isinstance(s, dict) and tcid not in seen_w
                    ),
                    key=lambda x: x[0],
                    reverse=True,
                )
                for _, tcid in ranked_tr[: max(0, 16 - len(wal_cids))]:
                    seen_w.add(tcid)
                    wal_cids.append(tcid)
        if need_ann_scan:
            ann_sig = _scandir_files_sig(ann_dir_str)
            with self._lock:
                self._ann_dir_ns = ann_dir_ns
                self._ann_sig_cache_at = now_m
                self._ann_sig_cache_val = ann_sig
        else:
            ann_sig = cached_ann
        wal_sig = 0
        for w_cid in wal_cids:
            w_ns, w_sz = _stat_mtime_and_size(str(conv_dir / f"{safe_cid(w_cid)}.db-wal"))
            if w_ns > 0 or w_sz > 0:
                wal_sig ^= (hash(w_cid) & 0x7FFFFFFF) ^ w_ns ^ (w_sz << 16)
        cfg_ns = _stat_mtime_ns(str(self.config_path))
        fio_ns = _stat_mtime_ns(str(self.fast_io_stats_path))
        return (conv_ns, sum_ns, ann_sig, cfg_ns, fio_ns, wal_sig)

    def _compute_conv_entry_sig(self, conv_dir_ns: Optional[int] = None) -> int:
        conv_dir_str = str(self.app_data_dir / "conversations")
        dir_ns = _stat_mtime_ns(conv_dir_str) if conv_dir_ns is None else conv_dir_ns
        with self._lock:
            if dir_ns == self._conv_dir_ns and self._conv_dir_ns >= 0:
                return self._conv_entry_sig_cache
        acc = 0
        try:
            with os.scandir(conv_dir_str) as it:
                for e in it:
                    if not e.name.startswith("."):
                        acc = (acc + 1) ^ (hash(e.name) & 0x7FFFFFFF)
        except OSError:
            pass
        with self._lock:
            self._conv_dir_ns = dir_ns
            self._conv_entry_sig_cache = acc
        return acc

    def can_short_circuit_tasks(self, since_rev: int, hint_conversation_id: str = "") -> bool:
        """Ultra-fast (<0.1ms) unchanged-state check for GET /api/tasks?sinceRev=<rev>."""
        if since_rev <= 0:
            return False
        now = time.time()
        with self._lock:
            if not self._has_built_once or since_rev != self._state_rev:
                return False
            hint = (hint_conversation_id or "").strip()
            if hint and hint not in self._known_cids and hint not in self._checked_hints:
                return False
            if self._next_recent_expiry_at > 0.0 and now >= self._next_recent_expiry_at:
                return False
            any_decoding = any(
                rt.ts_active_step_idx >= 0 and rt.ts_phase != "waiting_ttft"
                for rt in self._runtimes.values()
            )
            any_active = (
                self._last_running_count > 0
                or self._last_total_live_tps > 0.0
                or any_decoding
                or any(rt.composite_running or rt.ts_active_step_idx >= 0 or rt.db_bg_script is not None for rt in self._runtimes.values())
            )
            built_age = now - self._last_built_at
            if any_active:
                max_age = 0.50 if (any_decoding or self._last_total_live_tps > 0.0) else 1.50
                if built_age < 0.0 or built_age >= max_age:
                    return False
            elif self._last_stalled_count > 0:
                if built_age < 0.0 or built_age >= 1.5:
                    return False
            if self._pending_fs_mutation and (now - self._last_trajectories_sync_at) >= self.SYNC_FS_MIN_INTERVAL_SEC:
                self._sync_wake_event.set()
                return False
            if not self._poller_started and self.ls_address:
                sync_cap = self.SYNC_ACTIVE_INTERVAL_SEC if any_active else self.SYNC_IDLE_INTERVAL_SEC
                if (now - self._last_trajectories_sync_at) > sync_cap:
                    return False
            exp_fs = self._watched_fs_sig
            exp_conv_entries = self._watched_conv_entry_sig

        cur_fs = self._compute_fast_fs_sig()
        if any_active:
            # While active, routine summary DB flushes are handled off-thread by _background_loop;
            # break short-circuit on dir, annotation, config, fastio, or active .db-wal changes.
            if cur_fs[0] != exp_fs[0] or cur_fs[2:] != exp_fs[2:]:
                return False
        elif cur_fs != exp_fs:
            return False
        if built_age < 0.05 and self._compute_conv_entry_sig(conv_dir_ns=cur_fs[0]) != exp_conv_entries:
            return False
        return True

    def _consume_fs_mutation_signal(self) -> tuple[bool, tuple[int, int, int, int, int, int]]:
        fs_sig = self._compute_fast_fs_sig()
        sig4 = (fs_sig[0], fs_sig[1], fs_sig[2], fs_sig[5])
        with self._lock:
            if sig4 != self._dir_watch_sig:
                self._dir_watch_sig = sig4
                self._checked_hints.clear()
                self._pending_fs_mutation = True
                return True, fs_sig
            return self._pending_fs_mutation, fs_sig

    # ------------------------------------------------------------------
    # Configuration & Fast I/O Stats
    # ------------------------------------------------------------------

    def get_config_snapshot(self) -> dict[str, Any]:
        self._load_config_safe(force=False)
        with self._lock:
            hud_copy = dict(self.config.get("hud") or DEFAULT_HUD_CONFIG)
            hud_copy["pinnedIds"] = list(hud_copy.get("pinnedIds") or [])
            return {
                "stallThresholdSec": int(self.config.get("stallThresholdSec", 90)),
                "hud": hud_copy,
                "fastIo": _sanitize_fast_io_config(self.config.get("fastIo")),
            }

    def get_fast_io_stats(self) -> dict[str, Any]:
        try:
            st = os.stat(str(self.fast_io_stats_path))
            sig = (int(st.st_mtime_ns), int(st.st_size))
        except OSError:
            return dict(DEFAULT_FAST_IO_STATS)
        with self._lock:
            if sig == self._fast_io_stats_sig:
                return dict(self._fast_io_stats_cache)
        try:
            raw = json.loads(self.fast_io_stats_path.read_text(encoding="utf-8"))
        except Exception:
            with self._lock:
                return dict(self._fast_io_stats_cache)
        if not isinstance(raw, dict):
            return dict(DEFAULT_FAST_IO_STATS)
        out = dict(DEFAULT_FAST_IO_STATS)
        for k in ("batchReadCalls", "batchReadFiles", "grepCalls", "globCalls", "outlineCalls"):
            out[k] = max(0, _to_int_safe(raw.get(k)))
        sv = _finite_float(raw.get("savedSecEstimate"))
        out["savedSecEstimate"] = round(max(0.0, sv), 1) if sv is not None else 0.0
        with self._lock:
            changed = out != self._fast_io_stats_cache
            self._fast_io_stats_sig = sig
            self._fast_io_stats_cache = dict(out)
            if changed:
                self._bump_rev_locked()
        return out

    def _load_config_safe(self, force: bool = False) -> None:
        cfg_changed = False
        try:
            st_ns = _stat_mtime_ns(str(self.config_path))
            if st_ns <= 0:
                return
            with self._lock:
                if not force and self._config_mtime_ns > 0 and st_ns == self._config_mtime_ns:
                    return
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                with self._lock:
                    prev_cfg = (
                        int(self.config.get("stallThresholdSec", 90)),
                        dict(self.config.get("hud") or {}),
                        dict(self.config.get("fastIo") or {}),
                    )
                    if raw.get("stallThresholdSec") is not None:
                        self.config["stallThresholdSec"] = max(0, min(3600, int(raw["stallThresholdSec"])))

                    raw_hud = raw.get("hud") if isinstance(raw.get("hud"), dict) else {}
                    sanitized_hud = _sanitize_hud_config(raw_hud, self.config.get("hud"))
                    sanitized_fast_io = _sanitize_fast_io_config(raw.get("fastIo"), self.config.get("fastIo"))
                    self.config["hud"] = sanitized_hud
                    self.config["fastIo"] = sanitized_fast_io
                    self._config_mtime_ns = st_ns
                    next_cfg = (
                        int(self.config["stallThresholdSec"]),
                        dict(sanitized_hud),
                        dict(sanitized_fast_io),
                    )
                    if next_cfg != prev_cfg or self._state_rev <= 0:
                        cfg_changed = True
                        self._bump_rev_locked()
        except Exception:
            return
        if cfg_changed and (cb := self._on_config_change) is not None:
            try:
                cb()
            except Exception:
                pass

    def update_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(patch, dict):
            return {"ok": True, "config": self.get_config_snapshot()}
        self._load_config_safe(force=False)
        with self._lock:
            if patch.get("stallThresholdSec") is not None:
                self.config["stallThresholdSec"] = max(0, min(3600, int(patch["stallThresholdSec"])))

            hud_patch: dict[str, Any] = {}
            for src in (patch.get("hud"), patch.get("prefs")):
                if isinstance(src, dict):
                    hud_patch.update(src)
            for hk in DEFAULT_HUD_CONFIG:
                if hk in patch and hk not in hud_patch:
                    hud_patch[hk] = patch[hk]

            cur_hud = _sanitize_hud_config(hud_patch, self.config.get("hud"))
            cur_hud["pinnedIds"] = list(cur_hud.get("pinnedIds") or [])[:32]
            self.config["hud"] = cur_hud

            fast_io_patch: dict[str, Any] = {}
            for container in (patch, patch.get("prefs"), patch.get("hud"), patch.get("fastIoConfig"), patch.get("fastIo")):
                if not isinstance(container, dict):
                    continue
                for sub_k in ("fastIo", "fastIoConfig"):
                    if isinstance(container.get(sub_k), dict):
                        fast_io_patch.update(container[sub_k])
                for src_k in ("mcpEnabled", "fastIoMcpEnabled"):
                    if container.get(src_k) is not None:
                        fast_io_patch["mcpEnabled"] = container[src_k]

            cur_fast_io = _sanitize_fast_io_config(fast_io_patch, self.config.get("fastIo"))
            self.config["fastIo"] = cur_fast_io
            self._bump_rev_locked()
            snapshot = {
                "stallThresholdSec": int(self.config["stallThresholdSec"]),
                "hud": {**cur_hud, "pinnedIds": list(cur_hud["pinnedIds"])},
                "fastIo": dict(cur_fast_io),
            }

        self._write_json_atomic(self.config_path, snapshot)
        with self._lock:
            self._config_mtime_ns = _stat_mtime_ns(str(self.config_path))
        return {"ok": True, "config": snapshot}

    def _build_ls_headers(self, content_type: str = "application/json") -> dict[str, str]:
        headers = {"Content-Type": content_type, "Connect-Protocol-Version": "1"}
        if self.csrf_token:
            headers["x-antigravity-csrf-token"] = self.csrf_token
            headers["x-codeium-csrf-token"] = self.csrf_token
        return headers

    def _close_local_rpc_conn(self) -> None:
        conn = getattr(self._rpc_local, "conn", None)
        self._rpc_local.conn = None
        self._rpc_local.key = None
        if conn is not None:
            with self._rpc_conns_lock:
                self._rpc_conns.discard(conn)
            try:
                conn.close()
            except Exception:
                pass

    def _get_rpc_conn(self, host: str, port: int, timeout: float, force_new: bool = False) -> http.client.HTTPConnection:
        key = (host, port)
        conn = None if force_new else getattr(self._rpc_local, "conn", None)
        if conn is not None and getattr(self._rpc_local, "key", None) == key:
            conn.timeout = timeout
            sock = getattr(conn, "sock", None)
            if sock is not None:
                try:
                    sock.settimeout(timeout)
                    return conn
                except OSError:
                    self._close_local_rpc_conn()
            else:
                return conn
        else:
            self._close_local_rpc_conn()
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        self._rpc_local.conn = conn
        self._rpc_local.key = key
        with self._rpc_conns_lock:
            self._rpc_conns.add(conn)
        return conn

    def rpc_call(
        self,
        method: str,
        payload: Optional[dict[str, Any]] = None,
        timeout: float = 4.0,
    ) -> dict[str, Any]:
        if not self.ls_address:
            raise RuntimeError("ANTIGRAVITY_LS_ADDRESS is not configured")
        host, port = self._parse_host_port()
        path = f"/exa.language_server_pb.LanguageServerService/{method}"
        data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
        headers = self._build_ls_headers("application/json")
        headers["Connection"] = "keep-alive"
        for attempt in range(2):
            conn = self._get_rpc_conn(host, port, timeout, force_new=(attempt > 0))
            try:
                conn.request("POST", path, body=data, headers=headers)
                resp = conn.getresponse()
                raw = resp.read()
                status, reason, will_close = resp.status, resp.reason, resp.will_close
                if will_close:
                    self._close_local_rpc_conn()
                if status >= 400:
                    err_body = raw.decode("utf-8", errors="replace") if raw else ""
                    raise RuntimeError(f"RPC {method} HTTP {status}: {err_body or reason}")
                return _loads_json_buf(raw) if raw else {}
            except TimeoutError:
                self._close_local_rpc_conn()
                raise
            except (
                http.client.RemoteDisconnected,
                http.client.BadStatusLine,
                http.client.CannotSendRequest,
                http.client.ResponseNotReady,
                http.client.IncompleteRead,
                BrokenPipeError,
                ConnectionResetError,
                ConnectionAbortedError,
                OSError,
            ):
                self._close_local_rpc_conn()
                if attempt == 0:
                    continue
                raise
        return {}

    # ------------------------------------------------------------------
    # Trajectory Discovery & Stream Pool
    # ------------------------------------------------------------------

    @staticmethod
    def _is_subagent_summary(cid: str, summary: dict[str, Any]) -> bool:
        meta = summary.get("trajectoryMetadata")
        if not isinstance(meta, dict):
            return False
        parent_cid = str(meta.get("parentConversationId") or "").strip()
        root_cid = str(meta.get("rootConversationId") or "").strip()
        if (parent_cid and parent_cid != cid) or (root_cid and root_cid != cid):
            return True
        if int(meta.get("nestingDepth") or 0) > 0 or meta.get("subagentSpec"):
            return True
        script = meta.get("agentScript")
        return isinstance(script, dict) and bool(script.get("subagent"))

    @classmethod
    def _classify_agent_kind(cls, cid: str, summary: dict[str, Any]) -> str:
        if not cls._is_subagent_summary(cid, summary):
            return "root"
        meta = summary.get("trajectoryMetadata") if isinstance(summary.get("trajectoryMetadata"), dict) else {}
        script = meta.get("agentScript") if isinstance(meta.get("agentScript"), dict) else {}
        spec = meta.get("subagentSpec") if isinstance(meta.get("subagentSpec"), dict) else {}
        script_name = str(script.get("name") or "").strip().lower()
        if script_name == "monitor" or script_name.startswith("monitor-"):
            return "swarm_monitor"
        if str(spec.get("typeName") or "").strip().lower() == "worker" or bool(script.get("subagent")):
            return "swarm_worker"
        return "subagent"

    @classmethod
    def _resolve_root_cid(cls, cid: str, summaries: dict[str, dict[str, Any]]) -> str:
        cur = cid
        visited: set[str] = set()
        for _ in range(8):
            if not cur or cur in visited:
                break
            visited.add(cur)
            s = summaries.get(cur)
            if not isinstance(s, dict):
                break
            if not cls._is_subagent_summary(cur, s):
                return cur
            meta = s.get("trajectoryMetadata") if isinstance(s.get("trajectoryMetadata"), dict) else {}
            r_cid = str(meta.get("rootConversationId") or "").strip()
            p_cid = str(meta.get("parentConversationId") or "").strip()
            if r_cid and r_cid != cur and r_cid in summaries and not cls._is_subagent_summary(r_cid, summaries[r_cid]):
                return r_cid
            nxt = p_cid if (p_cid and p_cid != cur) else r_cid
            if not nxt or nxt == cur:
                break
            cur = nxt
        s0 = summaries.get(cid)
        if isinstance(s0, dict):
            meta0 = s0.get("trajectoryMetadata") if isinstance(s0.get("trajectoryMetadata"), dict) else {}
            fallback = str(meta0.get("rootConversationId") or meta0.get("parentConversationId") or "").strip()
            if fallback and fallback != cid:
                return fallback
        return ""

    @classmethod
    def _is_subagent_abnormal(cls, summary: dict[str, Any], s_status: str, term_reason: str, now: float) -> bool:
        if s_status in ("ERROR", "FAILED", "ABORTED"):
            return True
        if not term_reason or any(ok_kw in term_reason for ok_kw in cls._NORMAL_TERM_KEYWORDS):
            return False
        if any(kw in term_reason for kw in ("ERROR", "DEADLINE", "QUOTA", "EXCEEDED", "INTERRUPTED", "PANIC")):
            mod_ts = parse_iso_ts(str(summary.get("lastModifiedTime") or ""))
            return mod_ts <= 0 or (now - mod_ts) < 7200
        return False

    @staticmethod
    def _summary_sort_ts(summary: dict[str, Any]) -> float:
        ann = summary.get("annotations") if isinstance(summary.get("annotations"), dict) else {}
        return max(
            parse_iso_ts(str(ann.get("lastUserViewTime") or "")),
            parse_iso_ts(str(summary.get("lastUserInputTime") or "")),
            parse_iso_ts(str(summary.get("lastModifiedTime") or "")),
            parse_iso_ts(str(summary.get("createdTime") or "")),
        )

    @staticmethod
    def _extract_title(cid: str, summary: dict[str, Any]) -> str:
        ann = summary.get("annotations") if isinstance(summary.get("annotations"), dict) else {}
        for candidate in (ann.get("title"), summary.get("summary")):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return f"会话 {cid[:8]}"

    def _is_annotation_file_archived(self, cid: str) -> bool:
        if not cid:
            return False
        sk = safe_cid(cid)
        ann_file = self.app_data_dir / "annotations" / f"{sk}.pbtxt"
        try:
            st = os.stat(str(ann_file))
            mtime_ns, f_size = int(st.st_mtime_ns), int(st.st_size)
        except OSError:
            with self._lock:
                self._annotation_archive_cache.pop(sk, None)
            return False
        with self._lock:
            cached = self._annotation_archive_cache.get(sk)
            if cached is not None and cached[0] == mtime_ns and cached[1] == f_size:
                return cached[2]
        try:
            removed = bool(self._ANNOTATION_REMOVED_RE.search(ann_file.read_text(encoding="utf-8", errors="ignore")))
        except OSError:
            removed = False
        with self._lock:
            if len(self._annotation_archive_cache) > 512:
                self._annotation_archive_cache.clear()
            self._annotation_archive_cache[sk] = (mtime_ns, f_size, removed)
        return removed

    def _is_conversation_removed(
        self, cid: str, summary: Optional[dict[str, Any]], now: float
    ) -> bool:
        if not cid or not isinstance(summary, dict):
            return True
        if any(bool(summary.get(k, False)) for k in ("killed", "deleted", "archived", "isDeleted", "isArchived")):
            return True
        ann = summary.get("annotations")
        if isinstance(ann, dict) and (bool(ann.get("archived", False)) or bool(ann.get("deleted", False))):
            return True
        if self._is_annotation_file_archived(cid):
            return True

        conv_dir = self.app_data_dir / "conversations"
        if not conv_dir.is_dir():
            return False
        if (conv_dir / f"{safe_cid(cid)}.db").is_file():
            with self._lock:
                self._db_seen_cids[cid] = now
                if (rt_ok := self._runtimes.get(cid)) is not None:
                    rt_ok.had_db_on_disk = True
                self._removed_cids.pop(cid, None)
            return False

        with self._lock:
            rt = self._runtimes.get(cid)
            previously_had_db = (cid in self._db_seen_cids) or bool(rt and rt.had_db_on_disk)
        if previously_had_db:
            return True

        s_stat = short_run_status(str(summary.get("status") or "IDLE"))
        if s_stat == "RUNNING" or s_stat in ACTIVE_SUBAGENT_STATUSES or bool(
            rt and (rt.composite_running or (rt.last_stream_frame_at > 0.0 and (now - rt.last_stream_frame_at) < 20.0))
        ):
            return False

        created_ts = parse_iso_ts(str(summary.get("createdTime") or ""))
        mod_ts = self._summary_sort_ts(summary)
        with self._lock:
            if len(self._first_seen_at) > 1024:
                self._first_seen_at.clear()
            first_seen = self._first_seen_at.setdefault(cid, now)
        ref_ts = max(created_ts, mod_ts) or first_seen
        grace_sec = self.NEW_ZERO_STEP_DB_GRACE_SEC if int(summary.get("stepCount") or 0) == 0 else self.NEW_CONV_DB_GRACE_SEC
        return (now - ref_ts) > grace_sec

    def is_cid_known_removed(self, cid: str) -> bool:
        if not (c := (cid or "").strip()):
            return False
        if self._is_annotation_file_archived(c):
            return True
        with self._lock:
            if c in self._removed_cids:
                return True
            had_db = c in self._db_seen_cids
        if had_db:
            conv_dir = self.app_data_dir / "conversations"
            if conv_dir.is_dir() and not (conv_dir / f"{safe_cid(c)}.db").is_file():
                return True
        return False

    def _resolve_child_root_locked(self, cid: str) -> str:
        if (parent_info := self._raw_parent_map.get(cid)) is not None and parent_info[0]:
            return parent_info[0]
        if isinstance(s := self._trajectories.get(cid), dict) and self._is_subagent_summary(cid, s):
            return self._resolve_root_cid(cid, self._trajectories)
        return ""

    def _purge_removed_cids_locked(
        self,
        cids_to_purge: set[str],
        explicitly_removed: set[str],
        now: float,
        still_in_ls: Optional[set[str]] = None,
    ) -> tuple[list[_StreamWorkerHandle], list[Path], bool]:
        stopped_handles: list[_StreamWorkerHandle] = []
        unlink_paths: list[Path] = []
        pinned_changed = False
        cur_hud = self.config.get("hud")
        cur_pinned = list((cur_hud or {}).get("pinnedIds") or [])

        for pcid in cids_to_purge:
            if not pcid:
                continue
            aged_out_only = bool(still_in_ls and pcid in still_in_ls and pcid not in explicitly_removed)
            if (handle := self._streams.pop(pcid, None)) is not None:
                handle.stop_event.set()
                stopped_handles.append(handle)
            if (dead_rt := self._runtimes.get(pcid)) is not None and dead_rt.usage_by_gm:
                p_root_cid = self._resolve_child_root_locked(pcid)
                if p_root_cid and p_root_cid not in cids_to_purge and (root_rt := self._runtimes.get(p_root_cid)) is not None:
                    summary_6 = _sum_usage_by_gm(dead_rt.usage_by_gm)
                    if root_rt.archived_children_usage.get(pcid) != summary_6:
                        root_rt.archived_children_usage[pcid] = summary_6
                        root_rt.dirty_state = True
                        self._bump_rev_locked()
            self._runtimes.pop(pcid, None)
            self._known_cids.discard(pcid)
            self._first_seen_at.pop(pcid, None)
            if not aged_out_only:
                self._removed_cids[pcid] = now
            if self._active_main_cid == pcid:
                self._active_main_cid = ""
            if not aged_out_only and pcid in cur_pinned:
                cur_pinned = [x for x in cur_pinned if x != pcid]
                pinned_changed = True

            if not aged_out_only:
                sk = safe_cid(pcid)
                unlink_paths.append(self.state_dir / f"{sk}.json")

        if len(self._db_seen_cids) > 2048:
            for old_k, _ in sorted(self._db_seen_cids.items(), key=lambda kv: kv[1])[: len(self._db_seen_cids) - 1536]:
                self._db_seen_cids.pop(old_k, None)
        if len(self._removed_cids) > 256:
            for old_k, _ in sorted(self._removed_cids.items(), key=lambda kv: kv[1])[: len(self._removed_cids) - 192]:
                self._removed_cids.pop(old_k, None)
        if pinned_changed and isinstance(cur_hud, dict):
            cur_hud["pinnedIds"] = cur_pinned
        return stopped_handles, unlink_paths, pinned_changed

    def _record_model_max_tokens_locked(self, max_tokens: int, *keys: str) -> None:
        iv = int(max_tokens) if max_tokens else 0
        if iv < 1024:
            return
        if len(self._model_max_tokens) > 512:
            self._model_max_tokens.clear()
        for raw in keys:
            s = (raw or "").strip()
            if not s or s.lower() == "auto":
                continue
            self._model_max_tokens[s] = iv
            self._model_max_tokens[s.upper()] = iv

    def _resolve_model_max_tokens_locked(self, *candidates: str) -> int:
        expanded: list[str] = []
        seen: set[str] = set()
        for raw in candidates:
            s = (raw or "").strip()
            if not s:
                continue
            for cand in (s, s.upper()):
                if (live_val := self._model_max_tokens.get(cand)) and live_val > 0:
                    return int(live_val)
            lbl = self._model_labels.get(s) or self._model_labels.get(s.upper()) or ""
            if lbl:
                for cand in (lbl, lbl.upper()):
                    if (live_val := self._model_max_tokens.get(cand)) and live_val > 0:
                        return int(live_val)
            probe = f"{s} {lbl}" if (lbl and lbl != s) else s
            if probe not in seen:
                seen.add(probe)
                expanded.append(probe)
            if lbl and lbl not in seen:
                seen.add(lbl)
                expanded.append(lbl)
        for cand in expanded:
            inferred = _infer_model_max_tokens(cand, self._model_max_tokens)
            if inferred != DEFAULT_CONTEXT_MAX_TOKENS:
                return inferred
        return DEFAULT_CONTEXT_MAX_TOKENS

    def _sync_models_if_needed(self, force: bool = False) -> None:
        if not self.ls_address:
            return
        now = time.time()
        if not force and (now - self._last_model_sync_at) < 60.0:
            return
        try:
            data = self.rpc_call("GetCascadeModelConfigData", {}, timeout=3.0)
            mapping: dict[str, str] = {}
            max_tokens_map: dict[str, int] = {}
            all_cfg_items: list[Any] = list(data.get("clientModelConfigs") or [])
            if isinstance(bm_cfgs := data.get("battleModeModelConfigs"), list):
                all_cfg_items.extend(bm_cfgs)
            for item in all_cfg_items:
                if not isinstance(item, dict):
                    continue
                label = str(item.get("label") or "").strip()
                moa = item.get("modelOrAlias") if isinstance(item.get("modelOrAlias"), dict) else {}
                mid = str(moa.get("model") or moa.get("alias") or "").strip()
                canon_id = str(item.get("modelId") or "").strip()
                if label:
                    if mid:
                        mapping[mid] = label
                        mapping[mid.upper()] = label
                    if canon_id:
                        mapping[canon_id] = label
                        mapping[canon_id.upper()] = label
                if (item_max_tok := _extract_model_max_tokens_from_config(item)) > 0:
                    for key in (mid, canon_id, label):
                        if key and key.lower() != "auto":
                            max_tokens_map[key] = item_max_tok
                            max_tokens_map[key.upper()] = item_max_tok

            if isinstance(grouped := data.get("groupedModels"), dict):
                for group_id, g_info in grouped.items():
                    if not isinstance(group_id, str) or not isinstance(g_info, dict):
                        continue
                    g_disp = str(g_info.get("displayName") or "").strip()
                    g_max = _extract_model_max_tokens_from_config(g_info)
                    if g_disp:
                        mapping.setdefault(group_id, g_disp)
                        mapping.setdefault(group_id.upper(), g_disp)
                    if g_max > 0:
                        for k in (group_id, g_disp):
                            if k and k.lower() != "auto":
                                max_tokens_map.setdefault(k, g_max)
                                max_tokens_map.setdefault(k.upper(), g_max)
                    for lvl in g_info.get("supportedThinkingLevels") or []:
                        if not isinstance(lvl, dict):
                            continue
                        sfx = str(lvl.get("thinkingSuffix") or "").strip()
                        t_disp = str(lvl.get("thinkingDisplayName") or "").strip()
                        if sfx and g_disp:
                            var_id = f"{group_id}-{sfx}"
                            var_lbl = f"{g_disp} ({t_disp})" if t_disp else g_disp
                            mapping.setdefault(var_id, var_lbl)
                            mapping.setdefault(var_id.upper(), var_lbl)
                            l_max = _extract_model_max_tokens_from_config(lvl) or g_max
                            if l_max > 0:
                                for k in (var_id, var_lbl):
                                    max_tokens_map.setdefault(k, l_max)
                                    max_tokens_map.setdefault(k.upper(), l_max)

            default_cfg = data.get("defaultOverrideModelConfig") if isinstance(data.get("defaultOverrideModelConfig"), dict) else {}
            default_moa = default_cfg.get("modelOrAlias") if isinstance(default_cfg.get("modelOrAlias"), dict) else {}
            default_id = str(default_moa.get("model") or default_moa.get("alias") or "").strip()
            with self._lock:
                self._model_labels.update(mapping)
                if max_tokens_map:
                    self._model_max_tokens.update(max_tokens_map)
                if default_id:
                    self._default_model_id = default_id
                self._last_model_sync_at = now
        except Exception:
            with self._lock:
                self._last_model_sync_at = now - 50.0

    def _sync_trajectories(self, force: bool = False, reason: str = "poll") -> bool:
        now_req = time.time()
        wait_event: Optional[threading.Event] = None
        with self._lock:
            has_active = any(rt.composite_running or rt.ts_active_step_idx >= 0 for rt in self._runtimes.values()) or self._last_running_count > 0
            if self._last_trajectories_sync_at <= 0.0 or force:
                min_interval = 0.0
            elif reason in ("fs", "hint"):
                min_interval = self.SYNC_FS_MIN_INTERVAL_SEC
            else:
                min_interval = self.SYNC_ACTIVE_INTERVAL_SEC if has_active else self.SYNC_IDLE_INTERVAL_SEC

            if self._traj_sync_in_flight:
                if not force and self._last_trajectories_sync_at > 0.0:
                    return False
                wait_event = self._traj_sync_done_event
            elif min_interval > 0.0:
                if (now_req - self._last_trajectories_sync_at) < min_interval:
                    return False
                if self._last_trajectories_attempt_at > 0.0 and (now_req - self._last_trajectories_attempt_at) < min(min_interval, 1.0):
                    return False

            if wait_event is None:
                self._traj_sync_in_flight = True
                self._traj_sync_done_event.clear()
                self._last_trajectories_attempt_at = now_req

        if wait_event is not None:
            wait_event.wait(timeout=2.5)
            if not force:
                return False
            with self._lock:
                if self._traj_sync_in_flight:
                    return False
                self._traj_sync_in_flight = True
                self._traj_sync_done_event.clear()
                self._last_trajectories_attempt_at = time.time()

        try:
            return self._sync_trajectories_inner()
        finally:
            with self._lock:
                self._traj_sync_in_flight = False
                self._traj_sync_done_event.set()

    def _sync_trajectories_inner(self) -> bool:
        raw_summaries: dict[str, Any] = {}
        if self.ls_address:
            try:
                data = self.rpc_call("GetAllCascadeTrajectories", {}, timeout=2.2)
            except Exception:
                return False
            fetched = data.get("trajectorySummaries")
            if not isinstance(fetched, dict):
                return False
            raw_summaries = fetched

        now = time.time()
        fs_sig = self._compute_fast_fs_sig()
        with self._lock:
            self._dir_watch_sig = (fs_sig[0], fs_sig[1], fs_sig[2], fs_sig[5])
            self._pending_fs_mutation = False
            if raw_summaries:
                self._all_ls_cids = set(raw_summaries.keys())

        with self._lock:
            pinned_set = set((self.config.get("hud") or {}).get("pinnedIds") or [])
            active_mem_cids = {
                cid for cid, rt in self._runtimes.items()
                if rt.composite_running or rt.just_finished_at > 0.0 or cid in self._streams
            }
            known_or_mem = self._known_cids | set(self._runtimes.keys()) | set(self._streams.keys())

        # Bound per-sync disk I/O & working set to O(1) even with 1,000+ historical conversations.
        subagent_cids: set[str] = set()
        selected_roots: set[str] = set()
        ranked_roots: list[tuple[float, str]] = []
        explicitly_removed: set[str] = set()

        for cid, s in raw_summaries.items():
            if not isinstance(s, dict):
                continue
            if self._is_subagent_summary(cid, s):
                subagent_cids.add(cid)
                continue
            ann = s.get("annotations")
            in_mem_removed = any(bool(s.get(k, False)) for k in ("killed", "deleted", "archived", "isDeleted", "isArchived")) or (
                isinstance(ann, dict) and (bool(ann.get("archived", False)) or bool(ann.get("deleted", False)))
            )
            if in_mem_removed:
                if cid in known_or_mem:
                    explicitly_removed.add(cid)
                continue
            if (
                short_run_status(str(s.get("status") or "IDLE")) in ACTIVE_ROOT_STATUSES
                or cid in pinned_set
                or cid == self.default_env_cid
                or cid in active_mem_cids
            ):
                selected_roots.add(cid)
            else:
                ranked_roots.append((self._summary_sort_ts(s), cid))

        if len(ranked_roots) > self.MAX_TRACKED_ROOTS:
            top_ranked = heapq.nlargest(self.MAX_TRACKED_ROOTS, ranked_roots, key=lambda x: x[0])
        else:
            top_ranked = ranked_roots
        selected_roots.update(cid for _, cid in top_ranked)

        raw_parent_map: dict[str, tuple[str, int]] = {}
        raw_all_children_by_root: dict[str, list[tuple[str, int]]] = {}
        selected_subs: set[str] = set()
        for r_cid_item in subagent_cids:
            r_sum = raw_summaries[r_cid_item]
            resolved_root = self._resolve_root_cid(r_cid_item, raw_summaries)
            s_active = (not bool(r_sum.get("killed", False))) and (
                short_run_status(str(r_sum.get("status") or "IDLE")) in ACTIVE_SUBAGENT_STATUSES
            )
            if resolved_root and resolved_root != r_cid_item and (resolved_root in selected_roots or s_active):
                if s_active and resolved_root in raw_summaries and resolved_root not in subagent_cids:
                    selected_roots.add(resolved_root)
                s_cnt = max(0, _to_int_safe(r_sum.get("stepCount")))
                raw_parent_map[r_cid_item] = (resolved_root, s_cnt)
                raw_all_children_by_root.setdefault(resolved_root, []).append((r_cid_item, s_cnt))
                selected_subs.add(r_cid_item)

        selected_cids = selected_roots | selected_subs
        if selected_cids:
            with self._lock:
                for r_cid in selected_cids:
                    self._ls_seen_cids[r_cid] = now
                if len(self._ls_seen_cids) > 2048:
                    for old_k, _ in sorted(self._ls_seen_cids.items(), key=lambda kv: kv[1])[: len(self._ls_seen_cids) - 1536]:
                        self._ls_seen_cids.pop(old_k, None)

        clean_summaries: dict[str, dict[str, Any]] = {}
        for cid in selected_cids:
            summary = raw_summaries[cid]
            if self._is_conversation_removed(cid, summary, now):
                explicitly_removed.add(cid)
                continue
            clean_summaries[cid] = dict(summary)

        traj_fp = tuple(
            sorted(
                (cid, str(s.get("status") or ""), int(s.get("stepCount") or 0), str(s.get("lastModifiedTime") or ""),
                 self._extract_title(cid, s), str(s.get("latestExecutorTerminationReason") or ""))
                for cid, s in clean_summaries.items()
            )
        )

        to_persist_idle: list[str] = []
        desired_streams: list[str] = []
        stopped_handles: list[_StreamWorkerHandle] = []
        unlink_paths: list[Path] = []
        pinned_changed_snap: Optional[dict[str, Any]] = None
        still_in_ls = set(raw_summaries.keys()) - explicitly_removed

        with self._lock:
            if raw_summaries or self.ls_address:
                self._raw_parent_map = raw_parent_map
                self._raw_all_children_by_root = raw_all_children_by_root
            was_cold_boot = not self._cold_boot_hydrated
            disappeared = (self._known_cids - set(clean_summaries.keys())) | explicitly_removed
            stale_mem = {k for k in (set(self._runtimes.keys()) | set(self._streams.keys())) if k not in clean_summaries}
            all_to_purge = disappeared | stale_mem
            if all_to_purge:
                stopped_handles, unlink_paths, pinned_changed = self._purge_removed_cids_locked(
                    all_to_purge, explicitly_removed, now, still_in_ls=still_in_ls
                )
                if pinned_changed:
                    cur_hud = dict(self.config.get("hud") or DEFAULT_HUD_CONFIG)
                    pinned_changed_snap = {
                        "stallThresholdSec": int(self.config.get("stallThresholdSec", 90)),
                        "hud": {**cur_hud, "pinnedIds": list(cur_hud.get("pinnedIds") or [])},
                        "fastIo": _sanitize_fast_io_config(self.config.get("fastIo")),
                    }

            if traj_fp != self._last_traj_fp or all_to_purge:
                self._last_traj_fp = traj_fp
                self._checked_hints.clear()
                self._bump_rev_locked()

            for alive_cid in clean_summaries:
                self._removed_cids.pop(alive_cid, None)
            self._known_cids = set(clean_summaries.keys())
            self._trajectories = clean_summaries
            self._last_trajectories_sync_at = now
            pinned_set = set((self.config.get("hud") or {}).get("pinnedIds") or [])

            children_by_root: dict[str, list[tuple[str, dict[str, Any]]]] = {}
            root_items: list[tuple[float, str, dict[str, Any]]] = []
            for cid, summary in clean_summaries.items():
                if not isinstance(summary, dict):
                    continue
                if self._is_subagent_summary(cid, summary):
                    if (r_cid := self._resolve_root_cid(cid, clean_summaries)) and r_cid in clean_summaries:
                        children_by_root.setdefault(r_cid, []).append((cid, summary))
                else:
                    root_items.append((self._summary_sort_ts(summary), cid, summary))

            root_items.sort(key=lambda x: x[0], reverse=True)
            main_candidates: list[tuple[int, float, str]] = []
            pri0_roots: list[tuple[float, str]] = []
            pri1_roots: list[tuple[float, str]] = []
            pri2_subs: list[tuple[float, str]] = []
            pri3_roots: list[tuple[float, str]] = []
            pri4_warm_subs: list[tuple[float, str]] = []

            for idx_pos, (sort_ts, cid, summary) in enumerate(root_items):
                raw_status = str(summary.get("status") or "CASCADE_RUN_STATUS_IDLE")
                root_short_stat = short_run_status(raw_status)
                rt_existing = self._runtimes.get(cid)
                root_running = self._is_root_live_active_locked(rt_existing, summary, root_short_stat)
                running_sub_cids: list[tuple[float, str]] = []
                warm_sub_cids: list[tuple[float, str]] = []
                has_sub_error = False
                for scid, s_sum in children_by_root.get(cid, []):
                    if bool(s_sum.get("killed", False)):
                        continue
                    kind = self._classify_agent_kind(scid, s_sum)
                    s_stat = short_run_status(str(s_sum.get("status") or "CASCADE_RUN_STATUS_IDLE"))
                    term = str(s_sum.get("latestExecutorTerminationReason") or "").upper()
                    s_mod = parse_iso_ts(str(s_sum.get("lastModifiedTime") or s_sum.get("createdTime") or ""))
                    sub_rt_ex = self._runtimes.get(scid)
                    sub_live_run = self._is_subagent_live_running_locked(sub_rt_ex, s_sum, s_stat)
                    sub_bg_ex = (
                        self._find_active_bg_script_locked(sub_rt_ex)
                        if sub_rt_ex is not None
                        else None
                    )
                    if sub_live_run or sub_bg_ex is not None:
                        running_sub_cids.append((s_mod, scid))
                    elif self._is_subagent_abnormal(s_sum, s_stat, term, now):
                        has_sub_error = True
                    elif kind != "swarm_monitor" and s_mod > 0.0 and (now - s_mod) <= self.WARM_SUB_WINDOW_SEC:
                        # Keep streams warm for swarm_workers, already-connected subagents, or depth-1 orchestrators;
                        # avoid cold-opening 30MB finished leaf /boost workers on idle boot.
                        sub_meta = s_sum.get("trajectoryMetadata") if isinstance(s_sum.get("trajectoryMetadata"), dict) else {}
                        depth = int(sub_meta.get("nestingDepth") or 1)
                        if kind == "swarm_worker" or scid in self._streams or depth <= 1:
                            warm_sub_cids.append((s_mod, scid))

                active_bg = self._find_active_bg_script_locked(rt_existing) if rt_existing is not None else None
                has_bg_script = active_bg is not None
                composite_running = root_running or bool(running_sub_cids) or has_bg_script
                main_candidates.append((1 if composite_running else 0, sort_ts, cid))
                if not (idx_pos < 28 or composite_running or has_sub_error or cid in pinned_set or cid == self.default_env_cid or cid in self._runtimes):
                    continue

                rt = self._get_or_create_runtime(cid)
                rt.title = self._extract_title(cid, summary)
                ws_name, ws_path, br_name = _extract_workspace_info(summary)
                if ws_name:
                    rt.workspace_name, rt.workspace_path = ws_name, ws_path
                if br_name:
                    rt.branch_name = br_name
                rt.last_modified_at = parse_iso_ts(str(summary.get("lastModifiedTime") or ""))
                rt.sort_ts = sort_ts
                rt.latest_termination_reason = str(summary.get("latestExecutorTerminationReason") or "")

                if not rt.stream_connected:
                    rt.raw_status = raw_status
                    if root_short_stat not in ACTIVE_ROOT_STATUSES:
                        rt.raw_executor_loop_status = ""
                    rt.total_steps = max(rt.total_steps, int(summary.get("stepCount") or 0))
                else:
                    rt.total_steps = max(rt.total_steps, int(summary.get("stepCount") or 0))

                bg_started_ts = float(active_bg["startedAt"]) if (not root_running and not running_sub_cids and active_bg and float(active_bg.get("startedAt") or 0.0) > 0.0) else 0.0
                if was_cold_boot:
                    rt.composite_running = composite_running
                    if composite_running and rt.run_batch_started_at <= 0.0:
                        rt.run_batch_started_at = bg_started_ts or rt.last_modified_at or now
                        rt.dirty_state = True
                elif composite_running and not rt.composite_running:
                    rt.composite_running = True
                    rt.run_batch_started_at, rt.just_finished_at = bg_started_ts or now, 0.0
                    rt.dirty_state = True
                    self._bump_rev_locked()
                elif not composite_running and rt.composite_running:
                    # RUNNING -> IDLE transition: persist runtime state once!
                    rt.composite_running = False
                    rt.just_finished_at, rt.run_batch_started_at = now, 0.0
                    rt.dirty_state = True
                    to_persist_idle.append(cid)
                    self._bump_rev_locked()
                elif composite_running and rt.run_batch_started_at <= 0.0:
                    rt.run_batch_started_at = bg_started_ts or rt.last_modified_at or now

                rt_err = rt.db_latest_error
                rt_term_up = (rt.latest_termination_reason or "").upper()
                has_rt_alert = has_sub_error or bool(
                    isinstance(rt_err, dict)
                    and not bool(rt_err.get("isServerRestart"))
                    and int(rt_err.get("stepIndex", -1)) > rt.last_user_input_idx
                    and not any(ok_kw in rt_term_up for ok_kw in self._NORMAL_TERM_KEYWORDS)
                    and (now - float(rt_err.get("timestamp") or 0.0)) <= 3600.0
                )
                is_recent = (
                    not composite_running and rt.just_finished_at > 0.0
                    and rt.just_finished_at > rt.dismissed_recent_at
                    and (now - rt.just_finished_at) <= self.RECENT_HOLD_SEC
                )
                if composite_running:
                    pri0_roots.append((sort_ts, cid))
                    pri2_subs.extend(running_sub_cids)
                    pri4_warm_subs.extend(warm_sub_cids)
                elif cid in pinned_set or has_rt_alert:
                    pri1_roots.append((sort_ts, cid))
                    pri4_warm_subs.extend(
                        (m_ts, sc) for m_ts, sc in warm_sub_cids if sc in self._streams
                    )
                elif is_recent:
                    pri3_roots.append((rt.just_finished_at, cid))
                    pri4_warm_subs.extend(warm_sub_cids)
                elif idx_pos < 4:
                    # Only retain already-open warm streams for idle roots (zero cold-boot 30MB downloads).
                    pri4_warm_subs.extend(
                        (m_ts, sc) for m_ts, sc in warm_sub_cids if sc in self._streams
                    )

            self._cold_boot_hydrated = True
            if main_candidates:
                main_candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
                self._active_main_cid = main_candidates[0][2]
            elif self.default_env_cid and self.default_env_cid in clean_summaries:
                self._active_main_cid = self.default_env_cid
            else:
                self._active_main_cid = ""

            for grp in (pri0_roots, pri1_roots, pri2_subs, pri3_roots, pri4_warm_subs):
                grp.sort(key=lambda x: x[0], reverse=True)
            seen_s: set[str] = set()
            for _, sc in pri0_roots + pri1_roots + pri2_subs + pri3_roots + pri4_warm_subs[: self.MAX_WARM_SUB_STREAMS]:
                if sc and sc not in seen_s:
                    seen_s.add(sc)
                    desired_streams.append(sc)
                if len(desired_streams) >= self.MAX_ACTIVE_STREAMS:
                    break
            if not desired_streams and self._active_main_cid:
                desired_streams.append(self._active_main_cid)

        for sh in stopped_handles:
            sh.stop()
        for up in unlink_paths:
            try:
                up.unlink(missing_ok=True)
            except OSError:
                pass
        if pinned_changed_snap is not None:
            self._write_json_atomic(self.config_path, pinned_changed_snap)
            with self._lock:
                self._config_mtime_ns = _stat_mtime_ns(str(self.config_path))
        for pcid in to_persist_idle:
            self._persist_runtime_state(pcid)
        self._reconcile_stream_pool(desired_streams)
        return True

    def _reconcile_stream_pool(self, desired_cids: list[str]) -> None:
        keep_set = set(desired_cids[: self.MAX_ACTIVE_STREAMS])
        now = time.time()
        reaped_handles: list[_StreamWorkerHandle] = []
        with self._lock:
            for scid, handle in list(self._streams.items()):
                if scid in keep_set:
                    continue
                summary = self._trajectories.get(scid)
                is_sub = self._is_subagent_summary(scid, summary) if isinstance(summary, dict) else True
                s_stat = short_run_status(str(summary.get("status") or "IDLE")) if isinstance(summary, dict) else "IDLE"
                killed = bool(summary.get("killed", False)) if isinstance(summary, dict) else True
                rt_chk = self._runtimes.get(scid)
                sub_live_active = is_sub and isinstance(summary, dict) and self._is_subagent_live_running_locked(rt_chk, summary, s_stat)
                if killed or (is_sub and not sub_live_active) or len(self._streams) > self.MAX_ACTIVE_STREAMS or (not is_sub and s_stat not in ACTIVE_ROOT_STATUSES and scid not in keep_set):
                    self._streams.pop(scid, None)
                    reaped_handles.append(handle)
                    if (rt := self._runtimes.get(scid)) is not None:
                        rt.stream_connected = rt.stream_initial_seen = False
                        rt.raw_executor_loop_status = ""
                        rt.ts_waiting_turn_since = 0.0
                        if rt.ts_active_step_idx >= 0:
                            if rt.ts_est_output_tokens > 0:
                                self._finalize_planner_step_locked(rt, idx=rt.ts_active_step_idx, st=None, gm_info=None, now=now, is_initial=False)
                            else:
                                self._reset_active_token_speed_locked(rt)

            if len(self._runtimes) > 96:
                pinned_set = set((self.config.get("hud") or {}).get("pinnedIds") or [])
                inactive_cids = sorted(
                    (rt.last_requested_at or rt.sort_ts, k)
                    for k, rt in self._runtimes.items()
                    if k not in self._streams and k not in keep_set and k not in pinned_set and k != self._active_main_cid
                )
                for _, old_cid in inactive_cids[: max(0, len(self._runtimes) - 72)]:
                    self._runtimes.pop(old_cid, None)

        for rh in reaped_handles:
            rh.stop()
        for d_cid in desired_cids[: self.MAX_ACTIVE_STREAMS]:
            self._ensure_stream(d_cid, wait_sec=0.0)

    def _open_stream_frame0(self, cid: str, timeout: float) -> tuple[http.client.HTTPConnection, Any, Optional[bytes | bytearray]]:
        host, port = self._parse_host_port()
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            body = json.dumps({"conversationId": cid}).encode("utf-8")
            envelope = b"\x00" + struct.pack(">I", len(body)) + body
            headers = self._build_ls_headers("application/connect+json")
            conn.request("POST", "/exa.language_server_pb.LanguageServerService/StreamAgentStateUpdates", body=envelope, headers=headers)
            resp = conn.getresponse()
            hdr = _read_exact(resp, 5) if resp.status == 200 else None
            return conn, resp, hdr
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise

    def _probe_snapshot_once(self, cid: str, timeout: float = 2.2) -> bool:
        if not cid or not self.ls_address:
            return False
        with self._lock:
            if cid in self._streams or cid in self._probing_cids:
                return False
            self._probing_cids.add(cid)

        conn: Optional[http.client.HTTPConnection] = None
        got_frame = False
        sem_held = self._init_conn_sem.acquire(timeout=2.5)
        try:
            conn, resp, hdr = self._open_stream_frame0(cid, timeout)
            if hdr is not None:
                _, got_frame = self._read_and_apply_stream_frame(
                    cid, resp, hdr, is_initial_snapshot=True, from_live_stream=False
                )
                hdr = None
        except Exception:
            pass
        finally:
            if sem_held:
                self._init_conn_sem.release()
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            with self._lock:
                self._probing_cids.discard(cid)
                if (rt := self._runtimes.get(cid)) is not None:
                    s = self._trajectories.get(cid)
                    step_cnt = int(s.get("stepCount") or 0) if isinstance(s, dict) else rt.total_steps
                    rt.snapshot_probed_steps = max(rt.snapshot_probed_steps, step_cnt, rt.total_steps)
        if got_frame and not self._is_subagent_cid(cid):
            self._persist_runtime_state(cid)
        return got_frame

    def _probe_child_usage_once(self, r_cid: str, sub_cid: str, timeout: float = 2.2) -> bool:
        """Fetch Frame 0 for a historical/killed child and roll its usage directly into root_rt.archived_children_usage."""
        if not r_cid or not sub_cid or not self.ls_address:
            return False
        with self._lock:
            if sub_cid in self._streams or sub_cid in self._probing_cids or sub_cid in self._probed_historical_subs:
                return False
            self._probing_cids.add(sub_cid)

        got_frame = False
        sub_usage_by_gm: dict[str, tuple[int, int, int, int, int]] = {}
        conn: Optional[http.client.HTTPConnection] = None
        sem_held = self._init_conn_sem.acquire(timeout=2.5)
        try:
            conn, resp, hdr = self._open_stream_frame0(sub_cid, timeout)
            if hdr is not None:
                ok_conn, upd = self._decode_stream_frame_update(resp, hdr, is_initial_snapshot=True)
                hdr = None
                if ok_conn and isinstance(upd, dict):
                    got_frame = True
                    if isinstance(main_upd := upd.get("mainTrajectoryUpdate"), dict):
                        items, _, _ = self._parse_generator_usage_items(main_upd, is_initial=True)
                        for gm_key, full_u in items:
                            sub_usage_by_gm[gm_key] = full_u
                upd = None
        except Exception:
            pass
        finally:
            if sem_held:
                self._init_conn_sem.release()
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            with self._lock:
                self._probing_cids.discard(sub_cid)
                self._probed_historical_subs.add(sub_cid)

        if got_frame and sub_usage_by_gm:
            root_rt = self._get_or_create_runtime(r_cid)
            summary_6 = _sum_usage_by_gm(sub_usage_by_gm)
            changed = False
            with self._lock:
                if root_rt.archived_children_usage.get(sub_cid) != summary_6:
                    root_rt.archived_children_usage[sub_cid] = summary_6
                    root_rt.dirty_state = True
                    self._bump_rev_locked()
                    changed = True
            if changed:
                self._persist_runtime_state(r_cid)
        return got_frame

    @staticmethod
    def _active_planner_phase_label(rt: ConversationRuntime) -> str:
        return SPEED_PHASE_LABELS.get(rt.ts_phase, "等待首字") if rt.ts_phase != "idle" else "等待首字"

    @staticmethod
    def _is_root_live_active_locked(
        rt: Optional[ConversationRuntime], summary: dict[str, Any], root_short_stat: str
    ) -> bool:
        if rt is not None and rt.stream_connected and rt.stream_initial_seen:
            live_act = (
                short_run_status(rt.raw_status) in ACTIVE_ROOT_STATUSES
                or short_run_status(rt.raw_executor_loop_status) in ACTIVE_ROOT_STATUSES
                or rt.ts_active_step_idx >= 0
            )
            if live_act:
                return True
            if root_short_stat in ACTIVE_ROOT_STATUSES:
                sum_mod = parse_iso_ts(str(summary.get("lastModifiedTime") or ""))
                if sum_mod > (rt.last_stream_frame_at + 0.5):
                    return True
            return False
        return root_short_stat in ACTIVE_ROOT_STATUSES

    @staticmethod
    def _is_subagent_live_running_locked(
        sub_rt: Optional[ConversationRuntime], summary: dict[str, Any], s_status: str
    ) -> bool:
        if bool(summary.get("killed", False)):
            return False
        if sub_rt is not None and sub_rt.stream_connected and sub_rt.stream_initial_seen:
            live_run = (
                short_run_status(sub_rt.raw_status) in ACTIVE_SUBAGENT_STATUSES
                or short_run_status(sub_rt.raw_executor_loop_status) in ACTIVE_SUBAGENT_STATUSES
                or sub_rt.ts_active_step_idx >= 0
            )
            if live_run:
                return True
            if s_status in ACTIVE_SUBAGENT_STATUSES:
                sum_mod = parse_iso_ts(str(summary.get("lastModifiedTime") or ""))
                if sum_mod > (sub_rt.last_stream_frame_at + 0.5):
                    return True
            return False
        return s_status in ACTIVE_SUBAGENT_STATUSES

    @staticmethod
    def _has_active_schedule_timer_locked(sub_rt: Optional[ConversationRuntime]) -> bool:
        """Return True ONLY when monitor runtime has an active non-cancelled schedule timer."""
        if sub_rt is None:
            return False
        bg_indices = FlowPilotEngine._collect_bg_step_indices_locked(sub_rt)
        for bg_idx in bg_indices:
            st = sub_rt.steps_by_idx.get(bg_idx)
            if isinstance(st, dict):
                if _normalize_tool_key(str(st.get("toolName") or "")) == "schedule" and st.get("status") not in ("CANCELED", "CANCELLED", "ERROR", "FAILED"):
                    return True
        for st in sub_rt.steps_by_idx.values():
            if not isinstance(st, dict):
                continue
            if _normalize_tool_key(str(st.get("toolName") or "")) == "schedule" and st.get("status") == "RUNNING":
                return True
        return False

    def _is_waiting_swarm_worker_locked(
        self,
        sub_rt: Optional[ConversationRuntime],
        summary: dict[str, Any],
        term_reason: str,
    ) -> bool:
        """Return True ONLY for current-LS-session IDLE swarm workers cleanly parked on swarm_wait."""
        sub_mod = parse_iso_ts(str(summary.get("lastModifiedTime") or summary.get("createdTime") or ""))
        if sub_mod <= 0.0 or sub_mod < (self._ls_start_ts - 2.0):
            return False
        parked_on_swarm_wait = "TERMINAL_CUSTOM_HOOK" in (term_reason or "").upper()
        if not parked_on_swarm_wait and sub_rt is not None and sub_rt.steps_by_idx:
            last_idx = max(sub_rt.steps_by_idx.keys())
            last_st = sub_rt.steps_by_idx.get(last_idx) or {}
            if _normalize_tool_key(str(last_st.get("toolName") or "")) == "swarm_wait":
                parked_on_swarm_wait = True
        return parked_on_swarm_wait

    def _extract_subagent_activity_locked(
        self, sub_rt: ConversationRuntime, sub_bg: Optional[dict[str, Any]] = None, loop_running: bool = True
    ) -> tuple[str, float]:
        if loop_running:
            fg_step = self._find_foreground_running_step_locked(sub_rt, include_planner=True)
            if isinstance(fg_step, dict):
                stype = str(fg_step.get("type") or "")
                t_name = str(fg_step.get("toolName") or "").strip()
                eff_started = float(fg_step.get("startedAt") or fg_step.get("createdAt") or 0.0)
                run_since = float(fg_step.get("runningSince") or 0.0)
                started = max(eff_started, run_since) if run_since > 0.0 else eff_started
                if stype == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
                    return self._active_planner_phase_label(sub_rt), float(started or sub_rt.ts_step_created_at or 0.0)
                if t_name:
                    return t_name, started
            if sub_rt.ts_active_step_idx >= 0:
                return self._active_planner_phase_label(sub_rt), float(sub_rt.ts_step_created_at or 0.0)
        if isinstance(sub_bg, dict):
            bg_lbl = "后台工作流" if sub_bg.get("kind") == "workflow" else f"{sub_bg.get('toolName') or 'run_command'} (后台)"
            return bg_lbl, float(sub_bg.get("startedAt") or 0.0)
        return "执行中", 0.0

    def _build_task_subagents_and_swarm(
        self, root_cid: str, child_entries: list[tuple[str, dict[str, Any]]], root_is_running: bool,
        root_last_mod: float, now_eval: float, ensure_streams: bool = False, stall_sec: int = 90,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        empty_swarm = {
            "isSwarm": False, "totalCount": 0, "totalChildren": 0, "runningCount": 0,
            "waitingCount": 0, "errorCount": 0, "monitorActive": False, "liveTokensPerSec": 0.0,
            "anyWaitingTtft": False, "anyGenerating": False,
        }
        if not root_cid or not child_entries:
            return [], empty_swarm

        raw_children: list[tuple[str, dict[str, Any], str, str, str, bool, bool, bool, bool, Optional[dict[str, Any]], Optional[dict[str, Any]]]] = []
        stall_log_checks: list[tuple[str, int, str, float]] = []
        monitor_active = has_swarm_worker = False
        running_children = waiting_workers = error_children = 0
        bg_stall_threshold = max(int(stall_sec) * 2, 120) if stall_sec > 0 else 0

        with self._lock:
            for sub_cid, summary in child_entries:
                if bool(summary.get("killed", False)):
                    continue
                agent_kind = self._classify_agent_kind(sub_cid, summary)
                s_status = short_run_status(str(summary.get("status") or "CASCADE_RUN_STATUS_IDLE"))
                term_reason = str(summary.get("latestExecutorTerminationReason") or "").upper()
                sub_rt = self._runtimes.get(sub_cid)
                loop_running = self._is_subagent_live_running_locked(sub_rt, summary, s_status)
                sub_fg = (
                    self._find_foreground_running_step_locked(sub_rt, include_planner=False)
                    if (sub_rt is not None and loop_running)
                    else None
                )
                sub_fg_idx = int(sub_fg["idx"]) if isinstance(sub_fg, dict) else -1
                sub_bg = (
                    self._find_active_bg_script_locked(sub_rt, exclude_step_idx=sub_fg_idx)
                    if sub_rt is not None
                    else None
                )
                is_running = bool(loop_running or sub_bg is not None)
                is_abnormal = False if is_running else self._is_subagent_abnormal(summary, s_status, term_reason, now_eval)

                if agent_kind == "swarm_monitor":
                    if is_running or self._has_active_schedule_timer_locked(sub_rt):
                        monitor_active = True
                    if not is_running and not is_abnormal:
                        continue
                    if is_running:
                        running_children += 1
                    elif is_abnormal:
                        error_children += 1
                    if stall_sec > 0 and not is_abnormal and is_running and isinstance(sub_fg, dict):
                        fg_tool = str(sub_fg.get("toolName") or "").strip()
                        fg_type = str(sub_fg.get("type") or "")
                        if fg_tool and not sub_fg.get("isDaemon") and not _is_intentional_wait_step(fg_tool, fg_type):
                            eff_st = float(sub_fg.get("startedAt") or sub_fg.get("createdAt") or 0.0)
                            run_s = float(sub_fg.get("runningSince") or 0.0)
                            fg_start = max(eff_st, run_s) if run_s > 0.0 else eff_st
                            if fg_start > 0.0 and (now_eval - fg_start) >= stall_sec:
                                fg_log_uri = str(sub_fg.get("logUri") or "").strip()
                                if _normalize_tool_key(fg_tool) == "run_command" or _normalize_tool_key(fg_type) == "run_command" or fg_log_uri:
                                    stall_log_checks.append((sub_cid, sub_fg_idx, fg_log_uri, 30.0))
                    raw_children.append((sub_cid, summary, agent_kind, s_status, term_reason, is_running, loop_running, is_abnormal, False, sub_fg, sub_bg))
                    continue

                if stall_sec > 0 and not is_abnormal and is_running:
                    if isinstance(sub_fg, dict):
                        fg_tool = str(sub_fg.get("toolName") or "").strip()
                        fg_type = str(sub_fg.get("type") or "")
                        if fg_tool and not sub_fg.get("isDaemon") and not _is_intentional_wait_step(fg_tool, fg_type):
                            eff_st = float(sub_fg.get("startedAt") or sub_fg.get("createdAt") or 0.0)
                            run_s = float(sub_fg.get("runningSince") or 0.0)
                            fg_start = max(eff_st, run_s) if run_s > 0.0 else eff_st
                            if fg_start > 0.0 and (now_eval - fg_start) >= stall_sec:
                                fg_log_uri = str(sub_fg.get("logUri") or "").strip()
                                if _normalize_tool_key(fg_tool) == "run_command" or _normalize_tool_key(fg_type) == "run_command" or fg_log_uri:
                                    stall_log_checks.append((sub_cid, sub_fg_idx, fg_log_uri, 30.0))
                    elif (not loop_running) and isinstance(sub_bg, dict) and sub_bg.get("kind") == "command" and bg_stall_threshold > 0:
                        bg_start = float(sub_bg.get("startedAt") or 0.0)
                        if bg_start > 0.0 and (now_eval - bg_start) >= bg_stall_threshold:
                            stall_log_checks.append((sub_cid, int(sub_bg.get("stepIndex", -1)), str(sub_bg.get("logUri") or ""), 60.0))

                is_waiting_worker = (
                    not is_running
                    and not is_abnormal
                    and agent_kind == "swarm_worker"
                    and self._is_waiting_swarm_worker_locked(sub_rt, summary, term_reason)
                )
                if agent_kind == "swarm_worker" and (is_running or is_abnormal or is_waiting_worker):
                    has_swarm_worker = True
                if is_running:
                    running_children += 1
                elif is_abnormal:
                    error_children += 1
                elif is_waiting_worker:
                    waiting_workers += 1
                raw_children.append((sub_cid, summary, agent_kind, s_status, term_reason, is_running, loop_running, is_abnormal, is_waiting_worker, sub_fg, sub_bg))

        log_fresh_map: dict[tuple[str, int], bool] = {}
        for s_cid_c, s_idx_c, s_uri_c, fresh_w in stall_log_checks:
            if s_idx_c >= 0:
                log_fresh_map[(s_cid_c, s_idx_c)] = self._is_step_log_recently_modified_cached(
                    s_cid_c, s_idx_c, s_uri_c, now_eval, fresh_sec=fresh_w
                )

        include_waiting = bool(
            root_is_running or running_children > 0 or error_children > 0 or (root_last_mod > 0 and (now_eval - root_last_mod) < 3600.0)
        )
        if ensure_streams or running_children > 0:
            for sub_cid, _s, _ak, _st, _tr, is_running, _lr, _abn, _wait, _fg, _bg in raw_children:
                if is_running:
                    self._ensure_stream(sub_cid, wait_sec=0.06)

        active_list: list[dict[str, Any]] = []
        swarm_live_tps = 0.0
        any_waiting_ttft = False
        any_generating = False
        root_rt = self._get_or_create_runtime(root_cid)

        with self._lock:
            for sub_cid, summary, agent_kind, s_status, term_reason, is_running, loop_running, is_abnormal, is_waiting, sub_fg, sub_bg in raw_children:
                if not is_running and not is_abnormal and not (include_waiting and is_waiting):
                    continue

                meta = summary.get("trajectoryMetadata") if isinstance(summary.get("trajectoryMetadata"), dict) else {}
                spec = meta.get("subagentSpec") if isinstance(meta.get("subagentSpec"), dict) else {}
                script = meta.get("agentScript") if isinstance(meta.get("agentScript"), dict) else {}
                ann = summary.get("annotations") if isinstance(summary.get("annotations"), dict) else {}
                name = extract_subagent_name(script.get("name"), spec.get("role"), spec.get("name"), spec.get("typeName"), ann.get("title"), fallback_cid=sub_cid)

                sub_rt = self._get_or_create_runtime(sub_cid)
                spec_model = str(spec.get("model") or meta.get("model") or "").strip()
                if spec_model and not sub_rt.model_id:
                    self._set_runtime_model_id_locked(sub_rt, spec_model)
                    sub_rt.model_label = self._resolve_model_label(sub_rt.model_id, sub_rt.model_name, sub_rt.model_display_name)
                    if not sub_rt.max_tokens_exact:
                        sub_rt.max_tokens = self._resolve_model_max_tokens_locked(sub_rt.model_id, sub_rt.model_name, sub_rt.model_label)
                elif _needs_model_label_refresh(sub_rt.model_label):
                    sub_rt.model_label = self._resolve_model_label(sub_rt.model_id or spec_model, sub_rt.model_name, sub_rt.model_display_name)

                sub_is_stalled = False
                sub_bg_stalled = False
                stall_tool_label = ""
                stall_elapsed = 0.0
                if stall_sec > 0 and not is_abnormal and is_running:
                    if isinstance(sub_fg, dict):
                        fg_tool = str(sub_fg.get("toolName") or "").strip()
                        fg_type = str(sub_fg.get("type") or "")
                        fg_idx = int(sub_fg.get("idx", -1))
                        if fg_tool and not sub_fg.get("isDaemon") and not _is_intentional_wait_step(fg_tool, fg_type):
                            eff_st = float(sub_fg.get("startedAt") or sub_fg.get("createdAt") or 0.0)
                            run_s = float(sub_fg.get("runningSince") or 0.0)
                            fg_start = max(eff_st, run_s) if run_s > 0.0 else eff_st
                            if fg_start > 0.0 and (now_eval - fg_start) >= stall_sec:
                                if not log_fresh_map.get((sub_cid, fg_idx), False):
                                    sub_is_stalled = True
                                    stall_tool_label = fg_tool
                                    stall_elapsed = max(0.0, round(now_eval - fg_start, 1))
                    elif (not loop_running) and isinstance(sub_bg, dict) and sub_bg.get("kind") == "command" and bg_stall_threshold > 0:
                        bg_start = float(sub_bg.get("startedAt") or 0.0)
                        bg_idx = int(sub_bg.get("stepIndex", -1))
                        if bg_start > 0.0 and (now_eval - bg_start) >= bg_stall_threshold:
                            if not log_fresh_map.get((sub_cid, bg_idx), False):
                                sub_is_stalled = True
                                sub_bg_stalled = True
                                stall_tool_label = f"{sub_bg.get('toolName') or 'run_command'} (后台)"
                                stall_elapsed = max(0.0, round(now_eval - bg_start, 1))

                eff_abnormal = bool(is_abnormal or sub_is_stalled)
                err_clean = (
                    (f"{stall_tool_label} 超时挂起" if stall_tool_label else "执行超时挂起")
                    if sub_is_stalled
                    else (term_reason.replace("EXECUTOR_TERMINATION_REASON_", "") if is_abnormal else "")
                )
                sub_ts = self._compute_token_speed_locked(sub_rt, now_eval)
                step_started_at = 0.0
                if is_running:
                    if loop_running and sub_ts.get("active") and sub_ts.get("phase") == "waiting_ttft":
                        current_tool = str(sub_ts.get("phaseLabel") or "等待首字")
                        wait_el = float(sub_ts.get("elapsedGenSec") or 0.0)
                        step_started_at = max(0.0, now_eval - wait_el) if wait_el > 0.0 else now_eval
                    else:
                        current_tool, step_started_at = self._extract_subagent_activity_locked(sub_rt, sub_bg=sub_bg, loop_running=loop_running)
                elif is_abnormal:
                    current_tool = err_clean or s_status or "异常"
                else:
                    current_tool = ""

                created_ts = parse_iso_ts(str(summary.get("createdTime") or meta.get("createdAt") or ""))
                mod_ts = parse_iso_ts(str(summary.get("lastModifiedTime") or ""))
                if sub_is_stalled and stall_elapsed > 0.0:
                    elapsed = stall_elapsed
                elif is_running and step_started_at > 0 and (now_eval - step_started_at) >= -1.0:
                    elapsed = max(0.0, round(now_eval - step_started_at, 1))
                else:
                    ref_ts = mod_ts or created_ts
                    elapsed = max(0.0, round(now_eval - ref_ts, 1)) if ref_ts > 0 else 0.0

                sub_max = (
                    sub_rt.max_tokens
                    if (sub_rt.max_tokens_exact and sub_rt.max_tokens > 0)
                    else self._resolve_model_max_tokens_locked(sub_rt.model_id or spec_model, sub_rt.model_name, sub_rt.model_label)
                )
                sub_used = max(0, sub_rt.tokens_used)
                sub_pct = round((sub_used / sub_max) * 100.0, 1) if sub_max > 0 else 0.0
                if is_running and not eff_abnormal:
                    sub_live = _live_tps(sub_ts)
                    swarm_live_tps += sub_live
                    if sub_ts.get("active"):
                        if sub_ts.get("phase") == "waiting_ttft":
                            any_waiting_ttft = True
                        elif sub_live > 0.0:
                            any_generating = True

                if sub_rt.usage_by_gm:
                    sub_in = sum(u[0] for u in sub_rt.usage_by_gm.values())
                    sub_cache = sum(u[1] for u in sub_rt.usage_by_gm.values())
                    sub_out = sum(u[2] for u in sub_rt.usage_by_gm.values())
                elif (arch_u := root_rt.archived_children_usage.get(sub_cid)) is not None:
                    sub_in, sub_cache, sub_out = arch_u[0], arch_u[1], arch_u[2]
                else:
                    sub_in = sub_cache = sub_out = 0
                sub_token_usage = _build_subagent_token_usage(
                    sub_in,
                    sub_cache,
                    sub_out,
                    last_turn=sub_rt.last_turn_usage,
                    cache_miss_streak=sub_rt.cache_miss_streak,
                )

                state_str = "error" if eff_abnormal else ("running" if is_running else "waiting")
                fg_stall_step_idx = int(sub_fg["idx"]) if (sub_is_stalled and isinstance(sub_fg, dict) and sub_fg.get("idx") is not None) else -1
                sub_item: dict[str, Any] = {
                    "conversationId": sub_cid,
                    "parentConversationId": str(meta.get("parentConversationId") or root_cid),
                    "rootConversationId": root_cid,
                    "nestingDepth": max(1, int(meta.get("nestingDepth") or 1)),
                    "agentKind": agent_kind,
                    "name": name,
                    "modelLabel": sub_rt.model_label,
                    "state": state_str,
                    "running": bool(is_running and not eff_abnormal),
                    "hasError": eff_abnormal,
                    "isStalled": sub_is_stalled,
                    "runningStepIndex": fg_stall_step_idx,
                    "currentTool": current_tool,
                    "stepCount": max(int(summary.get("stepCount") or 0), sub_rt.total_steps),
                    "elapsedSec": elapsed,
                    "errorReason": err_clean,
                    "context": {"used": sub_used, "max": sub_max, "percent": sub_pct},
                    "tokenSpeed": sub_ts,
                    "tokenUsage": sub_token_usage,
                }
                if isinstance(sub_bg, dict):
                    sub_bg_out = dict(sub_bg)
                    sub_bg_out["isStalled"] = sub_bg_stalled
                    sub_item["bgScript"] = sub_bg_out
                active_list.append(sub_item)

        state_rank = {"error": 0, "running": 1, "waiting": 2}
        active_list.sort(key=lambda x: (state_rank.get(x["state"], 3), -x["stepCount"], x["name"]))
        is_swarm = bool(monitor_active or has_swarm_worker or any(item["agentKind"] in ("swarm_worker", "swarm_monitor") for item in active_list))
        return active_list, {
            "isSwarm": is_swarm,
            "totalCount": len(active_list),
            "totalChildren": len(active_list),
            "runningCount": sum(1 for item in active_list if item["state"] == "running"),
            "waitingCount": sum(1 for item in active_list if item["state"] == "waiting"),
            "errorCount": sum(1 for item in active_list if item["state"] == "error"),
            "monitorActive": monitor_active,
            "liveTokensPerSec": round(swarm_live_tps, 1),
            "anyWaitingTtft": any_waiting_ttft,
            "anyGenerating": bool(any_generating and swarm_live_tps > 0.0),
        }

    # ------------------------------------------------------------------
    # Connect Streaming: StreamAgentStateUpdates
    # ------------------------------------------------------------------

    def _get_or_create_runtime(self, cid: str) -> ConversationRuntime:
        with self._lock:
            if (rt := self._runtimes.get(cid)) is not None:
                return rt
            rt = ConversationRuntime(cid)
            summary = self._trajectories.get(cid)
            is_sub = False
            if isinstance(summary, dict):
                is_sub = self._is_subagent_summary(cid, summary)
                rt.title = self._extract_title(cid, summary)
                rt.raw_status = str(summary.get("status") or "CASCADE_RUN_STATUS_IDLE")
                rt.total_steps = int(summary.get("stepCount") or 0)
                rt.workspace_name, rt.workspace_path, rt.branch_name = _extract_workspace_info(summary)
                rt.last_modified_at = parse_iso_ts(str(summary.get("lastModifiedTime") or ""))
                rt.sort_ts = self._summary_sort_ts(summary)
                meta_init = summary.get("trajectoryMetadata") if isinstance(summary.get("trajectoryMetadata"), dict) else {}
                spec_init = meta_init.get("subagentSpec") if isinstance(meta_init.get("subagentSpec"), dict) else {}
                if (init_model := str(spec_init.get("model") or meta_init.get("model") or "").strip()) and not rt.model_id:
                    self._set_runtime_model_id_locked(rt, init_model)
                    rt.max_tokens = self._resolve_model_max_tokens_locked(init_model)
            self._runtimes[cid] = rt

        if not is_sub:
            self._hydrate_runtime_from_disk_state(rt)
            with self._lock:
                self._seed_runtime_cal_from_model_locked(rt)
        return rt

    def _ensure_stream(self, cid: str, wait_sec: float = 0.0) -> Optional[_StreamWorkerHandle]:
        if not cid:
            return None
        newly_started = False
        evicted: list[_StreamWorkerHandle] = []
        with self._lock:
            rt = self._get_or_create_runtime(cid)
            rt.last_requested_at = time.time()
            handle = self._streams.get(cid)
            if handle is not None and handle.thread is not None and handle.thread.is_alive() and not handle.stop_event.is_set():
                ev = handle.first_frame_event
            else:
                evicted = self._prune_excess_streams_locked(exclude_cid=cid)
                handle = _StreamWorkerHandle(cid)
                t = threading.Thread(target=self._stream_worker, args=(cid, handle), name=f"pilot-stream-{cid[:8]}", daemon=True)
                handle.thread = t
                self._streams[cid] = handle
                ev = handle.first_frame_event
                newly_started = True
                t.start()
        for eh in evicted:
            eh.stop()
        if newly_started and wait_sec > 0.0 and not ev.is_set():
            ev.wait(timeout=wait_sec)
        return handle

    def _prune_excess_streams_locked(self, exclude_cid: str = "") -> list[_StreamWorkerHandle]:
        alive = {k: h for k, h in self._streams.items() if h.thread is not None and h.thread.is_alive() and not h.stop_event.is_set()}
        self._streams = alive
        evicted: list[_StreamWorkerHandle] = []
        if len(alive) < self.MAX_ACTIVE_STREAMS:
            return evicted
        candidates: list[tuple[int, int, float, str]] = []
        for k in alive:
            if k in (exclude_cid, self._active_main_cid):
                continue
            rt, s = self._runtimes.get(k), self._trajectories.get(k)
            is_sub = 1 if (isinstance(s, dict) and self._is_subagent_summary(k, s)) else 0
            if is_sub and isinstance(s, dict):
                s_st = short_run_status(str(s.get("status") or ""))
                is_running_now = self._is_subagent_live_running_locked(rt, s, s_st)
            else:
                s_stat = short_run_status(str(s.get("status") or "")) if isinstance(s, dict) else ""
                is_running_now = bool((rt and rt.composite_running) or s_stat in ACTIVE_ROOT_STATUSES)
            candidates.append((0 if is_running_now else -1, -is_sub, rt.last_requested_at if rt else 0.0, k))
        candidates.sort(key=lambda x: (x[0], x[1], x[2]))
        while len(self._streams) >= self.MAX_ACTIVE_STREAMS and candidates:
            _, _, _, victim = candidates.pop(0)
            if (vh := self._streams.pop(victim, None)) is not None:
                vh.stop_event.set()
                evicted.append(vh)
        return evicted

    def _parse_host_port(self) -> tuple[str, int]:
        if ":" in self.ls_address:
            host, port_s = self.ls_address.rsplit(":", 1)
            return host.strip("[]"), int(port_s)
        return self.ls_address.strip("[]"), 80

    @staticmethod
    def _extract_top_children_from_groups(
        raw_groups: Any, limit: int = 6, fallback_non_chat_groups: bool = False
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_groups, list):
            return []
        child_items: list[tuple[int, str, str]] = []
        fallback_items: list[tuple[int, str, str]] = []
        for g in raw_groups:
            if not isinstance(g, dict):
                continue
            g_name = str(g.get("name") or "").strip()
            g_type = str(g.get("type") or "").upper()
            g_label = g_name or g_type.replace("TOKEN_GROUP_TYPE_", "").replace("_", " ").strip().title() or "Other"
            if isinstance(raw_children := g.get("children"), list):
                for ch in raw_children:
                    if not isinstance(ch, dict):
                        continue
                    ch_name = str(ch.get("name") or "").strip()
                    ch_num = max(0, _to_int_safe(ch.get("numTokens")))
                    if ch_name and ch_num > 0:
                        child_items.append((ch_num, g_label[:32], ch_name[:64]))
            if fallback_non_chat_groups:
                g_num = max(0, _to_int_safe(g.get("numTokens")))
                if g_num > 0 and "CHAT" not in g_type and g_name.lower() != "chat messages":
                    fallback_items.append((g_num, g_label[:32], g_label[:64]))
        if child_items:
            child_items.sort(key=lambda x: (-x[0], x[2], x[1]))
            return [{"group": g_lbl, "name": c_name, "tokens": c_num} for c_num, g_lbl, c_name in child_items[:limit]]
        if fallback_non_chat_groups and fallback_items:
            fallback_items.sort(key=lambda x: (-x[0], x[2], x[1]))
            return [{"group": g_lbl, "name": c_name, "tokens": c_num} for c_num, g_lbl, c_name in fallback_items[:limit]]
        return []

    @staticmethod
    def _get_gm_token_breakdown_groups(gm_obj: dict[str, Any]) -> Optional[list[Any]]:
        cm = gm_obj.get("chatModel") if isinstance(gm_obj.get("chatModel"), dict) else gm_obj
        for holder in (cm, gm_obj):
            if not isinstance(holder, dict):
                continue
            csm = holder.get("chatStartMetadata")
            if not isinstance(csm, dict):
                continue
            cwm = csm.get("contextWindowMetadata")
            if not isinstance(cwm, dict):
                continue
            tb = cwm.get("tokenBreakdown")
            if isinstance(tb, dict) and isinstance(grps := tb.get("groups"), list) and grps:
                return grps
        return None

    @staticmethod
    def _slim_gm_item(g: dict[str, Any], keep_token_breakdown: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for gk in ("stepIndex", "stepIndices", "executionId", "messageId", "responseId", "timeToFirstToken", "streamingDuration", "usage"):
            if gk in g:
                out[gk] = g[gk]
        if isinstance(g_ris := g.get("retryInfos"), list):
            out["retryInfos"] = [
                {rk: r[rk] for rk in ("responseId", "messageId", "usage", "error", "errorMessage", "statusCode", "status", "retryDelay", "duration") if rk in r}
                for r in g_ris if isinstance(r, dict)
            ]
            out["retryCount"] = len(g_ris)
        if isinstance(cm := g.get("chatModel"), dict):
            cm_slim: dict[str, Any] = {}
            for ck in ("model", "responseModel", "modelDisplayName", "timeToFirstToken", "streamingDuration", "usage", "messageId", "responseId"):
                if ck in cm:
                    cm_slim[ck] = cm[ck]
            if isinstance(ris := cm.get("retryInfos"), list):
                cm_slim["retryInfos"] = [
                    {rk: r[rk] for rk in ("responseId", "messageId", "usage", "error", "errorMessage", "statusCode", "status", "retryDelay", "duration") if rk in r}
                    for r in ris if isinstance(r, dict)
                ]
                cm_slim["retryCount"] = len(ris)
            if keep_token_breakdown and (tb_groups := FlowPilotEngine._get_gm_token_breakdown_groups(g)):
                slim_groups: list[dict[str, Any]] = []
                for grp in tb_groups:
                    if not isinstance(grp, dict):
                        continue
                    sg: dict[str, Any] = {}
                    for gk2 in ("name", "type", "numTokens"):
                        if gk2 in grp:
                            sg[gk2] = grp[gk2]
                    if isinstance(grp_ch := grp.get("children"), list) and grp_ch:
                        sg["children"] = [
                            {ck2: ch[ck2] for ck2 in ("name", "numTokens") if ck2 in ch}
                            for ch in grp_ch if isinstance(ch, dict)
                        ]
                    if sg:
                        slim_groups.append(sg)
                if slim_groups:
                    cm_slim["chatStartMetadata"] = {"contextWindowMetadata": {"tokenBreakdown": {"groups": slim_groups}}}
            out["chatModel"] = cm_slim
        return out

    @staticmethod
    def _distill_large_update_dict(upd: dict[str, Any], is_initial_snapshot: bool) -> dict[str, Any]:
        """Distill a large update into a compact telemetry dict so temporary buffers free cleanly."""
        slim: dict[str, Any] = {}
        for k in ("conversationId", "status", "executorLoopStatus", "fullyIdle", "executorMetadata", "contextWindowUsage"):
            if k in upd:
                slim[k] = upd[k]
        if isinstance(ci := upd.get("compactionInfo"), dict) and isinstance(ci.get("compactedAtStepIndices"), list):
            slim["compactionInfo"] = {"compactedAtStepIndices": list(ci["compactedAtStepIndices"])}
        if isinstance(bg := upd.get("backgroundTasksUpdate"), dict):
            slim_bgs: list[dict[str, Any]] = []
            slim_idx: list[int] = []
            for i, t in zip(bg.get("indices") or [], bg.get("backgroundTasks") or []):
                if isinstance(i, int) and isinstance(t, dict):
                    snap = t.get("taskSnapshot") if isinstance(t.get("taskSnapshot"), dict) else {}
                    bg_entry: dict[str, Any] = {
                        "stepIndex": t.get("stepIndex"), "idx": t.get("idx"),
                        "taskSnapshot": {sk: snap[sk] for sk in ("stepIndex", "originStepIndex", "sourceStepIndex") if sk in snap},
                    }
                    if isinstance(bg_step := t.get("step"), dict):
                        bg_meta = bg_step.get("metadata") if isinstance(bg_step.get("metadata"), dict) else {}
                        src_info = bg_meta.get("sourceTrajectoryStepInfo") if isinstance(bg_meta.get("sourceTrajectoryStepInfo"), dict) else {}
                        if isinstance(sv := src_info.get("stepIndex"), int) and not isinstance(sv, bool):
                            bg_entry["step"] = {"metadata": {"sourceTrajectoryStepInfo": {"stepIndex": sv}}}
                    slim_idx.append(i)
                    slim_bgs.append(bg_entry)
            slim["backgroundTasksUpdate"] = {"totalLength": bg.get("totalLength", 0), "indices": slim_idx, "backgroundTasks": slim_bgs}
        if isinstance(mtu := upd.get("mainTrajectoryUpdate"), dict):
            slim_mtu: dict[str, Any] = {}
            if isinstance(su := mtu.get("stepsUpdate"), dict):
                tot = int(su.get("totalLength", 0) or 0)
                idxs = su.get("indices") or []
                stps = su.get("steps") or []
                n = min(len(idxs), len(stps))
                last_pr = -1
                for p in range(n - 1, -1, -1):
                    if isinstance(stps[p], dict) and stps[p].get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
                        last_pr = p
                        break
                cutoff = 0
                if is_initial_snapshot and n > 80:
                    cutoff = min(n - 80, last_pr if last_pr >= 0 else (n - 80))
                out_i: list[int] = []
                out_s: list[dict[str, Any]] = []
                max_old_ui_idx = -1
                max_old_ui_st: Optional[dict[str, Any]] = None
                for p in range(n):
                    idx, st = idxs[p], stps[p]
                    if not isinstance(idx, int) or not isinstance(st, dict):
                        continue
                    if p < cutoff and str(st.get("status") or "") not in ("CORTEX_STEP_STATUS_RUNNING", "RUNNING"):
                        if st.get("type") == "CORTEX_STEP_TYPE_USER_INPUT" and idx >= max_old_ui_idx:
                            max_old_ui_idx = idx
                            max_old_ui_st = {"type": "CORTEX_STEP_TYPE_USER_INPUT", "status": st.get("status")}
                        continue
                    meta = st.get("metadata") if isinstance(st.get("metadata"), dict) else {}
                    tc = meta.get("toolCall") if isinstance(meta.get("toolCall"), dict) else {}
                    s_slim: dict[str, Any] = {
                        "type": st.get("type"), "status": st.get("status"),
                        "metadata": {
                            mk: meta[mk] for mk in ("createdAt", "startedAt", "completedAt", "finishedGeneratingAt", "viewableAt", "generatorModel", "modelUsage", "logUri") if mk in meta
                        },
                    }
                    if "name" in tc:
                        s_slim["metadata"]["toolCall"] = {"name": tc["name"]}
                    if _is_daemon_step_dict(st, tc):
                        s_slim["isDaemon"] = True
                    if isinstance(td_st := st.get("taskDetails"), dict) and td_st.get("logUri"):
                        s_slim["taskDetails"] = {"logUri": td_st["logUri"]}
                    if isinstance(st.get("runWorkflow"), dict):
                        s_slim["runWorkflow"] = {}
                    if isinstance(rc_st := st.get("runCommand"), dict):
                        s_slim["runCommand"] = {"logUri": rc_st["logUri"]} if rc_st.get("logUri") else {}
                    keep_pr = (not is_initial_snapshot) or (p == last_pr) or (str(st.get("status") or "") in ACTIVE_PLANNER_STEP_STATUSES)
                    if keep_pr and st.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE" and isinstance(pr := st.get("plannerResponse"), dict):
                        pr_slim: dict[str, Any] = {pk: pr[pk] for pk in ("thinking", "response", "modifiedResponse", "thinkingDuration") if pk in pr}
                        if isinstance(raw_tcs := pr.get("toolCalls"), list):
                            pr_slim["toolCalls"] = [
                                {tk: tci[tk] for tk in ("name", "argumentsJson", "arguments", "argsJson", "input") if tk in tci}
                                for tci in raw_tcs if isinstance(tci, dict)
                            ]
                        s_slim["plannerResponse"] = pr_slim
                    out_i.append(idx)
                    out_s.append(s_slim)
                if max_old_ui_st is not None:
                    out_i.insert(0, max_old_ui_idx)
                    out_s.insert(0, max_old_ui_st)
                slim_mtu["stepsUpdate"] = {"totalLength": tot, "indices": out_i, "steps": out_s}
            gmu = mtu.get("generatorMetadatasUpdate") if isinstance(mtu.get("generatorMetadatasUpdate"), dict) else mtu.get("generatorMetadataUpdate")
            if isinstance(gmu, dict):
                slim_gmu: dict[str, Any] = {}
                if "totalLength" in gmu:
                    slim_gmu["totalLength"] = gmu["totalLength"]
                if isinstance(gmu.get("indices"), list):
                    slim_gmu["indices"] = gmu["indices"]
                if isinstance(gms := gmu.get("generatorMetadatas"), list):
                    last_tb_i = -1
                    for gi in range(len(gms) - 1, -1, -1):
                        if isinstance(gms[gi], dict) and FlowPilotEngine._get_gm_token_breakdown_groups(gms[gi]):
                            last_tb_i = gi
                            break
                    slim_gmu["generatorMetadatas"] = [
                        FlowPilotEngine._slim_gm_item(g, keep_token_breakdown=(gi == last_tb_i)) if isinstance(g, dict) else g
                        for gi, g in enumerate(gms)
                    ]
                elif isinstance(gm_single := gmu.get("generatorMetadata"), dict):
                    slim_gmu["generatorMetadata"] = FlowPilotEngine._slim_gm_item(gm_single, keep_token_breakdown=True)
                elif isinstance(gm_list := gmu.get("generatorMetadata"), list):
                    last_tb_i = -1
                    for gi in range(len(gm_list) - 1, -1, -1):
                        if isinstance(gm_list[gi], dict) and FlowPilotEngine._get_gm_token_breakdown_groups(gm_list[gi]):
                            last_tb_i = gi
                            break
                    slim_gmu["generatorMetadata"] = [
                        FlowPilotEngine._slim_gm_item(g, keep_token_breakdown=(gi == last_tb_i)) if isinstance(g, dict) else g
                        for gi, g in enumerate(gm_list)
                    ]
                else:
                    slim_gmu = gmu
                slim_mtu["generatorMetadatasUpdate"] = slim_gmu
            elif isinstance(mtu.get("generatorMetadata"), dict):
                slim_mtu["generatorMetadata"] = FlowPilotEngine._slim_gm_item(mtu["generatorMetadata"], keep_token_breakdown=True)
            slim["mainTrajectoryUpdate"] = slim_mtu
        return slim

    @classmethod
    def _distill_large_update_bytes(cls, upd: dict[str, Any], is_initial_snapshot: bool) -> bytes:
        """Distill a multi-MB Frame-0 update into compact UTF-8 bytes (<25KB) so 100% of temporary pymalloc arenas free cleanly."""
        slim = cls._distill_large_update_dict(upd, is_initial_snapshot=is_initial_snapshot)
        return json.dumps(slim, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _reclaim_large_frame_memory(self) -> None:
        now = time.time()
        do_gc = False
        trim_above = 0
        with self._lock:
            if (now - self._last_gc_collect_at) >= 2.0:
                self._last_gc_collect_at = now
                do_gc = True
            if (now - self._last_ws_trim_at) >= 4.0:
                self._last_ws_trim_at = now
                trim_above = 38 * 1024 * 1024
        if do_gc or trim_above > 0:
            _reclaim_native_heap(trim_ws_above_bytes=trim_above, run_gc=do_gc)

    def _decode_stream_frame_update(
        self, resp: Any, hdr: bytes | bytearray, is_initial_snapshot: bool
    ) -> tuple[bool, Optional[dict[str, Any]]]:
        flags, frame_len = hdr[0], struct.unpack(">I", hdr[1:5])[0]
        if flags & 0x02:
            if 0 < frame_len <= 65536:
                _ = _read_exact(resp, frame_len)
            return False, None

        if frame_len > LARGE_FRAME_BYTES:
            needs_arena_reclaim = is_initial_snapshot or frame_len >= (4 * 1024 * 1024)
            if not needs_arena_reclaim:
                payload_buf = _read_exact(resp, frame_len)
                if payload_buf is None:
                    return False, None
                msg: Any = _loads_json_buf(payload_buf)
                payload_buf = None
                if not isinstance(msg, dict):
                    return True, None
                if "error" in msg and "update" not in msg:
                    return False, None
                upd = msg.get("update")
                slim_upd = self._distill_large_update_dict(upd, is_initial_snapshot=False) if isinstance(upd, dict) else None
                upd = msg = None
                return True, slim_upd

            slim_bytes: Optional[bytes] = None
            with self._large_frame_lock:
                payload_buf = _read_exact(resp, frame_len)
                if payload_buf is None:
                    return False, None
                msg = _loads_json_buf(payload_buf)
                payload_buf = None
                if not isinstance(msg, dict):
                    msg = None
                    self._reclaim_large_frame_memory()
                    return True, None
                if "error" in msg and "update" not in msg:
                    msg = None
                    self._reclaim_large_frame_memory()
                    return False, None
                upd = msg.get("update")
                if isinstance(upd, dict):
                    slim_bytes = self._distill_large_update_bytes(upd, is_initial_snapshot=is_initial_snapshot)
                upd = msg = None
                self._reclaim_large_frame_memory()
            if slim_bytes is not None:
                slim_upd = _loads_json_buf(slim_bytes)
                slim_bytes = None
                if isinstance(slim_upd, dict):
                    return True, slim_upd
            return True, None

        payload_buf = _read_exact(resp, frame_len) if frame_len > 0 else bytearray(b"{}")
        if payload_buf is None:
            return False, None
        msg = _loads_json_buf(payload_buf)
        payload_buf = None
        if not isinstance(msg, dict):
            return True, None
        if "error" in msg and "update" not in msg:
            return False, None
        upd = msg.get("update")
        return True, upd if isinstance(upd, dict) else None

    def _read_and_apply_stream_frame(
        self, cid: str, resp: Any, hdr: bytes | bytearray, is_initial_snapshot: bool, from_live_stream: bool
    ) -> tuple[bool, bool]:
        """Read, parse, apply, and immediately drop frame references in an isolated stack frame."""
        ok_conn, upd = self._decode_stream_frame_update(resp, hdr, is_initial_snapshot=is_initial_snapshot)
        if not ok_conn:
            return False, False
        if isinstance(upd, dict):
            self._apply_agent_state_update(cid, upd, is_initial_snapshot=is_initial_snapshot, from_live_stream=from_live_stream)
            return True, True
        return True, False

    def _stream_worker(self, cid: str, handle: _StreamWorkerHandle) -> None:
        if not self.ls_address:
            handle.first_frame_event.set()
            while not self._stop_event.is_set() and not handle.stop_event.is_set():
                handle.stop_event.wait(timeout=0.5)
            return

        backoff = 0.4
        while not self._stop_event.is_set() and not handle.stop_event.is_set():
            conn: Optional[http.client.HTTPConnection] = None
            init_sem_held = self._init_conn_sem.acquire(timeout=2.5)
            try:
                if self._stop_event.is_set() or handle.stop_event.is_set():
                    break
                host, port = self._parse_host_port()
                conn = http.client.HTTPConnection(host, port, timeout=self.STREAM_READ_TIMEOUT_SEC)
                handle.bind_conn(conn)
                body = json.dumps({"conversationId": cid}).encode("utf-8")
                envelope = b"\x00" + struct.pack(">I", len(body)) + body
                headers = self._build_ls_headers("application/connect+json")
                conn.request("POST", "/exa.language_server_pb.LanguageServerService/StreamAgentStateUpdates", body=envelope, headers=headers)
                resp = conn.getresponse()
                if resp.status != 200:
                    _ = resp.read()
                    raise RuntimeError(f"Stream HTTP {resp.status}")

                with self._lock:
                    self._get_or_create_runtime(cid).stream_connected = True
                backoff = 0.4
                is_first_conn_frame = True

                while not self._stop_event.is_set() and not handle.stop_event.is_set():
                    hdr = _read_exact(resp, 5)
                    if not hdr:
                        break
                    ok_conn, applied = self._read_and_apply_stream_frame(
                        cid, resp, hdr, is_initial_snapshot=is_first_conn_frame, from_live_stream=True
                    )
                    hdr = None
                    if init_sem_held:
                        init_sem_held = False
                        self._init_conn_sem.release()
                    if not ok_conn:
                        break
                    if applied:
                        is_first_conn_frame = False
                        handle.first_frame_event.set()
            except Exception:
                pass
            finally:
                if init_sem_held:
                    init_sem_held = False
                    self._init_conn_sem.release()
                handle.bind_conn(None)
                with self._lock:
                    if (rt := self._runtimes.get(cid)) is not None:
                        rt.stream_connected = rt.stream_initial_seen = False
                        rt.raw_executor_loop_status = ""
                        rt.ts_waiting_turn_since = 0.0
                        if rt.ts_active_step_idx >= 0:
                            if rt.ts_est_output_tokens > 0:
                                self._finalize_planner_step_locked(rt, idx=rt.ts_active_step_idx, st=None, gm_info=None, now=time.time(), is_initial=False)
                            else:
                                self._reset_active_token_speed_locked(rt)
                handle.first_frame_event.set()
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

            if self._stop_event.is_set() or handle.stop_event.is_set():
                break
            handle.stop_event.wait(timeout=backoff)
            backoff = min(4.0, backoff * 1.8)

    def _resolve_model_label(self, model_id: str, model_name: str, display_name: str = "") -> str:
        mid, mname = (model_id or "").strip(), (model_name or "").strip()
        disp = (display_name or "").strip()
        with self._lock:
            for cand in (mid, mid.upper()):
                if cand and cand in self._model_labels:
                    return self._model_labels[cand]
            default_label = self._model_labels.get(self._default_model_id) or "Auto"
        if disp and not disp.upper().startswith("MODEL_PLACEHOLDER_"):
            return disp
        with self._lock:
            for cand in (mname, mname.upper()):
                if cand and cand in self._model_labels:
                    return self._model_labels[cand]
        raw = mname if (mname and not mname.upper().startswith("MODEL_PLACEHOLDER_")) else (mid or mname)
        if not raw:
            return default_label
        if raw.upper().startswith("MODEL_PLACEHOLDER_"):
            suffix = raw.upper().replace("MODEL_PLACEHOLDER_", "").strip()
            return f"Model ({suffix})" if suffix else default_label
        if " " in raw:
            return raw.strip() or default_label
        low = raw.lower()
        explicit_tier = (
            "XHigh" if "xhigh" in low else
            ("High" if "high" in low else
             ("Low" if "low" in low else
              ("Medium" if "medium" in low else
               ("Tiered" if "tiered" in low else ""))))
        )
        if "sonnet" in low or "opus" in low:
            kind = "Sonnet" if "sonnet" in low else "Opus"
            vm = _CLAUDE_VER_RE.search(low)
            ver = vm.group(1).replace("-", ".") if vm else ""
            base = f"Claude {kind} {ver}" if ver else f"Claude {kind}"
            return f"{base} ({explicit_tier})" if explicit_tier else base
        if "flash" in low:
            fm = _GEMINI_FLASH_VER_RE.search(low)
            fver = fm.group(1).replace("-", ".") if fm else ""
            base = f"Gemini {fver} Flash" if fver else "Gemini Flash"
            return f"{base} ({explicit_tier})" if explicit_tier else base
        if "-" in raw or "_" in raw:
            return self._prettify_model_slug(raw, explicit_tier) or default_label
        return raw.strip() or default_label

    @staticmethod
    def _prettify_model_slug(raw: str, explicit_tier: str) -> str:
        tokens = [t for t in re.split(r"[-_]+", raw.strip()) if t]
        if not tokens:
            return raw.strip()
        if tokens[0].lower() == "model" and len(tokens) > 1:
            tokens = tokens[1:]
        if explicit_tier:
            while len(tokens) > 1 and tokens[-1].lower() in ("xhigh", "high", "low", "medium", "tiered"):
                tokens.pop()
        merged: list[str] = []
        i = 0
        while i < len(tokens):
            if (
                i + 1 < len(tokens)
                and tokens[i].isdigit()
                and tokens[i + 1].isdigit()
                and len(tokens[i]) <= 2
                and len(tokens[i + 1]) <= 2
            ):
                merged.append(f"{tokens[i]}.{tokens[i + 1]}")
                i += 2
            else:
                merged.append(tokens[i])
                i += 1
        acronyms = {"gpt": "GPT", "oss": "OSS", "ai": "AI", "o1": "o1", "o3": "o3", "o4": "o4"}
        parts = [
            acronyms[t.lower()] if t.lower() in acronyms else (
                t.upper() if re.fullmatch(r"\d+[bBkKmM]", t) else (t.capitalize() if t.isalpha() else t)
            )
            for t in merged
        ]
        base = " ".join(parts).strip()
        if not base:
            return raw.strip()
        return f"{base} ({explicit_tier})" if explicit_tier else base

    def _resolve_routed_model_label_locked(self, resp_model: str, req_label: str, resp_display_name: str = "") -> str:
        rm = (resp_model or "").strip()
        rdisp = (resp_display_name or "").strip()
        req = (req_label or "").strip()
        if (not rm and not rdisp) or not req or req.lower() == "auto":
            return ""
        routed = ""
        used_rdisp = False
        if rm:
            with self._lock:
                routed = self._model_labels.get(rm) or self._model_labels.get(rm.upper()) or ""
        if not routed and rdisp and not rdisp.upper().startswith("MODEL_PLACEHOLDER_"):
            routed = rdisp
            used_rdisp = True
        if not routed and rm:
            routed = self._resolve_model_label(rm, rm, "").strip()
        if not routed or routed.lower() == "auto" or routed.lower() == req.lower():
            return ""
        tiers = ("xhigh", "high", "low", "medium", "tiered")
        routed_low = routed.lower()
        rm_low = rm.lower()
        routed_has_tier = any(t in routed_low for t in tiers)
        source_has_tier = (
            any(t in rm_low for t in tiers)
            or (rm.upper().startswith("MODEL_PLACEHOLDER_") and routed_has_tier)
            or (used_rdisp and routed_has_tier)
        )
        if not (routed_has_tier and source_has_tier):
            req_base = _PAREN_NAME_RE.sub("", req).strip().lower()
            routed_base = _PAREN_NAME_RE.sub("", routed).strip().lower()
            if req_base and req_base == routed_base:
                return ""
        return routed

    def _apply_agent_state_update(
        self, cid: str, upd: dict[str, Any], is_initial_snapshot: Optional[bool] = None,
        now_override: Optional[float] = None, from_live_stream: bool = True,
    ) -> None:
        now = float(now_override) if now_override is not None else time.time()
        should_persist = False

        with self._lock:
            rt = self._get_or_create_runtime(cid)
            was_loop_active = (
                short_run_status(rt.raw_status) == "RUNNING"
                or short_run_status(rt.raw_executor_loop_status) == "RUNNING"
                or rt.ts_active_step_idx >= 0
            )
            if from_live_stream:
                rt.stream_connected = True
            rt.last_stream_frame_at = now
            is_initial = (not rt.stream_initial_seen) if is_initial_snapshot is None else bool(is_initial_snapshot)
            if from_live_stream:
                rt.stream_initial_seen = True

            if upd.get("status"):
                rt.raw_status = str(upd["status"])
            if upd.get("executorLoopStatus"):
                rt.raw_executor_loop_status = str(upd["executorLoopStatus"])

            if isinstance(ci := upd.get("compactionInfo"), dict) and isinstance(indices := ci.get("compactedAtStepIndices"), list):
                rt.compaction_count = len(indices)

            had_exec_plan_model = False
            if isinstance(exec_meta := upd.get("executorMetadata"), dict):
                override_pc = exec_meta.get("requestOverrideConfig", {}).get("plannerConfig", {}) if isinstance(exec_meta.get("requestOverrideConfig"), dict) else {}
                base_pc = exec_meta.get("cascadeConfig", {}).get("plannerConfig", {}) if isinstance(exec_meta.get("cascadeConfig"), dict) else {}
                req_ov_m = override_pc.get("requestedModel") if isinstance(override_pc.get("requestedModel"), dict) else {}
                req_bs_m = base_pc.get("requestedModel") if isinstance(base_pc.get("requestedModel"), dict) else {}
                plan_model = str(
                    override_pc.get("planModel")
                    or req_ov_m.get("model")
                    or base_pc.get("planModel")
                    or req_bs_m.get("model")
                    or ""
                ).strip()
                m_name = str(base_pc.get("modelName") or "").strip()
                if plan_model:
                    had_exec_plan_model = True
                    self._set_runtime_model_id_locked(rt, plan_model)
                if m_name:
                    rt.model_name = m_name
                    self._seed_runtime_cal_from_model_locked(rt)
                if not rt.max_tokens_exact and (plan_model or m_name):
                    rt.max_tokens = self._resolve_model_max_tokens_locked(plan_model, m_name)

            cwu_reported_max = 0
            cwu = upd.get("contextWindowUsage")
            if isinstance(cwu, dict) and (cwu.get("estimatedTokensUsed") is not None or cwu.get("maxContextTokens") is not None or "estimatedAfterCompaction" in cwu or "tokenGroups" in cwu):
                if (new_used := int(cwu.get("estimatedTokensUsed") or 0)) > 0:
                    rt.tokens_used = new_used
                if (raw_cwu_max := int(cwu.get("maxContextTokens") or 0)) > 0:
                    rt.max_tokens = raw_cwu_max
                    rt.max_tokens_exact = True
                    cwu_reported_max = raw_cwu_max
                elif rt.max_tokens <= 0:
                    rt.max_tokens = self._resolve_model_max_tokens_locked(rt.model_id, rt.model_name, rt.model_label)
                if "lastStepIndex" in cwu:
                    rt.last_step_index = int(cwu.get("lastStepIndex") or 0)
                if "estimatedAfterCompaction" in cwu:
                    rt.estimated_after_compaction = bool(cwu.get("estimatedAfterCompaction"))
                elif cwu.get("estimatedTokensUsed") is not None:
                    rt.estimated_after_compaction = False
                if isinstance(raw_groups := cwu.get("tokenGroups"), list):
                    sys_tok = ext_tok = tools_tok = chat_tok = 0
                    for g in raw_groups:
                        if not isinstance(g, dict):
                            continue
                        g_name = str(g.get("name") or "").strip()
                        g_type = str(g.get("type") or "").upper()
                        g_num = max(0, _to_int_safe(g.get("numTokens")))
                        if g_num <= 0:
                            continue
                        if "SYSTEM_PROMPT" in g_type or g_name == "System Prompt":
                            sys_tok += g_num
                        elif "TOOLS" in g_type or g_name in ("Tools", "MCP Tools"):
                            tools_tok += g_num
                        elif "CHAT" in g_type or g_name == "Chat Messages":
                            chat_tok += g_num
                        else:
                            ext_tok += g_num
                    rt.groups = {"systemPrompt": sys_tok, "extensions": ext_tok, "tools": tools_tok, "chatMessages": chat_tok}
                    cwu_children = self._extract_top_children_from_groups(raw_groups, limit=6, fallback_non_chat_groups=False)
                    if cwu_children:
                        rt.context_top_children = cwu_children
                        rt.context_top_children_from_breakdown = True
                    elif not rt.context_top_children_from_breakdown:
                        fallback_top = self._extract_top_children_from_groups(raw_groups, limit=6, fallback_non_chat_groups=True)
                        if fallback_top:
                            rt.context_top_children = fallback_top

            if "backgroundTasksUpdate" in upd:
                prev_bg_indices = self._collect_bg_step_indices_locked(rt)
                bg_upd = upd.get("backgroundTasksUpdate")
                if not isinstance(bg_upd, dict):
                    rt.bg_tasks_by_idx.clear()
                else:
                    raw_bg_indices = bg_upd.get("indices") or []
                    raw_bg_tasks = bg_upd.get("backgroundTasks") or []
                    bg_len = int(bg_upd.get("totalLength", 0) or 0)
                    if bg_len <= 0 and not raw_bg_tasks:
                        rt.bg_tasks_by_idx.clear()
                    else:
                        eff_len = bg_len if bg_len > 0 else (max((i for i in raw_bg_indices if isinstance(i, int)), default=-1) + 1)
                        for i, task in zip(raw_bg_indices, raw_bg_tasks):
                            if isinstance(i, int) and isinstance(task, dict):
                                rt.bg_tasks_by_idx[i] = _extract_bg_task_step_indices(i, task)
                        for old_bg_k in [k for k in rt.bg_tasks_by_idx if k >= eff_len]:
                            rt.bg_tasks_by_idx.pop(old_bg_k, None)
                new_bg_indices = self._collect_bg_step_indices_locked(rt)
                if (
                    not is_initial
                    and (new_bg_indices - prev_bg_indices)
                    and rt.ts_active_step_idx < 0
                    and not self._has_foreground_active_tool_locked(rt)
                ):
                    rt.ts_waiting_turn_since = now
            elif is_initial:
                rt.bg_tasks_by_idx.clear()

            active_step_updated = False
            gm_by_step: dict[int, dict[str, Any]] = {}
            latest_gm: Optional[dict[str, Any]] = None
            latest_done_planner_idx = -1
            finalized_in_loop: set[int] = set()
            gm_mid_seen = False
            observed_resp_model = ""
            observed_resp_disp = ""
            last_step_resp_model = ""
            last_step_resp_disp = ""

            if isinstance(main_upd := upd.get("mainTrajectoryUpdate"), dict):
                steps_upd = main_upd.get("stepsUpdate") if isinstance(main_upd.get("stepsUpdate"), dict) else None
                min_gm_idx = 0
                if steps_upd is not None and is_initial:
                    tot_hint = int(steps_upd.get("totalLength", 0) or 0)
                    if tot_hint > 80:
                        min_gm_idx = max(0, tot_hint - 84)
                gm_by_step, latest_gm = self._parse_generator_metadatas_update(main_upd, min_step_idx=min_gm_idx)
                usage_items, latest_u5, starts_from_zero = self._parse_generator_usage_items(main_upd, is_initial=is_initial)
                if starts_from_zero:
                    rt.usage_by_gm.pop("_folded", None)
                for gm_key, u5 in usage_items:
                    rt.usage_by_gm[gm_key] = u5
                self._update_turn_usage_and_cache_streak_locked(rt, usage_items, latest_u5)
                if latest_gm:
                    if isinstance(gm_top := latest_gm.get("topChildren"), list) and gm_top:
                        rt.context_top_children = gm_top
                        rt.context_top_children_from_breakdown = True
                    gm_mid = str(latest_gm.get("model") or "").strip()
                    gm_resp = str(latest_gm.get("responseModel") or "").strip()
                    gm_disp = str(latest_gm.get("modelDisplayName") or "").strip()
                    if gm_mid:
                        gm_mid_seen = True
                    if gm_resp:
                        observed_resp_model = gm_resp
                    if gm_disp:
                        observed_resp_disp = gm_disp
                    if gm_mid and (not is_initial or not rt.model_id):
                        self._set_runtime_model_id_locked(rt, gm_mid)
                    req_lbl_from_mid = self._resolve_model_label(rt.model_id, "", "") if rt.model_id else ""
                    is_routed_resp = bool(
                        req_lbl_from_mid
                        and not _needs_model_label_refresh(req_lbl_from_mid)
                        and (gm_resp or gm_disp)
                        and self._resolve_routed_model_label_locked(gm_resp, req_lbl_from_mid, gm_disp)
                    )
                    if gm_resp and not is_routed_resp and (not is_initial or not rt.model_name):
                        if not rt.model_id or rt.model_id.upper().startswith("MODEL_PLACEHOLDER_") or rt.model_id == gm_resp:
                            rt.model_name = gm_resp
                        self._seed_runtime_cal_from_model_locked(rt)
                    if gm_disp and not is_routed_resp and (not rt.model_id or not gm_mid or gm_mid == rt.model_id):
                        rt.model_display_name = gm_disp
                        self._seed_runtime_cal_from_model_locked(rt)

                if steps_upd is not None:
                    prev_steps_snap = dict(rt.steps_by_idx) if is_initial else None
                    if is_initial:
                        rt.steps_by_idx.clear()
                    if "totalLength" in steps_upd:
                        total_len = max(0, int(steps_upd.get("totalLength") or 0))
                        rt.total_steps = total_len
                        rt.last_step_index = max(-1, total_len - 1) if is_initial else max(rt.last_step_index, total_len - 1)
                        for stale_k in [k for k in rt.steps_by_idx if k >= total_len]:
                            rt.steps_by_idx.pop(stale_k, None)
                    elif (total_len := int(steps_upd.get("totalLength", rt.total_steps) or 0)) > 0:
                        rt.total_steps = total_len
                        rt.last_step_index = max(rt.last_step_index, total_len - 1)

                    raw_indices = steps_upd.get("indices") or []
                    raw_steps = steps_upd.get("steps") or []
                    pair_len = min(len(raw_indices), len(raw_steps))
                    full_cutoff_pos = 0
                    if is_initial and pair_len > 80:
                        last_pr_pos = -1
                        for scan_p in range(pair_len - 1, -1, -1):
                            s_cand = raw_steps[scan_p]
                            if isinstance(s_cand, dict) and s_cand.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
                                last_pr_pos = scan_p
                                break
                        full_cutoff_pos = min(pair_len - 80, last_pr_pos if last_pr_pos >= 0 else (pair_len - 80))

                    last_hist_gen_model = ""
                    for pos_i in range(pair_len):
                        idx, st = raw_indices[pos_i], raw_steps[pos_i]
                        if not isinstance(idx, int) or not isinstance(st, dict):
                            continue
                        if pos_i < full_cutoff_pos:
                            st_pre_short = short_run_status(str(st.get("status") or ""))
                            if st_pre_short != "RUNNING":
                                if st.get("type") == "CORTEX_STEP_TYPE_USER_INPUT":
                                    s_st = str(st.get("status") or "")
                                    if s_st not in ("CORTEX_STEP_STATUS_PENDING", "CORTEX_STEP_STATUS_QUEUED", "CORTEX_STEP_STATUS_WAITING"):
                                        rt.last_user_input_idx = max(rt.last_user_input_idx, idx)
                                continue

                        was_seen = idx in rt.ts_seen_step_indices
                        rt.ts_seen_step_indices.add(idx)

                        stype, sstatus = str(st.get("type") or ""), str(st.get("status") or "")
                        short_st = short_run_status(sstatus)
                        if short_st == "WAITING_FOR_USER":
                            short_st = "WAITING"
                        is_active_pr_step = (
                            stype == "CORTEX_STEP_TYPE_PLANNER_RESPONSE"
                            and (sstatus in ACTIVE_PLANNER_STEP_STATUSES or short_st in ACTIVE_PLANNER_STEP_STATUSES)
                        )

                        meta = st.get("metadata") if isinstance(st.get("metadata"), dict) else {}
                        gen_model = str(meta.get("generatorModel") or "").strip()
                        step_mu = meta.get("modelUsage") if isinstance(meta.get("modelUsage"), dict) else {}
                        if not gen_model and step_mu:
                            gen_model = str(step_mu.get("model") or "").strip()
                        step_resp_m = str(step_mu.get("responseModel") or "").strip()
                        if not step_resp_m and (step_gm_r := gm_by_step.get(idx)):
                            step_resp_m = str(step_gm_r.get("responseModel") or "").strip()
                        if step_resp_m:
                            last_step_resp_model = step_resp_m
                        if gen_model:
                            if (not is_initial) or is_active_pr_step:
                                self._set_runtime_model_id_locked(rt, gen_model)
                            else:
                                last_hist_gen_model = gen_model
                        if (step_gm := gm_by_step.get(idx)) and step_gm.get("modelDisplayName"):
                            s_gm_disp = str(step_gm["modelDisplayName"]).strip()
                            s_gm_resp = str(step_gm.get("responseModel") or "").strip()
                            if s_gm_disp:
                                last_step_resp_disp = s_gm_disp
                            s_req_lbl = self._resolve_model_label(rt.model_id, "", "") if rt.model_id else ""
                            s_is_routed = bool(
                                s_req_lbl
                                and not _needs_model_label_refresh(s_req_lbl)
                                and self._resolve_routed_model_label_locked(s_gm_resp, s_req_lbl, s_gm_disp)
                            )
                            if not s_is_routed and (not rt.model_id or not step_gm.get("model") or step_gm["model"] == rt.model_id):
                                rt.model_display_name = s_gm_disp
                                self._seed_runtime_cal_from_model_locked(rt)

                        tc = meta.get("toolCall") if isinstance(meta.get("toolCall"), dict) else {}
                        tool_name = str(tc.get("name") or "").strip()
                        if not tool_name:
                            if isinstance(st.get("runWorkflow"), dict):
                                tool_name = "run_workflow"
                            elif stype == "CORTEX_STEP_TYPE_RUN_COMMAND" or isinstance(st.get("runCommand"), dict):
                                tool_name = "run_command"
                            elif stype == "CORTEX_STEP_TYPE_WAIT":
                                tool_name = "wait"
                        is_daemon_step = bool(st.get("isDaemon")) or _is_daemon_step_dict(st, tc)

                        if stype == "CORTEX_STEP_TYPE_USER_INPUT":
                            if sstatus not in ("CORTEX_STEP_STATUS_PENDING", "CORTEX_STEP_STATUS_QUEUED", "CORTEX_STEP_STATUS_WAITING"):
                                if idx > rt.last_user_input_idx:
                                    rt.last_user_input_idx = idx
                                    rt.has_error_step_hint = False
                                if not is_initial:
                                    rt.ts_waiting_turn_since = now
                        elif stype != "CORTEX_STEP_TYPE_PLANNER_RESPONSE" and short_st == "DONE" and not is_initial:
                            rt.ts_waiting_turn_since = now

                        if ("ERROR" in stype or short_st in ("ERROR", "FAILED")) and idx >= rt.last_user_input_idx:
                            rt.has_error_step_hint = True

                        created_ts = parse_iso_ts(str(meta.get("createdAt") or ""))
                        started_raw_ts = parse_iso_ts(str(meta.get("startedAt") or ""))
                        completed_ts = parse_iso_ts(str(meta.get("completedAt") or meta.get("finishedGeneratingAt") or ""))
                        eff_started_ts = (created_ts or started_raw_ts) if stype == "CORTEX_STEP_TYPE_PLANNER_RESPONSE" else (started_raw_ts or created_ts)
                        prev_step = prev_steps_snap.get(idx) if prev_steps_snap is not None else rt.steps_by_idx.get(idx)
                        prev_status = str(prev_step.get("status") or "") if isinstance(prev_step, dict) else ""
                        prev_running_since = float(prev_step.get("runningSince") or 0.0) if isinstance(prev_step, dict) else 0.0
                        prev_viewable_ts = float(prev_step.get("viewableAt") or 0.0) if isinstance(prev_step, dict) else 0.0
                        viewable_ts = parse_iso_ts(str(meta.get("viewableAt") or "")) or prev_viewable_ts
                        if short_st == "RUNNING":
                            if prev_status in ("WAITING", "WAITING_FOR_USER"):
                                running_since = now
                            elif prev_status == "RUNNING" and prev_running_since > 0.0:
                                running_since = prev_running_since
                            elif is_initial and eff_started_ts > 0.0:
                                running_since = eff_started_ts
                            else:
                                running_since = now
                        else:
                            running_since = 0.0
                        step_log_uri = ""
                        if isinstance(td_st := st.get("taskDetails"), dict) and td_st.get("logUri"):
                            step_log_uri = str(td_st["logUri"]).strip()
                        elif isinstance(rc_st := st.get("runCommand"), dict) and rc_st.get("logUri"):
                            step_log_uri = str(rc_st["logUri"]).strip()
                        elif meta.get("logUri"):
                            step_log_uri = str(meta["logUri"]).strip()
                        rt.steps_by_idx[idx] = {
                            "idx": idx, "type": stype, "status": short_st, "toolName": tool_name,
                            "isDaemon": is_daemon_step,
                            "createdAt": created_ts, "startedAt": eff_started_ts, "completedAt": completed_ts,
                            "viewableAt": viewable_ts,
                            "runningSince": running_since,
                            "logUri": step_log_uri,
                        }
                        if stype != "CORTEX_STEP_TYPE_PLANNER_RESPONSE" and short_st in ("RUNNING", "WAITING", "WAITING_FOR_USER", "PENDING") and not is_daemon_step:
                            rt.ts_waiting_turn_since = 0.0

                        if stype == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
                            pr = st.get("plannerResponse") if isinstance(st.get("plannerResponse"), dict) else {}
                            think_str = str(pr.get("thinking") or "")
                            resp_str = str(pr.get("modifiedResponse") or pr.get("response") or "")
                            tc_str = _extract_tool_calls_text(pr.get("toolCalls"))
                            has_thinking_done = bool(pr.get("thinkingDuration"))
                            rt.steps_by_idx[idx]["hasContent"] = bool(think_str.strip() or resp_str.strip() or tc_str.strip())

                            if sstatus in ACTIVE_PLANNER_STEP_STATUSES or short_st in ACTIVE_PLANNER_STEP_STATUSES:
                                if idx >= rt.ts_active_step_idx:
                                    if rt.ts_active_step_idx >= 0 and idx > rt.ts_active_step_idx and rt.ts_est_output_tokens > 0:
                                        self._finalize_planner_step_locked(rt, idx=rt.ts_active_step_idx, st=None, gm_info=gm_by_step.get(rt.ts_active_step_idx), now=now, is_initial=is_initial)
                                    self._update_planner_step_live_locked(rt, idx=idx, think_str=think_str, resp_str=resp_str, tc_str=tc_str, created_ts=eff_started_ts, viewable_ts=viewable_ts, now=now, is_initial=is_initial, was_seen_before=was_seen, has_thinking_done=has_thinking_done)
                                    active_step_updated = True
                            else:
                                self._finalize_planner_step_locked(rt, idx=idx, st=st, gm_info=gm_by_step.get(idx), now=now, is_initial=is_initial)
                                finalized_in_loop.add(idx)
                                latest_done_planner_idx = max(latest_done_planner_idx, idx)
                        elif rt.ts_active_step_idx >= 0 and idx > rt.ts_active_step_idx:
                            prev_active_idx = rt.ts_active_step_idx
                            if rt.ts_est_output_tokens > 0:
                                self._finalize_planner_step_locked(rt, idx=prev_active_idx, st=None, gm_info=gm_by_step.get(prev_active_idx), now=now, is_initial=is_initial)
                                finalized_in_loop.add(prev_active_idx)
                                latest_done_planner_idx = max(latest_done_planner_idx, prev_active_idx)
                            else:
                                self._reset_active_token_speed_locked(rt)

                    if is_initial and last_hist_gen_model and not had_exec_plan_model and not gm_mid_seen and not active_step_updated:
                        self._set_runtime_model_id_locked(rt, last_hist_gen_model)
                    if len(rt.steps_by_idx) > 80:
                        for old_k in sorted(rt.steps_by_idx.keys())[:-80]:
                            if rt.steps_by_idx[old_k].get("status") not in ("RUNNING", "WAITING", "WAITING_FOR_USER"):
                                rt.steps_by_idx.pop(old_k, None)
                    if len(rt.steps_by_idx) > 96:
                        for old_k in sorted(rt.steps_by_idx.keys())[:-96]:
                            rt.steps_by_idx.pop(old_k, None)
                    if len(rt.ts_seen_step_indices) > 240:
                        rt.ts_seen_step_indices = set(sorted(rt.ts_seen_step_indices)[-160:])
                    if len(rt.ts_calibrated_step_indices) > 240:
                        rt.ts_calibrated_step_indices = set(sorted(rt.ts_calibrated_step_indices)[-160:])
                    if len(rt.ts_step_raw_chars_by_idx) > 40:
                        for old_k in sorted(rt.ts_step_raw_chars_by_idx.keys())[:-30]:
                            rt.ts_step_raw_chars_by_idx.pop(old_k, None)
                            rt.ts_step_peak_by_idx.pop(old_k, None)

                if gm_by_step:
                    for g_idx, g_info in sorted(gm_by_step.items()):
                        if g_idx in finalized_in_loop or g_info.get("tokens") is None:
                            continue
                        known_st = rt.steps_by_idx.get(g_idx)
                        if known_st is not None and known_st.get("type") != "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
                            continue
                        if g_idx == rt.ts_active_step_idx and active_step_updated:
                            continue
                        if g_idx >= int((rt.ts_last_completed or {}).get("stepIndex", -1)):
                            self._finalize_planner_step_locked(rt, idx=g_idx, st=None, gm_info=g_info, now=now, is_initial=is_initial)
                elif latest_gm is not None and latest_gm.get("tokens") is not None:
                    fallback_idx = latest_done_planner_idx if latest_done_planner_idx >= 0 else int((rt.ts_last_completed or {}).get("stepIndex", -1))
                    if fallback_idx >= 0 and fallback_idx != rt.ts_active_step_idx:
                        self._finalize_planner_step_locked(rt, idx=fallback_idx, st=None, gm_info=latest_gm, now=now, is_initial=is_initial)

            explicit_stop = bool(upd.get("fullyIdle", False)) or (
                "status" in upd
                and short_run_status(str(upd["status"])) in ("IDLE", "CANCELED", "CANCELLED", "ABORTED", "ERROR", "FAILED")
                and ("executorLoopStatus" not in upd or short_run_status(str(upd["executorLoopStatus"])) not in ACTIVE_ROOT_STATUSES)
            )
            if rt.ts_active_step_idx >= 0 and explicit_stop and not active_step_updated:
                if rt.ts_est_output_tokens > 0:
                    self._finalize_planner_step_locked(rt, idx=rt.ts_active_step_idx, st=None, gm_info=gm_by_step.get(rt.ts_active_step_idx), now=now, is_initial=is_initial)
                else:
                    self._reset_active_token_speed_locked(rt)
            if explicit_stop and not active_step_updated:
                rt.raw_executor_loop_status = ""
                if bool(upd.get("fullyIdle", False)) and "status" not in upd:
                    rt.raw_status = "CASCADE_RUN_STATUS_IDLE"
                rt.ts_waiting_turn_since = 0.0

            if is_initial:
                rt.usage_hydrated = True
            if rt.usage_hydrated and rt.total_steps > 0:
                rt.snapshot_probed_steps = max(rt.snapshot_probed_steps, rt.total_steps)
            if rt.usage_by_gm:
                root_cid = self._resolve_child_root_locked(cid)
                if root_cid and root_cid not in self._removed_cids:
                    summary_6 = _sum_usage_by_gm(rt.usage_by_gm)
                    root_rt = self._get_or_create_runtime(root_cid)
                    if root_rt.archived_children_usage.get(cid) != summary_6:
                        root_rt.archived_children_usage[cid] = summary_6
                        root_rt.dirty_state = True

            rt.model_label = self._resolve_model_label(rt.model_id, rt.model_name, rt.model_display_name)
            eff_resp_model = observed_resp_model or last_step_resp_model
            eff_resp_disp = observed_resp_disp or last_step_resp_disp
            if eff_resp_model or eff_resp_disp:
                rt.routed_model_label = self._resolve_routed_model_label_locked(eff_resp_model, rt.model_label, eff_resp_disp)
            elif latest_gm is not None and gm_mid_seen:
                rt.routed_model_label = ""
            if cwu_reported_max > 0:
                self._record_model_max_tokens_locked(cwu_reported_max, rt.model_id, rt.model_name, rt.model_label)
            elif not rt.max_tokens_exact and (rt.model_id or rt.model_name or rt.model_label):
                rt.max_tokens = self._resolve_model_max_tokens_locked(rt.model_id, rt.model_name, rt.model_label)
            rt.dirty_state = True
            self._bump_rev_locked()

            # Zero periodic streaming disk writes: persist only on RUNNING -> IDLE stop or initial Frame-0 hydration.
            if not self._is_subagent_cid(cid) and from_live_stream:
                if was_loop_active and explicit_stop and not active_step_updated:
                    should_persist = True
                elif is_initial and rt.last_state_persist_at <= 0.0 and not rt.disk_hydrated and rt.tokens_used > 0:
                    should_persist = True
            should_persist_cal = self._model_cal_dirty

        if should_persist:
            self._persist_runtime_state(cid)
        if should_persist_cal:
            self._persist_model_calibrations(force=False)

    @staticmethod
    def _extract_gm_raw_items(main_upd: dict[str, Any]) -> tuple[list[Any], Optional[list[Any]]]:
        gm_upd = main_upd.get("generatorMetadatasUpdate") if isinstance(main_upd.get("generatorMetadatasUpdate"), dict) else main_upd.get("generatorMetadataUpdate")
        raw_items: list[Any] = []
        indices: Optional[list[Any]] = None
        if isinstance(gm_upd, dict):
            if isinstance(raw_idx := gm_upd.get("indices"), list):
                indices = raw_idx
            candidates = gm_upd.get("generatorMetadatas") if gm_upd.get("generatorMetadatas") is not None else gm_upd.get("generatorMetadata")
            if isinstance(candidates, list):
                raw_items.extend(candidates)
            elif isinstance(candidates, dict):
                raw_items.append(candidates)
            elif "chatModel" in gm_upd or "usage" in gm_upd:
                raw_items.append(gm_upd)
        elif isinstance(main_upd.get("generatorMetadata"), dict):
            raw_items.append(main_upd["generatorMetadata"])
        return raw_items, indices

    @staticmethod
    def _gm_key_rank(gm_key: str) -> tuple[int, int]:
        if gm_key.startswith("idx:"):
            try:
                return (2, int(gm_key[4:]))
            except ValueError:
                pass
        elif gm_key.startswith("st:"):
            try:
                return (1, int(gm_key[3:]))
            except ValueError:
                pass
        return (0, -1)

    @classmethod
    def _update_turn_usage_and_cache_streak_locked(
        cls,
        rt: ConversationRuntime,
        usage_items: list[tuple[str, tuple[int, int, int, int, int]]],
        fallback_latest: Optional[tuple[int, int, int, int, int]],
    ) -> None:
        if not usage_items:
            if fallback_latest is not None and not rt.last_cache_gm_key:
                rt.last_turn_usage = fallback_latest
            return
        ordered = [
            item for _, item in sorted(
                enumerate(usage_items),
                key=lambda pair: (cls._gm_key_rank(pair[1][0]), pair[0]),
            )
        ]
        for gm_key, u5 in ordered:
            prev_key = rt.last_cache_gm_key
            cur_rank = cls._gm_key_rank(gm_key)
            prev_rank = cls._gm_key_rank(prev_key) if prev_key else (-1, -1)
            if not prev_key:
                is_newer = True
                is_same_or_newer = True
            elif gm_key == prev_key:
                is_newer = False
                is_same_or_newer = True
            elif cur_rank[0] > 0 and prev_rank[0] == cur_rank[0]:
                is_newer = cur_rank[1] > prev_rank[1]
                is_same_or_newer = cur_rank[1] >= prev_rank[1]
            else:
                is_newer = True
                is_same_or_newer = True

            if is_same_or_newer:
                rt.last_turn_usage = u5
            if is_newer:
                rt.last_cache_gm_key = gm_key
                denom = int(u5[0]) + int(u5[1])
                if denom >= 40_000:
                    hit_pct = (float(u5[1]) / float(denom)) * 100.0
                    if hit_pct < 10.0:
                        rt.cache_miss_streak += 1
                    elif hit_pct >= 20.0:
                        rt.cache_miss_streak = 0

    @classmethod
    def _parse_generator_usage_items(
        cls, main_upd: dict[str, Any], is_initial: bool
    ) -> tuple[list[tuple[str, tuple[int, int, int, int, int]]], Optional[tuple[int, int, int, int, int]], bool]:
        """Parse ALL generatorMetadatasUpdate items for cumulative & last-turn tokenUsage."""
        raw_items, indices = cls._extract_gm_raw_items(main_upd)
        parsed: list[tuple[str, tuple[int, int, int, int, int]]] = []
        latest_usage: Optional[tuple[int, int, int, int, int]] = None
        best_order: Optional[tuple[int, int, int, int]] = None
        for pos_i, gm in enumerate(raw_items):
            if not isinstance(gm, dict):
                continue
            chat_m = gm.get("chatModel") if isinstance(gm.get("chatModel"), dict) else gm
            full_u = _extract_full_usage(chat_m)
            if full_u is None and chat_m is not gm:
                full_u = _extract_full_usage(gm)
            if full_u is None:
                continue
            u = chat_m.get("usage") if isinstance(chat_m.get("usage"), dict) else (gm.get("usage") if isinstance(gm.get("usage"), dict) else {})
            max_step_idx = -1
            if isinstance(step_idxs_all := gm.get("stepIndices"), list):
                int_steps = [s for s in step_idxs_all if isinstance(s, int) and not isinstance(s, bool)]
                if int_steps:
                    max_step_idx = max(int_steps)
            elif isinstance(gm.get("stepIndex"), int) and not isinstance(gm.get("stepIndex"), bool):
                max_step_idx = int(gm["stepIndex"])

            if indices is not None and pos_i < len(indices) and isinstance(indices[pos_i], int) and not isinstance(indices[pos_i], bool):
                gm_idx = int(indices[pos_i])
                gm_key = f"idx:{gm_idx}"
                order_key = (2, gm_idx, max_step_idx, pos_i)
            elif indices is None and is_initial:
                gm_key = f"idx:{pos_i}"
                order_key = (2, pos_i, max_step_idx, pos_i)
            elif max_step_idx >= 0:
                step_key = int(gm["stepIndices"][0]) if (isinstance(gm.get("stepIndices"), list) and gm["stepIndices"] and isinstance(gm["stepIndices"][0], int)) else max_step_idx
                gm_key = f"st:{step_key}"
                order_key = (1, max_step_idx, -1, pos_i)
            else:
                uid = u.get("messageId") or u.get("responseId") or gm.get("executionId")
                gm_key = f"id:{uid if uid else pos_i}"
                order_key = (0, pos_i, -1, pos_i)
            parsed.append((gm_key, full_u))
            if best_order is None or order_key >= best_order:
                best_order = order_key
                latest_usage = full_u
        starts_from_zero = bool(parsed) and is_initial and (indices is None or (len(indices) > 0 and indices[0] == 0))
        return parsed, latest_usage, starts_from_zero

    @classmethod
    def _parse_generator_metadatas_update(
        cls, main_upd: dict[str, Any], min_step_idx: int = 0
    ) -> tuple[dict[int, dict[str, Any]], Optional[dict[str, Any]]]:
        by_step: dict[int, dict[str, Any]] = {}
        latest: Optional[dict[str, Any]] = None
        latest_top_children: list[dict[str, Any]] = []
        raw_items, _ = cls._extract_gm_raw_items(main_upd)

        for gm in raw_items:
            if not isinstance(gm, dict):
                continue
            chat_m = gm.get("chatModel") if isinstance(gm.get("chatModel"), dict) else gm
            usage_raw = chat_m.get("usage") if isinstance(chat_m.get("usage"), dict) else gm.get("usage")
            tokens_tuple = _extract_usage_tokens(usage_raw)
            ris = chat_m.get("retryInfos") if isinstance(chat_m.get("retryInfos"), list) else (gm.get("retryInfos") if isinstance(gm.get("retryInfos"), list) else None)
            retry_cnt = len(ris) if isinstance(ris, list) else max(0, _to_int_safe(chat_m.get("retryCount") if chat_m.get("retryCount") is not None else gm.get("retryCount")))
            tb_groups = cls._get_gm_token_breakdown_groups(gm)
            tb_top = cls._extract_top_children_from_groups(tb_groups, limit=6, fallback_non_chat_groups=False) if tb_groups else []
            if tb_top:
                latest_top_children = tb_top
            info: dict[str, Any] = {
                "tokens": tokens_tuple,
                "ttftSec": _parse_proto_duration_sec(chat_m.get("timeToFirstToken") or gm.get("timeToFirstToken")),
                "streamSec": _parse_proto_duration_sec(chat_m.get("streamingDuration") or gm.get("streamingDuration")),
                "model": str(chat_m.get("model") or (usage_raw.get("model") if isinstance(usage_raw, dict) else "") or "").strip(),
                "responseModel": str(chat_m.get("responseModel") or "").strip(),
                "modelDisplayName": str(chat_m.get("modelDisplayName") or "").strip(),
                "retryCount": retry_cnt,
            }
            if tb_top:
                info["topChildren"] = tb_top
            if isinstance(step_indices := gm.get("stepIndices"), list):
                for si in step_indices:
                    if isinstance(si, int) and si >= min_step_idx:
                        by_step[si] = info
            elif isinstance(gm.get("stepIndex"), int) and gm["stepIndex"] >= min_step_idx:
                by_step[int(gm["stepIndex"])] = info
            if tokens_tuple is not None or latest is None:
                latest = info
        if latest is not None and latest_top_children and not latest.get("topChildren"):
            latest = {**latest, "topChildren": latest_top_children}
        return by_step, latest

    def _model_cal_keys_locked(
        self, model_id: str = "", model_name: str = "", display_name: str = ""
    ) -> tuple[str, list[str]]:
        mid = (model_id or "").strip()
        mname = (model_name or "").strip()
        disp = (display_name or "").strip()
        if mid.lower() == "auto":
            mid = ""
        if mname.lower() == "auto":
            mname = ""
        if disp.lower() == "auto":
            disp = ""
        if not (mid or mname or disp):
            return "", []
        lbl = self._resolve_model_label(mid, mname, disp).strip()
        primary = ""
        if lbl and lbl.lower() != "auto" and not _needs_model_label_refresh(lbl):
            primary = lbl.upper()
        else:
            for cand in (mid, mname, disp):
                if cand:
                    primary = cand.upper()
                    break
        if not primary:
            return "", []
        keys: list[str] = [primary]
        for cand in (mid, mname, disp):
            cu = cand.upper()
            if cu and cu not in keys:
                keys.append(cu)
        return primary, keys

    def _get_model_cal_locked(self, rt: ConversationRuntime) -> Optional[dict[str, float]]:
        _, keys = self._model_cal_keys_locked(rt.model_id, rt.model_name, rt.model_display_name)
        for k in keys:
            if (entry := self._model_calibrations.get(k)) is not None:
                return entry
        return None

    def _seed_runtime_cal_from_model_locked(self, rt: ConversationRuntime) -> Optional[dict[str, float]]:
        mc = self._get_model_cal_locked(rt)
        if mc is None:
            return None
        if not rt.ts_cal_think_once and "think" in mc:
            rt.tok_cal_think = float(mc["think"])
        if not rt.ts_cal_resp_once and "resp" in mc:
            rt.tok_cal_resp = float(mc["resp"])
        if not rt.ts_calibrated_once and "overall" in mc and "think" not in mc and "resp" not in mc:
            rt.tok_calibration = float(mc["overall"])
        return mc

    def _effective_tok_calibration_locked(self, rt: ConversationRuntime) -> tuple[float, float]:
        mc = self._seed_runtime_cal_from_model_locked(rt)
        if rt.ts_cal_think_once:
            eff_think = rt.tok_cal_think
        elif mc is not None and "think" in mc:
            eff_think = float(mc["think"])
        elif rt.ts_calibrated_once and not rt.ts_cal_resp_once:
            eff_think = rt.tok_calibration
        elif mc is not None and "overall" in mc and "resp" not in mc:
            eff_think = float(mc["overall"])
        else:
            eff_think = 1.0

        if rt.ts_cal_resp_once:
            eff_resp = rt.tok_cal_resp
        elif mc is not None and "resp" in mc:
            eff_resp = float(mc["resp"])
        elif rt.ts_calibrated_once and not rt.ts_cal_think_once:
            eff_resp = rt.tok_calibration
        elif mc is not None and "overall" in mc and "think" not in mc:
            eff_resp = float(mc["overall"])
        else:
            eff_resp = 1.0
        return eff_think, eff_resp

    def _set_runtime_model_id_locked(self, rt: ConversationRuntime, new_model_id: str) -> None:
        new_mid = (new_model_id or "").strip()
        if not new_mid:
            return
        old_mid = rt.model_id
        if old_mid and old_mid != new_mid:
            old_key = self._model_cal_keys_locked(old_mid, rt.model_name, rt.model_display_name)[0]
            new_key = self._model_cal_keys_locked(new_mid, "", "")[0]
            rt.model_display_name = ""
            rt.routed_model_label = ""
            rt.max_tokens_exact = False
            if old_key and new_key and old_key != new_key:
                rt.model_name = ""
                rt.ts_calibrated_once = rt.ts_cal_think_once = rt.ts_cal_resp_once = False
                rt.tok_calibration = rt.tok_cal_think = rt.tok_cal_resp = 1.0
        rt.model_id = new_mid
        self._seed_runtime_cal_from_model_locked(rt)

    def _update_shared_model_cal_locked(
        self,
        rt: ConversationRuntime,
        overall: Optional[float],
        think: Optional[float],
        resp: Optional[float],
        gm_info: Optional[dict[str, Any]] = None,
        step_model: str = "",
        is_initial: bool = False,
    ) -> None:
        gm_mid = str(gm_info.get("model") or "").strip() if isinstance(gm_info, dict) else ""
        gm_resp = str(gm_info.get("responseModel") or "").strip() if isinstance(gm_info, dict) else ""
        gm_disp = str(gm_info.get("modelDisplayName") or "").strip() if isinstance(gm_info, dict) else ""
        explicit_mid = gm_mid or (step_model or "").strip()
        if explicit_mid and rt.model_id and explicit_mid != rt.model_id:
            m_id, m_name, m_disp = explicit_mid, gm_resp, gm_disp
        else:
            m_id = explicit_mid or rt.model_id
            m_name = gm_resp or rt.model_name
            m_disp = gm_disp or rt.model_display_name
        primary, keys = self._model_cal_keys_locked(m_id, m_name, m_disp)
        if not primary:
            return
        entry: Optional[dict[str, float]] = self._model_calibrations.get(primary)
        if entry is None:
            for k in keys[1:]:
                if (migrated := self._model_calibrations.pop(k, None)) is not None:
                    entry = dict(migrated)
                    break
        if entry is None:
            entry = {}
        changed = False
        for dim, val, lo, hi in (
            ("overall", overall, 0.55, 2.40),
            ("think", think, 0.70, 5.00),
            ("resp", resp, 0.55, 2.40),
        ):
            if val is None or not math.isfinite(val):
                continue
            if is_initial and dim in entry:
                continue
            clamped = max(lo, min(hi, float(val)))
            if dim not in entry:
                entry[dim] = round(clamped, 4)
            else:
                entry[dim] = round(max(lo, min(hi, (0.35 * clamped) + (0.65 * float(entry[dim])))), 4)
            changed = True
        if ("think" in entry or "resp" in entry) and "overall" in entry:
            entry.pop("overall", None)
            changed = True
        if not changed:
            return
        entry["updatedAt"] = round(time.time(), 1)
        self._model_calibrations[primary] = entry
        if len(self._model_calibrations) > self.MAX_MODEL_CALIBRATIONS:
            oldest = sorted(
                self._model_calibrations.items(),
                key=lambda kv: float(kv[1].get("updatedAt") or 0.0),
            )[: len(self._model_calibrations) - self.MAX_MODEL_CALIBRATIONS]
            for old_k, _ in oldest:
                self._model_calibrations.pop(old_k, None)
        self._model_cal_dirty = True

    @classmethod
    def _parse_model_cal_dict(cls, raw: Any) -> dict[str, dict[str, float]]:
        if not isinstance(raw, dict) or raw.get("v") != 1 or not isinstance(raw.get("models"), dict):
            return {}
        loaded: dict[str, dict[str, float]] = {}
        for k, v in raw["models"].items():
            if not isinstance(k, str) or not (ku := k.strip().upper()) or ku == "AUTO" or not isinstance(v, dict):
                continue
            item: dict[str, float] = {}
            for dim, lo, hi in (("overall", 0.55, 2.40), ("think", 0.70, 5.00), ("resp", 0.55, 2.40)):
                fv = _finite_float(v.get(dim))
                if fv is not None and lo <= fv <= hi:
                    item[dim] = round(fv, 4)
            if "think" in item or "resp" in item:
                item.pop("overall", None)
            if item:
                u_ts = _finite_float(v.get("updatedAt"))
                item["updatedAt"] = round(u_ts, 1) if (u_ts is not None and u_ts > 0.0) else 0.0
                loaded[ku] = item
                if len(loaded) >= cls.MAX_MODEL_CALIBRATIONS:
                    break
        return loaded

    def _load_model_calibrations(self, force: bool = False) -> None:
        st_ns = _stat_mtime_ns(str(self.model_cal_path))
        if st_ns <= 0:
            return
        with self._lock:
            if not force and self._model_cal_mtime_ns > 0 and st_ns == self._model_cal_mtime_ns:
                return
        try:
            raw = json.loads(self.model_cal_path.read_text(encoding="utf-8"))
        except Exception:
            return
        loaded = self._parse_model_cal_dict(raw)
        with self._lock:
            self._model_cal_mtime_ns = st_ns
            for k, disk_item in loaded.items():
                cur = self._model_calibrations.get(k)
                if cur is None or float(disk_item.get("updatedAt") or 0.0) > float(cur.get("updatedAt") or 0.0):
                    self._model_calibrations[k] = dict(disk_item)
                else:
                    for dim in ("think", "resp", "overall"):
                        if dim not in cur and dim in disk_item and not (dim == "overall" and ("think" in cur or "resp" in cur)):
                            cur[dim] = disk_item[dim]
            if len(self._model_calibrations) > self.MAX_MODEL_CALIBRATIONS:
                oldest = sorted(
                    self._model_calibrations.items(),
                    key=lambda kv: float(kv[1].get("updatedAt") or 0.0),
                )[: len(self._model_calibrations) - self.MAX_MODEL_CALIBRATIONS]
                for old_k, _ in oldest:
                    self._model_calibrations.pop(old_k, None)

    def _persist_model_calibrations(self, force: bool = False) -> None:
        now = time.time()
        with self._lock:
            if not self._model_cal_dirty:
                return
            if not force and (now - self._last_model_cal_persist_at) < self.MODEL_CAL_PERSIST_INTERVAL_SEC:
                return
            known_mtime = self._model_cal_mtime_ns
            mem_models = {k: dict(v) for k, v in self._model_calibrations.items()}
            self._model_cal_dirty = False
            self._last_model_cal_persist_at = now

        disk_models: dict[str, dict[str, float]] = {}
        disk_ns = _stat_mtime_ns(str(self.model_cal_path))
        if disk_ns > 0 and disk_ns != known_mtime:
            try:
                disk_models = self._parse_model_cal_dict(json.loads(self.model_cal_path.read_text(encoding="utf-8")))
            except Exception:
                disk_models = {}

        merged: dict[str, dict[str, float]] = dict(disk_models)
        for k, mem_item in mem_models.items():
            d_item = merged.get(k)
            if d_item is None or float(mem_item.get("updatedAt") or 0.0) >= float(d_item.get("updatedAt") or 0.0):
                combined = dict(d_item or {})
                combined.update(mem_item)
                if "think" in combined or "resp" in combined:
                    combined.pop("overall", None)
                merged[k] = combined
            else:
                combined = dict(mem_item)
                combined.update(d_item)
                if "think" in combined or "resp" in combined:
                    combined.pop("overall", None)
                merged[k] = combined

        if len(merged) > self.MAX_MODEL_CALIBRATIONS:
            oldest = sorted(merged.items(), key=lambda kv: float(kv[1].get("updatedAt") or 0.0))[: len(merged) - self.MAX_MODEL_CALIBRATIONS]
            for old_k, _ in oldest:
                merged.pop(old_k, None)

        payload = {"v": 1, "models": merged}
        self._write_json_atomic(self.model_cal_path, payload)
        new_ns = _stat_mtime_ns(str(self.model_cal_path))
        with self._lock:
            self._model_cal_mtime_ns = new_ns
            for k, m_item in merged.items():
                cur = self._model_calibrations.get(k)
                if cur is None or float(m_item.get("updatedAt") or 0.0) > float(cur.get("updatedAt") or 0.0):
                    self._model_calibrations[k] = dict(m_item)

    def _calibrate_token_estimator_locked(
        self,
        rt: ConversationRuntime,
        step_idx: int,
        exact_output_tokens: int,
        exact_think_tokens: int = 0,
        exact_resp_tokens: int = 0,
        gm_info: Optional[dict[str, Any]] = None,
        step_model: str = "",
        is_initial: bool = False,
    ) -> None:
        raw_pair = rt.ts_step_raw_chars_by_idx.get(step_idx)
        if raw_pair is None or exact_output_tokens <= 0 or step_idx in rt.ts_calibrated_step_indices:
            return
        rt.ts_calibrated_step_indices.add(step_idx)
        raw_think, raw_resp = float(raw_pair[0]), float(raw_pair[1])
        is_invisible_think = raw_think <= 0.0 and exact_think_tokens > 0

        gm_mid = str(gm_info.get("model") or "").strip() if isinstance(gm_info, dict) else ""
        gm_resp = str(gm_info.get("responseModel") or "").strip() if isinstance(gm_info, dict) else ""
        gm_disp = str(gm_info.get("modelDisplayName") or "").strip() if isinstance(gm_info, dict) else ""
        explicit_mid = gm_mid or (step_model or "").strip()
        step_key = self._model_cal_keys_locked(explicit_mid, gm_resp, gm_disp)[0] if (explicit_mid or gm_resp or gm_disp) else ""
        rt_key = self._model_cal_keys_locked(rt.model_id, rt.model_name, rt.model_display_name)[0]
        apply_to_rt = not (step_key and rt_key and step_key != rt_key)

        obs_overall: Optional[float] = None
        obs_think: Optional[float] = None
        obs_resp: Optional[float] = None
        if (raw_sum := raw_think + raw_resp) >= 10.0 and not is_invisible_think:
            target = max(0.55, min(2.40, float(exact_output_tokens) / raw_sum))
            if exact_think_tokens == 0:
                obs_overall = target
            if apply_to_rt:
                if not rt.ts_calibrated_once:
                    rt.tok_calibration, rt.ts_calibrated_once = round(target, 4), True
                else:
                    rt.tok_calibration = round(max(0.55, min(2.40, (0.6 * target) + (0.4 * rt.tok_calibration))), 4)
        if raw_think >= 10.0 and exact_think_tokens > 0:
            t_thk = max(0.70, min(5.00, float(exact_think_tokens) / raw_think))
            obs_think = t_thk
            if apply_to_rt:
                if not rt.ts_cal_think_once:
                    rt.tok_cal_think, rt.ts_cal_think_once = round(t_thk, 4), True
                else:
                    rt.tok_cal_think = round(max(0.70, min(5.00, (0.6 * t_thk) + (0.4 * rt.tok_cal_think))), 4)
        if raw_resp >= 10.0 and exact_resp_tokens > 0:
            t_rsp = max(0.55, min(2.40, float(exact_resp_tokens) / raw_resp))
            obs_resp = t_rsp
            if apply_to_rt:
                if not rt.ts_cal_resp_once:
                    rt.tok_cal_resp, rt.ts_cal_resp_once = round(t_rsp, 4), True
                else:
                    rt.tok_cal_resp = round(max(0.55, min(2.40, (0.6 * t_rsp) + (0.4 * rt.tok_cal_resp))), 4)
        if obs_overall is not None or obs_think is not None or obs_resp is not None:
            self._update_shared_model_cal_locked(
                rt, obs_overall, obs_think, obs_resp, gm_info=gm_info, step_model=step_model, is_initial=is_initial
            )

    @staticmethod
    def _reset_active_token_speed_locked(rt: ConversationRuntime) -> None:
        rt.ts_active_step_idx = -1
        rt.ts_step_created_at = rt.ts_first_token_at = rt.ts_first_chunk_tokens = 0.0
        rt.ts_last_growth_at = rt.ts_baseline_at = rt.ts_baseline_tokens = rt.ts_thinking_ended_at = 0.0
        rt.ts_baseline_raw = (0.0, 0.0)
        rt.ts_joined_midstream = rt.ts_window_ready = False
        rt.ts_phase = "idle"
        rt.ts_raw_think_chars = rt.ts_raw_resp_chars = rt.ts_raw_tc_chars = 0
        rt.ts_est_think_tokens = rt.ts_est_resp_tokens = rt.ts_est_output_tokens = 0
        rt.ts_cum_points = []
        rt.ts_live_tps = rt.ts_peak_tps = 0.0
        rt.ts_ttft_ms = 0

    def _update_planner_step_live_locked(
        self, rt: ConversationRuntime, idx: int, think_str: str, resp_str: str, tc_str: str,
        created_ts: float, viewable_ts: float, now: float, is_initial: bool, was_seen_before: bool,
        has_thinking_done: bool = False,
    ) -> None:
        think_len, resp_len, tc_len = len(think_str), len(resp_str), len(tc_str)
        has_content = bool(think_str.strip() or resp_str.strip() or tc_str.strip())
        raw_think = _estimate_raw_text_tokens(think_str)
        raw_resp = _estimate_raw_text_tokens(resp_str) + _estimate_raw_text_tokens(tc_str)
        rt.ts_step_raw_chars_by_idx[idx] = (raw_think, raw_resp)

        eff_cal_think, eff_cal_resp = self._effective_tok_calibration_locked(rt)
        cal_think, cal_resp = raw_think * eff_cal_think, raw_resp * eff_cal_resp
        cal_total = cal_think + cal_resp
        est_think_int, est_resp_int = int(round(cal_think)), int(round(cal_resp))
        est_out_int = est_think_int + est_resp_int
        if has_content and est_out_int <= 0:
            if think_str.strip() and not (resp_str.strip() or tc_str.strip()):
                est_think_int = 1
            else:
                est_resp_int = 1
            est_out_int = est_think_int + est_resp_int

        if idx != rt.ts_active_step_idx:
            waiting_since = rt.ts_waiting_turn_since
            self._reset_active_token_speed_locked(rt)
            rt.ts_active_step_idx = idx
            rt.ts_waiting_turn_since = 0.0
            if created_ts > 0 and -2.0 <= (now - created_ts) < self.PLANNER_CREATED_TS_MAX_AGE_SEC:
                rt.ts_step_created_at = min(now, created_ts)
            elif waiting_since > 0 and 0.0 <= (now - waiting_since) < self.PLANNER_CREATED_TS_MAX_AGE_SEC:
                rt.ts_step_created_at = waiting_since
            else:
                rt.ts_step_created_at = now
            rt.ts_last_growth_at = rt.ts_baseline_at = now

            is_midstream_join = is_initial or (
                not was_seen_before and viewable_ts > 0.0 and (now - viewable_ts) >= 1.5 and cal_total > 45.0
            )
            if has_content and is_midstream_join:
                rt.ts_joined_midstream = True
                rt.ts_baseline_at, rt.ts_baseline_tokens = now, cal_total
                rt.ts_baseline_raw = (raw_think, raw_resp)
                rt.ts_first_token_at = viewable_ts if viewable_ts > 0 else now
                rt.ts_ttft_ms = max(1, int(round((viewable_ts - created_ts) * 1000.0))) if (viewable_ts > created_ts > 0) else 0
                rt.ts_phase = "tool_calls" if tc_str.strip() else ("generating" if resp_str.strip() else "thinking")
                if has_thinking_done and resp_len == 0 and tc_len == 0:
                    rt.ts_thinking_ended_at = now
                    rt.ts_phase = "generating"
                rt.ts_raw_think_chars, rt.ts_raw_resp_chars, rt.ts_raw_tc_chars = think_len, resp_len, tc_len
                rt.ts_est_think_tokens, rt.ts_est_resp_tokens, rt.ts_est_output_tokens = est_think_int, est_resp_int, est_out_int
                rt.ts_cum_points = [(now, raw_think, raw_resp)]
                return
            if not has_content:
                rt.ts_phase = "waiting_ttft"
                return
        elif is_initial:
            if has_content:
                rt.ts_cum_points = [(now, raw_think, raw_resp)]
                if rt.ts_joined_midstream:
                    rt.ts_baseline_tokens = (rt.ts_baseline_raw[0] * eff_cal_think) + (rt.ts_baseline_raw[1] * eff_cal_resp)
                rt.ts_raw_think_chars = max(rt.ts_raw_think_chars, think_len)
                rt.ts_raw_resp_chars = max(rt.ts_raw_resp_chars, resp_len)
                rt.ts_raw_tc_chars = max(rt.ts_raw_tc_chars, tc_len)
                rt.ts_est_think_tokens, rt.ts_est_resp_tokens, rt.ts_est_output_tokens = est_think_int, est_resp_int, est_out_int
                rt.ts_last_growth_at = now
            else:
                if created_ts > 0 and rt.ts_first_token_at <= 0 and -2.0 <= (now - created_ts) < self.PLANNER_CREATED_TS_MAX_AGE_SEC:
                    rt.ts_step_created_at = min(rt.ts_step_created_at, created_ts) if rt.ts_step_created_at > 0 else min(now, created_ts)
                rt.ts_phase = "waiting_ttft"
            return

        if rt.ts_joined_midstream:
            rt.ts_baseline_tokens = (rt.ts_baseline_raw[0] * eff_cal_think) + (rt.ts_baseline_raw[1] * eff_cal_resp)

        if created_ts > 0 and rt.ts_first_token_at <= 0 and -2.0 <= (now - created_ts) < self.PLANNER_CREATED_TS_MAX_AGE_SEC:
            rt.ts_step_created_at = min(rt.ts_step_created_at, created_ts) if rt.ts_step_created_at > 0 else min(now, created_ts)
        if not has_content:
            rt.ts_phase = "waiting_ttft"
            return

        prev_had_resp_or_tc = (rt.ts_raw_resp_chars > 0) or (rt.ts_raw_tc_chars > 0)
        d_think, d_resp, d_tc = think_len - rt.ts_raw_think_chars, resp_len - rt.ts_raw_resp_chars, tc_len - rt.ts_raw_tc_chars
        grew = (d_think > 0) or (d_resp > 0) or (d_tc > 0)
        if d_tc > 0:
            rt.ts_phase = "tool_calls"
        elif d_resp > 0:
            rt.ts_phase = "generating"
        elif d_think > 0:
            rt.ts_phase = "thinking"
        elif has_thinking_done and not prev_had_resp_or_tc:
            if rt.ts_thinking_ended_at <= 0.0:
                rt.ts_thinking_ended_at = now
            rt.ts_phase = "generating"
        elif rt.ts_phase not in ("thinking", "generating", "tool_calls"):
            rt.ts_phase = "tool_calls" if tc_str.strip() else ("generating" if resp_str.strip() else "thinking")

        if has_thinking_done and rt.ts_thinking_ended_at <= 0.0 and not prev_had_resp_or_tc and resp_len == 0 and tc_len == 0:
            rt.ts_thinking_ended_at = now

        if rt.ts_first_token_at <= 0:
            rt.ts_first_token_at, rt.ts_first_chunk_tokens = now, cal_total
            if rt.ts_step_created_at > 0 and now > rt.ts_step_created_at:
                rt.ts_ttft_ms = max(1, int(round((now - rt.ts_step_created_at) * 1000.0)))
            elif viewable_ts > created_ts > 0:
                rt.ts_ttft_ms = max(1, int(round((viewable_ts - created_ts) * 1000.0)))
            rt.ts_cum_points = [(now, raw_think, raw_resp)]
            rt.ts_last_growth_at = now
        elif grew:
            pts = rt.ts_cum_points
            mono_th = max(pts[-1][1] if pts else 0.0, raw_think)
            mono_rs = max(pts[-1][2] if pts else 0.0, raw_resp)
            # When first response/toolCalls chunk arrives after thinkingDuration ended, anchor
            # the window at thinking_ended_at so the preceding silent thinking tail doesn't crush rate.
            if (d_resp > 0 or d_tc > 0) and not prev_had_resp_or_tc and rt.ts_thinking_ended_at > 0.0 and now > rt.ts_thinking_ended_at:
                anchor_th = pts[-1][1] if pts else raw_think
                anchor_rs = pts[-1][2] if pts else 0.0
                pts[:] = [(rt.ts_thinking_ended_at, anchor_th, anchor_rs), (now, mono_th, mono_rs)]
                rt.ts_last_growth_at = rt.ts_thinking_ended_at
            elif pts and now <= pts[-1][0]:
                pts[-1] = (pts[-1][0], mono_th, mono_rs)
            else:
                pts.append((now, mono_th, mono_rs))

            win_sec = 4.5 if rt.ts_phase == "thinking" else 2.0
            win_cutoff = now - win_sec
            while len(pts) >= 2 and pts[1][0] <= win_cutoff:
                pts.pop(0)
            if len(pts) >= 2 and pts[0][0] < win_cutoff < pts[1][0]:
                t0, th0, rs0 = pts[0]
                t1, th1, rs1 = pts[1]
                if (t1 - t0) > 1e-6:
                    frac = (win_cutoff - t0) / (t1 - t0)
                    pts[0] = (win_cutoff, th0 + frac * (th1 - th0), rs0 + frac * (rs1 - rs0))

            span = (pts[-1][0] - pts[0][0]) if len(pts) >= 2 else 0.0
            if len(pts) >= 2 and span >= 0.25:
                v_start = (pts[0][1] * eff_cal_think) + (pts[0][2] * eff_cal_resp)
                v_end = (pts[-1][1] * eff_cal_think) + (pts[-1][2] * eff_cal_resp)
                inst_tps = max(0.0, v_end - v_start) / max(0.25, span)
                prev_gap, prev_live = now - rt.ts_last_growth_at, rt.ts_live_tps
                decay_grace = 5.5 if rt.ts_phase == "thinking" else 3.5
                if prev_gap > decay_grace and prev_live > 0.0:
                    prev_live *= math.exp(-0.45 * (prev_gap - decay_grace))
                rt.ts_live_tps = inst_tps if (not rt.ts_window_ready or prev_live <= 0.0) else ((0.45 * inst_tps) + (0.55 * prev_live))
                rt.ts_window_ready = True
                rt.ts_peak_tps = max(rt.ts_peak_tps, rt.ts_live_tps)
                rt.ts_step_peak_by_idx[idx] = rt.ts_peak_tps
            rt.ts_last_growth_at = now
        elif not rt.ts_window_ready and len(rt.ts_cum_points) == 1:
            rt.ts_first_chunk_tokens = cal_total

        rt.ts_raw_think_chars = max(rt.ts_raw_think_chars, think_len)
        rt.ts_raw_resp_chars = max(rt.ts_raw_resp_chars, resp_len)
        rt.ts_raw_tc_chars = max(rt.ts_raw_tc_chars, tc_len)
        rt.ts_est_think_tokens, rt.ts_est_resp_tokens, rt.ts_est_output_tokens = est_think_int, est_resp_int, est_out_int

    def _finalize_planner_step_locked(
        self, rt: ConversationRuntime, idx: int, st: Optional[dict[str, Any]], gm_info: Optional[dict[str, Any]], now: float, is_initial: bool
    ) -> None:
        meta = st.get("metadata") if isinstance(st, dict) and isinstance(st.get("metadata"), dict) else {}
        pr = st.get("plannerResponse") if isinstance(st, dict) and isinstance(st.get("plannerResponse"), dict) else {}
        if pr:
            raw_think = _estimate_raw_text_tokens(str(pr.get("thinking") or ""))
            raw_resp = _estimate_raw_text_tokens(str(pr.get("modifiedResponse") or pr.get("response") or "")) + _estimate_raw_text_tokens(_extract_tool_calls_text(pr.get("toolCalls")))
            if (raw_think + raw_resp) > 0:
                rt.ts_step_raw_chars_by_idx[idx] = (raw_think, raw_resp)

        created_ts = parse_iso_ts(str(meta.get("createdAt") or "")) or float(rt.steps_by_idx.get(idx, {}).get("createdAt") or 0.0)
        viewable_ts = parse_iso_ts(str(meta.get("viewableAt") or "")) or float(rt.steps_by_idx.get(idx, {}).get("viewableAt") or 0.0)
        finished_ts = parse_iso_ts(str(meta.get("finishedGeneratingAt") or meta.get("completedAt") or "")) or float(rt.steps_by_idx.get(idx, {}).get("completedAt") or 0.0)
        exact_tokens = (gm_info.get("tokens") if isinstance(gm_info, dict) else None) or _extract_usage_tokens(meta.get("modelUsage"))
        prev_rec = rt.ts_last_completed if (isinstance(rt.ts_last_completed, dict) and int(rt.ts_last_completed.get("stepIndex", -1)) == idx) else None

        step_model = str(meta.get("generatorModel") or "").strip()
        if not step_model and isinstance(meta.get("modelUsage"), dict):
            step_model = str(meta["modelUsage"].get("model") or "").strip()

        is_exact = False
        if exact_tokens is not None:
            out_tokens, think_tokens, resp_tokens = exact_tokens
            is_exact = True
            self._calibrate_token_estimator_locked(
                rt, idx, out_tokens, exact_think_tokens=think_tokens, exact_resp_tokens=resp_tokens,
                gm_info=gm_info, step_model=step_model, is_initial=is_initial,
            )
        elif prev_rec is not None and bool(prev_rec.get("isExact")):
            out_tokens, think_tokens, resp_tokens = int(prev_rec.get("outputTokens") or 0), int(prev_rec.get("thinkingTokens") or 0), int(prev_rec.get("responseTokens") or 0)
            is_exact = True
        elif idx == rt.ts_active_step_idx and rt.ts_est_output_tokens > 0:
            out_tokens, think_tokens, resp_tokens = rt.ts_est_output_tokens, rt.ts_est_think_tokens, rt.ts_est_resp_tokens
        elif idx in rt.ts_step_raw_chars_by_idx:
            rt_pair = rt.ts_step_raw_chars_by_idx[idx]
            eff_cal_think, eff_cal_resp = self._effective_tok_calibration_locked(rt)
            think_tokens, resp_tokens = int(round(rt_pair[0] * eff_cal_think)), int(round(rt_pair[1] * eff_cal_resp))
            out_tokens = think_tokens + resp_tokens
        elif prev_rec is not None:
            out_tokens, think_tokens, resp_tokens = int(prev_rec.get("outputTokens") or 0), int(prev_rec.get("thinkingTokens") or 0), int(prev_rec.get("responseTokens") or 0)
        else:
            out_tokens = think_tokens = resp_tokens = 0

        ttft_sec = float(gm_info.get("ttftSec") or 0.0) if isinstance(gm_info, dict) else 0.0
        stream_sec = float(gm_info.get("streamSec") or 0.0) if isinstance(gm_info, dict) else 0.0
        was_live_full = idx == rt.ts_active_step_idx and not rt.ts_joined_midstream
        live_ttft_ms = rt.ts_ttft_ms if (idx == rt.ts_active_step_idx and rt.ts_ttft_ms > 0) else 0
        live_first_at = rt.ts_first_token_at if was_live_full else 0.0
        live_last_at = rt.ts_last_growth_at if was_live_full else 0.0
        live_created_at = rt.ts_step_created_at if was_live_full else 0.0

        if live_ttft_ms > 0:
            ttft_ms = live_ttft_ms
        elif prev_rec is not None and int(prev_rec.get("ttftMs") or 0) > 0:
            ttft_ms = int(prev_rec["ttftMs"])
        elif viewable_ts > created_ts > 0:
            ttft_ms = max(1, int(round((viewable_ts - created_ts) * 1000.0)))
        elif ttft_sec > 0.0:
            ttft_ms = max(1, int(round(ttft_sec * 1000.0)))
        else:
            ttft_ms = 0

        gm_total_sec = (ttft_sec + stream_sec) if (ttft_sec > 0.0 or stream_sec > 0.0) else 0.0
        meta_total_sec = (finished_ts - created_ts) if (finished_ts > created_ts > 0.0) else 0.0
        meta_view_sec = (finished_ts - viewable_ts) if (finished_ts > viewable_ts > 0.0) else 0.0
        live_end_ts = finished_ts if (finished_ts > live_first_at > 0.0) else max(now, live_last_at)
        live_stream_sec = (live_end_ts - live_first_at) if (live_first_at > 0.0 and live_end_ts > live_first_at) else 0.0
        live_total_sec = (now - live_created_at) if (live_created_at > 0.0 and now > live_created_at) else 0.0

        raw_pair_known = rt.ts_step_raw_chars_by_idx.get(idx)
        invisible_thinking_only = bool(
            raw_pair_known is not None and raw_pair_known[0] <= 0.0 and think_tokens > 0 and resp_tokens > 0
        )

        gen_sec = 0.0
        avg_tps = 0.0
        if out_tokens > 0:
            if invisible_thinking_only and stream_sec >= 0.18 and (resp_tokens / stream_sec) <= 380.0:
                gen_sec = stream_sec
                avg_tps = round(resp_tokens / stream_sec, 1)
            elif stream_sec >= 0.25 and (out_tokens / stream_sec) <= 380.0:
                gen_sec = stream_sec
            elif live_stream_sec >= 0.35 and (out_tokens / live_stream_sec) <= 380.0:
                gen_sec = live_stream_sec
            elif meta_view_sec >= 0.35 and (out_tokens / meta_view_sec) <= 380.0:
                gen_sec = meta_view_sec
            elif gm_total_sec >= 0.20:
                gen_sec = gm_total_sec
            elif meta_total_sec >= 0.20:
                gen_sec = meta_total_sec
            elif live_total_sec >= 0.20:
                gen_sec = live_total_sec
            elif prev_rec is not None and float(prev_rec.get("elapsedGenSec") or 0.0) >= 0.20:
                gen_sec = float(prev_rec["elapsedGenSec"])
            else:
                gen_sec = max(stream_sec, gm_total_sec, meta_total_sec, live_stream_sec, 0.0)

        if avg_tps <= 0.0:
            avg_tps = round(out_tokens / gen_sec, 1) if (out_tokens > 0 and gen_sec >= 0.15) else 0.0
        if avg_tps <= 0.0 and prev_rec is not None and float(prev_rec.get("avgTokensPerSec") or 0.0) > 0.0:
            avg_tps = float(prev_rec["avgTokensPerSec"])

        recorded_peak = rt.ts_step_peak_by_idx.get(idx, 0.0)
        if idx == rt.ts_active_step_idx:
            recorded_peak = max(recorded_peak, rt.ts_peak_tps)
        if prev_rec is not None:
            recorded_peak = max(recorded_peak, float(prev_rec.get("peakTokensPerSec") or 0.0))
        peak_tps = round(max(recorded_peak, avg_tps), 1)

        if (out_tokens > 0 or prev_rec is not None) and idx >= int((rt.ts_last_completed or {}).get("stepIndex", -1)):
            rt.ts_last_completed = {
                "active": False, "phase": "idle", "phaseLabel": "最近一轮",
                "displayTokensPerSec": avg_tps, "liveTokensPerSec": 0.0, "avgTokensPerSec": avg_tps, "peakTokensPerSec": peak_tps,
                "outputTokens": int(out_tokens), "thinkingTokens": int(think_tokens), "responseTokens": int(resp_tokens),
                "isExact": bool(is_exact), "ttftMs": int(ttft_ms), "elapsedGenSec": round(gen_sec, 2), "stepIndex": int(idx),
            }
        if idx == rt.ts_active_step_idx:
            self._reset_active_token_speed_locked(rt)

    def _infer_waiting_turn_since_locked(self, rt: ConversationRuntime, now: float) -> float:
        """Infer turn-wait start timestamp when a live stream connects mid-TTFT on a RUNNING loop."""
        bg_indices = self._collect_bg_step_indices_locked(rt)
        for idx in sorted(rt.steps_by_idx.keys(), reverse=True):
            if idx in bg_indices:
                continue
            st = rt.steps_by_idx[idx]
            stype = str(st.get("type") or "")
            sstat = str(st.get("status") or "")
            if stype == "CORTEX_STEP_TYPE_PLANNER_RESPONSE" and sstat == "DONE":
                if short_run_status(rt.raw_executor_loop_status) != "RUNNING":
                    return 0.0
                cand = float(st.get("completedAt") or st.get("startedAt") or st.get("createdAt") or 0.0)
                if cand > 0.0 and 0.0 <= (now - cand) <= self.PLANNER_CREATED_TS_MAX_AGE_SEC:
                    return min(now, cand)
                return 0.0
            cand = float(st.get("completedAt") or st.get("startedAt") or st.get("createdAt") or 0.0)
            if cand > 0.0 and -2.0 <= (now - cand) <= self.PLANNER_CREATED_TS_MAX_AGE_SEC:
                return min(now, cand)
            break
        fallback = float(rt.last_stream_frame_at or rt.last_modified_at or 0.0)
        if fallback > 0.0 and 0.0 <= (now - fallback) <= 30.0:
            return min(now, fallback)
        return 0.0

    def _compute_token_speed_locked(self, rt: ConversationRuntime, now: float) -> dict[str, Any]:
        if rt.ts_active_step_idx >= 0:
            phase = rt.ts_phase if rt.ts_phase in ("waiting_ttft", "thinking", "generating", "tool_calls") else "waiting_ttft"
            cur_tok = float(rt.ts_est_output_tokens)
            if phase == "waiting_ttft" or cur_tok <= 0.0:
                avg_tps = 0.0
                elapsed_gen = max(0.0, now - rt.ts_step_created_at) if rt.ts_step_created_at > 0 else 0.0
            elif rt.ts_joined_midstream:
                dt_mid = max(0.0, now - rt.ts_baseline_at)
                d_mid_tok = max(0.0, cur_tok - float(rt.ts_baseline_tokens))
                if dt_mid >= 0.25 and d_mid_tok > 0.0:
                    avg_tps = round(d_mid_tok / max(0.35, dt_mid), 1)
                    elapsed_gen = dt_mid
                elif rt.ts_first_token_at > 0.0 and rt.ts_first_token_at < rt.ts_baseline_at and cur_tok > 0.0:
                    dt_view = now - rt.ts_first_token_at
                    if 0.25 <= dt_view <= 300.0:
                        est_mid = cur_tok / dt_view
                        avg_tps = round(est_mid, 1) if est_mid <= 380.0 else 0.0
                        elapsed_gen = dt_view
                    else:
                        avg_tps = 0.0
                        elapsed_gen = dt_mid
                else:
                    avg_tps = 0.0
                    elapsed_gen = dt_mid
            else:
                dt_first = max(0.0, now - rt.ts_first_token_at) if rt.ts_first_token_at > 0 else 0.0
                if rt.ts_first_chunk_tokens > 25.0:
                    if rt.ts_step_created_at > 0.0 and 0.0 < (rt.ts_first_token_at - rt.ts_step_created_at) <= 2.5:
                        decode_start = rt.ts_step_created_at
                    else:
                        est_burst = min(1.5, max(0.25, rt.ts_first_chunk_tokens / 120.0))
                        decode_start = rt.ts_first_token_at - est_burst
                else:
                    decode_start = (
                        max(rt.ts_step_created_at, rt.ts_first_token_at - 0.12)
                        if rt.ts_step_created_at > 0.0
                        else (rt.ts_first_token_at - 0.12)
                    )
                first_silence = max(0.0, now - rt.ts_last_growth_at) if rt.ts_last_growth_at > 0 else 0.0
                chunk1_grace = 4.5 if phase == "thinking" else 2.5
                if not rt.ts_window_ready and cur_tok < 15.0 and first_silence > 0.5:
                    avg_tps = 0.0
                elif not rt.ts_window_ready and first_silence <= chunk1_grace and rt.ts_last_growth_at > decode_start:
                    avg_tps = round(cur_tok / max(0.25, rt.ts_last_growth_at - decode_start), 1)
                else:
                    avg_tps = round(cur_tok / max(0.25, now - decode_start), 1)
                elapsed_gen = dt_first

            raw_live = rt.ts_live_tps if rt.ts_window_ready else 0.0
            silence_sec = (now - rt.ts_last_growth_at) if rt.ts_last_growth_at > 0 else 0.0
            decay_grace = 5.5 if phase == "thinking" else 3.5
            silence_zero = 16.0 if phase == "thinking" else 12.0
            if silence_sec > silence_zero:
                live_tps = 0.0
            elif silence_sec > decay_grace and raw_live > 0.0:
                decayed = raw_live * math.exp(-0.45 * (silence_sec - decay_grace))
                live_tps = round(decayed, 1) if decayed >= 0.1 else 0.0
            else:
                live_tps = round(raw_live, 1)

            if silence_sec > silence_zero:
                display_tps = 0.0
            elif not rt.ts_window_ready:
                display_tps = avg_tps
            elif silence_sec <= (silence_zero - 2.0) and cur_tok > 0.0 and live_tps < 0.5:
                display_tps = round(max(live_tps, avg_tps), 1)
            else:
                display_tps = live_tps if live_tps > 0.0 else (avg_tps if silence_sec <= (silence_zero - 2.0) else 0.0)
            is_ts_active = True if phase == "waiting_ttft" else (silence_sec <= silence_zero and display_tps > 0.0)
            return {
                "active": is_ts_active, "phase": phase, "phaseLabel": SPEED_PHASE_LABELS.get(phase, "输出中"),
                "displayTokensPerSec": display_tps, "liveTokensPerSec": live_tps, "avgTokensPerSec": avg_tps,
                "peakTokensPerSec": round(max(rt.ts_peak_tps, live_tps), 1),
                "outputTokens": int(rt.ts_est_output_tokens), "thinkingTokens": int(rt.ts_est_think_tokens),
                "responseTokens": int(rt.ts_est_resp_tokens), "isExact": False,
                "ttftMs": int(rt.ts_ttft_ms), "elapsedGenSec": round(elapsed_gen, 2), "stepIndex": int(rt.ts_active_step_idx),
            }

        is_loop_running = (
            short_run_status(rt.raw_status) == "RUNNING"
            or short_run_status(rt.raw_executor_loop_status) == "RUNNING"
        )
        if is_loop_running and not self._has_foreground_active_tool_locked(rt):
            wait_since = rt.ts_waiting_turn_since
            if wait_since <= 0.0 and rt.stream_connected and rt.stream_initial_seen:
                wait_since = self._infer_waiting_turn_since_locked(rt, now)
            elif wait_since <= 0.0 and rt.last_modified_at > 0.0 and 0.0 <= (now - rt.last_modified_at) <= 60.0:
                wait_since = min(now, rt.last_modified_at)
            if wait_since > 0.0 and (now - wait_since) <= self.PLANNER_CREATED_TS_MAX_AGE_SEC:
                return {
                    "active": True, "phase": "waiting_ttft", "phaseLabel": SPEED_PHASE_LABELS["waiting_ttft"],
                    "displayTokensPerSec": 0.0, "liveTokensPerSec": 0.0, "avgTokensPerSec": 0.0,
                    "peakTokensPerSec": round(rt.ts_peak_tps, 1), "outputTokens": 0, "thinkingTokens": 0, "responseTokens": 0,
                    "isExact": False, "ttftMs": 0, "elapsedGenSec": round(max(0.0, now - wait_since), 2), "stepIndex": -1,
                }

        if isinstance(rt.ts_last_completed, dict):
            avg_done = round(float(rt.ts_last_completed.get("avgTokensPerSec") or 0.0), 1)
            return {
                "active": False, "phase": "idle", "phaseLabel": "最近一轮",
                "displayTokensPerSec": avg_done, "liveTokensPerSec": 0.0, "avgTokensPerSec": avg_done,
                "peakTokensPerSec": round(float(rt.ts_last_completed.get("peakTokensPerSec") or avg_done), 1),
                "outputTokens": int(rt.ts_last_completed.get("outputTokens") or 0),
                "thinkingTokens": int(rt.ts_last_completed.get("thinkingTokens") or 0),
                "responseTokens": int(rt.ts_last_completed.get("responseTokens") or 0),
                "isExact": bool(rt.ts_last_completed.get("isExact", False)),
                "ttftMs": int(rt.ts_last_completed.get("ttftMs") or 0),
                "elapsedGenSec": round(float(rt.ts_last_completed.get("elapsedGenSec") or 0.0), 2),
                "stepIndex": int(rt.ts_last_completed.get("stepIndex", -1)),
            }
        return _empty_token_speed()

    # ------------------------------------------------------------------
    # Pure On-Demand Read-Only SQLite Anomaly Diagnosis (step_type = 17)
    # ------------------------------------------------------------------

    def _needs_sqlite_diagnosis_locked(self, rt: ConversationRuntime, summary: Optional[dict[str, Any]], now: float, stall_sec: int) -> bool:
        """Return True ONLY when stream or trajectory exhibits error/interruption/stall symptoms."""
        if rt.has_error_step_hint or rt.db_latest_error is not None:
            return True
        for raw_st in (rt.raw_status, rt.raw_executor_loop_status, summary.get("status") if isinstance(summary, dict) else ""):
            if short_run_status(str(raw_st or "")) in ("ERROR", "FAILED", "ABORTED"):
                return True
        term = (rt.latest_termination_reason or (str(summary.get("latestExecutorTerminationReason") or "") if isinstance(summary, dict) else "")).upper()
        if term and not any(ok in term for ok in self._NORMAL_TERM_KEYWORDS) and any(bad in term for bad in self._ANOMALY_TERM_KEYWORDS):
            return True

        if not (rt.composite_running or short_run_status(rt.raw_status) == "RUNNING" or short_run_status(rt.raw_executor_loop_status) == "RUNNING"):
            return False

        fg_tool = self._find_foreground_running_step_locked(rt, include_planner=False)
        if isinstance(fg_tool, dict):
            if _is_intentional_wait_step(str(fg_tool.get("toolName") or ""), str(fg_tool.get("type") or "")):
                return False
            eff_started = float(fg_tool.get("startedAt") or fg_tool.get("createdAt") or 0.0)
            running_since = float(fg_tool.get("runningSince") or 0.0)
            started = max(eff_started, running_since) if running_since > 0 else eff_started
            if stall_sec > 0 and started > 0 and (now - started) >= stall_sec:
                return True
        return bool(rt.stream_connected and rt.last_stream_frame_at > 0 and (now - rt.last_stream_frame_at) >= 12.0)

    def _diagnose_sqlite_if_needed(self, cid: str, now: float, stall_sec: int) -> None:
        if not cid:
            return
        with self._lock:
            rt = self._runtimes.get(cid)
            if rt is None or not self._needs_sqlite_diagnosis_locked(rt, self._trajectories.get(cid), now, stall_sec):
                return
        self._diagnose_sqlite_anomaly(cid, force=False, include_recent_steps=False)

    def _diagnose_sqlite_anomaly(self, cid: str, force: bool = False, include_recent_steps: bool = False) -> None:
        """Open conversations/<cid>.db read-only strictly on anomaly, cached by (db_mtime_ns, wal_mtime_ns, wal_size)."""
        if not cid:
            return
        db_file = self.app_data_dir / "conversations" / f"{safe_cid(cid)}.db"
        if (db_ns := _stat_mtime_ns(str(db_file))) <= 0:
            return
        wal_ns, wal_sz = _stat_mtime_and_size(str(db_file) + "-wal")
        sig = (db_ns, wal_ns, wal_sz)
        now = time.time()

        with self._lock:
            self._db_seen_cids[cid] = now
            rt = self._get_or_create_runtime(cid)
            rt.had_db_on_disk = True
            if not force and rt.db_sig == sig and (not include_recent_steps or rt.db_recent_steps):
                return

        conn: Optional[sqlite3.Connection] = None
        try:
            conn = sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True, timeout=self.SQLITE_READ_TIMEOUT_SEC)
            cur = conn.cursor()
            origin_row = cur.execute(
                "SELECT COALESCE(MAX(idx), -1) FROM steps WHERE step_type = 14 AND status = 3"
            ).fetchone()
            origin_user_idx = int(origin_row[0] if (origin_row and origin_row[0] is not None) else -1)

            last_ok_row = cur.execute(
                "SELECT COALESCE(MAX(idx), -1) FROM steps WHERE idx > ? AND step_type = 15 AND status IN (2, 3)",
                (origin_user_idx,),
            ).fetchone()
            last_ok_idx = int(last_ok_row[0] if (last_ok_row and last_ok_row[0] is not None) else -1)
            err_floor_idx = max(origin_user_idx, last_ok_idx)
            consec_row = cur.execute(
                "SELECT COUNT(*) FROM steps WHERE step_type = 17 AND idx > ?",
                (err_floor_idx,),
            ).fetchone()
            consecutive_errors = int(consec_row[0] if (consec_row and consec_row[0] is not None) else 0)

            err_rows = cur.execute(
                "SELECT idx, status, metadata, error_details, step_payload FROM steps WHERE step_type = 17 AND idx > ? ORDER BY idx DESC LIMIT 8",
                (err_floor_idx,),
            ).fetchall()
            latest_err: Optional[dict[str, Any]] = None
            err_kws = (
                "interrupted due to server shutdown", "server shutdown", "server restart",
                "stream idle timeout", "stream was interrupted", "deadline exceeded",
                "api error", "quota", "request failed", "eof", "terminated",
            )
            for e_idx, e_status, e_meta, e_details, e_payload in err_rows:
                if int(e_idx) <= err_floor_idx:
                    continue
                meta_info = _parse_step_metadata_blob(e_meta)
                err_texts = _extract_proto_strings(e_details or b"") + _extract_proto_strings(e_payload or b"")
                best_msg = next((s for s in err_texts if any(k in s.lower() for k in err_kws)), "")
                if not best_msg and (non_uuid := [x for x in err_texts if not _UUID_LIKE_RE.fullmatch(x)]):
                    best_msg = max(non_uuid, key=len)
                if best_msg:
                    attempt_m = _ATTEMPT_RE.search(best_msg)
                    low_msg = best_msg.lower()
                    is_server_restart = any(rk in low_msg for rk in self._SERVER_RESTART_EXEMPT_KEYWORDS) or any(
                        any(rk in s.lower() for rk in self._SERVER_RESTART_EXEMPT_KEYWORDS) for s in err_texts
                    )
                    is_interrupted = (not is_server_restart) and any(
                        dk in low_msg for dk in self._GENUINE_STREAM_DROP_KEYWORDS
                    )
                    latest_err = {
                        "stepIndex": int(e_idx), "status": int(e_status), "message": best_msg[:360],
                        "isTimeout": "idle timeout" in low_msg or "deadline exceeded" in low_msg,
                        "isServerRestart": is_server_restart,
                        "isInterrupted": is_interrupted,
                        "attempt": int(attempt_m.group(1)) if attempt_m else 0,
                        "consecutiveErrors": int(consecutive_errors),
                        "originTurnIdx": int(err_floor_idx),
                        "timestamp": meta_info["createdAt"] or now,
                    }
                    break

            db_steps: list[dict[str, Any]] = []
            if include_recent_steps or latest_err is not None:
                for r_idx, r_type, r_status, r_meta, r_payload in cur.execute(
                    "SELECT idx, step_type, status, metadata, step_payload FROM steps ORDER BY idx DESC LIMIT 8"
                ).fetchall():
                    m_info = _parse_step_metadata_blob(r_meta)
                    r_strs = _extract_proto_strings(r_payload or b"") + _extract_proto_strings(r_meta or b"")
                    is_d = any(_DAEMON_JSON_RE.search(s) for s in r_strs)
                    db_steps.append({
                        "idx": int(r_idx), "stepTypeInt": int(r_type), "statusInt": int(r_status),
                        "toolName": m_info["toolName"], "isDaemon": is_d, "createdAt": m_info["createdAt"],
                        "startedAt": m_info["startedAt"] or m_info["createdAt"], "completedAt": m_info["completedAt"],
                    })

            with self._lock:
                rt = self._get_or_create_runtime(cid)
                prev_err = rt.db_latest_error
                rt.db_sig = sig
                if origin_user_idx >= 0:
                    rt.last_user_input_idx = max(rt.last_user_input_idx, origin_user_idx)
                rt.db_latest_error = latest_err
                if db_steps:
                    rt.db_recent_steps = db_steps
                    if (max_db_idx := max(int(d["idx"]) for d in db_steps)) + 1 > rt.total_steps:
                        rt.total_steps = max_db_idx + 1
                        rt.last_step_index = max(rt.last_step_index, max_db_idx)
                if latest_err != prev_err:
                    self._bump_rev_locked()
        except Exception:
            pass
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _scan_sqlite_bg_script(self, cid: str) -> None:
        """Fast mtime-cached read-only check for verified active status=2 background workflow/command steps."""
        if not cid:
            return
        db_file = self.app_data_dir / "conversations" / f"{safe_cid(cid)}.db"
        db_ns = _stat_mtime_ns(str(db_file))
        if db_ns <= 0:
            with self._lock:
                if (rt := self._runtimes.get(cid)) is not None:
                    rt.db_bg_sig = (0, 0, 0)
                    if rt.db_bg_script is not None:
                        rt.db_bg_script = None
                        self._bump_rev_locked()
            return
        wal_ns, wal_sz = _stat_mtime_and_size(str(db_file) + "-wal")
        sig = (db_ns, wal_ns, wal_sz)
        now = time.time()
        with self._lock:
            rt = self._get_or_create_runtime(cid)
            if rt.db_bg_sig == sig:
                return
            steps_snap = dict(rt.steps_by_idx)
            bg_indices_snap = self._collect_bg_step_indices_locked(rt)
            stream_live = bool(rt.stream_connected and rt.stream_initial_seen)
            traj_s = self._trajectories.get(cid)
            traj_short = short_run_status(str(traj_s.get("status") or "IDLE")) if isinstance(traj_s, dict) else "IDLE"
            is_sub = isinstance(traj_s, dict) and self._is_subagent_summary(cid, traj_s)
            is_conv_live_active = (
                self._is_subagent_live_running_locked(rt, traj_s or {}, traj_short)
                if is_sub
                else self._is_root_live_active_locked(rt, traj_s or {}, traj_short)
            )
            ls_start_ts = self._ls_start_ts

        conn: Optional[sqlite3.Connection] = None
        found: Optional[dict[str, Any]] = None
        try:
            conn = sqlite3.connect(f"file:{db_file.as_posix()}?mode=ro", uri=True, timeout=self.SQLITE_READ_TIMEOUT_SEC)
            rows = conn.execute(
                "SELECT idx, step_type, metadata, step_payload FROM steps WHERE status = 2 ORDER BY idx DESC LIMIT 6"
            ).fetchall()
            for r_idx, _, r_meta, r_payload in rows:
                idx_int = int(r_idx)
                known_st = steps_snap.get(idx_int)
                if isinstance(known_st, dict) and (known_st.get("status") != "RUNNING" or bool(known_st.get("isDaemon"))):
                    continue
                m_info = _parse_step_metadata_blob(r_meta)
                tool_name = str(m_info.get("toolName") or "").strip()
                log_uri = ""
                if tool_name == "run_workflow":
                    kind = "workflow"
                elif tool_name == "run_command":
                    raw_blob = (r_payload or b"") + b"\x00" + (r_meta or b"")
                    strs = _extract_proto_strings(r_payload or b"") + _extract_proto_strings(r_meta or b"")
                    if any(_DAEMON_JSON_RE.search(s) for s in strs) or b'"is_daemon":true' in raw_blob.lower().replace(b" ", b""):
                        continue
                    for s_val in strs:
                        low_s = s_val.lower()
                        if low_s.startswith("file://") and low_s.endswith(".log"):
                            log_uri = s_val.strip()
                            break
                    kind = "command"
                else:
                    continue
                started_at = float(m_info.get("startedAt") or m_info.get("createdAt") or 0.0)
                if started_at > 0.0 and started_at < (ls_start_ts - 2.0):
                    continue
                if stream_live:
                    confirmed_by_stream = (idx_int in bg_indices_snap) or (
                        isinstance(known_st, dict) and known_st.get("status") == "RUNNING" and not bool(known_st.get("isDaemon"))
                    )
                    if not confirmed_by_stream:
                        continue
                elif not is_conv_live_active:
                    if idx_int not in bg_indices_snap:
                        if started_at <= 0.0 or not self._is_step_log_recently_modified_cached(cid, idx_int, log_uri, now, fresh_sec=120.0):
                            continue
                found = {
                    "kind": kind,
                    "toolName": tool_name,
                    "stepIndex": idx_int,
                    "startedAt": round(started_at, 3) if started_at > 0 else 0.0,
                    "logUri": log_uri,
                }
                break
        except Exception:
            return
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        with self._lock:
            rt = self._get_or_create_runtime(cid)
            rt.db_bg_sig = sig
            if rt.db_bg_script != found:
                rt.db_bg_script = found
                if found is None and not rt.stream_connected:
                    for st in rt.steps_by_idx.values():
                        if st.get("status") == "RUNNING" and st.get("toolName") in ("run_workflow", "run_command"):
                            st["status"] = "DONE"
                self._bump_rev_locked()

    # ------------------------------------------------------------------
    # Disk Snapshot Persistence & GC (<PLUGIN_DATA>/state/<cid>.json)
    # ------------------------------------------------------------------

    def _state_file_path(self, cid: str) -> Path:
        return self.state_dir / f"{safe_cid(cid)}.json"

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
            tmp.write_bytes(raw)
            os.replace(tmp, path)
        except Exception:
            try:
                path.write_bytes(raw)
            except Exception:
                pass

    def _hydrate_runtime_from_disk_state(self, rt: ConversationRuntime) -> None:
        p = self._state_file_path(rt.cid)
        try:
            if not p.is_file() or not isinstance(disk := json.loads(p.read_text(encoding="utf-8")), dict):
                return
        except Exception:
            return

        with self._lock:
            rt.disk_hydrated = True
            for dk, attr in (("modelId", "model_id"), ("modelName", "model_name"), ("modelDisplayName", "model_display_name"), ("workspaceName", "workspace_name"), ("workspacePath", "workspace_path"), ("branchName", "branch_name")):
                if isinstance(disk.get(dk), str) and disk[dk].strip() and not getattr(rt, attr):
                    setattr(rt, attr, disk[dk].strip())
            if rt.model_id or rt.model_name or rt.model_display_name:
                rt.model_label = self._resolve_model_label(rt.model_id, rt.model_name, rt.model_display_name)
            elif isinstance(disk.get("modelLabel"), str) and (lbl := disk["modelLabel"].strip()) and not _needs_model_label_refresh(lbl):
                rt.model_label = lbl
            if not rt.routed_model_label and isinstance(disk.get("routedModelLabel"), str) and (rlbl := disk["routedModelLabel"].strip()):
                if rlbl != rt.model_label:
                    rt.routed_model_label = rlbl
            if bool(disk.get("hadDbOnDisk", False)):
                rt.had_db_on_disk = True
                self._db_seen_cids[rt.cid] = time.time()
            if isinstance(disk.get("numTotalSteps"), int) and disk["numTotalSteps"] > rt.total_steps:
                rt.total_steps = disk["numTotalSteps"]
            if isinstance(disk.get("lastStepIndex"), int) and disk["lastStepIndex"] > rt.last_step_index:
                rt.last_step_index = disk["lastStepIndex"]

            if isinstance(tu_state := disk.get("tokenUsageState"), dict) and tu_state.get("v") == 1:
                if isinstance(self_gm := tu_state.get("selfByGm"), dict) and not rt.usage_by_gm:
                    for gk, gvals in self_gm.items():
                        if isinstance(gvals, (list, tuple)) and len(gvals) >= 5:
                            rt.usage_by_gm[str(gk)] = (
                                max(0, _to_int_safe(gvals[0])), max(0, _to_int_safe(gvals[1])),
                                max(0, _to_int_safe(gvals[2])), max(0, _to_int_safe(gvals[3])),
                                max(0, _to_int_safe(gvals[4])),
                            )
                if rt.last_turn_usage is None and isinstance(last_t := tu_state.get("lastTurn"), (list, tuple)) and len(last_t) >= 5:
                    rt.last_turn_usage = (
                        max(0, _to_int_safe(last_t[0])), max(0, _to_int_safe(last_t[1])),
                        max(0, _to_int_safe(last_t[2])), max(0, _to_int_safe(last_t[3])),
                        max(0, _to_int_safe(last_t[4])),
                    )
                if isinstance(ch_map := tu_state.get("children"), dict):
                    for scid, cvals in ch_map.items():
                        if str(scid) not in rt.archived_children_usage and isinstance(cvals, (list, tuple)) and len(cvals) >= 6:
                            rt.archived_children_usage[str(scid)] = (
                                max(0, _to_int_safe(cvals[0])), max(0, _to_int_safe(cvals[1])),
                                max(0, _to_int_safe(cvals[2])), max(0, _to_int_safe(cvals[3])),
                                max(0, _to_int_safe(cvals[4])), max(0, _to_int_safe(cvals[5])),
                            )
                if rt.cache_miss_streak == 0 and isinstance(tu_state.get("cacheMissStreak"), int) and not isinstance(tu_state.get("cacheMissStreak"), bool):
                    rt.cache_miss_streak = max(0, int(tu_state["cacheMissStreak"]))
                if not rt.last_cache_gm_key and isinstance(tu_state.get("lastCacheGmKey"), str):
                    rt.last_cache_gm_key = tu_state["lastCacheGmKey"].strip()
                rt.usage_hydrated = True

            if isinstance(cwu := disk.get("contextWindowUsage"), dict):
                was_unprobed = (rt.tokens_used == 0)
                if (d_used := int(cwu.get("used") or 0)) > 0 and was_unprobed:
                    rt.tokens_used = d_used
                    if rt.usage_hydrated:
                        rt.snapshot_probed_steps = max(rt.snapshot_probed_steps, rt.total_steps)
                if (d_max := int(cwu.get("max") or 0)) > 0 and (was_unprobed or not rt.max_tokens_exact or rt.max_tokens == DEFAULT_CONTEXT_MAX_TOKENS):
                    rt.max_tokens = d_max
                    rt.max_tokens_exact = True
                if isinstance(grp := cwu.get("groups"), dict) and sum(rt.groups.values()) == 0:
                    rt.groups = {
                        "systemPrompt": max(0, int(grp.get("systemPrompt") or 0)),
                        "extensions": max(0, int(grp.get("extensions") or 0)),
                        "tools": max(0, int(grp.get("tools") or 0)),
                        "chatMessages": max(0, int(grp.get("chatMessages") or 0)),
                    }
                if (d_comp := max(0, _to_int_safe(cwu.get("compactionCount")))) > rt.compaction_count:
                    rt.compaction_count = d_comp
                if was_unprobed and bool(cwu.get("estimatedAfterCompaction", False)):
                    rt.estimated_after_compaction = True
                if not rt.context_top_children and isinstance(d_tc := cwu.get("topChildren"), list):
                    rt.context_top_children = [
                        {"group": str(c.get("group") or "")[:32], "name": str(c.get("name") or "")[:64], "tokens": max(0, _to_int_safe(c.get("tokens")))}
                        for c in d_tc[:6]
                        if isinstance(c, dict) and str(c.get("name") or "").strip() and _to_int_safe(c.get("tokens")) > 0
                    ]
                    if rt.context_top_children:
                        rt.context_top_children_from_breakdown = True

            for fk, attr in (("runBatchStartedAt", "run_batch_started_at"), ("justFinishedAt", "just_finished_at"), ("dismissedRecentAt", "dismissed_recent_at")):
                if isinstance(disk.get(fk), (int, float)) and float(disk[fk]) > 0:
                    setattr(rt, attr, float(disk[fk]))
            if isinstance(last_ts := disk.get("lastTokenSpeed"), dict) and rt.ts_last_completed is None and int(last_ts.get("outputTokens") or 0) > 0:
                rt.ts_last_completed = dict(last_ts)

    def _persist_runtime_state(self, cid: str) -> None:
        if self._is_subagent_cid(cid):
            return
        now = time.time()
        with self._lock:
            if (rt := self._runtimes.get(cid)) is None:
                return
            if len(rt.usage_by_gm) > 512:
                folded = list(rt.usage_by_gm.get("_folded") or (0, 0, 0, 0, 0))
                others = [(k, v) for k, v in rt.usage_by_gm.items() if k != "_folded"]
                cutoff = max(0, len(others) - 511)
                for _, fv in others[:cutoff]:
                    for i in range(5):
                        folded[i] += int(fv[i])
                self_by_gm_out: dict[str, list[int]] = {"_folded": [int(x) for x in folded]}
                for k, v in others[cutoff:]:
                    self_by_gm_out[str(k)] = [int(x) for x in v[:5]]
            else:
                self_by_gm_out = {str(k): [int(x) for x in v[:5]] for k, v in rt.usage_by_gm.items()}
            token_usage_state = {
                "v": 1,
                "selfByGm": self_by_gm_out,
                "lastTurn": [int(x) for x in rt.last_turn_usage[:5]] if rt.last_turn_usage is not None else None,
                "children": {str(scid): [int(x) for x in tup[:6]] for scid, tup in rt.archived_children_usage.items()},
                "cacheMissStreak": max(0, int(rt.cache_miss_streak)),
                "lastCacheGmKey": str(rt.last_cache_gm_key),
            }
            payload: dict[str, Any] = {
                "conversationId": cid, "updatedAt": round(now, 3),
                "hadDbOnDisk": bool(rt.had_db_on_disk or cid in self._db_seen_cids),
                "modelId": rt.model_id, "modelName": rt.model_name or rt.model_id, "modelDisplayName": rt.model_display_name, "modelLabel": rt.model_label, "routedModelLabel": rt.routed_model_label,
                "workspaceName": rt.workspace_name, "workspacePath": rt.workspace_path, "branchName": rt.branch_name,
                "numTotalSteps": rt.total_steps, "lastStepIndex": rt.last_step_index,
                "runBatchStartedAt": rt.run_batch_started_at, "justFinishedAt": rt.just_finished_at, "dismissedRecentAt": rt.dismissed_recent_at,
                "contextWindowUsage": {
                    "used": rt.tokens_used,
                    "max": rt.max_tokens,
                    "groups": {
                        "systemPrompt": max(0, int(rt.groups.get("systemPrompt") or 0)),
                        "extensions": max(0, int(rt.groups.get("extensions") or 0)),
                        "tools": max(0, int(rt.groups.get("tools") or 0)),
                        "chatMessages": max(0, int(rt.groups.get("chatMessages") or 0)),
                    },
                    "compactionCount": max(0, int(rt.compaction_count)),
                    "estimatedAfterCompaction": bool(rt.estimated_after_compaction),
                    "topChildren": list(rt.context_top_children[:6]),
                },
                "lastTokenSpeed": dict(rt.ts_last_completed) if isinstance(rt.ts_last_completed, dict) else None,
                "tokenUsageState": token_usage_state,
            }
            rt.last_state_persist_at = now
            rt.dirty_state = False
            rt.disk_hydrated = True
        self._write_json_atomic(self._state_file_path(cid), payload)

    def _gc_disk_state_files(self) -> None:
        now = time.time()
        with self._lock:
            if (now - self._last_gc_at) < 600.0:
                return
            self._last_gc_at = now
            summaries = dict(self._trajectories)
            all_ls_safe = {safe_cid(k) for k in self._all_ls_cids} if self._all_ls_cids else set()
            if summaries:
                all_ls_safe.update(safe_cid(k) for k in summaries.keys())
            removed_safe = {safe_cid(k) for k in self._removed_cids.keys()}

        sub_safe_cids: set[str] = {
            safe_cid(k) for k, s in summaries.items()
            if isinstance(s, dict) and self._is_subagent_summary(k, s)
        }

        for folder in (self.plugin_data_dir, self.state_dir, self.nodes_dir):
            try:
                if folder.is_dir():
                    for tmp_f in folder.glob(".*.tmp"):
                        try:
                            if (now - float(tmp_f.stat().st_mtime)) > 60.0:
                                tmp_f.unlink(missing_ok=True)
                        except OSError:
                            pass
            except OSError:
                pass

        try:
            if self.state_dir.is_dir():
                kept: list[tuple[float, Path]] = []
                for f in self.state_dir.glob("*.json"):
                    try:
                        st_m = float(f.stat().st_mtime)
                        age = max(0.0, now - st_m)
                        drop_sub = f.stem in sub_safe_cids
                        drop_ls_absent = False
                        if all_ls_safe and f.stem not in all_ls_safe:
                            if f.stem in removed_safe or age > self.LS_ABSENT_STATE_TTL_SEC:
                                drop_ls_absent = True
                            elif not _UUID_LIKE_RE.fullmatch(f.stem) and age > 300.0:
                                drop_ls_absent = True
                        if drop_sub or drop_ls_absent or age > self.DISK_STATE_TTL_SEC:
                            f.unlink(missing_ok=True)
                        else:
                            kept.append((st_m, f))
                    except OSError:
                        pass
                if len(kept) > self.MAX_DISK_STATE_FILES:
                    kept.sort(key=lambda x: x[0], reverse=True)
                    for _, old_f in kept[self.MAX_DISK_STATE_FILES:]:
                        try:
                            old_f.unlink(missing_ok=True)
                        except OSError:
                            pass
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Heartbeat & Watchdog Self-Healing
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_bg_step_indices_locked(rt: ConversationRuntime) -> set[int]:
        bg_indices: set[int] = set()
        for indices in rt.bg_tasks_by_idx.values():
            bg_indices.update(indices)
        return bg_indices

    @staticmethod
    def _current_turn_floor_idx_locked(rt: ConversationRuntime) -> int:
        floor_idx = max(0, int(rt.last_user_input_idx))
        for idx, st in rt.steps_by_idx.items():
            if st.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE" and st.get("status") == "DONE":
                if idx > floor_idx:
                    floor_idx = idx
        return floor_idx

    def _find_foreground_running_step_locked(
        self, rt: ConversationRuntime, include_planner: bool = True
    ) -> Optional[dict[str, Any]]:
        bg_indices = self._collect_bg_step_indices_locked(rt)
        floor_idx = self._current_turn_floor_idx_locked(rt)
        tool_candidates: list[tuple[float, int, dict[str, Any]]] = []
        planner_candidate: Optional[dict[str, Any]] = None
        for idx in sorted(rt.steps_by_idx.keys(), reverse=True):
            if idx < floor_idx or idx in bg_indices:
                continue
            st = rt.steps_by_idx[idx]
            s_stat = st.get("status")
            stype = str(st.get("type") or "")
            if stype == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
                if include_planner and s_stat in ("GENERATING", "RUNNING", "PENDING") and planner_candidate is None:
                    planner_candidate = st
                continue
            if s_stat == "RUNNING" and not st.get("isDaemon"):
                eff_started = float(st.get("startedAt") or st.get("createdAt") or 0.0)
                run_since = float(st.get("runningSince") or 0.0)
                started = max(eff_started, run_since) if run_since > 0.0 else eff_started
                tool_candidates.append((started if started > 0.0 else float("inf"), idx, st))
        if tool_candidates:
            tool_candidates.sort(key=lambda item: (item[0], item[1]))
            return tool_candidates[0][2]
        return planner_candidate

    def _has_foreground_active_tool_locked(self, rt: ConversationRuntime) -> bool:
        bg_indices = self._collect_bg_step_indices_locked(rt)
        floor_idx = self._current_turn_floor_idx_locked(rt)
        for idx, st in rt.steps_by_idx.items():
            if idx < floor_idx or idx in bg_indices:
                continue
            if st.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
                continue
            if st.get("status") in ("RUNNING", "WAITING", "WAITING_FOR_USER", "PENDING") and not st.get("isDaemon"):
                return True
        return False

    @staticmethod
    def _find_active_bg_script_locked(rt: ConversationRuntime, exclude_step_idx: int = -1) -> Optional[dict[str, Any]]:
        for st in sorted(rt.steps_by_idx.values(), key=lambda item: int(item.get("idx") or 0), reverse=True):
            if st.get("status") != "RUNNING":
                continue
            st_idx = int(st.get("idx") or 0)
            if exclude_step_idx >= 0 and st_idx == exclude_step_idx:
                continue
            stype = str(st.get("type") or "")
            tool_name = str(st.get("toolName") or "").strip()
            if tool_name == "run_workflow":
                kind = "workflow"
                resolved_tool = "run_workflow"
            elif (tool_name == "run_command" or stype == "CORTEX_STEP_TYPE_RUN_COMMAND") and not st.get("isDaemon"):
                kind = "command"
                resolved_tool = tool_name or "run_command"
            else:
                continue
            started_at = float(st.get("startedAt") or st.get("createdAt") or 0.0)
            return {
                "kind": kind,
                "toolName": resolved_tool,
                "stepIndex": st_idx,
                "startedAt": round(started_at, 3) if started_at > 0 else 0.0,
                "logUri": str(st.get("logUri") or ""),
            }
        if isinstance(rt.db_bg_script, dict):
            bg_idx = int(rt.db_bg_script.get("stepIndex", -1))
            if exclude_step_idx >= 0 and bg_idx == exclude_step_idx:
                return None
            if bg_idx in rt.steps_by_idx and (rt.steps_by_idx[bg_idx].get("status") != "RUNNING" or bool(rt.steps_by_idx[bg_idx].get("isDaemon"))):
                rt.db_bg_script = None
                return None
            if rt.stream_connected and rt.stream_initial_seen:
                bg_live_set = FlowPilotEngine._collect_bg_step_indices_locked(rt)
                if bg_idx not in bg_live_set and bg_idx not in rt.steps_by_idx:
                    rt.db_bg_script = None
                    return None
            db_started = float(rt.db_bg_script.get("startedAt") or 0.0)
            return {
                "kind": str(rt.db_bg_script.get("kind") or "command"),
                "toolName": str(rt.db_bg_script.get("toolName") or ""),
                "stepIndex": bg_idx,
                "startedAt": round(db_started, 3) if db_started > 0 else 0.0,
                "logUri": str(rt.db_bg_script.get("logUri") or ""),
            }
        return None

    @staticmethod
    def _max_known_step_idx_locked(rt: ConversationRuntime) -> int:
        max_idx = max(rt.total_steps - 1 if rt.total_steps > 0 else -1, rt.last_step_index)
        if rt.steps_by_idx:
            max_idx = max(max_idx, max(rt.steps_by_idx.keys()))
        if rt.db_recent_steps:
            max_idx = max(max_idx, max(int(d["idx"]) for d in rt.db_recent_steps))
        return max_idx

    def _collect_hb_log_checks_locked(
        self, rt: ConversationRuntime, now: float, stall_sec: int
    ) -> list[tuple[int, str, float]]:
        if stall_sec <= 0:
            return []
        checks: list[tuple[int, str, float]] = []
        raw_short = short_run_status(rt.raw_status)
        is_loop_running = (
            short_run_status(rt.raw_executor_loop_status) == "RUNNING"
            or raw_short == "RUNNING"
            or rt.ts_active_step_idx >= 0
        )
        fg_step = self._find_foreground_running_step_locked(rt, include_planner=False) if is_loop_running else None
        if fg_step is None and is_loop_running and rt.db_recent_steps:
            bg_step_indices = self._collect_bg_step_indices_locked(rt)
            turn_floor_idx = self._current_turn_floor_idx_locked(rt)
            for dbs in rt.db_recent_steps:
                d_idx = int(dbs["idx"])
                known_st = rt.steps_by_idx.get(d_idx)
                if isinstance(known_st, dict) and (known_st.get("status") != "RUNNING" or bool(known_st.get("isDaemon"))):
                    continue
                if bool(dbs.get("isDaemon")):
                    continue
                if dbs.get("statusInt") == 2 and d_idx not in bg_step_indices and d_idx >= turn_floor_idx:
                    fg_step = {
                        "idx": d_idx, "type": str(dbs["stepTypeInt"]), "status": "RUNNING",
                        "toolName": dbs.get("toolName") or "", "startedAt": dbs.get("startedAt") or dbs.get("createdAt") or 0.0,
                    }
                    break
        if isinstance(fg_step, dict):
            fg_idx = int(fg_step.get("idx", -1))
            fg_tool = str(fg_step.get("toolName") or "").strip()
            fg_type = str(fg_step.get("type") or "")
            if fg_idx >= 0 and fg_tool and not _is_intentional_wait_step(fg_tool, fg_type):
                eff_st = float(fg_step.get("startedAt") or fg_step.get("createdAt") or 0.0)
                run_s = float(fg_step.get("runningSince") or 0.0)
                started = max(eff_st, run_s) if run_s > 0.0 else eff_st
                if started > 0.0 and (now - started) >= stall_sec:
                    step_uri = str(fg_step.get("logUri") or (rt.steps_by_idx.get(fg_idx) or {}).get("logUri") or "").strip()
                    if _normalize_tool_key(fg_tool) == "run_command" or _normalize_tool_key(fg_type) == "run_command" or step_uri:
                        checks.append((fg_idx, step_uri, 30.0))
        elif not is_loop_running:
            bg = self._find_active_bg_script_locked(rt, exclude_step_idx=-1)
            bg_thresh = max(int(stall_sec) * 2, 120)
            if isinstance(bg, dict) and bg.get("kind") == "command":
                bg_idx = int(bg.get("stepIndex", -1))
                bg_start = float(bg.get("startedAt") or 0.0)
                if bg_idx >= 0 and bg_start > 0.0 and (now - bg_start) >= bg_thresh:
                    checks.append((bg_idx, str(bg.get("logUri") or ""), 60.0))
        return checks

    def _resolve_hb_log_fresh_map(
        self, cid: str, checks: list[tuple[int, str, float]], now: float
    ) -> dict[tuple[str, int], bool]:
        out: dict[tuple[str, int], bool] = {}
        for s_idx, s_uri, fresh_w in checks:
            if s_idx >= 0:
                out[(cid, s_idx)] = self._is_step_log_recently_modified_cached(
                    cid, s_idx, s_uri, now, fresh_sec=fresh_w
                )
        return out

    def _compute_heartbeat_locked(
        self,
        rt: ConversationRuntime,
        now: float,
        stall_sec: int,
        log_fresh_map: Optional[dict[tuple[str, int], bool]] = None,
    ) -> dict[str, Any]:
        raw_short = short_run_status(rt.raw_status)
        exec_short = short_run_status(rt.raw_executor_loop_status)
        is_root_waiting_user = (raw_short == "WAITING_FOR_USER" or exec_short == "WAITING_FOR_USER")
        is_loop_running = (
            exec_short in ("RUNNING", "QUEUED")
            or raw_short in ("RUNNING", "QUEUED")
            or rt.ts_active_step_idx >= 0
        )
        is_terminal_stop = raw_short in ("CANCELED", "CANCELLED", "ABORTED", "ERROR", "FAILED")
        bg_step_indices = self._collect_bg_step_indices_locked(rt)
        max_known_idx = self._max_known_step_idx_locked(rt)
        turn_floor_idx = self._current_turn_floor_idx_locked(rt)
        running_step: Optional[dict[str, Any]] = (
            self._find_foreground_running_step_locked(rt, include_planner=True) if is_loop_running else None
        )
        waiting_step: Optional[dict[str, Any]] = None
        latest_step: Optional[dict[str, Any]] = None
        seen_done_planner = False

        for idx in sorted(rt.steps_by_idx.keys(), reverse=True):
            st = rt.steps_by_idx[idx]
            if latest_step is None:
                latest_step = st
            st_status = st.get("status")
            if (
                waiting_step is None
                and not is_terminal_stop
                and not seen_done_planner
                and st_status in ("WAITING", "WAITING_FOR_USER")
                and st.get("type") != "CORTEX_STEP_TYPE_USER_INPUT"
                and idx not in bg_step_indices
                and idx >= rt.last_user_input_idx
            ):
                waiting_step = st
            if st.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE" and st_status == "DONE":
                seen_done_planner = True

        if rt.db_recent_steps:
            if latest_step is None:
                d0 = rt.db_recent_steps[0]
                latest_step = {
                    "idx": d0["idx"], "type": str(d0["stepTypeInt"]),
                    "status": "RUNNING" if (is_loop_running and d0["statusInt"] == 2 and not d0.get("isDaemon") and int(d0["idx"]) >= turn_floor_idx) else "DONE",
                    "toolName": d0.get("toolName") or "", "startedAt": d0.get("startedAt") or d0.get("createdAt") or 0.0,
                }
            if running_step is None and is_loop_running:
                for dbs in rt.db_recent_steps:
                    d_idx = int(dbs["idx"])
                    known_st = rt.steps_by_idx.get(d_idx)
                    if isinstance(known_st, dict) and (known_st.get("status") != "RUNNING" or bool(known_st.get("isDaemon"))):
                        continue
                    if bool(dbs.get("isDaemon")):
                        continue
                    if dbs.get("statusInt") == 2 and d_idx not in bg_step_indices and d_idx >= turn_floor_idx:
                        running_step = {
                            "idx": d_idx, "type": str(dbs["stepTypeInt"]), "status": "RUNNING",
                            "toolName": dbs.get("toolName") or "", "startedAt": dbs.get("startedAt") or dbs.get("createdAt") or 0.0,
                        }
                        break

        is_waiting_approval = bool(
            (waiting_step is not None and running_step is None and rt.ts_active_step_idx < 0)
            or (is_root_waiting_user and not is_terminal_stop and running_step is None and rt.ts_active_step_idx < 0)
        )
        target = running_step or (waiting_step if is_waiting_approval else None) or latest_step
        step_idx = int(target["idx"]) if target else max(0, max_known_idx)
        raw_tool_name = str(target.get("toolName") or "").strip() if target else ""
        step_type = str(target.get("type") or "") if target else ""
        is_running_tool = bool(running_step is not None and raw_tool_name and step_type != "CORTEX_STEP_TYPE_PLANNER_RESPONSE")

        if is_waiting_approval:
            tool_name = raw_tool_name or "等待审批"
            wait_started_at = float(target.get("startedAt") or target.get("createdAt") or 0.0) if target else 0.0
            elapsed = max(0.0, round(now - wait_started_at, 1)) if wait_started_at > 0 else 0.0
        elif is_loop_running:
            if raw_tool_name and running_step is not None:
                tool_name = raw_tool_name
            elif step_type == "CORTEX_STEP_TYPE_PLANNER_RESPONSE" or rt.ts_active_step_idx >= 0:
                tool_name = self._active_planner_phase_label(rt)
            else:
                tool_name = raw_tool_name or "执行中"
            if running_step is not None:
                eff_started_ts = float(running_step.get("startedAt") or running_step.get("createdAt") or 0.0)
                running_since = float(running_step.get("runningSince") or 0.0)
                started_at = max(eff_started_ts, running_since) if running_since > 0 else eff_started_ts
            elif rt.ts_active_step_idx >= 0 and rt.ts_step_created_at > 0.0:
                started_at = rt.ts_step_created_at
            elif rt.ts_waiting_turn_since > 0.0:
                started_at = rt.ts_waiting_turn_since
            else:
                started_at = 0.0
            elapsed = max(0.0, round(now - started_at, 1)) if started_at > 0 else 0.0
        else:
            tool_name, elapsed = "", 0.0

        effective_running = bool(is_loop_running or is_waiting_approval)
        retry_alert: Optional[dict[str, Any]] = None
        err = rt.db_latest_error
        term_upper = (rt.latest_termination_reason or "").upper()
        is_normal_term = any(kw in term_upper for kw in self._NORMAL_TERM_KEYWORDS)
        is_user_canceled_status = raw_short in ("CANCELED", "CANCELLED")
        if (
            isinstance(err, dict)
            and not bool(err.get("isServerRestart", False))
            and not is_user_canceled_status
            and (effective_running or not is_normal_term)
        ):
            raw_consec = err.get("consecutiveErrors")
            if raw_consec is None or int(raw_consec or 0) > 0:
                err_idx = int(err.get("stepIndex", -1))
                has_recovered = False
                if rt.ts_active_step_idx > err_idx and (
                    rt.ts_est_output_tokens > 0 or rt.ts_phase in ("thinking", "generating", "tool_calls")
                ):
                    has_recovered = True
                if not has_recovered:
                    for k, v in rt.steps_by_idx.items():
                        if k <= err_idx or not isinstance(v, dict):
                            continue
                        vstype = str(v.get("type") or "").upper()
                        vstat = str(v.get("status") or "").upper()
                        if "ERROR" in vstype or vstat in ("ERROR", "FAILED", "CANCELED", "CANCELLED", "ABORTED"):
                            continue
                        if vstype == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
                            if vstat in ("RUNNING", "GENERATING", "DONE") or bool(v.get("hasContent")):
                                has_recovered = True
                                break
                        elif vstype not in (
                            "CORTEX_STEP_TYPE_USER_INPUT",
                            "CORTEX_STEP_TYPE_CHECKPOINT",
                            "CORTEX_STEP_TYPE_CONVERSATION_HISTORY",
                            "CORTEX_STEP_TYPE_EPHEMERAL_MESSAGE",
                        ):
                            if v.get("toolName") or vstat in ("RUNNING", "GENERATING", "WAITING", "WAITING_FOR_USER", "DONE"):
                                has_recovered = True
                                break
                if not has_recovered and rt.db_recent_steps:
                    for d in rt.db_recent_steps:
                        d_idx = int(d["idx"])
                        if d_idx <= err_idx:
                            continue
                        d_type = int(d.get("stepTypeInt") or 0)
                        d_stat = int(d.get("statusInt") or 0)
                        if d_type == 15 and d_stat in (2, 3):
                            has_recovered = True
                            break
                        if d_type not in (14, 17) and d_stat in (2, 3) and bool(d.get("toolName")):
                            has_recovered = True
                            break
                if err_idx > rt.last_user_input_idx and not has_recovered:
                    origin_turn = (
                        int(err["originTurnIdx"])
                        if err.get("originTurnIdx") is not None
                        else max(rt.last_user_input_idx, turn_floor_idx)
                    )
                    consec_val = int(raw_consec) if raw_consec is not None else 1
                    retry_alert = {
                        **err,
                        "active": True,
                        "attempt": int(err.get("attempt") or 0),
                        "consecutiveErrors": consec_val,
                        "originTurnIdx": origin_turn,
                        "isTimeout": bool(err.get("isTimeout", False)),
                        "isServerRestart": False,
                        "isInterrupted": bool(err.get("isInterrupted", False)),
                        "ageSec": max(0.0, round(now - float(err.get("timestamp") or now), 1)),
                        "message": str(err.get("message") or ""),
                    }
        is_stalled = bool(
            not is_waiting_approval
            and is_running_tool
            and stall_sec > 0
            and elapsed >= stall_sec
            and not _is_intentional_wait_step(raw_tool_name, step_type)
        )
        fg_run_idx = int(running_step["idx"]) if is_running_tool and running_step is not None else -1
        if is_stalled and fg_run_idx >= 0 and running_step is not None:
            step_log_uri = str(
                running_step.get("logUri")
                or (rt.steps_by_idx.get(fg_run_idx) or {}).get("logUri")
                or ""
            ).strip()
            if _normalize_tool_key(raw_tool_name) == "run_command" or _normalize_tool_key(step_type) == "run_command" or step_log_uri:
                if log_fresh_map is not None and (rt.cid, fg_run_idx) in log_fresh_map:
                    fg_log_fresh = log_fresh_map[(rt.cid, fg_run_idx)]
                else:
                    fg_log_fresh = self._is_step_log_recently_modified_cached(rt.cid, fg_run_idx, step_log_uri, now, fresh_sec=30.0)
                if fg_log_fresh:
                    is_stalled = False

        raw_bg_script = self._find_active_bg_script_locked(rt, exclude_step_idx=fg_run_idx)
        bg_script: Optional[dict[str, Any]] = None
        bg_is_stalled = False
        if isinstance(raw_bg_script, dict):
            bg_script = dict(raw_bg_script)
            bg_start = float(bg_script.get("startedAt") or 0.0)
            bg_idx = int(bg_script.get("stepIndex", -1))
            bg_elapsed = max(0.0, round(now - bg_start, 1)) if bg_start > 0.0 else 0.0
            bg_script["elapsedSec"] = bg_elapsed
            bg_script["isStalled"] = False
            bg_stall_threshold = max(int(stall_sec) * 2, 120) if stall_sec > 0 else 0
            if (
                not effective_running
                and not retry_alert
                and bg_script.get("kind") == "command"
                and bg_stall_threshold > 0
                and bg_start > 0.0
                and (now - bg_start) >= bg_stall_threshold
            ):
                bg_log_uri = str(bg_script.get("logUri") or "")
                if log_fresh_map is not None and (rt.cid, bg_idx) in log_fresh_map:
                    bg_log_fresh = log_fresh_map[(rt.cid, bg_idx)]
                elif bg_idx >= 0 or bg_log_uri:
                    bg_log_fresh = self._is_step_log_recently_modified_cached(rt.cid, bg_idx, bg_log_uri, now, fresh_sec=60.0)
                else:
                    bg_log_fresh = False
                if not bg_log_fresh:
                    bg_is_stalled = True
                    bg_script["isStalled"] = True
                    is_stalled = True
                    tool_name = f"{bg_script.get('toolName') or 'run_command'} (后台)"
                    elapsed = bg_elapsed
                    if bg_idx >= 0:
                        step_idx = bg_idx

        if retry_alert and not effective_running:
            if retry_alert.get("isTimeout"):
                hb_state, hb_label = "interrupted", "超时中断"
            elif retry_alert.get("isInterrupted"):
                hb_state, hb_label = "interrupted", "断流待续"
            else:
                hb_state, hb_label = "error", "执行异常"
        elif retry_alert:
            hb_state, hb_label = "retrying", "重试告警"
        elif is_waiting_approval:
            hb_state, hb_label = "waiting_approval", "等待审批"
        elif is_stalled:
            hb_state, hb_label = "stalled", ("后台超时" if bg_is_stalled else "响应超时")
        elif is_loop_running:
            hb_state, hb_label = "running", "运行中"
        else:
            hb_state, hb_label = "idle", "空闲"

        hb_out: dict[str, Any] = {
            "state": hb_state, "stateLabel": hb_label, "isRunning": effective_running, "rootIsRunning": effective_running,
            "isStalled": is_stalled, "stepIndex": step_idx,
            "runningStepIndex": fg_run_idx,
            "toolName": tool_name, "elapsedSec": elapsed, "retryAlert": retry_alert,
        }
        if bg_script is not None:
            hb_out["bgScript"] = bg_script
        return hb_out

    def _spawn_snapshot_probes_async(
        self, probe_targets: list[str], child_probe_targets: Optional[list[tuple[str, str]]] = None
    ) -> None:
        c_targets = list(child_probe_targets or [])
        if not probe_targets and not c_targets:
            return
        with self._lock:
            if self._probe_worker_busy:
                return
            self._probe_worker_busy = True

        def _worker(targets: list[str], child_targets: list[tuple[str, str]]) -> None:
            try:
                for p_cid in targets:
                    if self._stop_event.is_set():
                        break
                    self._probe_snapshot_once(p_cid, timeout=1.5)
                for r_cid, sub_cid in child_targets:
                    if self._stop_event.is_set():
                        break
                    self._probe_child_usage_once(r_cid, sub_cid, timeout=1.8)
            finally:
                with self._lock:
                    self._probe_worker_busy = False

        threading.Thread(target=_worker, args=(list(probe_targets), c_targets), name="flow-pilot-snap-probe", daemon=True).start()

    def _background_loop(self) -> None:
        while not self._stop_event.is_set():
            self._sync_wake_event.clear()
            now = time.time()
            has_active = False
            try:
                self._load_config_safe(force=False)
                self._load_model_calibrations(force=False)
                self._sync_models_if_needed()
                fs_mut, _ = self._consume_fs_mutation_signal()
                if self._sync_trajectories(force=False, reason="fs" if fs_mut else "poll"):
                    self._gc_disk_state_files()

                probe_targets: list[str] = []
                child_probe_targets: list[tuple[str, str]] = []
                with self._lock:
                    cfg = dict(self.config)
                    stall_sec = int(cfg.get("stallThresholdSec") or 90)
                    monitored_roots = [cid for cid in self._streams.keys() if not self._is_subagent_cid(cid)]
                    if self._active_main_cid and self._active_main_cid not in monitored_roots:
                        monitored_roots.insert(0, self._active_main_cid)

                    if self._next_recent_expiry_at > 0.0 and now >= self._next_recent_expiry_at:
                        self._next_recent_expiry_at = 0.0
                        self._bump_rev_locked()

                    bg_scan_cids: list[str] = []
                    bg_seen: set[str] = set()
                    hot_root_ids: set[str] = set((cfg.get("hud") or {}).get("pinnedIds") or [])
                    if self._active_main_cid:
                        hot_root_ids.add(self._active_main_cid)

                    active_tick_fp: list[tuple[Any, ...]] = []
                    for r_cid, r_rt in self._runtimes.items():
                        is_rt_active = bool(r_rt.composite_running or r_rt.ts_active_step_idx >= 0)
                        if is_rt_active:
                            has_active = True
                            ts_snap = self._compute_token_speed_locked(r_rt, now)
                            hb_stalled = False
                            if stall_sec > 0:
                                fg = self._find_foreground_running_step_locked(r_rt, include_planner=False)
                                if isinstance(fg, dict):
                                    fg_tool = str(fg.get("toolName") or "").strip()
                                    fg_type = str(fg.get("type") or "")
                                    if fg_tool and not fg.get("isDaemon") and not _is_intentional_wait_step(fg_tool, fg_type):
                                        eff_s = float(fg.get("startedAt") or fg.get("createdAt") or 0.0)
                                        run_s = float(fg.get("runningSince") or 0.0)
                                        st_ts = max(eff_s, run_s) if run_s > 0.0 else eff_s
                                        if st_ts > 0.0 and (now - st_ts) >= stall_sec:
                                            hb_stalled = True
                            active_tick_fp.append((
                                r_cid,
                                bool(ts_snap.get("active")),
                                str(ts_snap.get("phase") or ""),
                                float(ts_snap.get("displayTokensPerSec") or 0.0),
                                hb_stalled,
                            ))
                        if (r_rt.composite_running or r_rt.db_bg_script is not None) and not self._is_subagent_cid(r_cid):
                            bg_scan_cids.append(r_cid)
                            bg_seen.add(r_cid)
                        if r_rt.composite_running or (r_rt.just_finished_at > 0 and (now - r_rt.just_finished_at) <= self.RECENT_HOLD_SEC):
                            hot_root_ids.add(r_cid)

                    if has_active:
                        tick_fp_t = tuple(sorted(active_tick_fp))
                        if (
                            self._last_bg_active_tick_fp is not None
                            and tick_fp_t != self._last_bg_active_tick_fp
                            and self._state_rev == self._last_built_rev
                        ):
                            self._bump_rev_locked()
                        self._last_bg_active_tick_fp = tick_fp_t
                    else:
                        self._last_bg_active_tick_fp = None

                    idle_roots: list[tuple[float, str, int]] = []
                    sub_entries: list[tuple[str, dict[str, Any], int]] = []
                    for k, s in self._trajectories.items():
                        if not isinstance(s, dict) or bool(s.get("killed", False)):
                            continue
                        s_cnt = int(s.get("stepCount") or 0)
                        if self._is_subagent_summary(k, s):
                            if s_cnt > 0:
                                sub_entries.append((k, s, s_cnt))
                        else:
                            idle_roots.append((self._summary_sort_ts(s), k, s_cnt))
                    idle_roots.sort(key=lambda x: x[0], reverse=True)

                    for sort_ts, r_cid, s_cnt in idle_roots[: self.MAX_BG_SCAN_ROOTS]:
                        if r_cid in bg_seen or s_cnt <= 0:
                            continue
                        if sort_ts > 0.0 and (now - sort_ts) > self.BG_SCAN_RECENT_WINDOW_SEC:
                            break
                        bg_scan_cids.append(r_cid)
                        bg_seen.add(r_cid)
                        if len(bg_scan_cids) >= self.MAX_BG_SCAN_ROOTS:
                            break
                    for _, r_cid, s_cnt in idle_roots[: self.MAX_SNAPSHOT_ROOTS]:
                        if r_cid in self._streams or r_cid in self._probing_cids or s_cnt <= 0:
                            continue
                        rt = self._get_or_create_runtime(r_cid)
                        if rt.snapshot_probed_steps < s_cnt:
                            probe_targets.append(r_cid)
                            if len(probe_targets) >= 3:
                                break

                    hot_root_ids.update(r_cid for _, r_cid, _ in idle_roots[:6])

                    sub_bg_candidates: list[tuple[float, str]] = []
                    waiting_sub_candidates: list[tuple[float, str]] = []
                    for k, s, s_cnt in sub_entries:
                        kind = self._classify_agent_kind(k, s)
                        if kind == "swarm_monitor":
                            continue
                        if self._resolve_root_cid(k, self._trajectories) not in hot_root_ids:
                            continue
                        sub_mod = parse_iso_ts(str(s.get("lastModifiedTime") or s.get("createdTime") or ""))
                        if k not in bg_seen and not (sub_mod > 0.0 and (now - sub_mod) > self.BG_SCAN_RECENT_WINDOW_SEC):
                            sub_bg_candidates.append((sub_mod, k))
                        if (
                            kind == "swarm_worker"
                            and k not in self._streams
                            and k not in self._probing_cids
                            and short_run_status(str(s.get("status") or "")) not in ACTIVE_SUBAGENT_STATUSES
                            and "TERMINAL_CUSTOM_HOOK" in str(s.get("latestExecutorTerminationReason") or "").upper()
                        ):
                            sub_rt = self._get_or_create_runtime(k)
                            if sub_rt.snapshot_probed_steps < s_cnt and (sub_rt.tokens_used == 0 or sub_rt.ts_last_completed is None):
                                waiting_sub_candidates.append((sub_mod, k))

                    sub_bg_candidates.sort(key=lambda x: x[0], reverse=True)
                    for _, sub_bg_cid in sub_bg_candidates[: self.MAX_SUB_BG_SCAN]:
                        bg_scan_cids.append(sub_bg_cid)
                        bg_seen.add(sub_bg_cid)
                    waiting_sub_candidates.sort(key=lambda x: x[0], reverse=True)
                    probe_targets.extend(w_cid for _, w_cid in waiting_sub_candidates[:2])
                    probe_targets_set = set(probe_targets)

                    child_check_roots: list[str] = []
                    seen_cr: set[str] = set()
                    for cand_r in ([self._active_main_cid] if self._active_main_cid else []) + [r_cid for _, r_cid, _ in idle_roots[:24]]:
                        if cand_r and cand_r not in seen_cr:
                            seen_cr.add(cand_r)
                            child_check_roots.append(cand_r)
                    for r_cid in child_check_roots:
                        r_rt = self._get_or_create_runtime(r_cid)
                        for sub_cid, sub_steps in self._raw_all_children_by_root.get(r_cid, []):
                            if (
                                sub_steps > 0
                                and sub_cid not in r_rt.archived_children_usage
                                and sub_cid not in self._streams
                                and sub_cid not in self._probed_historical_subs
                                and sub_cid not in self._probing_cids
                                and sub_cid not in probe_targets_set
                            ):
                                child_probe_targets.append((r_cid, sub_cid))
                                if len(child_probe_targets) >= 4:
                                    break
                        if len(child_probe_targets) >= 4:
                            break

                for sc_cid in bg_scan_cids:
                    self._scan_sqlite_bg_script(sc_cid)
                for cid in monitored_roots[: self.MAX_ACTIVE_STREAMS]:
                    self._diagnose_sqlite_if_needed(cid, now, stall_sec)
                if (probe_targets or child_probe_targets) and not self._stop_event.is_set():
                    self._spawn_snapshot_probes_async(probe_targets, child_probe_targets)
            except Exception:
                pass
            wait_sec = 0.50 if has_active else 2.5
            with self._lock:
                if self._pending_fs_mutation:
                    rem_fs = self.SYNC_FS_MIN_INTERVAL_SEC - (time.time() - self._last_trajectories_sync_at) + 0.02
                    wait_sec = min(wait_sec, max(0.05, rem_fs))
            self._sync_wake_event.wait(timeout=wait_sec)

    def _is_subagent_cid(self, cid: str) -> bool:
        with self._lock:
            if cid in self._raw_parent_map:
                return True
            s = self._trajectories.get(cid)
        return self._is_subagent_summary(cid, s) if isinstance(s, dict) else False

    def _is_step_log_recently_modified(
        self, cid: str, step_idx: int, step_log_uri: str, now: float, fresh_sec: float = 30.0
    ) -> bool:
        candidates: list[Path] = []
        if step_log_uri and (lp := _file_uri_to_local_path(step_log_uri)) is not None:
            candidates.append(lp)
        fallback = self.app_data_dir / "brain" / safe_cid(cid) / ".system_generated" / "tasks" / f"task-{step_idx}.log"
        if fallback not in candidates:
            candidates.append(fallback)
        for cand in candidates:
            try:
                st = os.stat(str(cand))
                if max(0.0, now - float(st.st_mtime)) < fresh_sec:
                    return True
            except OSError:
                continue
        return False

    def _is_step_log_recently_modified_cached(
        self, cid: str, step_idx: int, step_log_uri: str, now: float, fresh_sec: float = 30.0
    ) -> bool:
        key = (cid, int(step_idx), step_log_uri or "", int(round(fresh_sec)))
        now_m = time.monotonic()
        with self._lock:
            cached = self._log_fresh_cache.get(key)
            if cached is not None and (now_m - cached[0]) < 2.0 and abs(now - cached[1]) < 2.0:
                return cached[2]
        res = self._is_step_log_recently_modified(cid, step_idx, step_log_uri, now, fresh_sec=fresh_sec)
        with self._lock:
            if len(self._log_fresh_cache) > 128:
                self._log_fresh_cache.clear()
            self._log_fresh_cache[key] = (now_m, now, res)
        return res

    # ------------------------------------------------------------------
    # Public HTTP API Operations & Shared Finalization
    # ------------------------------------------------------------------

    @classmethod
    def finalize_tasks_and_summary(cls, tasks_out: list[dict[str, Any]]) -> dict[str, Any]:
        """Deterministic MAX_RECENT_CARDS cap, anti-jitter bucket sort, and 3-tier global summary."""
        recent_tasks = [t for t in tasks_out if t.get("bucket") == "RECENT"]
        if len(recent_tasks) > cls.MAX_RECENT_CARDS:
            recent_tasks.sort(key=lambda x: float(x.get("justFinishedAt") or 0.0), reverse=True)
            demote_keys = {t.get("taskKey") for t in recent_tasks[cls.MAX_RECENT_CARDS :]}
            for t in tasks_out:
                if t.get("taskKey") in demote_keys:
                    t["bucket"] = "IDLE"

        bucket_rank = {"ALERT": 0, "RUNNING": 2, "RECENT": 3, "IDLE": 4}

        def _sort_key(item: dict[str, Any]) -> tuple[int, float, str]:
            b = str(item.get("bucket") or "IDLE")
            rank = 0 if b == "ALERT" else (1 if item.get("isPinned") else bucket_rank.get(b, 4))
            if rank <= 2:
                return (rank, float(item.get("runBatchStartedAt") or item.get("lastModifiedAt") or 0.0), str(item.get("taskKey") or ""))
            if rank == 3:
                return (rank, -float(item.get("justFinishedAt") or 0.0), str(item.get("taskKey") or ""))
            return (rank, -float(item.get("lastModifiedAt") or 0.0), str(item.get("taskKey") or ""))

        tasks_out.sort(key=_sort_key)
        total_live_tps = round(sum(float(t.get("effectiveLiveTps") or 0.0) for t in tasks_out), 1)
        return {
            "runningCount": sum(1 for t in tasks_out if t.get("bucket") == "RUNNING"),
            "alertCount": sum(1 for t in tasks_out if t.get("bucket") == "ALERT"),
            "recentCount": sum(1 for t in tasks_out if t.get("bucket") == "RECENT"),
            "idleCount": sum(1 for t in tasks_out if t.get("bucket") == "IDLE"),
            "totalLiveTps": total_live_tps,
            "anyWaitingTtft": any(
                bool(t.get("anyWaitingTtft"))
                or bool((t.get("tokenSpeed") or {}).get("active") and (t.get("tokenSpeed") or {}).get("phase") == "waiting_ttft")
                or bool((t.get("swarmSummary") or {}).get("anyWaitingTtft"))
                for t in tasks_out
            ),
            "anyGenerating": bool(
                total_live_tps > 0.0
                and any(
                    bool(t.get("anyGenerating"))
                    or bool(
                        (t.get("tokenSpeed") or {}).get("active")
                        and (t.get("tokenSpeed") or {}).get("phase") != "waiting_ttft"
                        and _live_tps(t.get("tokenSpeed")) > 0.0
                    )
                    or bool((t.get("swarmSummary") or {}).get("anyGenerating"))
                    for t in tasks_out
                )
            ),
        }

    def owns_conversation(self, conversation_id: str) -> bool:
        if not (cid := (conversation_id or "").strip()):
            return True
        with self._lock:
            if cid in self._trajectories:
                return True
        fs_mut, _ = self._consume_fs_mutation_signal()
        if self._sync_trajectories(force=False, reason="fs" if fs_mut else "hint"):
            with self._lock:
                if cid in self._trajectories:
                    return True
        return False

    def get_local_tasks_payload(self, hint_conversation_id: str = "", now_override: Optional[float] = None) -> dict[str, Any]:
        """Build complete multi-task telemetry snapshot for all Root Cascades on this Language Server."""
        self._load_config_safe(force=False)
        self._sync_models_if_needed()
        hint_cid = (hint_conversation_id or "").strip()
        fs_mutated, fs_sig_before = self._consume_fs_mutation_signal()
        with self._lock:
            hint_missing = bool(hint_cid and hint_cid not in self._trajectories and hint_cid not in self._checked_hints)
            need_inline_sync = (not self._poller_started) or (self._last_trajectories_sync_at <= 0.0) or hint_missing
        did_sync = False
        if need_inline_sync:
            sync_reason = "fs" if fs_mutated else ("hint" if hint_missing else "poll")
            did_sync = self._sync_trajectories(force=False, reason=sync_reason)
        elif fs_mutated:
            self._sync_wake_event.set()

        now = float(now_override) if now_override is not None else time.time()
        cfg_snap = self.get_config_snapshot()
        stall_sec = int(cfg_snap.get("stallThresholdSec") or 90)
        pinned_set = set((cfg_snap.get("hud") or {}).get("pinnedIds") or [])

        pre_build_fs_sig = self._compute_fast_fs_sig() if did_sync else fs_sig_before
        pre_build_conv_entries = self._compute_conv_entry_sig(conv_dir_ns=pre_build_fs_sig[0])

        with self._lock:
            if hint_cid:
                if len(self._checked_hints) > 128:
                    self._checked_hints.clear()
                self._checked_hints.add(hint_cid)
            summaries = dict(self._trajectories)

        children_by_root: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        root_entries: list[tuple[float, str, dict[str, Any]]] = []
        for cid, s in summaries.items():
            if not isinstance(s, dict):
                continue
            if self._is_subagent_summary(cid, s):
                if (r_cid := self._resolve_root_cid(cid, summaries)) and r_cid in summaries:
                    children_by_root.setdefault(r_cid, []).append((cid, s))
            elif not self.is_cid_known_removed(cid):
                root_entries.append((self._summary_sort_ts(s), cid, s))
        root_entries.sort(key=lambda x: x[0], reverse=True)

        selected_roots: list[tuple[float, str, dict[str, Any]]] = []
        for idx_pos, (sort_ts, cid, s) in enumerate(root_entries):
            with self._lock:
                rt_chk = self._runtimes.get(cid)
                is_hot = bool(rt_chk and (rt_chk.composite_running or (rt_chk.just_finished_at > 0 and (now - rt_chk.just_finished_at) <= self.RECENT_HOLD_SEC)))
            if idx_pos < 64 or is_hot or cid in pinned_set or cid == hint_cid or cid == self.default_env_cid:
                selected_roots.append((sort_ts, cid, s))

        tasks_out: list[dict[str, Any]] = []
        for sort_ts, cid, s in selected_roots:
            self._diagnose_sqlite_if_needed(cid, now, stall_sec)
            root_raw_stat = str(s.get("status") or "CASCADE_RUN_STATUS_IDLE")
            root_short_stat = short_run_status(root_raw_stat)
            root_mod = parse_iso_ts(str(s.get("lastModifiedTime") or ""))
            with self._lock:
                rt_pre = self._runtimes.get(cid)
                comp_run = bool(rt_pre and rt_pre.composite_running)
                root_live_active = self._is_root_live_active_locked(rt_pre, s, root_short_stat)

            subs, swarm_sum = self._build_task_subagents_and_swarm(
                root_cid=cid, child_entries=children_by_root.get(cid, []), root_is_running=root_live_active,
                root_last_mod=root_mod, now_eval=now, ensure_streams=(root_live_active or comp_run or cid in pinned_set),
                stall_sec=stall_sec,
            )

            rt = self._get_or_create_runtime(cid)
            with self._lock:
                hb_checks = self._collect_hb_log_checks_locked(rt, now, stall_sec)
            hb_log_fresh = self._resolve_hb_log_fresh_map(cid, hb_checks, now) if hb_checks else {}

            with self._lock:
                synced_lbl = (self._model_labels.get(rt.model_id) or self._model_labels.get(rt.model_id.upper()) or "") if rt.model_id else ""
                if _needs_model_label_refresh(rt.model_label) or (synced_lbl and synced_lbl != rt.model_label):
                    rt.model_label = self._resolve_model_label(rt.model_id, rt.model_name, rt.model_display_name)
                    if rt.model_name or rt.model_display_name:
                        rt.routed_model_label = self._resolve_routed_model_label_locked(
                            rt.model_name, rt.model_label, rt.model_display_name
                        )

                max_tok = (
                    rt.max_tokens
                    if (rt.max_tokens_exact and rt.max_tokens > 0)
                    else self._resolve_model_max_tokens_locked(rt.model_id, rt.model_name, rt.model_label)
                )
                used_tok = max(0, rt.tokens_used)
                pct = round((used_tok / max_tok) * 100.0, 1) if max_tok > 0 else 0.0
                grp = rt.groups if isinstance(rt.groups, dict) else {}
                groups_copy = {
                    "systemPrompt": max(0, int(grp.get("systemPrompt") or 0)),
                    "extensions": max(0, int(grp.get("extensions") or 0)),
                    "tools": max(0, int(grp.get("tools") or 0)),
                    "chatMessages": max(0, int(grp.get("chatMessages") or 0)),
                }
                compaction_cnt = max(0, int(rt.compaction_count))
                token_speed = self._compute_token_speed_locked(rt, now)
                merged_children = dict(rt.archived_children_usage)
                for sub_cid, _ in children_by_root.get(cid, []):
                    if (sub_rt := self._runtimes.get(sub_cid)) is not None and sub_rt.usage_by_gm:
                        merged_children[sub_cid] = _sum_usage_by_gm(sub_rt.usage_by_gm)
                token_usage = _build_root_token_usage(
                    rt.usage_by_gm,
                    rt.last_turn_usage,
                    merged_children,
                    cache_miss_streak=rt.cache_miss_streak,
                )
                hb = self._compute_heartbeat_locked(rt, now, stall_sec, log_fresh_map=hb_log_fresh)

                running_sub_cnt = int(swarm_sum.get("runningCount") or 0)
                error_sub_cnt = int(swarm_sum.get("errorCount") or 0)
                bg_script = hb.get("bgScript") if isinstance(hb.get("bgScript"), dict) else None
                if not hb["isRunning"] and running_sub_cnt > 0 and not hb.get("retryAlert") and not hb.get("isStalled"):
                    hb["state"] = "waiting_subagents"
                    hb["stateLabel"] = f"蜂群协作中 ({running_sub_cnt} 运行)" if swarm_sum.get("isSwarm") else f"等待子代理 ({running_sub_cnt} 运行)"
                elif not hb["isRunning"] and bg_script is not None and not hb.get("retryAlert") and not hb.get("isStalled"):
                    hb["state"] = "running_bg_script"
                    hb["stateLabel"] = "工作流运行中" if bg_script["kind"] == "workflow" else "后台脚本运行中"

                root_live_now = self._is_root_live_active_locked(rt, s, root_short_stat)
                effective_status = "RUNNING" if (hb["isRunning"] or running_sub_cnt > 0 or bg_script is not None or root_live_now) else (
                    "IDLE" if (rt.stream_connected and rt.stream_initial_seen and root_short_stat in ACTIVE_ROOT_STATUSES) else root_short_stat
                )
                self_live_tps = _live_tps(token_speed)
                effective_live_tps = round(self_live_tps + float(swarm_sum.get("liveTokensPerSec") or 0.0), 1)
                ra_obj = hb.get("retryAlert")
                has_hb_alert = (
                    bool(isinstance(ra_obj, dict) and not ra_obj.get("isServerRestart"))
                    or hb.get("state") in ("interrupted", "retrying", "error")
                    or bool(hb.get("isStalled"))
                )
                is_recent = rt.just_finished_at > 0.0 and rt.just_finished_at > rt.dismissed_recent_at and (now - rt.just_finished_at) <= self.RECENT_HOLD_SEC

                if has_hb_alert or error_sub_cnt > 0:
                    bucket = "ALERT"
                elif effective_status == "RUNNING" or bool(token_speed.get("active")):
                    bucket = "RUNNING"
                elif is_recent:
                    bucket = "RECENT"
                else:
                    bucket = "IDLE"

                if bucket == "RUNNING" and rt.run_batch_started_at <= 0.0:
                    rt.run_batch_started_at = (bg_script["startedAt"] if bg_script and bg_script.get("startedAt") else 0.0) or rt.last_modified_at or sort_ts or now

                tasks_out.append({
                    "taskKey": f"{self.ls_instance_id}:{cid}", "lsInstanceId": self.ls_instance_id, "conversationId": cid,
                    "title": rt.title or self._extract_title(cid, s),
                    "workspaceName": rt.workspace_name, "workspacePath": rt.workspace_path, "branchName": rt.branch_name,
                    "isPinned": cid in pinned_set, "bucket": bucket,
                    "runBatchStartedAt": round(rt.run_batch_started_at, 3) if rt.run_batch_started_at > 0 else 0.0,
                    "justFinishedAt": round(rt.just_finished_at, 3) if rt.just_finished_at > 0 else 0.0,
                    "finishedAgoSec": round(max(0.0, now - rt.just_finished_at), 1) if rt.just_finished_at > 0 else 0.0,
                    "lastModifiedAt": round(rt.last_modified_at or sort_ts, 3),
                    "modelLabel": rt.model_label, "routedModelLabel": rt.routed_model_label,
                    "status": effective_status, "rootStatus": root_short_stat,
                    "streamConnected": rt.stream_connected, "effectiveLiveTps": effective_live_tps,
                    "anyWaitingTtft": bool((token_speed.get("active") and token_speed.get("phase") == "waiting_ttft") or swarm_sum.get("anyWaitingTtft")),
                    "anyGenerating": bool((token_speed.get("active") and token_speed.get("phase") != "waiting_ttft" and self_live_tps > 0.0) or swarm_sum.get("anyGenerating")),
                    "context": {
                        "used": used_tok, "max": max_tok, "percent": pct, "groups": groups_copy,
                        "compactionCount": compaction_cnt,
                        "estimatedAfterCompaction": bool(rt.estimated_after_compaction),
                        "topChildren": list(rt.context_top_children),
                    },
                    "tokenSpeed": token_speed, "tokenUsage": token_usage, "heartbeat": hb, "swarmSummary": swarm_sum, "activeSubagents": subs,
                })

        summary_block = self.finalize_tasks_and_summary(tasks_out)
        fast_io_cfg = _sanitize_fast_io_config(cfg_snap.get("fastIo"))
        fast_io_stats = self.get_fast_io_stats()
        recent_expiries = [float(t["justFinishedAt"]) + self.RECENT_HOLD_SEC for t in tasks_out if t.get("bucket") == "RECENT" and float(t.get("justFinishedAt") or 0.0) > 0.0]

        state_fp = (
            summary_block["runningCount"],
            summary_block["alertCount"],
            summary_block["recentCount"],
            summary_block["idleCount"],
            summary_block["totalLiveTps"],
            bool(summary_block["anyWaitingTtft"]),
            bool(summary_block["anyGenerating"]),
            tuple(
                (t["taskKey"], t["bucket"], t["status"], t["title"], t["workspaceName"], t["branchName"], t["modelLabel"], t["routedModelLabel"], t["isPinned"],
                 t["effectiveLiveTps"],
                 t["context"]["used"], t["context"]["max"], t["context"]["compactionCount"], t["context"]["estimatedAfterCompaction"],
                 tuple((c["group"], c["name"], c["tokens"]) for c in t["context"]["topChildren"]),
                 bool(t["tokenSpeed"]["active"]), t["tokenSpeed"]["phase"], t["tokenSpeed"]["displayTokensPerSec"],
                 t["tokenSpeed"]["outputTokens"], t["tokenSpeed"]["isExact"], t["tokenSpeed"]["ttftMs"],
                 t["tokenUsage"]["netTokens"], t["tokenUsage"]["cacheReadTokens"], t["tokenUsage"]["lastTurnCacheHitPct"], t["tokenUsage"]["cacheMissStreak"],
                 t["heartbeat"]["state"], t["heartbeat"]["toolName"], t["heartbeat"]["stepIndex"], t["heartbeat"].get("runningStepIndex", -1),
                 bool(t["heartbeat"]["retryAlert"]), bool(t["heartbeat"].get("isStalled")),
                 bool((t["heartbeat"].get("bgScript") or {}).get("isStalled")),
                 t["swarmSummary"]["totalCount"], t["swarmSummary"]["runningCount"], t["swarmSummary"]["waitingCount"], t["swarmSummary"]["errorCount"],
                 tuple(
                     (sub_i.get("conversationId"), sub_i.get("state"), bool(sub_i.get("isStalled")), sub_i.get("currentTool"),
                      (sub_i.get("tokenSpeed") or {}).get("displayTokensPerSec", 0.0))
                     for sub_i in (t.get("activeSubagents") or [])
                 ))
                for t in tasks_out
            ),
            cfg_snap,
            fast_io_stats,
        )
        stalled_cnt = sum(
            1
            for t in tasks_out
            if bool((t.get("heartbeat") or {}).get("isStalled"))
            or any(bool(sub_i.get("isStalled")) for sub_i in (t.get("activeSubagents") or []))
        )

        with self._lock:
            self._watched_fs_sig = pre_build_fs_sig
            self._watched_conv_entry_sig = pre_build_conv_entries
            self._last_running_count = int(summary_block["runningCount"])
            self._last_stalled_count = stalled_cnt
            self._last_total_live_tps = float(summary_block["totalLiveTps"])
            self._next_recent_expiry_at = min(recent_expiries) if recent_expiries else 0.0
            self._last_built_at = time.time()
            self._has_built_once = True

            if self._state_rev <= 0:
                self._last_calm_fp = state_fp
                self._bump_rev_locked()
            elif self._state_rev != self._last_built_rev:
                self._last_calm_fp = state_fp
            elif state_fp != self._last_calm_fp:
                self._last_calm_fp = state_fp
                self._bump_rev_locked()
            self._last_built_rev = self._state_rev
            rev = self._state_rev

        return {
            "ok": True, "rev": rev, "timestamp": now, "lsInstanceId": self.ls_instance_id,
            "summary": summary_block, "config": cfg_snap, "fastIoConfig": fast_io_cfg, "fastIoStats": fast_io_stats, "tasks": tasks_out,
        }

    def dismiss_recent(self, conversation_id: str) -> dict[str, Any]:
        if not (cid := (conversation_id or "").strip()):
            return {"ok": True, "conversationId": ""}
        now = time.time()
        with self._lock:
            rt = self._get_or_create_runtime(cid)
            rt.dismissed_recent_at = max(now, rt.just_finished_at + 0.001)
            self._bump_rev_locked()
        self._persist_runtime_state(cid)
        return {"ok": True, "conversationId": cid}

    def resolve_target_cid(self, requested_cid: str = "", hint_cid: str = "", force_sync: bool = False) -> tuple[str, bool]:
        req = (requested_cid or "").strip()
        if req and req.lower() not in ("auto", "active", "__auto__", "null", "undefined"):
            return req, False
        fs_mut, _ = self._consume_fs_mutation_signal()
        self._sync_trajectories(force=force_sync, reason="force" if force_sync else ("fs" if fs_mut else "poll"))
        hint = (hint_cid or "").strip()
        with self._lock:
            if hint and hint.lower() not in ("auto", "active", "__auto__", "null", "undefined"):
                if isinstance(s_hint := self._trajectories.get(hint), dict):
                    meta = s_hint.get("trajectoryMetadata") if isinstance(s_hint.get("trajectoryMetadata"), dict) else {}
                    hint_root = str(meta.get("rootConversationId") or meta.get("parentConversationId") or hint).strip()
                    active_rt = self._runtimes.get(self._active_main_cid)
                    active_s = self._trajectories.get(self._active_main_cid, {})
                    active_running = bool((active_rt and active_rt.composite_running) or short_run_status(str(active_s.get("status") or "")) in ACTIVE_ROOT_STATUSES)
                    if not active_running and hint_root in self._trajectories:
                        return hint_root, True
            cid = self._active_main_cid or (hint if hint in self._trajectories else "") or (self.default_env_cid if self.default_env_cid in self._trajectories else "")
        return cid, True

    def _build_send_user_payload(self, cid: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        payload: dict[str, Any] = {"cascadeId": cid, "items": items, "deliveryStrategy": "MESSAGE_DELIVERY_STRATEGY_NEXT_INVOCATION"}
        with self._lock:
            rt, s = self._runtimes.get(cid), self._trajectories.get(cid)
            total_steps = max(rt.total_steps if rt else 0, int(s.get("stepCount") or 0) if isinstance(s, dict) else 0)
            model_id = (rt.model_id if rt and rt.model_id else "") or self._default_model_id
        if total_steps == 0 and items:
            planner_cfg: dict[str, Any] = {
                "conversational": {"plannerMode": "CONVERSATIONAL_PLANNER_MODE_DEFAULT", "agenticMode": True},
            }
            if model_id:
                planner_cfg["requestedModel"] = {"model": model_id}
            payload["cascadeConfig"] = {"plannerConfig": planner_cfg}
        return payload

    def _send_agent_or_user_msg(self, cid: str, title: str, text: str) -> tuple[dict[str, Any], str]:
        try:
            resp = self.rpc_call("SendAgentMessage", {"recipient": cid, "senderId": "agy-flow-pilot", "displayTitle": title, "content": text}, timeout=4.0)
            return resp, "SendAgentMessage"
        except Exception:
            resp = self.rpc_call("SendUserCascadeMessage", self._build_send_user_payload(cid, [{"text": text}]), timeout=5.0)
            return resp, "SendUserCascadeMessage"

    def _resolve_running_step_indices(self, cid: str, explicit_step_index: Optional[int]) -> list[int]:
        if isinstance(explicit_step_index, int) and explicit_step_index >= 0:
            return [explicit_step_index]
        with self._lock:
            if (rt := self._runtimes.get(cid)) is not None:
                fg_step = self._find_foreground_running_step_locked(rt, include_planner=False)
                if isinstance(fg_step, dict):
                    return [int(fg_step["idx"])]

        self._diagnose_sqlite_anomaly(cid, force=True, include_recent_steps=True)
        with self._lock:
            if (rt := self._runtimes.get(cid)) is None:
                return []
            s = self._trajectories.get(cid)
            traj_running = isinstance(s, dict) and short_run_status(str(s.get("status") or "")) == "RUNNING"
            if short_run_status(rt.raw_executor_loop_status) != "RUNNING" and short_run_status(rt.raw_status) != "RUNNING" and not traj_running:
                return []
            bg_indices = self._collect_bg_step_indices_locked(rt)
            floor_idx = self._current_turn_floor_idx_locked(rt)
            for dbs in rt.db_recent_steps:
                d_idx = int(dbs["idx"])
                known_st = rt.steps_by_idx.get(d_idx)
                if isinstance(known_st, dict) and (known_st.get("status") != "RUNNING" or bool(known_st.get("isDaemon"))):
                    continue
                if bool(dbs.get("isDaemon")):
                    continue
                if dbs.get("statusInt") == 2 and d_idx not in bg_indices and d_idx >= floor_idx:
                    return [d_idx]
        return []

    def _clear_cancelled_bg_steps_locked(
        self, rt: ConversationRuntime, indices: list[int], clear_all_bg: bool = False
    ) -> None:
        idx_set = set(indices)
        if isinstance(rt.db_bg_script, dict):
            if clear_all_bg or int(rt.db_bg_script.get("stepIndex", -1)) in idx_set:
                rt.db_bg_script = None
        for idx in idx_set:
            if isinstance(st := rt.steps_by_idx.get(idx), dict) and st.get("status") == "RUNNING":
                st["status"] = "CANCELED"
        if clear_all_bg:
            bg_indices = self._collect_bg_step_indices_locked(rt)
            for idx, st in rt.steps_by_idx.items():
                if not isinstance(st, dict) or st.get("status") != "RUNNING" or st.get("isDaemon"):
                    continue
                t_key = _normalize_tool_key(str(st.get("toolName") or ""))
                s_key = _normalize_tool_key(str(st.get("type") or ""))
                if idx in bg_indices or t_key in ("run_command", "run_workflow") or s_key == "run_command":
                    st["status"] = "CANCELED"
            rt.bg_tasks_by_idx.clear()
        elif idx_set:
            for bg_k in [k for k, tup in rt.bg_tasks_by_idx.items() if tup and set(tup).issubset(idx_set)]:
                rt.bg_tasks_by_idx.pop(bg_k, None)

    def heal(self, conversation_id: str, mode: str, step_index: Optional[int] = None, text: str = "", kill_background_tasks: bool = False) -> dict[str, Any]:
        cid, _ = self.resolve_target_cid(conversation_id, force_sync=True)
        if not cid:
            raise ValueError("conversationId is required")
        m = (mode or "wake").strip().lower()

        if m == "wake":
            is_sub = self._is_subagent_cid(cid)
            if not is_sub:
                self._diagnose_sqlite_anomaly(cid, force=False)
            with self._lock:
                rt = self._runtimes.get(cid)
                s = self._trajectories.get(cid)
                traj_stat = short_run_status(str(s.get("status") or "IDLE")) if isinstance(s, dict) else "IDLE"
                if is_sub:
                    is_actively_running = self._is_subagent_live_running_locked(rt, s or {}, traj_stat)
                else:
                    is_actively_running = self._is_root_live_active_locked(rt, s or {}, traj_stat)
                    if not is_actively_running and rt is not None:
                        is_actively_running = bool(
                            short_run_status(rt.raw_status) in ACTIVE_ROOT_STATUSES
                            or short_run_status(rt.raw_executor_loop_status) in ACTIVE_ROOT_STATUSES
                            or rt.ts_active_step_idx >= 0
                        )
                has_interrupt = bool(
                    rt
                    and rt.db_latest_error
                    and rt.db_latest_error.get("isInterrupted")
                    and not rt.db_latest_error.get("isServerRestart")
                    and int(rt.db_latest_error.get("stepIndex", -1)) > rt.last_user_input_idx
                )
            if is_actively_running:
                return {
                    "ok": False,
                    "error": "already_running",
                    "conversationId": cid,
                    "mode": "wake",
                    "message": "任务仍在活跃运行/重试中，请先点击中断步骤后再唤醒",
                }
            if (has_interrupt or text.strip()) and not is_sub:
                prompt = text.strip() or "请从刚才中断或停滞的位置继续执行当前任务，无需重复已完成的工作。"
                resp = self.rpc_call("SendUserCascadeMessage", self._build_send_user_payload(cid, [{"text": prompt}]), timeout=5.0)
                via = "SendUserCascadeMessage"
            else:
                msg_text = text.strip() or "[Flow Pilot] 用户触发唤醒检查：请检查当前任务与后台状态并继续推进。"
                resp, via = self._send_agent_or_user_msg(cid, "Flow Pilot", msg_text)
            self.bump_rev()
            return {"ok": True, "conversationId": cid, "mode": "wake", "via": via, "response": resp}

        if m == "cancel_step":
            indices = self._resolve_running_step_indices(cid, step_index)
            if not indices and kill_background_tasks:
                with self._lock:
                    if (rt := self._runtimes.get(cid)) is not None:
                        if isinstance(bg_s := self._find_active_bg_script_locked(rt), dict) and int(bg_s.get("stepIndex", -1)) >= 0:
                            indices = [int(bg_s["stepIndex"])]
            if not indices:
                if kill_background_tasks:
                    resp = self.rpc_call("CancelCascadeInvocation", {"cascadeId": cid, "killBackgroundTasks": True}, timeout=4.0)
                    with self._lock:
                        if (rt := self._runtimes.get(cid)) is not None:
                            self._clear_cancelled_bg_steps_locked(rt, [], clear_all_bg=True)
                    self.bump_rev()
                    return {"ok": True, "conversationId": cid, "mode": "cancel_step", "via": "CancelCascadeInvocation", "stepIndices": [], "response": resp}
                return {"ok": True, "conversationId": cid, "mode": "cancel_step", "noop": True, "stepIndices": []}
            via = "CancelCascadeSteps"
            try:
                resp = self.rpc_call("CancelCascadeSteps", {"cascadeId": cid, "stepIndices": indices}, timeout=4.0)
            except Exception:
                if not kill_background_tasks:
                    raise
                via = "CancelCascadeInvocation"
                resp = self.rpc_call("CancelCascadeInvocation", {"cascadeId": cid, "killBackgroundTasks": True}, timeout=4.0)
            with self._lock:
                if (rt := self._runtimes.get(cid)) is not None:
                    self._clear_cancelled_bg_steps_locked(
                        rt, indices, clear_all_bg=bool(kill_background_tasks and via == "CancelCascadeInvocation")
                    )
            self.bump_rev()
            return {"ok": True, "conversationId": cid, "mode": "cancel_step", "via": via, "stepIndices": indices, "response": resp}

        if m == "cancel_invocation":
            resp = self.rpc_call("CancelCascadeInvocation", {"cascadeId": cid, "killBackgroundTasks": bool(kill_background_tasks)}, timeout=4.0)
            if kill_background_tasks:
                with self._lock:
                    if (rt := self._runtimes.get(cid)) is not None:
                        self._clear_cancelled_bg_steps_locked(rt, [], clear_all_bg=True)
            self.bump_rev()
            return {"ok": True, "conversationId": cid, "mode": "cancel_invocation", "response": resp}

        raise ValueError(f"Unsupported heal mode: {mode}")
