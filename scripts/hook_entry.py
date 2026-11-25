#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ultra-lightweight (-S, zero-regex) session micro-beacon hook for Flow Pilot v3.8.0."""

import json
import os
import sys
import time

sys.dont_write_bytecode = True
SAFE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
_META_TAGS = ("additional_metadata", "user_settings_change", "context_summary")


def _safe_cid(cid: str) -> str:
    return "".join(ch if ch in SAFE_CHARS else "_" for ch in cid.strip())


def _plugin_data_dir() -> str:
    d = os.environ.get("PLUGIN_DATA", "").strip()
    if d:
        return d
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".gemini", "antigravity", "plugin_data", "agy-flow-pilot")


def _is_sys_text(raw: str) -> bool:
    h = raw[:600]
    return any(
        k in h
        for k in ("<SYSTEM_MESSAGE>", "not actually sent by the user", "You have just started. Your assignment", "[swarm] new board message")
    )


def _clean_title(raw: str) -> str:
    if not isinstance(raw, str):
        return ""
    txt = raw.strip()
    if not txt or _is_sys_text(txt):
        return ""
    low = txt.lower()
    if (i := low.find("<user_request>")) >= 0:
        j = low.find("</user_request>", i + 14)
        txt = txt[i + 14 : j if j >= 0 else len(txt)].strip()
    else:
        for tag in _META_TAGS:
            ot, ct = f"<{tag}>", f"</{tag}>"
            while (s := txt.lower().find(ot)) >= 0:
                e = txt.lower().find(ct, s + len(ot))
                txt = (txt[:s] + " " + txt[e + len(ct) :]) if e >= 0 else txt[:s]
        pos = 0
        while (s := txt.lower().find("[file ", pos)) >= 0:
            if (e := txt.find("]", s)) < 0:
                break
            if "was omitted" in txt[s : e + 1].lower():
                txt = txt[:s] + " " + txt[e + 1 :]
                pos = s
            else:
                pos = e + 1
    txt = " ".join(txt.split())
    if len(txt) > 22:
        txt = txt[:19] + "..."
    while len(txt.encode("utf-8")) > 36 and len(txt) > 6:
        txt = txt[:-4] + "..."
    return txt


def _read_json(path: str) -> dict:
    try:
        with open(path, "rb") as fh:
            obj = json.loads(fh.read().decode("utf-8", errors="replace"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _write_atomic(path: str, data: dict) -> None:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    parent = os.path.dirname(path)
    tmp = os.path.join(parent, f".{os.path.basename(path)}.{os.getpid()}.tmp")
    try:
        try:
            with open(tmp, "wb") as fh:
                fh.write(raw)
        except FileNotFoundError:
            os.makedirs(parent, exist_ok=True)
            with open(tmp, "wb") as fh:
                fh.write(raw)
        os.replace(tmp, path)
    except Exception:
        try:
            with open(path, "wb") as fh:
                fh.write(raw)
        except Exception:
            pass


def _run_hook() -> None:
    raw = sys.stdin.buffer.read()
    if not raw:
        return
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    if not (s := raw.decode("utf-8", errors="replace").strip()):
        return
    data = json.loads(s)
    if not isinstance(data, dict):
        return
    cid = str(data.get("conversationId") or os.environ.get("ANTIGRAVITY_CONVERSATION_ID") or "").strip()
    if not cid:
        return

    ev = str(data.get("hookEventName") or "PreInvocation").strip()
    hp = os.path.join(_plugin_data_dir(), "hook_state", f"{_safe_cid(cid)}.json")
    now = time.time()
    try:
        st = os.stat(hp)
    except OSError:
        st = None

    prev = None
    if st is not None and ev != "SessionStart":
        inv = data.get("invocationNum")
        if isinstance(inv, int) and not isinstance(inv, bool) and inv >= 1:
            return
        prev = _read_json(hp)
        try:
            prev_pre = float(prev.get("lastPreInvocationAt") or 0.0)
        except (TypeError, ValueError):
            prev_pre = 0.0
        if prev.get("isSubagent") or (prev_pre > 0.0 and (now - prev_pre) < 1.5 and prev.get("firstUserTitle")):
            return
    if prev is None:
        prev = _read_json(hp) if st is not None else {}

    raw_u = str(data.get("lastUserInput") or "").strip()
    u_title = _clean_title(raw_u) if raw_u else ""
    parent_cid = str(data.get("parentConversationId") or "").strip()
    root_cid = str(data.get("rootConversationId") or "").strip()
    is_sub = bool(
        parent_cid
        or (root_cid and root_cid != cid)
        or prev.get("isSubagent")
        or (not u_title and not prev.get("firstUserTitle") and bool(raw_u) and _is_sys_text(raw_u))
    )
    first_title = "" if is_sub else str(prev.get("firstUserTitle") or u_title or "").strip()

    ts = round(now, 2)
    beacon = {"cid": cid, "isSubagent": is_sub}
    if ev == "SessionStart":
        beacon["lastSessionStartAt"] = ts
        if prev.get("lastPreInvocationAt"):
            beacon["lastPreInvocationAt"] = prev["lastPreInvocationAt"]
    else:
        beacon["lastPreInvocationAt"] = ts
    if first_title:
        beacon["firstUserTitle"] = first_title

    m_in = str(data.get("modelName") or "").strip()
    m_val = m_in if (m_in and m_in.lower() != "auto") else str(prev.get("modelName") or "").strip()
    if m_val and m_val.lower() != "auto":
        beacon["modelName"] = m_val[:40]

    ws_in = data.get("workspacePaths")
    ws = ws_in if (isinstance(ws_in, list) and ws_in) else prev.get("workspacePaths")
    if isinstance(ws, list) and ws and (p0 := str(ws[0] or "").strip().replace("\\", "/").rstrip("/")):
        beacon["workspacePaths"] = [p0]

    _write_atomic(hp, beacon)


def main() -> None:
    try:
        _run_hook()
    except BaseException:
        pass
    try:
        sys.stdout.buffer.write(b"{}\n")
        sys.stdout.buffer.flush()
    except BaseException:
        pass


if __name__ == "__main__":
    main()
