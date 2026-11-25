#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Zero-dependency lightweight read-only MCP Server for Flow Pilot v3.8.0 (`python -S -B -u` ready)."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time

sys.dont_write_bytecode = True

SERVER_NAME = "fast_io"
SERVER_VERSION = "3.8.0"

MAX_OUTPUT_BYTES = 8_800          # Hard UTF-8 output cap across ALL 4 tools (zero ~10KB spill)
MAX_BATCH_ITEMS = 15
MAX_OUTLINE_FILES = 30
MAX_GREP_QUERIES = 15
DEFAULT_MAX_LINES = 1200
HARD_MAX_LINES_PER_ITEM = 2500
MAX_DISK_READ_BYTES = 4_194_304   # 4 MB per-file disk read guard
MAX_GREP_FILE_SIZE = 2_097_152    # 2 MB per-file grep/outline guard
MAX_STDIO_FRAME_BYTES = 4_194_304 # 4 MB defensive stdio Content-Length / JSONL frame cap

DEFAULT_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".tox", ".nox", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
    "target", "out", ".next", ".nuxt", ".turbo", "coverage", ".idea", ".vs", "obj",
    ".system_generated",
})

BINARY_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff",
    ".mp3", ".mp4", ".wav", ".ogg", ".webm", ".avi", ".mov",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".pdb", ".obj", ".lib", ".a", ".o",
    ".pyc", ".pyo", ".pyd", ".whl", ".jar", ".class", ".wasm",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".db", ".sqlite", ".sqlite3", ".bin", ".dat", ".pack",
})

DEFAULT_STATS = {
    "batchReadCalls": 0, "batchReadFiles": 0, "grepCalls": 0, "globCalls": 0,
    "outlineCalls": 0, "savedSecEstimate": 0.0,
}

_REGEX_META_CHARS = frozenset("$^.*+?[](){}|\\")
_STATS_MEM_LOCK = threading.Lock()
_DEFER_STATS = False
_PENDING_STATS: list[tuple[str, dict[str, float | int]]] = []
_IO_POOL = None
_IO_POOL_LOCK = threading.Lock()
_CFG_CACHE: dict[str, tuple[int, int, dict[str, bool]]] = {}


def _get_io_pool():
    global _IO_POOL
    if _IO_POOL is None:
        with _IO_POOL_LOCK:
            if _IO_POOL is None:
                from concurrent.futures import ThreadPoolExecutor

                _IO_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="fast-io")
    return _IO_POOL


def _safe_int(val, default: int, lo: int, hi: int) -> int:
    if isinstance(val, bool) or val is None:
        return default
    try:
        iv = int(val)
    except (ValueError, TypeError):
        return default
    return max(lo, min(hi, iv))


def _utf8_len(s: str) -> int:
    return len(s.encode("utf-8", errors="replace"))


def _clip_utf8_bytes(s: str, max_bytes: int) -> str:
    if max_bytes <= 0 or not s:
        return ""
    raw = s.encode("utf-8", errors="replace")
    if len(raw) <= max_bytes:
        return s
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _enforce_hard_cap(text: str, max_bytes: int = MAX_OUTPUT_BYTES, notice: str = "") -> str:
    if _utf8_len(text) <= max_bytes:
        return text
    tail = notice or "\n... [output capped at 8.8KB; narrow scope or use read_file]"
    tail_bytes = _utf8_len(tail)
    if tail_bytes >= max_bytes:
        return _clip_utf8_bytes(tail, max_bytes)
    avail = max_bytes - tail_bytes
    clipped = _clip_utf8_bytes(text, avail)
    nl = clipped.rfind("\n")
    if nl >= int(len(clipped) * 0.65):
        clipped = clipped[:nl]
    out = clipped.rstrip("\n") + tail
    if _utf8_len(out) > max_bytes:
        out = _clip_utf8_bytes(out, max_bytes)
    return out


def _compute_water_fill_budgets(needs: list[int], total_budget: int) -> list[int]:
    n = len(needs)
    if n == 0 or total_budget <= 0:
        return [0] * n
    sanitized = [max(0, int(v)) for v in needs]
    if sum(sanitized) <= total_budget:
        return sanitized
    budgets = [0] * n
    order = sorted(range(n), key=lambda i: sanitized[i])
    rem_budget = total_budget
    for pos, idx in enumerate(order):
        rem_count = n - pos
        fair = rem_budget // rem_count
        if sanitized[idx] <= fair:
            budgets[idx] = sanitized[idx]
            rem_budget -= sanitized[idx]
        else:
            extra = rem_budget - fair * rem_count
            for r_pos in range(pos, n):
                r_idx = order[r_pos]
                budgets[r_idx] = fair + (1 if (r_pos - pos) < extra else 0)
            break
    return budgets


def _home_dir() -> str:
    return os.environ.get("USERPROFILE") or os.environ.get("HOME") or os.path.expanduser("~")


def get_plugin_data_dir() -> str:
    d = os.environ.get("PLUGIN_DATA", "").strip()
    return d or os.path.join(_home_dir(), ".gemini", "antigravity", "plugin_data", "agy-flow-pilot")


def load_fast_io_config(plugin_data: str | None = None) -> dict[str, bool]:
    pdata = plugin_data or get_plugin_data_dir()
    cfg_path = os.path.join(pdata, "config.json")
    try:
        st = os.stat(cfg_path)
    except OSError:
        _CFG_CACHE.pop(cfg_path, None)
        return {"mcpEnabled": True}

    cached = _CFG_CACHE.get(cfg_path)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return dict(cached[2])

    out = {"mcpEnabled": True}
    try:
        with open(cfg_path, "rb") as fh:
            data = json.loads(fh.read().decode("utf-8", errors="replace"))
        if isinstance(data, dict):
            fio = data.get("fastIo")
            if isinstance(fio, dict):
                if fio.get("mcpEnabled") is not None:
                    out["mcpEnabled"] = bool(fio["mcpEnabled"])
            if data.get("fastIoMcpEnabled") is not None:
                out["mcpEnabled"] = bool(data["fastIoMcpEnabled"])
        if len(_CFG_CACHE) > 16:
            _CFG_CACHE.clear()
        _CFG_CACHE[cfg_path] = (st.st_mtime_ns, st.st_size, dict(out))
    except Exception:
        pass
    return out


def _write_stats_deltas(pdata: str, deltas: dict[str, float | int]) -> None:
    stats_path = os.path.join(pdata, "fast_io_stats.json")
    lock_path = os.path.join(pdata, ".fast_io_stats.lock")

    with _STATS_MEM_LOCK:
        lock_fd = None
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                try:
                    lock_fd = open(lock_path, "a+b")
                except FileNotFoundError:
                    os.makedirs(pdata, exist_ok=True)
                    lock_fd = open(lock_path, "a+b")
                for _ in range(8):
                    try:
                        lock_fd.seek(0)
                        msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
                        locked = True
                        break
                    except OSError:
                        time.sleep(0.002)
            else:
                os.makedirs(pdata, exist_ok=True)

            cur = dict(DEFAULT_STATS)
            try:
                with open(stats_path, "rb") as fh:
                    loaded = json.loads(fh.read().decode("utf-8", errors="replace"))
                if isinstance(loaded, dict):
                    for k, dv in DEFAULT_STATS.items():
                        rv = loaded.get(k, dv)
                        cur[k] = float(rv or 0.0) if isinstance(dv, float) else int(rv or 0)
            except Exception:
                pass

            for k, dv in deltas.items():
                if k in DEFAULT_STATS:
                    cur[k] = round(float(cur[k]) + float(dv), 2) if isinstance(DEFAULT_STATS[k], float) else int(cur[k]) + int(dv)

            tmp_path = os.path.join(pdata, f".fast_io_stats.{os.getpid()}.{threading.get_ident()}.tmp")
            with open(tmp_path, "wb") as fh:
                fh.write(json.dumps(cur, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            os.replace(tmp_path, stats_path)
        except Exception:
            pass
        finally:
            if lock_fd is not None:
                if locked and os.name == "nt":
                    try:
                        import msvcrt

                        lock_fd.seek(0)
                        msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
                    except Exception:
                        pass
                try:
                    lock_fd.close()
                except Exception:
                    pass


def flush_pending_stats() -> None:
    if not _PENDING_STATS:
        return
    merged: dict[str, dict[str, float | int]] = {}
    while _PENDING_STATS:
        pd, dl = _PENDING_STATS.pop(0)
        acc = merged.setdefault(pd, {})
        for k, v in dl.items():
            acc[k] = round(float(acc.get(k, 0.0)) + float(v), 2) if isinstance(DEFAULT_STATS.get(k), float) else int(acc.get(k, 0)) + int(v)
    for pd, dl in merged.items():
        _write_stats_deltas(pd, dl)


def update_fast_io_stats(plugin_data: str | None = None, **deltas: float | int) -> None:
    if not deltas:
        return
    pdata = plugin_data or get_plugin_data_dir()
    if _DEFER_STATS:
        _PENDING_STATS.append((pdata, deltas))
    else:
        _write_stats_deltas(pdata, deltas)


def check_safe_local_path(raw_path, allow_directory: bool = True) -> tuple[bool, str, str]:
    if not isinstance(raw_path, str):
        return False, "", "路径参数必须为非空字符串"
    s = raw_path.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    if not s:
        return False, "", "路径不能为空"
    if any(ord(ch) < 32 for ch in s):
        return False, "", "路径包含非法控制字符"

    if s[:5].lower() == "file:":
        if len(s) >= 8 and s[:8].lower() == "file:///":
            rest = s[8:]
            if "%" in rest:
                import urllib.parse

                rest = urllib.parse.unquote(rest)
            if os.name == "nt" and len(rest) >= 2 and rest[0].isalpha() and rest[1] == ":":
                s = rest
            elif os.name != "nt":
                s = "/" + rest.lstrip("/")
            else:
                s = rest
        elif s[:7].lower() == "file://":
            s = s[7:]
        else:
            s = s[5:]

    if s.startswith("~"):
        home = _home_dir()
        s = home if s == "~" else (os.path.join(home, s[2:]) if s.startswith(("~/", "~\\")) else os.path.expanduser(s))

    norm = os.path.normpath(os.path.abspath(s))
    if not allow_directory and os.path.isdir(norm):
        return False, norm, f"目标路径是目录而非文件: {norm}"
    return True, norm, ""


def _check_safe_scan_root(norm_path: str) -> tuple[bool, str]:
    p = os.path.normpath(os.path.abspath(norm_path))
    p_clean = p[4:] if (os.name == "nt" and p.startswith(("\\\\?\\", "\\\\.\\")) and len(p) > 4) else p
    p_clean = os.path.normpath(p_clean)
    if os.path.dirname(p) == p or os.path.dirname(p_clean) == p_clean:
        return False, f"拒绝直接扫描盘符/文件系统根目录 ({p})，请指定具体子目录"
    home = os.path.normpath(os.path.abspath(_home_dir()))
    if os.path.normcase(p_clean.rstrip("\\/")) == os.path.normcase(home.rstrip("\\/")):
        return False, f"拒绝直接扫描用户家目录 ({p})，请指定具体项目子目录"
    return True, ""


def decode_text_bytes(raw: bytes, truncated: bool = False) -> str | None:
    if not raw:
        return ""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError as exc:
            if truncated and exc.start >= len(raw) - 2 and exc.start > 2:
                try:
                    return raw[: exc.start].decode("utf-16")
                except UnicodeDecodeError:
                    pass
            return None
    if b"\x00" in raw[:4096]:
        return None
    buf = raw[3:] if raw.startswith(b"\xef\xbb\xbf") else raw
    try:
        return buf.decode("utf-8")
    except UnicodeDecodeError as exc:
        if (truncated or len(buf) >= 65536) and exc.start >= len(buf) - 3 and exc.start > 0:
            try:
                return buf[: exc.start].decode("utf-8")
            except UnicodeDecodeError:
                pass
    for enc in ("gb18030", "latin-1"):
        try:
            return buf.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


class _GitIgnoreMatcher:
    def __init__(self, root_dir: str) -> None:
        self.root_dir = os.path.normpath(root_dir)
        self.rules: list[tuple[bool, bool, bool, str]] = []
        candidates: list[str] = []
        cur = self.root_dir
        for _ in range(6):
            gi = os.path.join(cur, ".gitignore")
            if os.path.isfile(gi):
                candidates.append(gi)
            if os.path.isdir(os.path.join(cur, ".git")):
                break
            parent = os.path.dirname(cur)
            if not parent or parent == cur:
                break
            cur = parent
        for gi_path in reversed(candidates):
            self._parse_file(gi_path)

    def _parse_file(self, gi_path: str) -> None:
        try:
            with open(gi_path, "rb") as fh:
                raw = fh.read(65536)
            for line in (decode_text_bytes(raw, truncated=(len(raw) == 65536)) or "").splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                neg = s.startswith("!")
                if neg:
                    s = s[1:].strip()
                dir_only = s.endswith("/")
                s = s.strip("/")
                if s:
                    self.rules.append((neg, dir_only, "/" in s, s))
        except Exception:
            pass

    def is_ignored(self, full_path: str, name: str, is_dir: bool) -> bool:
        low_name = name.lower()
        if is_dir and low_name in DEFAULT_SKIP_DIRS:
            return True
        if not is_dir and os.path.splitext(low_name)[1] in BINARY_EXTENSIONS:
            return True
        if not self.rules:
            return False
        import fnmatch

        try:
            rel = os.path.relpath(full_path, self.root_dir).replace("\\", "/")
        except ValueError:
            rel = name
        ignored = False
        for neg, dir_only, has_slash, pat in self.rules:
            if dir_only and not is_dir:
                continue
            if has_slash:
                cp = pat[3:] if pat.startswith("**/") else pat
                matched = fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, cp) or rel.startswith(cp + "/")
            else:
                matched = fnmatch.fnmatch(name, pat)
            if matched:
                ignored = not neg
        return ignored


_BRACE_RE = re.compile(r"\{([^{}]+)\}")
_SLICE_SHORTHAND_RE = re.compile(r"^(.+):(\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*)$")


def _expand_glob_patterns(raw_glob: str) -> list[str]:
    if not raw_glob or not isinstance(raw_glob, str):
        return []
    s = raw_glob.strip()
    if not s:
        return []
    m = _BRACE_RE.search(s)
    if m:
        pre, suf = s[: m.start()], s[m.end() :]
        return [g for opt in m.group(1).split(",") if opt.strip() for g in _expand_glob_patterns(f"{pre}{opt.strip()}{suf}")]
    return [p.strip() for p in s.split(",") if p.strip()]


def _fmt_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes}B"
    kb = num_bytes / 1024.0
    return f"{kb:.1f}KB" if kb < 1024 else f"{kb / 1024.0:.2f}MB"


def _extract_path_list(args: dict) -> list[str]:
    raw_paths = args.get("paths")
    if isinstance(raw_paths, list):
        return [str(p) for p in raw_paths if isinstance(p, str) and p.strip()]
    if isinstance(raw_paths, str) and raw_paths.strip():
        return [raw_paths.strip()]
    if isinstance(args.get("path"), str) and args["path"].strip():
        return [args["path"].strip()]
    return []


def _parse_shorthand_item(spec: str) -> list[dict]:
    s = spec.strip()
    if not s:
        return []
    m = _SLICE_SHORTHAND_RE.match(s)
    if m and len(m.group(1).strip()) >= 2:
        p_part = m.group(1).strip()
        ranges_part = m.group(2)
        out: list[dict] = []
        for seg in ranges_part.split(","):
            if "-" in seg:
                a, b = seg.split("-", 1)
                out.append({"path": p_part, "start_line": int(a), "end_line": int(b)})
            else:
                ln = int(seg)
                out.append({"path": p_part, "start_line": ln, "end_line": ln})
        return out
    return [{"path": s}]


# --- Tool 1: fast_io_batch_read ---

def _read_single_item_data(idx: int, total: int, req: dict) -> dict:
    raw_path = req.get("path") or req.get("Path") or ""
    ok, norm, err = check_safe_local_path(raw_path, allow_directory=False)
    if not ok:
        return {"ok": False, "static": f"=== [{idx}/{total}] {raw_path} -> ERROR: {err} ==="}
    if not os.path.exists(norm):
        return {"ok": False, "static": f"=== [{idx}/{total}] {norm} -> ERROR: 文件不存在 ==="}
    if not os.path.isfile(norm):
        return {"ok": False, "static": f"=== [{idx}/{total}] {norm} -> ERROR: 目标不是普通文件 ==="}

    try:
        fsize = os.path.getsize(norm)
        if os.path.splitext(norm.lower())[1] in BINARY_EXTENSIONS:
            return {"ok": False, "static": f"=== [{idx}/{total}] {norm} -> SKIPPED: 二进制格式 ({_fmt_size(fsize)}) ==="}

        ml_req = req.get("max_lines")
        max_lines = _safe_int(ml_req, DEFAULT_MAX_LINES, 1, HARD_MAX_LINES_PER_ITEM)
        if not isinstance(ml_req, bool) and isinstance(ml_req, (int, float)) and int(ml_req) <= 0:
            max_lines = DEFAULT_MAX_LINES

        s_req = req.get("start_line")
        start_line = max(1, int(s_req)) if not isinstance(s_req, bool) and isinstance(s_req, (int, float)) and int(s_req) >= 1 else 1

        e_req = req.get("end_line")
        if not isinstance(e_req, bool) and isinstance(e_req, (int, float)) and int(e_req) >= 1:
            end_int = int(e_req)
            if end_int < start_line:
                return {"ok": False, "static": f"=== [{idx}/{total}] {norm} -> ERROR: end_line ({end_int}) 小于 start_line ({start_line}) ==="}
            end_line = min(end_int, start_line + HARD_MAX_LINES_PER_ITEM - 1)
        else:
            end_line = start_line + max_lines - 1

        with open(norm, "rb") as fh:
            raw_bytes = fh.read(min(fsize, MAX_DISK_READ_BYTES) if fsize > 0 else MAX_DISK_READ_BYTES)
        partial = fsize > len(raw_bytes)

        text = decode_text_bytes(raw_bytes, truncated=partial)
        del raw_bytes
        if text is None:
            return {"ok": False, "static": f"=== [{idx}/{total}] {norm} -> SKIPPED: 检测到二进制内容 ({_fmt_size(fsize)}) ==="}

        all_lines = text.splitlines()
        del text
        total_lines = len(all_lines)
        if total_lines == 0:
            return {"ok": True, "static": f"=== [{idx}/{total}] {norm} (empty file, 0 lines, {_fmt_size(fsize)}) ==="}
        if start_line > total_lines:
            return {"ok": True, "static": f"=== [{idx}/{total}] {norm} (start_line={start_line} 超出总行数 {total_lines}) ==="}

        req_end = min(total_lines, end_line)
        formatted: list[tuple[int, str, int]] = []
        acc_bytes = 0
        for ln in range(start_line, req_end + 1):
            lt = all_lines[ln - 1]
            if len(lt) > 600:
                lt = lt[:600] + " ... [line truncated]"
            fmt = f"{ln}: {lt}"
            b_len = _utf8_len(fmt) + 1
            formatted.append((ln, fmt, b_len))
            acc_bytes += b_len
            if acc_bytes > MAX_OUTPUT_BYTES * 2:
                break
        del all_lines

        return {
            "ok": True,
            "idx": idx,
            "total": total,
            "norm": norm,
            "fsize": fsize,
            "total_lines": total_lines,
            "partial": partial,
            "start_line": start_line,
            "req_end": req_end,
            "lines": formatted,
        }
    except Exception as exc:
        return {"ok": False, "static": f"=== [{idx}/{total}] {norm} -> ERROR: {exc} ==="}


def _read_item_full_bytes(item: dict) -> int:
    plus = "+" if item["partial"] else ""
    hdr = f"=== [{item['idx']}/{item['total']}] {item['norm']} (lines {item['start_line']}-{item['req_end']} of {item['total_lines']}{plus}, {_fmt_size(item['fsize'])}) ==="
    body = sum(b for _, _, b in item["lines"])
    has_more = item["req_end"] < item["total_lines"] or item["partial"] or (item["lines"] and item["lines"][-1][0] < item["total_lines"])
    foot = (
        _utf8_len(f"\n... [truncated (range cap); continue: read_file L{item['total_lines'] + 1}-L{item['total_lines'] + 800} of {item['total_lines']}{plus}]")
        if has_more
        else 0
    )
    return _utf8_len(hdr) + body + foot + 8


def _render_read_item_with_budget(item: dict, budget: int) -> str:
    idx, total, norm = item["idx"], item["total"], item["norm"]
    total_lines, partial, fsize = item["total_lines"], item["partial"], item["fsize"]
    start_line = item["start_line"]
    lines = item["lines"]
    plus = "+" if partial else ""

    max_hdr = f"=== [{idx}/{total}] {norm} (lines {start_line}-{total_lines} of {total_lines}{plus}, {_fmt_size(fsize)}) ==="
    hdr_reserve = _utf8_len(max_hdr) + 1
    max_foot = f"\n... [truncated (8.8KB cap); continue: read_file L{total_lines + 1}-L{total_lines + 800} of {total_lines}{plus}]"
    foot_reserve = _utf8_len(max_foot)

    full_need = _read_item_full_bytes(item)
    can_fit_all = full_need <= budget and (not lines or lines[-1][0] >= item["req_end"])

    out_lines: list[str] = []
    last_ln = start_line - 1
    if can_fit_all:
        for ln, fmt, _ in lines:
            out_lines.append(fmt)
            last_ln = ln
    else:
        body_budget = max(0, budget - hdr_reserve - foot_reserve)
        used = 0
        for ln, fmt, b_len in lines:
            if used + b_len > body_budget:
                if not out_lines and body_budget >= 32:
                    clipped_line = _clip_utf8_bytes(fmt, body_budget - 4) + " ..."
                    out_lines.append(clipped_line)
                    last_ln = ln
                break
            out_lines.append(fmt)
            used += b_len
            last_ln = ln

    if last_ln >= start_line:
        header = f"=== [{idx}/{total}] {norm} (lines {start_line}-{last_ln} of {total_lines}{plus}, {_fmt_size(fsize)}) ==="
    else:
        header = f"=== [{idx}/{total}] {norm} (0 lines shown of {total_lines}{plus}, {_fmt_size(fsize)}) ==="

    truncated_more = (last_ln < total_lines) or partial
    footer = ""
    if truncated_more:
        next_s = (last_ln + 1) if last_ln >= start_line else start_line
        next_e = min(total_lines, next_s + 799) if next_s <= total_lines else (next_s + 799)
        reason = "8.8KB cap" if not can_fit_all else "range cap"
        footer = f"\n... [truncated ({reason}); continue: read_file L{next_s}-L{next_e} of {total_lines}{plus}]"

    if out_lines:
        block = header + "\n" + "\n".join(out_lines) + footer
    else:
        block = header + footer
    if _utf8_len(block) <= budget:
        return block
    return _enforce_hard_cap(block, max(budget, 80)) if budget >= 80 else _clip_utf8_bytes(block, max(budget, 0))


def handle_batch_read(args: dict, plugin_data: str | None = None) -> tuple[str, bool]:
    items: list[dict] = []
    raw_items = args.get("items")
    if isinstance(raw_items, list):
        for it in raw_items:
            if isinstance(it, dict):
                items.append(it)
            elif isinstance(it, str):
                items.extend(_parse_shorthand_item(it))
    raw_paths = args.get("paths")
    if isinstance(raw_paths, list):
        for p in raw_paths:
            if isinstance(p, str):
                items.extend(_parse_shorthand_item(p))
    elif isinstance(raw_paths, str) and raw_paths.strip():
        items.extend(_parse_shorthand_item(raw_paths))
    elif isinstance(args.get("path"), str) and not items:
        parsed = _parse_shorthand_item(args["path"])
        if len(parsed) == 1 and "start_line" not in parsed[0]:
            items.append({k: args.get(k) for k in ("path", "start_line", "end_line", "max_lines")})
        else:
            items.extend(parsed)

    if not items:
        return '参数错误：请提供非空的 items 数组（例如 [{"path": "C:/path/file.py", "start_line": 1, "end_line": 60}]）', True

    omitted_items = max(0, len(items) - MAX_BATCH_ITEMS)
    trunc_note = f"\n\n[提示：单次最多并发读取 {MAX_BATCH_ITEMS} 项，已忽略其余 {omitted_items} 项；大段请用并行 read_file]" if omitted_items else ""
    items = items[:MAX_BATCH_ITEMS]
    total = len(items)

    if total <= 5:
        raw_results = [_read_single_item_data(i + 1, total, it) for i, it in enumerate(items)]
    else:
        pool = _get_io_pool()
        raw_results = [f.result() for f in [pool.submit(_read_single_item_data, i + 1, total, it) for i, it in enumerate(items)]]

    ok_count = sum(1 for r in raw_results if r["ok"])
    sep_bytes = max(0, len(raw_results) - 1) * 2
    trunc_bytes = _utf8_len(trunc_note)

    static_blocks: dict[int, str] = {}
    static_total = 0
    dyn_indices: list[int] = []
    dyn_needs: list[int] = []
    for i, r in enumerate(raw_results):
        if "static" in r:
            st_txt = _clip_utf8_bytes(r["static"], 360)
            static_blocks[i] = st_txt
            static_total += _utf8_len(st_txt)
        else:
            dyn_indices.append(i)
            dyn_needs.append(_read_item_full_bytes(r))

    avail_dyn = max(0, MAX_OUTPUT_BYTES - sep_bytes - trunc_bytes - static_total - 24)
    dyn_budgets = _compute_water_fill_budgets(dyn_needs, avail_dyn)

    rendered: list[str] = [""] * len(raw_results)
    for i, st_txt in static_blocks.items():
        rendered[i] = st_txt
    for pos, r_idx in enumerate(dyn_indices):
        rendered[r_idx] = _render_read_item_with_budget(raw_results[r_idx], dyn_budgets[pos])

    if ok_count > 0:
        update_fast_io_stats(plugin_data, batchReadCalls=1, batchReadFiles=ok_count, savedSecEstimate=round(1.8 + max(0, ok_count - 1) * 3.2, 2))

    final_text = "\n\n".join(s for s in rendered if s) + trunc_note
    return _enforce_hard_cap(final_text, MAX_OUTPUT_BYTES), (ok_count == 0)


# --- Tool 2: fast_io_grep ---

def _collect_search_files(targets: list[str], include_globs: list[str], extra_exclude_dirs: set[str], max_files: int = 4000) -> tuple[list[str], list[str]]:
    if include_globs:
        import fnmatch

    files: list[str] = []
    errors: list[str] = []
    seen: set[str] = set()

    def _add(fp: str) -> None:
        key = fp.lower() if os.name == "nt" else fp
        if key in seen:
            return
        name = os.path.basename(fp)
        if os.path.splitext(name.lower())[1] in BINARY_EXTENSIONS:
            return
        if include_globs:
            fp_s = fp.replace("\\", "/")
            if not any(fnmatch.fnmatch(name, g) or fnmatch.fnmatch(fp_s, g) for g in include_globs):
                return
        seen.add(key)
        files.append(fp)

    for raw_t in targets:
        ok, norm, err = check_safe_local_path(raw_t, allow_directory=True)
        if not ok:
            errors.append(f"{raw_t}: {err}")
            continue
        if not os.path.exists(norm):
            errors.append(f"{norm}: 路径不存在")
            continue
        if os.path.isfile(norm):
            _add(norm)
            continue
        if not os.path.isdir(norm):
            continue

        root_ok, root_err = _check_safe_scan_root(norm)
        if not root_ok:
            errors.append(root_err)
            continue

        gi = _GitIgnoreMatcher(norm)
        stack = [norm]
        while stack and len(files) < max_files:
            cur_dir = stack.pop()
            try:
                with os.scandir(cur_dir) as it:
                    subdirs: list[str] = []
                    for entry in it:
                        try:
                            if entry.is_symlink():
                                continue
                            ename = entry.name
                            if entry.is_dir(follow_symlinks=False):
                                if ename.lower() not in extra_exclude_dirs and not gi.is_ignored(entry.path, ename, True):
                                    subdirs.append(entry.path)
                            elif entry.is_file(follow_symlinks=False):
                                if not gi.is_ignored(entry.path, ename, False):
                                    st = entry.stat(follow_symlinks=False)
                                    if 0 < st.st_size <= MAX_GREP_FILE_SIZE:
                                        _add(entry.path)
                                        if len(files) >= max_files:
                                            break
                        except OSError:
                            continue
                    subdirs.sort(reverse=True)
                    stack.extend(subdirs)
            except OSError:
                continue
    return files, errors


def _extract_grep_queries(args: dict) -> tuple[list[str], int]:
    raw_list: list[str] = []
    seen: set[str] = set()

    def _push(val) -> None:
        if isinstance(val, str) and val:
            if val not in seen:
                seen.add(val)
                raw_list.append(val)
        elif isinstance(val, dict):
            p = val.get("pattern") or val.get("query")
            if isinstance(p, str) and p and p not in seen:
                seen.add(p)
                raw_list.append(p)

    raw_queries = args.get("queries")
    if isinstance(raw_queries, list):
        for item in raw_queries:
            _push(item)
    elif isinstance(raw_queries, str) and raw_queries:
        _push(raw_queries)

    pat = args.get("pattern")
    if isinstance(pat, str) and pat:
        _push(pat)

    dropped = max(0, len(raw_list) - MAX_GREP_QUERIES)
    return raw_list[:MAX_GREP_QUERIES], dropped


def _render_grep_single_query(
    file_records: list[dict],
    output_mode: str,
    max_matches: int,
    budget: int,
    section_header: str,
) -> str:
    hdr_bytes = _utf8_len(section_header) + 1 if section_header else 0
    if not file_records:
        return section_header

    body_budget = max(0, budget - hdr_bytes)

    if output_mode == "files_only":
        lines_out: list[str] = []
        used = 0
        foot_reserve = 78
        omitted = 0
        for idx, rec in enumerate(file_records):
            line = rec["fp"]
            b = _utf8_len(line) + 1
            rem_after = len(file_records) - idx - 1
            limit = body_budget - (foot_reserve if rem_after > 0 else 0)
            if used + b > limit and lines_out:
                omitted = len(file_records) - idx
                break
            if used + b > body_budget:
                omitted = len(file_records) - idx
                break
            lines_out.append(line)
            used += b
        if omitted > 0:
            lines_out.append(f"... [+{omitted} more matched files omitted (8.8KB cap); narrow paths/include_glob]")
        body = "\n".join(lines_out)
        return f"{section_header}\n{body}" if section_header else body

    if output_mode == "count":
        ordered = sorted(file_records, key=lambda r: (-r["total_count"], r["fp"].lower()))
        lines_out = []
        used = 0
        foot_reserve = 78
        omitted = 0
        for idx, rec in enumerate(ordered):
            line = f"{rec['fp']}: {rec['total_count']}"
            b = _utf8_len(line) + 1
            rem_after = len(ordered) - idx - 1
            limit = body_budget - (foot_reserve if rem_after > 0 else 0)
            if used + b > limit and lines_out:
                omitted = len(ordered) - idx
                break
            if used + b > body_budget:
                omitted = len(ordered) - idx
                break
            lines_out.append(line)
            used += b
        if omitted > 0:
            lines_out.append(f"... [+{omitted} more matched files omitted (8.8KB cap)]")
        body = "\n".join(lines_out)
        return f"{section_header}\n{body}" if section_header else body

    # output_mode == "matches": two-phase fair per-file budgeting
    # Phase 1: guarantee every matched file gets its first match before extra hits fill remaining budget
    foot_reserve = 105
    p1_chunks: list[tuple[int, str, int]] = []  # (rec_idx, first_text, bytes)
    p1_used = 0
    omitted_files: list[dict] = []

    for idx, rec in enumerate(file_records):
        first_txt = rec["entries"][0][2]
        b = _utf8_len(first_txt) + 1
        rem_files = len(file_records) - idx - 1
        limit = body_budget - (foot_reserve if rem_files > 0 else 0)
        if p1_used + b > limit and p1_chunks:
            omitted_files = file_records[idx:]
            break
        if p1_used + b > body_budget:
            if not p1_chunks and body_budget > 48:
                clipped = _clip_utf8_bytes(first_txt, body_budget - 4) + " ..."
                p1_chunks.append((idx, clipped, _utf8_len(clipped) + 1))
                p1_used += p1_chunks[-1][2]
                omitted_files = file_records[idx + 1 :]
            else:
                omitted_files = file_records[idx:]
            break
        p1_chunks.append((idx, first_txt, b))
        p1_used += b

    if omitted_files:
        out_parts = [txt for _, txt, _ in p1_chunks]
        rem_hits = sum(r["total_count"] for r in omitted_files)
        out_parts.append(
            f"... [+{len(omitted_files)} more matched files ({rem_hits} hits) omitted (8.8KB cap); use output_mode=\"files_only\" or narrow paths]"
        )
        body = "\n".join(out_parts)
        return f"{section_header}\n{body}" if section_header else body

    # All matched files got their first match! Reserve note budget for files with >1 hit, then water-fill extra hits.
    per_file_shown: list[list[str]] = [[txt] for _, txt, _ in p1_chunks]
    per_file_shown_hits: list[int] = [file_records[i]["entries"][0][3] for i in range(len(file_records))]
    total_shown_hits = sum(per_file_shown_hits)

    extra_indices: list[int] = []
    extra_needs: list[int] = []
    note_reserve_total = 0
    for idx, rec in enumerate(file_records):
        if rec["total_count"] > per_file_shown_hits[idx]:
            note_reserve_total += 74
        if len(rec["entries"]) > 1:
            extra_indices.append(idx)
            extra_needs.append(sum(_utf8_len(e[2]) + 1 for e in rec["entries"][1:]))

    rem_for_extra = max(0, body_budget - p1_used - min(note_reserve_total, max(0, body_budget - p1_used) // 2))
    if extra_indices and rem_for_extra > 0 and total_shown_hits < max_matches:
        extra_budgets = _compute_water_fill_budgets(extra_needs, rem_for_extra)
        for pos, f_idx in enumerate(extra_indices):
            fb = extra_budgets[pos]
            used_f = 0
            rec = file_records[f_idx]
            for e_start, e_end, e_txt, e_cnt in rec["entries"][1:]:
                if total_shown_hits >= max_matches:
                    break
                b = _utf8_len(e_txt) + 1
                if used_f + b > fb:
                    break
                per_file_shown[f_idx].append(e_txt)
                per_file_shown_hits[f_idx] += e_cnt
                total_shown_hits += e_cnt
                used_f += b

    # Assemble per-file blocks with concise per-file continuation hints if hits remain
    out_parts: list[str] = []
    used_final = 0
    omitted_summary_files = 0
    omitted_summary_hits = 0
    for idx, rec in enumerate(file_records):
        chunk = "\n".join(per_file_shown[idx])
        out_parts.append(chunk)
        used_final += _utf8_len(chunk) + 1
        rem_in_file = rec["total_count"] - per_file_shown_hits[idx]
        if rem_in_file > 0:
            shown_e_cnt = len(per_file_shown[idx])
            if shown_e_cnt < len(rec["entries"]):
                next_ln = rec["entries"][shown_e_cnt][0]
            else:
                next_ln = rec["entries"][-1][1] + 1
            last_ln = max(next_ln, rec["last_hit_ln"])
            hint = f"  ... [+{rem_in_file} more in {os.path.basename(rec['fp'])}; read_file L{next_ln}-L{min(last_ln, next_ln + 799)}]"
            hb = _utf8_len(hint) + 1
            if used_final + hb <= body_budget - 60:
                out_parts.append(hint)
                used_final += hb
            else:
                omitted_summary_files += 1
                omitted_summary_hits += rem_in_file

    if omitted_summary_files > 0:
        out_parts.append(
            f"... [+{omitted_summary_hits} additional hits across {omitted_summary_files} files omitted (8.8KB cap); use read_file or narrow pattern]"
        )

    body = "\n".join(out_parts)
    return f"{section_header}\n{body}" if section_header else body


def handle_grep(args: dict, plugin_data: str | None = None) -> tuple[str, bool]:
    t0 = time.perf_counter()
    query_list, dropped_queries = _extract_grep_queries(args)
    if not query_list:
        return "参数错误：请提供非空的 pattern 字符串或 queries 字符串数组（最多 15 条）", True

    targets = _extract_path_list(args)
    if not targets:
        return "参数错误：请提供 paths 搜索目录或文件列表", True

    raw_mode = str(args.get("output_mode") or "matches").strip().lower()
    mode_map = {
        "matches": "matches",
        "content": "matches",
        "files_only": "files_only",
        "files_with_matches": "files_only",
        "count": "count",
    }
    output_mode = mode_map.get(raw_mode)
    if output_mode is None:
        return f'参数错误：无效的 output_mode="{raw_mode}"，仅支持 "matches"、"files_only"、"count"', True

    is_regex = bool(args.get("is_regex", True))
    case_sensitive = bool(args.get("case_sensitive", True))
    ctx_lines = _safe_int(args.get("context_lines"), 0, 0, 5)
    max_matches = _safe_int(args.get("max_matches"), 120, 1, 400)
    include_globs = _expand_glob_patterns(str(args.get("include_glob") or ""))
    raw_excl = args.get("exclude_dirs")
    extra_excl = {str(d).strip().lower() for d in raw_excl if isinstance(d, str)} if isinstance(raw_excl, list) else set()

    compiled: list[tuple[str, re.Pattern, str, bool]] = []
    flags = 0 if case_sensitive else re.IGNORECASE
    for q_str in query_list:
        try:
            rx = re.compile(q_str if is_regex else re.escape(q_str), flags)
        except re.error as exc:
            return f'正则表达式语法错误 ({q_str!r}): {exc}（若需纯字面量搜索请设 is_regex=false）', True
        is_pure_lit = (not is_regex) or (not _REGEX_META_CHARS.intersection(q_str))
        lit_needle = q_str if (is_pure_lit and case_sensitive and "\n" not in q_str and "\r" not in q_str) else ""
        compiled.append((q_str, rx, lit_needle, is_pure_lit))

    files, path_errors = _collect_search_files(targets, include_globs, extra_excl)
    if not files and path_errors:
        return _enforce_hard_cap("搜索路径校验未通过:\n" + "\n".join(f"- {e}" for e in path_errors), MAX_OUTPUT_BYTES), True

    per_file_store_cap = min(max_matches, 60)

    def _scan_file(fp: str) -> list[tuple[int, dict]]:
        try:
            with open(fp, "rb") as fh:
                raw = fh.read(MAX_GREP_FILE_SIZE)
            text = decode_text_bytes(raw, truncated=(len(raw) == MAX_GREP_FILE_SIZE))
            del raw
            if not text:
                return []

            candidate_qs: list[int] = []
            for qi, (_, rx, lit_needle, is_pure_lit) in enumerate(compiled):
                if lit_needle:
                    if lit_needle in text:
                        candidate_qs.append(qi)
                elif is_pure_lit:
                    if rx.search(text):
                        candidate_qs.append(qi)
                else:
                    candidate_qs.append(qi)
            if not candidate_qs:
                return []

            lines = text.splitlines()
            del text
            out_hits: list[tuple[int, dict]] = []

            for qi in candidate_qs:
                _, rx, lit_needle, _ = compiled[qi]
                if output_mode == "files_only":
                    first_ln = 0
                    for idx, line in enumerate(lines):
                        if (lit_needle in line) if lit_needle else bool(rx.search(line)):
                            first_ln = idx + 1
                            break
                    if first_ln:
                        out_hits.append((qi, {"fp": fp, "total_count": 1, "last_hit_ln": first_ln, "entries": []}))
                    continue

                if output_mode == "count":
                    cnt = 0
                    last_ln = 0
                    for idx, line in enumerate(lines):
                        if (lit_needle in line) if lit_needle else bool(rx.search(line)):
                            cnt += 1
                            last_ln = idx + 1
                    if cnt:
                        out_hits.append((qi, {"fp": fp, "total_count": cnt, "last_hit_ln": last_ln, "entries": []}))
                    continue

                # output_mode == "matches"
                kept_hits: list[int] = []
                total_in_file = 0
                last_hit_ln = 0
                for idx, line in enumerate(lines):
                    if (lit_needle in line) if lit_needle else bool(rx.search(line)):
                        total_in_file += 1
                        last_hit_ln = idx + 1
                        if len(kept_hits) < per_file_store_cap:
                            kept_hits.append(idx)
                if not kept_hits:
                    continue

                entries: list[tuple[int, int, str, int]] = []
                if ctx_lines == 0:
                    for h in kept_hits:
                        lt = lines[h]
                        if len(lt) > 240:
                            lt = lt[:240] + " ..."
                        entries.append((h + 1, h + 1, f"{fp}:{h + 1}: {lt}", 1))
                else:
                    windows: list[tuple[int, int]] = []
                    hit_set = set(kept_hits)
                    for h in kept_hits:
                        ws, we = max(0, h - ctx_lines), min(len(lines) - 1, h + ctx_lines)
                        if windows and ws <= windows[-1][1] + 1:
                            windows[-1] = (windows[-1][0], max(windows[-1][1], we))
                        else:
                            windows.append((ws, we))
                    for wi, (ws, we) in enumerate(windows):
                        w_lines: list[str] = ["--"] if wi > 0 else []
                        w_hits = 0
                        for li in range(ws, we + 1):
                            is_h = li in hit_set
                            if is_h:
                                w_hits += 1
                            sep = ":" if is_h else "-"
                            lt = lines[li]
                            if len(lt) > 240:
                                lt = lt[:240] + " ..."
                            w_lines.append(f"{fp}{sep}{li + 1}{sep} {lt}")
                        entries.append((ws + 1, we + 1, "\n".join(w_lines), max(1, w_hits)))

                out_hits.append((qi, {"fp": fp, "total_count": total_in_file, "last_hit_ln": last_hit_ln, "entries": entries}))
            return out_hits
        except Exception:
            return []

    if len(files) <= 24:
        scanned = [_scan_file(fp) for fp in files]
    else:
        pool = _get_io_pool()
        scanned = []
        for i in range(0, len(files), 64):
            scanned.extend(pool.map(_scan_file, files[i : i + 64]))

    per_query_records: list[list[dict]] = [[] for _ in compiled]
    for file_hit_list in scanned:
        for qi, rec in file_hit_list:
            per_query_records[qi].append(rec)

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    update_fast_io_stats(plugin_data, grepCalls=1, savedSecEstimate=round(2.2 + max(0, len(compiled) - 1) * 1.6, 2))

    warn_prefix = ("路径提示: " + _clip_utf8_bytes("; ".join(path_errors), 320) + "\n") if path_errors else ""
    drop_note = f"[提示：单次最多 {MAX_GREP_QUERIES} 条 queries，已忽略其余 {dropped_queries} 条]\n" if dropped_queries else ""
    mode_tag = f" · mode={output_mode}" if output_mode != "matches" else ""

    if len(compiled) == 1:
        recs = per_query_records[0]
        total_hits = sum(r["total_count"] for r in recs)
        top_hdr = f"{warn_prefix}{drop_note}[{total_hits} matches in {len(recs)}/{len(files)} files{mode_tag} · {elapsed_ms:.1f}ms]"
        if not recs:
            return _enforce_hard_cap(top_hdr, MAX_OUTPUT_BYTES), False
        avail = MAX_OUTPUT_BYTES - _utf8_len(top_hdr) - 4
        body = _render_grep_single_query(recs, output_mode, max_matches, avail, "")
        return _enforce_hard_cap(f"{top_hdr}\n{body}", MAX_OUTPUT_BYTES), False

    # Multi-query fair water-fill across queries
    grand_hits = sum(sum(r["total_count"] for r in q_recs) for q_recs in per_query_records)
    top_hdr = f"{warn_prefix}{drop_note}[{len(compiled)} queries · {grand_hits} matches across {len(files)} files{mode_tag} · {elapsed_ms:.1f}ms]"
    sec_headers: list[str] = []
    q_needs: list[int] = []
    for qi, (q_str, _, _, _) in enumerate(compiled):
        recs = per_query_records[qi]
        q_total = sum(r["total_count"] for r in recs)
        q_disp = q_str if len(q_str) <= 60 else (q_str[:57] + "...")
        sh = f'## [Q{qi + 1}] "{q_disp}" ({q_total} matches in {len(recs)} files)'
        sec_headers.append(sh)
        if not recs:
            q_needs.append(_utf8_len(sh))
        elif output_mode == "files_only":
            q_needs.append(_utf8_len(sh) + 1 + sum(_utf8_len(r["fp"]) + 1 for r in recs))
        elif output_mode == "count":
            q_needs.append(_utf8_len(sh) + 1 + sum(_utf8_len(f"{r['fp']}: {r['total_count']}") + 1 for r in recs))
        else:
            q_needs.append(_utf8_len(sh) + 1 + sum(sum(_utf8_len(e[2]) + 1 for e in r["entries"]) for r in recs))

    sep_total = max(0, len(compiled) - 1) * 2
    avail_all = max(0, MAX_OUTPUT_BYTES - _utf8_len(top_hdr) - 1 - sep_total - 16)
    q_budgets = _compute_water_fill_budgets(q_needs, avail_all)

    sections: list[str] = []
    for qi in range(len(compiled)):
        sections.append(_render_grep_single_query(per_query_records[qi], output_mode, max_matches, q_budgets[qi], sec_headers[qi]))
    return _enforce_hard_cap(top_hdr + "\n" + "\n\n".join(sections), MAX_OUTPUT_BYTES), False


# --- Tool 3: fast_io_glob_tree ---

def _fuzzy_score(query: str, rel_path: str, filename: str) -> int | None:
    q = query.strip().lower()
    if not q:
        return 0
    rel_l = rel_path.lower()
    name_l = filename.lower()
    tokens = [t for t in q.split() if t]
    if not tokens:
        return 0

    total_score = 0
    for tok in tokens:
        pos = name_l.find(tok)
        if pos >= 0:
            total_score += 120 + len(tok) * 10 - pos * 2
            if name_l == tok:
                total_score += 180
            continue
        pos_rel = rel_l.find(tok)
        if pos_rel >= 0:
            total_score += 70 + len(tok) * 6 - pos_rel
            continue

        # Subsequence match against rel_path (preferring basename & word boundaries)
        ti = 0
        t_len = len(tok)
        streak = 0
        sub_score = 0
        name_start = len(rel_l) - len(name_l)
        for idx, ch in enumerate(rel_l):
            if ch == tok[ti]:
                streak += 1
                sub_score += 10 + (streak - 1) * 8
                if idx >= name_start:
                    sub_score += 8
                if idx == 0 or rel_l[idx - 1] in "/_-. " or (rel_path[idx].isupper() and rel_path[idx - 1].islower()):
                    sub_score += 14
                ti += 1
                if ti == t_len:
                    break
            else:
                streak = 0
        if ti < t_len:
            return None
        total_score += sub_score

    # Prefer shorter, shallower paths on ties
    total_score -= len(rel_path) // 2 + rel_path.count("/") * 3
    return total_score


def handle_glob_tree(args: dict, plugin_data: str | None = None) -> tuple[str, bool]:
    t0 = time.perf_counter()
    raw_root = args.get("root_path") or args.get("path") or ""
    ok, root, err = check_safe_local_path(raw_root, allow_directory=True)
    if not ok:
        return f"路径错误: {err}", True
    if not os.path.exists(root):
        return f"目录不存在: {root}", True
    if not os.path.isdir(root):
        return f"目标不是目录: {root}", True
    root_ok, root_err = _check_safe_scan_root(root)
    if not root_ok:
        return root_err, True

    raw_fuzzy = args.get("fuzzy")
    raw_pattern = str(args.get("pattern") or "").strip()
    if isinstance(raw_fuzzy, bool):
        fuzzy_query = raw_pattern if raw_fuzzy else ""
        globs = [] if raw_fuzzy else _expand_glob_patterns(raw_pattern)
    else:
        fuzzy_query = str(raw_fuzzy or "").strip()
        globs = _expand_glob_patterns(raw_pattern)

    if globs:
        import fnmatch

    default_depth = 8 if fuzzy_query else 4
    max_depth = _safe_int(args.get("max_depth"), default_depth, 1, 12)
    max_entries = _safe_int(args.get("max_entries"), 80 if fuzzy_query else 200, 1, 500)

    gi = _GitIgnoreMatcher(root)
    entries_out: list[str] = []
    truncated_by_entries = False

    if fuzzy_query:
        scored: list[tuple[int, str, int]] = []
        scanned_limit = 6000
        scanned_files = 0

        def _walk_fuzzy(cur_dir: str, depth: int) -> None:
            nonlocal scanned_files
            if depth > max_depth or scanned_files >= scanned_limit:
                return
            try:
                with os.scandir(cur_dir) as it:
                    items = list(it)
            except OSError:
                return
            items.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
            for entry in items:
                if scanned_files >= scanned_limit:
                    return
                try:
                    if entry.is_symlink():
                        continue
                    is_dir = entry.is_dir(follow_symlinks=False)
                    if gi.is_ignored(entry.path, entry.name, is_dir):
                        continue
                    if is_dir:
                        _walk_fuzzy(entry.path, depth + 1)
                    else:
                        scanned_files += 1
                        rel = os.path.relpath(entry.path, root).replace("\\", "/")
                        if globs and not any(
                            fnmatch.fnmatch(entry.name, g) or fnmatch.fnmatch(rel, g) or (g.startswith("**/") and fnmatch.fnmatch(rel, g[3:]))
                            for g in globs
                        ):
                            continue
                        sc = _fuzzy_score(fuzzy_query, rel, entry.name)
                        if sc is not None:
                            scored.append((sc, rel, entry.stat(follow_symlinks=False).st_size))
                except OSError:
                    continue

        _walk_fuzzy(root, 1)
        scored.sort(key=lambda item: (-item[0], len(item[1]), item[1].lower()))
        total_matched = len(scored)
        if total_matched > max_entries:
            truncated_by_entries = True
        entries_out = [f"{rel}  ({_fmt_size(sz)})" for _, rel, sz in scored[:max_entries]]
        extra_omitted_count = max(0, total_matched - len(entries_out))
    else:
        extra_omitted_count = 0

        def _walk(cur_dir: str, depth: int) -> None:
            nonlocal truncated_by_entries
            if depth > max_depth or len(entries_out) >= max_entries:
                if len(entries_out) >= max_entries:
                    truncated_by_entries = True
                return
            try:
                with os.scandir(cur_dir) as it:
                    items = list(it)
            except OSError:
                return
            items.sort(key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
            for entry in items:
                if len(entries_out) >= max_entries:
                    truncated_by_entries = True
                    return
                try:
                    if entry.is_symlink():
                        continue
                    is_dir = entry.is_dir(follow_symlinks=False)
                    if gi.is_ignored(entry.path, entry.name, is_dir):
                        continue
                    rel = os.path.relpath(entry.path, root).replace("\\", "/")
                    if globs:
                        if is_dir:
                            _walk(entry.path, depth + 1)
                        elif any(fnmatch.fnmatch(entry.name, g) or fnmatch.fnmatch(rel, g) or (g.startswith("**/") and fnmatch.fnmatch(rel, g[3:])) for g in globs):
                            entries_out.append(f"{rel}  ({_fmt_size(entry.stat(follow_symlinks=False).st_size)})")
                    elif is_dir:
                        entries_out.append(f"{rel}/")
                        _walk(entry.path, depth + 1)
                    else:
                        entries_out.append(f"{rel}  ({_fmt_size(entry.stat(follow_symlinks=False).st_size)})")
                except OSError:
                    continue

        _walk(root, 1)

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    update_fast_io_stats(plugin_data, globCalls=1, savedSecEstimate=2.0)

    mode_lbl = f' fuzzy="{fuzzy_query[:40]}"' if fuzzy_query else ""
    if not entries_out:
        header = f"=== {root} [0 entries{mode_lbl} · {elapsed_ms:.1f}ms] ==="
        return _enforce_hard_cap(f"{header}\n(无匹配条目)", MAX_OUTPUT_BYTES), False

    # Enforce 8,800 B hard cap cleanly
    est_hdr = f"=== {root} [{len(entries_out)} entries{mode_lbl} (truncated at {max_entries}) · {elapsed_ms:.1f}ms] ==="
    foot_reserve = 100
    body_budget = max(0, MAX_OUTPUT_BYTES - _utf8_len(est_hdr) - 2)

    kept: list[str] = []
    used = 0
    byte_omitted = 0
    for idx, line in enumerate(entries_out):
        b = _utf8_len(line) + 1
        rem_after = len(entries_out) - idx - 1
        limit = body_budget - (foot_reserve if (rem_after > 0 or truncated_by_entries) else 0)
        if used + b > limit and kept:
            byte_omitted = len(entries_out) - idx
            break
        if used + b > body_budget:
            byte_omitted = len(entries_out) - idx
            break
        kept.append(line)
        used += b

    trunc_tag = ""
    if byte_omitted > 0:
        trunc_tag = " (8.8KB cap)"
    elif truncated_by_entries:
        trunc_tag = f" (capped at {max_entries})"

    header = f"=== {root} [{len(kept)} entries{mode_lbl}{trunc_tag} · {elapsed_ms:.1f}ms] ==="
    footer = ""
    total_omitted = byte_omitted + extra_omitted_count
    if byte_omitted > 0 or truncated_by_entries:
        more_txt = f"+{total_omitted} more" if total_omitted > 0 else "more"
        footer = f"\n... [truncated ({more_txt} entries); narrow root_path, set pattern/fuzzy, or lower max_depth]"
    return _enforce_hard_cap(header + "\n" + "\n".join(kept) + footer, MAX_OUTPUT_BYTES), False


# --- Tool 4: fast_io_outline ---

_PY_FALLBACK_RE = re.compile(r"^(\s*)(async\s+def|def|class)\s+([A-Za-z_]\w*)\s*(\([^)\r\n]*\))?", re.MULTILINE)


def _outline_python(text: str) -> list[tuple[int, str, int]]:
    import ast

    out: list[tuple[int, str, int]] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        for m in _PY_FALLBACK_RE.finditer(text):
            ln = text.count("\n", 0, m.start()) + 1
            indent_lvl = 0 if len(m.group(1)) == 0 else 1
            out.append((indent_lvl, f"{'  ' * (len(m.group(1)) // 4)}[L{ln}] {m.group(2)} {m.group(3)}{m.group(4) or ''}", ln))
        return out

    def _args(a) -> str:
        ns = [x.arg for x in getattr(a, "posonlyargs", []) + a.args]
        if a.vararg:
            ns.append("*" + a.vararg.arg)
        ns.extend(x.arg for x in a.kwonlyargs)
        if a.kwarg:
            ns.append("**" + a.kwarg.arg)
        return f"({', '.join(ns[:6])}{', ...' if len(ns) > 6 else ''})"

    for node in tree.body:
        el = getattr(node, "end_lineno", node.lineno)
        if isinstance(node, ast.ClassDef):
            out.append((0, f"[L{node.lineno}-L{el}] class {node.name}", node.lineno))
            for ch in node.body:
                if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    kw = "async def" if isinstance(ch, ast.AsyncFunctionDef) else "def"
                    out.append((1, f"  [L{ch.lineno}-L{getattr(ch, 'end_lineno', ch.lineno)}] {kw} {ch.name}{_args(ch.args)}", ch.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kw = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            out.append((0, f"[L{node.lineno}-L{el}] {kw} {node.name}{_args(node.args)}", node.lineno))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.isupper() and len(t.id) >= 3:
                    out.append((0, f"[L{node.lineno}-L{el}] const {t.id}", node.lineno))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id.isupper() and len(node.target.id) >= 3:
            out.append((0, f"[L{node.lineno}-L{el}] const {node.target.id}", node.lineno))
    del tree
    return out


def _find_brace_block_end(lines: list[str], start_idx: int) -> int:
    depth, entered = 0, False
    for idx in range(start_idx, min(len(lines), start_idx + 1200)):
        ln = lines[idx]
        opens, closes = ln.count("{"), ln.count("}")
        if not opens and not closes:
            continue
        if opens and not closes:
            depth += opens
            entered = True
        elif closes and not opens:
            if entered:
                depth -= closes
                if depth <= 0:
                    return idx + 1
        else:
            for ch in ln:
                if ch == "{":
                    depth += 1
                    entered = True
                elif ch == "}" and entered:
                    depth -= 1
                    if depth <= 0:
                        return idx + 1
    return start_idx + 1


_JS_TS_PATTERNS = (
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(class|interface|type|enum)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("),
    re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>"),
    re.compile(r"^(?:\s{2,8}|\t)(?:public\s+|private\s+|protected\s+|static\s+|async\s+|get\s+|set\s+)*([A-Za-z_$][\w$]*)\s*\([^;]*\)\s*(?::\s*[^{]+)?\{"),
)
_JS_KEYWORDS = frozenset({"if", "for", "while", "switch", "catch", "return", "sizeof", "typeof", "function"})


def _outline_js_ts(lines: list[str], line_offset: int = 0) -> list[tuple[int, str, int]]:
    out: list[tuple[int, str, int]] = []
    for idx, line in enumerate(lines):
        if len(out) >= 180:
            break
        s = line.rstrip()
        if not s or s.lstrip().startswith(("//", "/*", "*")):
            continue
        indent = len(line) - len(line.lstrip(" \t"))
        ln1 = idx + 1 + line_offset
        m0 = _JS_TS_PATTERNS[0].match(s)
        if m0:
            el = (_find_brace_block_end(lines, idx) if "{" in s else idx + 1) + line_offset
            out.append((0 if indent == 0 else 1, f"[L{ln1}-L{el}] {m0.group(1)} {m0.group(2)}", ln1))
            continue
        if "function" in s:
            m1 = _JS_TS_PATTERNS[1].match(s)
            if m1:
                el = _find_brace_block_end(lines, idx) + line_offset
                out.append((0 if indent == 0 else 1, f"[L{ln1}-L{el}] {'async function' if m1.group(1) else 'function'} {m1.group(2)}()", ln1))
                continue
        if "=>" in s:
            m2 = _JS_TS_PATTERNS[2].match(s)
            if m2:
                el = (_find_brace_block_end(lines, idx) if "{" in s else idx + 1) + line_offset
                out.append((0 if indent == 0 else 1, f"[L{ln1}-L{el}] const {m2.group(1)} =>", ln1))
                continue
        if "(" in s and "{" in s:
            m3 = _JS_TS_PATTERNS[3].match(s)
            if m3 and m3.group(1) not in _JS_KEYWORDS:
                el = _find_brace_block_end(lines, idx) + line_offset
                out.append((1, f"  [L{ln1}-L{el}] method {m3.group(1)}()", ln1))
    return out


_CS_NAMESPACE_RE = re.compile(r"^\s*namespace\s+([A-Za-z_][\w.]*)")
_CS_TYPE_RE = re.compile(r"^\s*(?:(?:public|internal|private|protected|static|sealed|abstract|partial)\s+)*(class|struct|interface|enum|record)\s+([A-Za-z_]\w*)")
_CS_METHOD_RE = re.compile(
    r"^\s*(?:(?:public|internal|private|protected|static|virtual|override|async|extern|unsafe|new|readonly)\s+)+[\w<>\[\]?,.]+(?:\s*,\s*[\w<>\[\]?,.]+)*\s+([A-Za-z_]\w*)\s*\(([^)]*)\)"
)
_CS_TYPE_KEYWORDS = ("class ", "struct ", "interface ", "enum ", "record ")
_CS_MODIFIER_HEADS = frozenset({
    "public", "internal", "private", "protected", "static", "virtual", "override",
    "async", "extern", "unsafe", "new", "readonly", "sealed", "abstract", "partial",
})


def _outline_csharp(lines: list[str]) -> list[tuple[int, str, int]]:
    out: list[tuple[int, str, int]] = []
    for idx, line in enumerate(lines):
        if len(out) >= 180:
            break
        s = line.strip()
        if not s or s[0] in ("/", "[", "#", "{", "}"):
            continue
        ft = s.split(None, 1)[0]
        if ft == "namespace":
            mn = _CS_NAMESPACE_RE.match(line)
            if mn:
                out.append((0, f"[L{idx + 1}-L{_find_brace_block_end(lines, idx)}] namespace {mn.group(1)}", idx + 1))
                continue
        if any(kw in s for kw in _CS_TYPE_KEYWORDS):
            mt = _CS_TYPE_RE.match(line)
            if mt:
                out.append((0, f"[L{idx + 1}-L{_find_brace_block_end(lines, idx)}] {mt.group(1)} {mt.group(2)}", idx + 1))
                continue
        if "(" in s and ft in _CS_MODIFIER_HEADS:
            mm = _CS_METHOD_RE.match(line)
            if mm and mm.group(1) not in _JS_KEYWORDS:
                has_body = ("{" in s) or (not s.endswith(";") and idx + 1 < len(lines) and "{" in lines[idx + 1])
                out.append((1, f"  [L{idx + 1}-L{_find_brace_block_end(lines, idx) if has_body else idx + 1}] {mm.group(1)}()", idx + 1))
    return out


_GO_RE = re.compile(r"^\s*(?:type\s+([A-Za-z_]\w*)\s+(struct|interface)|func\s+(?:\([^)]+\)\s*)?([A-Za-z_]\w*)\s*\()")
_RS_RE = re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(mod|struct|enum|trait|impl|fn)\s+([A-Za-z_][\w<>:]*)")
_MD_HEAD_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$")
_HTML_TAG_RE = re.compile(r"<\s*(style|script|template|header|section|footer|main|nav)\b([^>]*)>", re.IGNORECASE)
_HTML_ID_RE = re.compile(r'\bid\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_VUE_SFC_TAG_RE = re.compile(r"^<\s*(template|script|style)\b([^>]*)>", re.IGNORECASE)
_VUE_TMPL_OPEN_RE = re.compile(r"<\s*template\b[^>]*>", re.IGNORECASE)
_VUE_TMPL_CLOSE_RE = re.compile(r"</\s*template\b[^>]*>", re.IGNORECASE)
_VUE_EXPORT_RE = re.compile(r"^\s*(export\s+default\b|export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)|(?:const\s+\w+\s*=\s*)?(defineProps|defineEmits|defineExpose|defineModel|defineOptions)\b)")
_YAML_KEY_RE = re.compile(r'^([ \t]*)([A-Za-z0-9_.\-/]+|"[^"]+"|\'[^\']+\')\s*:(?:\s+(.*)|\s*)$')
_LINE_RANGE_SHIFT_RE = re.compile(r"\[L(\d+)-L(\d+)\]")


def _outline_go_or_rust(lines: list[str], is_go: bool) -> list[tuple[int, str, int]]:
    out: list[tuple[int, str, int]] = []
    rx = _GO_RE if is_go else _RS_RE
    for idx, line in enumerate(lines):
        if len(out) >= 180:
            break
        m = rx.match(line)
        if m:
            indent = len(line) - len(line.lstrip(" \t"))
            el = _find_brace_block_end(lines, idx) if "{" in line else idx + 1
            lbl = (f"type {m.group(1)} {m.group(2)}" if m.group(1) else f"func {m.group(3)}()") if is_go else f"{m.group(1)} {m.group(2)}"
            out.append((0 if indent == 0 else 1, f"[L{idx + 1}-L{el}] {lbl}", idx + 1))
    return out


def _outline_markdown(lines: list[str]) -> list[tuple[int, str, int]]:
    in_fence, headings = False, []
    for idx, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
        elif not in_fence:
            m = _MD_HEAD_RE.match(line)
            if m:
                headings.append((idx + 1, len(m.group(1)), m.group(2)))
    out: list[tuple[int, str, int]] = []
    for i, (ln, lvl, title) in enumerate(headings):
        end_ln = next((nl - 1 for nl, nlvl, _ in headings[i + 1 :] if nlvl <= lvl), len(lines))
        prio = 0 if lvl <= 2 else 1
        out.append((prio, f"{'  ' * (lvl - 1)}[L{ln}-L{end_ln}] {'#' * lvl} {title}", ln))
    return out


def _outline_css(lines: list[str]) -> list[tuple[int, str, int]]:
    out: list[tuple[int, str, int]] = []
    for idx, line in enumerate(lines):
        if len(out) >= 140:
            break
        s = line.strip()
        if not s or s.startswith(("/*", "*", "//", "--")) or (":" in s and not s.startswith((":root", "@media", "@keyframes")) and not s.endswith("{")):
            continue
        if s.endswith("{") and not s.startswith(("from", "to", "0%", "100%")):
            sel = s[:-1].strip()
            if sel:
                indent = len(line) - len(line.lstrip(" \t"))
                out.append((0 if indent == 0 else 1, f"[L{idx + 1}-L{_find_brace_block_end(lines, idx)}] {sel[:90]}", idx + 1))
    return out


def _outline_html(lines: list[str]) -> list[tuple[int, str, int]]:
    out: list[tuple[int, str, int]] = []
    idx, total = 0, len(lines)
    while idx < total:
        m = _HTML_TAG_RE.search(lines[idx])
        if not m:
            idx += 1
            continue
        tag = m.group(1).lower()
        mid = _HTML_ID_RE.search(m.group(2) or "")
        close_needle = f"</{tag}"
        end_idx = next((j for j in range(idx, total) if close_needle in lines[j].lower()), idx)
        out.append((0, f"[L{idx + 1}-L{end_idx + 1}] <{tag}{f'#{mid.group(1)}' if mid else ''}>", idx + 1))
        if tag == "script" and end_idx > idx + 1:
            for lvl, sym, sln in _outline_js_ts(lines[idx + 1 : end_idx], line_offset=idx + 1)[:120]:
                out.append((lvl + 1, "  " + sym, sln))
            idx = end_idx + 1
        elif tag == "style" and end_idx > idx + 1:
            off = idx + 1
            for lvl, csym, cln in _outline_css(lines[off:end_idx])[:30]:
                shifted = _LINE_RANGE_SHIFT_RE.sub(lambda mm: f"[L{int(mm.group(1)) + off}-L{int(mm.group(2)) + off}]", csym)
                out.append((lvl + 1, "  " + shifted, cln + off))
            idx = end_idx + 1
        else:
            idx += 1
    return out


def _outline_json(lines: list[str], text: str) -> list[tuple[int, str, int]]:
    stripped = text.lstrip()
    if not stripped:
        return []
    parsed_meta: dict[str, str] = {}
    root_array_len: int | None = None
    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            for k, v in loaded.items():
                if isinstance(v, dict):
                    parsed_meta[str(k)] = f"object, {len(v)} keys"
                elif isinstance(v, list):
                    parsed_meta[str(k)] = f"array, {len(v)} items"
                elif isinstance(v, bool):
                    parsed_meta[str(k)] = "bool"
                elif v is None:
                    parsed_meta[str(k)] = "null"
                elif isinstance(v, (int, float)):
                    parsed_meta[str(k)] = "number"
                elif isinstance(v, str):
                    parsed_meta[str(k)] = "string"
        elif isinstance(loaded, list):
            root_array_len = len(loaded)
    except Exception:
        pass

    if stripped.startswith("["):
        cnt_lbl = f" ({root_array_len} items)" if root_array_len is not None else ""
        return [(0, f"[L1-L{max(1, len(lines))}] [root array]{cnt_lbl}", 1)]
    if not stripped.startswith("{"):
        return []

    # Lightweight depth-1 scanner for exact [Lstart-Lend] of every top-level JSON key
    out: list[tuple[int, str, int]] = []
    n = len(text)
    i = 0
    ln = 1
    depth = 0
    in_str = False
    esc = False
    expect_key = False
    cur_key: str | None = None
    cur_key_line = 1
    cur_val_type = ""
    await_val_type = False

    def _finish_key(end_ln: int) -> None:
        nonlocal cur_key, cur_val_type
        if cur_key is not None:
            vmeta = parsed_meta.get(cur_key) or cur_val_type or "value"
            out.append((0, f'[L{cur_key_line}-L{max(cur_key_line, end_ln)}] "{cur_key}" ({vmeta})', cur_key_line))
            cur_key = None
            cur_val_type = ""

    while i < n and len(out) < 200:
        ch = text[i]
        if ch == "\n":
            ln += 1
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue

        if ch == '"':
            if depth == 1 and expect_key:
                # Capture key literal
                start_q = i + 1
                k_ln = ln
                j = start_q
                k_esc = False
                while j < n:
                    cj = text[j]
                    if cj == "\n":
                        ln += 1
                    if k_esc:
                        k_esc = False
                    elif cj == "\\":
                        k_esc = True
                    elif cj == '"':
                        break
                    j += 1
                raw_k = text[start_q:j]
                try:
                    cur_key = json.loads('"' + raw_k + '"')
                except Exception:
                    cur_key = raw_k
                cur_key_line = k_ln
                expect_key = False
                i = j + 1
                continue
            if depth == 1 and await_val_type:
                cur_val_type = "string"
                await_val_type = False
            in_str = True
            i += 1
            continue

        if ch == "{":
            if depth == 1 and await_val_type:
                cur_val_type = "object"
                await_val_type = False
            depth += 1
            if depth == 1:
                expect_key = True
        elif ch == "}":
            if depth == 1:
                _finish_key(ln)
            depth = max(0, depth - 1)
        elif ch == "[":
            if depth == 1 and await_val_type:
                cur_val_type = "array"
                await_val_type = False
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif depth == 1 and ch == ":" and cur_key is not None:
            await_val_type = True
        elif depth == 1 and ch == ",":
            _finish_key(ln)
            expect_key = True
            await_val_type = False
        elif depth == 1 and await_val_type and not ch.isspace():
            if ch in "tf":
                cur_val_type = "bool"
            elif ch == "n":
                cur_val_type = "null"
            elif ch in "-0123456789":
                cur_val_type = "number"
            await_val_type = False
        i += 1

    if cur_key is not None and len(out) < 200:
        _finish_key(ln)
    return out


def _outline_yaml(lines: list[str]) -> list[tuple[int, str, int]]:
    top_entries: list[tuple[int, str, str]] = []
    sub_entries: list[tuple[int, str]] = []
    block_indent: int | None = None

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if block_indent is not None:
            if indent > block_indent:
                continue
            block_indent = None
        if stripped in ("---", "..."):
            continue
        if stripped.startswith("-"):
            continue
        m = _YAML_KEY_RE.match(line)
        if not m:
            continue
        ind_str, key, val = m.group(1), m.group(2), (m.group(3) or "").strip()
        if "\t" in ind_str:
            continue
        ind_len = len(ind_str)
        if val in ("|", "|-", "|+", ">", ">-", ">+"):
            block_indent = ind_len
        if ind_len == 0:
            preview = ""
            if val and not val.startswith(("#", "|", ">")):
                clean_v = val.split(" #", 1)[0].strip()
                preview = f": {clean_v[:36]}{'...' if len(clean_v) > 36 else ''}" if clean_v else ""
            top_entries.append((idx + 1, key, preview))
        elif ind_len in (2, 4) and len(sub_entries) < 160:
            sub_entries.append((idx + 1, key))

    out: list[tuple[int, str, int]] = []
    sub_idx = 0
    total_lines = len(lines)
    for i, (ln, key, preview) in enumerate(top_entries):
        next_top_ln = top_entries[i + 1][0] if i + 1 < len(top_entries) else (total_lines + 1)
        end_ln = next_top_ln - 1
        while end_ln > ln and (not lines[end_ln - 1].strip() or lines[end_ln - 1].strip().startswith("#")):
            end_ln -= 1
        out.append((0, f"[L{ln}-L{max(ln, end_ln)}] {key}{preview}", ln))
        while sub_idx < len(sub_entries) and sub_entries[sub_idx][0] < next_top_ln:
            sln, skey = sub_entries[sub_idx]
            if sln > ln:
                out.append((1, f"  [L{sln}] {skey}", sln))
            sub_idx += 1
    return out


def _outline_vue(lines: list[str]) -> list[tuple[int, str, int]]:
    out: list[tuple[int, str, int]] = []
    idx, total = 0, len(lines)
    while idx < total:
        m = _VUE_SFC_TAG_RE.match(lines[idx])
        if not m:
            idx += 1
            continue
        tag = m.group(1).lower()
        attrs = " ".join((m.group(2) or "").strip().split())
        if tag == "template":
            depth = 0
            end_idx = total - 1
            for j in range(idx, total):
                lj = lines[j]
                opens = sum(1 for om in _VUE_TMPL_OPEN_RE.finditer(lj) if not om.group(0).rstrip().endswith("/>"))
                closes = len(_VUE_TMPL_CLOSE_RE.findall(lj))
                depth += opens - closes
                if depth <= 0:
                    end_idx = j
                    break
        else:
            close_needle = f"</{tag}"
            end_idx = next((j for j in range(idx, total) if close_needle in lines[j].lower()), idx)
        attr_suffix = f" {attrs[:48]}" if attrs else ""
        out.append((0, f"[L{idx + 1}-L{end_idx + 1}] <{tag}{attr_suffix}>", idx + 1))

        if tag == "script" and end_idx > idx + 1:
            script_lines = lines[idx + 1 : end_idx]
            off = idx + 1
            js_syms = _outline_js_ts(script_lines, line_offset=off)
            seen_lns = {sln for _, _, sln in js_syms}
            extra_vue: list[tuple[int, str, int]] = []
            for s_i, s_line in enumerate(script_lines):
                abs_ln = s_i + 1 + off
                if abs_ln in seen_lns:
                    continue
                mv = _VUE_EXPORT_RE.match(s_line)
                if mv:
                    lbl = mv.group(3) or (f"export {mv.group(2)}" if mv.group(2) else "export default")
                    el = (_find_brace_block_end(script_lines, s_i) if "{" in s_line else s_i + 1) + off
                    extra_vue.append((1, f"  [L{abs_ln}-L{el}] {lbl}", abs_ln))
            merged = [(lvl + 1, "  " + sym, sln) for lvl, sym, sln in js_syms] + extra_vue
            merged.sort(key=lambda item: item[2])
            out.extend(merged[:120])
            idx = end_idx + 1
        elif tag == "style" and end_idx > idx + 1:
            off = idx + 1
            for lvl, csym, cln in _outline_css(lines[off:end_idx])[:30]:
                shifted = _LINE_RANGE_SHIFT_RE.sub(lambda mm: f"[L{int(mm.group(1)) + off}-L{int(mm.group(2)) + off}]", csym)
                out.append((lvl + 2, "  " + shifted, cln + off))
            idx = end_idx + 1
        else:
            idx = end_idx + 1
    return out


def _render_outline_file_with_budget(rec: dict, budget: int) -> str:
    norm, line_cnt, syms = rec["norm"], rec["line_cnt"], rec["syms"]
    header = f"=== {norm} ({line_cnt} lines, {len(syms)} symbols) ==="
    if not syms:
        return f"{header}\n(未提取到顶层符号定义)"

    hdr_bytes = _utf8_len(header) + 1
    foot_reserve = 96
    body_budget = max(0, budget - hdr_bytes)

    full_body_bytes = sum(_utf8_len(s[1]) + 1 for s in syms)
    if full_body_bytes <= body_budget:
        return header + "\n" + "\n".join(s[1] for s in syms)

    # Prioritize top-level symbols (level 0 -> level 1 -> level 2...) within budget
    usable = max(0, body_budget - foot_reserve)
    selected: set[int] = set()
    used = 0
    levels = sorted({s[0] for s in syms})
    for lvl in levels:
        level_exhausted = False
        for idx, (s_lvl, s_txt, _) in enumerate(syms):
            if s_lvl != lvl:
                continue
            b = _utf8_len(s_txt) + 1
            if used + b > usable:
                level_exhausted = True
                break
            selected.add(idx)
            used += b
        if level_exhausted and lvl == 0:
            break

    lines_out = [syms[i][1] for i in sorted(selected)]
    omitted = [syms[i] for i in range(len(syms)) if i not in selected]
    if omitted:
        min_ln = min(s[2] for s in omitted)
        max_ln = max(s[2] for s in omitted)
        end_hint = min(line_cnt, max(max_ln, min_ln + 799))
        lines_out.append(f"... [+{len(omitted)} symbols omitted (top-level prioritized); read_file L{min_ln}-L{end_hint}]")
    return header + "\n" + "\n".join(lines_out)


def handle_outline(args: dict, plugin_data: str | None = None) -> tuple[str, bool]:
    raw_paths = _extract_path_list(args)
    if not raw_paths:
        return "参数错误：请提供 paths 文件路径数组", True

    omitted_files = max(0, len(raw_paths) - MAX_OUTLINE_FILES)
    trunc_note = f"\n\n[提示：单次最多提取 {MAX_OUTLINE_FILES} 个文件骨架，已忽略其余 {omitted_files} 个]" if omitted_files else ""
    paths = raw_paths[:MAX_OUTLINE_FILES]

    records: list[dict] = []
    ok_count = 0
    for p in paths:
        ok, norm, err = check_safe_local_path(p, allow_directory=False)
        if not ok:
            records.append({"static": f"=== {p} -> ERROR: {err} ==="})
            continue
        if not os.path.isfile(norm):
            records.append({"static": f"=== {norm} -> ERROR: 文件不存在 ==="})
            continue
        try:
            with open(norm, "rb") as fh:
                raw = fh.read(MAX_GREP_FILE_SIZE)
            text = decode_text_bytes(raw, truncated=(len(raw) == MAX_GREP_FILE_SIZE))
            del raw
            if text is None:
                records.append({"static": f"=== {norm} -> SKIPPED: 二进制文件 ==="})
                continue
            lines = text.splitlines()
            line_cnt = len(lines)
            ext = os.path.splitext(norm.lower())[1]
            if ext == ".py":
                syms = _outline_python(text)
            elif ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
                syms = _outline_js_ts(lines)
            elif ext == ".cs":
                syms = _outline_csharp(lines)
            elif ext == ".go":
                syms = _outline_go_or_rust(lines, True)
            elif ext == ".rs":
                syms = _outline_go_or_rust(lines, False)
            elif ext in (".md", ".markdown"):
                syms = _outline_markdown(lines)
            elif ext in (".html", ".htm"):
                syms = _outline_html(lines)
            elif ext in (".css", ".scss"):
                syms = _outline_css(lines)
            elif ext == ".json":
                syms = _outline_json(lines, text)
            elif ext in (".yaml", ".yml"):
                syms = _outline_yaml(lines)
            elif ext == ".vue":
                syms = _outline_vue(lines)
            else:
                syms = [(0, f"(暂未内置 {ext or '无扩展名'} 语法树提取，总计 {line_cnt} 行，请用 read_file 读取)", 1)]
            del text, lines
            ok_count += 1
            records.append({"norm": norm, "line_cnt": line_cnt, "syms": syms})
        except Exception as exc:
            records.append({"static": f"=== {norm} -> ERROR: {exc} ==="})

    sep_bytes = max(0, len(records) - 1) * 2
    trunc_bytes = _utf8_len(trunc_note)
    static_total = 0
    dyn_indices: list[int] = []
    dyn_needs: list[int] = []
    for i, r in enumerate(records):
        if "static" in r:
            r["static"] = _clip_utf8_bytes(r["static"], 320)
            static_total += _utf8_len(r["static"])
        else:
            hdr = f"=== {r['norm']} ({r['line_cnt']} lines, {len(r['syms'])} symbols) ==="
            need = _utf8_len(hdr) + 1 + (sum(_utf8_len(s[1]) + 1 for s in r["syms"]) if r["syms"] else 32)
            dyn_indices.append(i)
            dyn_needs.append(need)

    avail = max(0, MAX_OUTPUT_BYTES - sep_bytes - trunc_bytes - static_total - 24)
    dyn_budgets = _compute_water_fill_budgets(dyn_needs, avail)

    rendered: list[str] = [""] * len(records)
    for i, r in enumerate(records):
        if "static" in r:
            rendered[i] = r["static"]
    for pos, r_idx in enumerate(dyn_indices):
        rendered[r_idx] = _render_outline_file_with_budget(records[r_idx], dyn_budgets[pos])

    if ok_count > 0:
        update_fast_io_stats(plugin_data, outlineCalls=1, savedSecEstimate=round(1.8 + max(0, ok_count - 1) * 1.5, 2))
    return _enforce_hard_cap("\n\n".join(rendered) + trunc_note, MAX_OUTPUT_BYTES), (ok_count == 0)


# --- MCP Tool Schemas & Stdio JSON-RPC 2.0 Loop ---

_READONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": False,
}

MCP_TOOLS = [
    {
        "name": "fast_io_batch_read",
        "description": "并发读多文件小切片（<=8.8KB 零溢盘，公平分配，支持 'path:s-e'；常规/已知行段用并行 read_file）。",
        "annotations": dict(_READONLY_ANNOTATIONS),
        "inputSchema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "start_line": {"type": "integer"},
                            "end_line": {"type": "integer"},
                            "max_lines": {"type": "integer"},
                        },
                        "required": ["path"],
                    },
                },
                "paths": {"type": "array", "items": {"type": "string"}, "description": "路径或 'path:s-e' 简写列表。"},
            },
        },
    },
    {
        "name": "fast_io_grep",
        "description": "多查询并发搜代码（<=8.8KB 零溢盘，queries[<=15]、output_mode、每文件首条公平保障，跳过 .gitignore/二进制）。",
        "annotations": dict(_READONLY_ANNOTATIONS),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "单条搜索词（与 queries 二选一）。"},
                "queries": {"type": "array", "items": {"type": "string"}, "description": "最多 15 条并发搜索词。"},
                "paths": {"type": "array", "items": {"type": "string"}, "description": "目录/文件绝对路径列表。"},
                "output_mode": {"type": "string", "enum": ["matches", "files_only", "count"]},
                "include_glob": {"type": "string", "description": "如 '*.py' 或 '*.{ts,js}'。"},
                "exclude_dirs": {"type": "array", "items": {"type": "string"}},
                "is_regex": {"type": "boolean"},
                "case_sensitive": {"type": "boolean"},
                "context_lines": {"type": "integer"},
                "max_matches": {"type": "integer"},
            },
            "required": ["paths"],
        },
    },
    {
        "name": "fast_io_glob_tree",
        "description": "目录树、通配符定位与 fuzzy 模糊找文件（<=8.8KB 零溢盘，感知 .gitignore）。",
        "annotations": dict(_READONLY_ANNOTATIONS),
        "inputSchema": {
            "type": "object",
            "properties": {
                "root_path": {"type": "string", "description": "起始目录绝对路径。"},
                "pattern": {"type": "string", "description": "可选通配符如 '*.cs'。"},
                "fuzzy": {"type": "string", "description": "可选模糊路径查询如 'pilot host'。"},
                "max_depth": {"type": "integer"},
                "max_entries": {"type": "integer"},
            },
            "required": ["root_path"],
        },
    },
    {
        "name": "fast_io_outline",
        "description": "批量提最多 30 文件骨架及 [Lstart-Lend]（<=8.8KB 顶层优先，支持 py/ts/js/cs/go/rs/md/html/css/json/yaml/vue）。",
        "annotations": dict(_READONLY_ANNOTATIONS),
        "inputSchema": {
            "type": "object",
            "properties": {"paths": {"type": "array", "items": {"type": "string"}, "description": "文件绝对路径列表（<=30）。"}},
            "required": ["paths"],
        },
    },
]


def _dispatch_rpc_request(msg: dict, plugin_data: str | None = None) -> dict | None:
    if "id" not in msg or msg["id"] is None:
        return None
    req_id = msg["id"]
    method = str(msg.get("method") or "")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": str(params.get("protocolVersion") or "2024-11-05"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": MCP_TOOLS}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}

    if method == "tools/call":
        if not load_fast_io_config(plugin_data)["mcpEnabled"]:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": "[Fast I/O MCP 加速当前已在 Flow Pilot 设置抽屉中关闭] 请改用原生 read_file 工具，或在悬浮窗设置中重新开启。"}],
                    "isError": True,
                },
            }
        tname = str(params.get("name") or "").strip()
        targs = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        handlers = {
            "fast_io_batch_read": handle_batch_read,
            "fast_io_grep": handle_grep,
            "fast_io_glob_tree": handle_glob_tree,
            "fast_io_outline": handle_outline,
        }
        fn = handlers.get(tname)
        if fn is None:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": f"Unknown tool: {tname}"}}
        text_out, is_err = fn(targs, plugin_data)
        text_out = _enforce_hard_cap(text_out, MAX_OUTPUT_BYTES)
        return {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": text_out}], "isError": bool(is_err)}}

    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            break
        buf.extend(chunk)
    return bytes(buf)


def read_stdio_frame(stream):
    while True:
        line = stream.readline(MAX_STDIO_FRAME_BYTES + 1)
        if not line:
            return None, "jsonl"
        if len(line) > MAX_STDIO_FRAME_BYTES and not line.endswith(b"\n"):
            # Drain remainder of oversized line to avoid stream desync
            while True:
                extra = stream.readline(65536)
                if not extra or extra.endswith(b"\n"):
                    break
            raise ValueError(f"JSONL frame exceeds {MAX_STDIO_FRAME_BYTES} byte limit")
        stripped = line.strip()
        if not stripped:
            continue
        first = stripped[:1]
        if first in (b"{", b"["):
            return json.loads(stripped.decode("utf-8", errors="replace")), "jsonl"
        if first in (b"C", b"c") and stripped.lower().startswith(b"content-length:"):
            content_len = int(stripped.split(b":", 1)[1].strip())
            if content_len <= 0 or content_len > MAX_STDIO_FRAME_BYTES:
                raise ValueError(f"Invalid Content-Length: {content_len} (max {MAX_STDIO_FRAME_BYTES})")
            while True:
                hline = stream.readline(8192)
                if not hline or hline in (b"\r\n", b"\n"):
                    break
            body = _read_exact(stream, content_len)
            return (json.loads(body.decode("utf-8", errors="replace")), "content-length") if len(body) == content_len else (None, "content-length")


def write_stdio_frame(stream, payload, mode: str) -> None:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8", errors="replace")
    stream.write((f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii") + raw) if mode == "content-length" else (raw + b"\n"))
    stream.flush()


def serve_stdio() -> None:
    global _DEFER_STATS
    _DEFER_STATS = True
    stdin, stdout, last_mode = sys.stdin.buffer, sys.stdout.buffer, "jsonl"
    try:
        while True:
            try:
                parsed, mode = read_stdio_frame(stdin)
                if parsed is None:
                    break
                last_mode = mode
                if isinstance(parsed, dict):
                    resp = _dispatch_rpc_request(parsed)
                    if resp is not None:
                        write_stdio_frame(stdout, resp, mode)
                elif isinstance(parsed, list):
                    resps = [r for item in parsed if isinstance(item, dict) for r in (_dispatch_rpc_request(item),) if r is not None]
                    if resps:
                        write_stdio_frame(stdout, resps, mode)
                flush_pending_stats()
            except (BrokenPipeError, EOFError, KeyboardInterrupt):
                break
            except json.JSONDecodeError as exc:
                try:
                    write_stdio_frame(stdout, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"Parse error: {exc}"}}, last_mode)
                except Exception:
                    break
            except Exception as exc:
                try:
                    sys.stderr.write(f"[fast_io_mcp] frame error: {exc}\n")
                    sys.stderr.flush()
                except Exception:
                    pass
    finally:
        _DEFER_STATS = False
        flush_pending_stats()


# --- Lightweight Local CLI Mode (for Swarm workers without MCP mount) ---

_CLI_TOOL_MAP = {
    "batch_read": handle_batch_read,
    "read": handle_batch_read,
    "fast_io_batch_read": handle_batch_read,
    "grep": handle_grep,
    "search": handle_grep,
    "fast_io_grep": handle_grep,
    "glob_tree": handle_glob_tree,
    "glob": handle_glob_tree,
    "tree": handle_glob_tree,
    "fast_io_glob_tree": handle_glob_tree,
    "outline": handle_outline,
    "fast_io_outline": handle_outline,
}


def _parse_cli_args(tool_key: str, rest: list[str]) -> tuple[dict | None, str]:
    if len(rest) == 1 and rest[0] == "-":
        raw = sys.stdin.buffer.read(MAX_STDIO_FRAME_BYTES).decode("utf-8", errors="replace").strip()
        try:
            obj = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            return None, f"JSON 解析错误 (stdin): {exc}"
        return (obj, "") if isinstance(obj, dict) else (None, "stdin JSON 必须为对象")

    if len(rest) == 1 and rest[0].startswith("{"):
        try:
            obj = json.loads(rest[0])
        except json.JSONDecodeError as exc:
            return None, f"JSON 解析错误: {exc}"
        return (obj, "") if isinstance(obj, dict) else (None, "JSON 参数必须为对象")

    args: dict = {}
    pos: list[str] = []
    paths: list[str] = []
    queries: list[str] = []
    excl: list[str] = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--json" and i + 1 < len(rest):
            try:
                obj = json.loads(rest[i + 1])
            except json.JSONDecodeError as exc:
                return None, f"JSON 解析错误: {exc}"
            if not isinstance(obj, dict):
                return None, "--json 参数必须为对象"
            args.update(obj)
            i += 2
        elif tok in ("-p", "--path", "--paths") and i + 1 < len(rest):
            paths.append(rest[i + 1])
            i += 2
        elif tok in ("-r", "--root", "--root-path") and i + 1 < len(rest):
            args["root_path"] = rest[i + 1]
            i += 2
        elif tok in ("-e", "--pattern") and i + 1 < len(rest):
            args["pattern"] = rest[i + 1]
            i += 2
        elif tok in ("-q", "--query") and i + 1 < len(rest):
            queries.append(rest[i + 1])
            i += 2
        elif tok in ("--mode", "--output-mode") and i + 1 < len(rest):
            args["output_mode"] = rest[i + 1]
            i += 2
        elif tok in ("--glob", "--include-glob") and i + 1 < len(rest):
            args["include_glob"] = rest[i + 1]
            i += 2
        elif tok in ("-f", "--fuzzy") and i + 1 < len(rest):
            args["fuzzy"] = rest[i + 1]
            i += 2
        elif tok in ("--exclude-dir", "--exclude") and i + 1 < len(rest):
            excl.append(rest[i + 1])
            i += 2
        elif tok in ("-C", "--context-lines") and i + 1 < len(rest):
            args["context_lines"] = int(rest[i + 1])
            i += 2
        elif tok == "--max-depth" and i + 1 < len(rest):
            args["max_depth"] = int(rest[i + 1])
            i += 2
        elif tok == "--max-entries" and i + 1 < len(rest):
            args["max_entries"] = int(rest[i + 1])
            i += 2
        elif tok == "--max-matches" and i + 1 < len(rest):
            args["max_matches"] = int(rest[i + 1])
            i += 2
        elif tok == "--start-line" and i + 1 < len(rest):
            args["start_line"] = int(rest[i + 1])
            i += 2
        elif tok == "--end-line" and i + 1 < len(rest):
            args["end_line"] = int(rest[i + 1])
            i += 2
        elif tok == "--max-lines" and i + 1 < len(rest):
            args["max_lines"] = int(rest[i + 1])
            i += 2
        elif tok in ("--no-regex", "--fixed", "-F"):
            args["is_regex"] = False
            i += 1
        elif tok in ("-i", "--ignore-case"):
            args["case_sensitive"] = False
            i += 1
        else:
            pos.append(tok)
            i += 1

    if queries:
        args["queries"] = queries
    if excl:
        args["exclude_dirs"] = excl

    fn = _CLI_TOOL_MAP.get(tool_key)
    if fn is handle_grep:
        if "pattern" not in args and "queries" not in args and pos:
            args["pattern"] = pos.pop(0)
        all_p = paths + pos
        if all_p:
            args.setdefault("paths", all_p)
    elif fn is handle_glob_tree:
        if "root_path" not in args and pos:
            args["root_path"] = pos.pop(0)
        if "pattern" not in args and pos:
            args["pattern"] = pos.pop(0)
    else:
        all_p = paths + pos
        if all_p:
            if len(all_p) == 1 and ("start_line" in args or "end_line" in args or "max_lines" in args):
                args["path"] = all_p[0]
            else:
                args.setdefault("paths", all_p)
    return args, ""


def run_cli(argv: list[str]) -> int:
    usage = (
        f"Flow Pilot Fast I/O CLI v{SERVER_VERSION}\n"
        "Usage: fast_io_mcp.py --cli <batch_read|grep|glob_tree|outline> [JSON | flags & args]\n"
        "Examples:\n"
        "  fast_io_mcp.py --cli outline a.py b.json c.vue\n"
        "  fast_io_mcp.py --cli grep -q foo -q bar --mode count C:/repo\n"
        "  fast_io_mcp.py --cli glob_tree C:/repo --fuzzy 'pilot host'\n"
        "  fast_io_mcp.py --cli batch_read a.py:1-40 b.py:10-30\n"
    )
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.buffer.write(usage.encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0

    tool_key = argv[0].strip().lower()
    fn = _CLI_TOOL_MAP.get(tool_key)
    if fn is None:
        sys.stderr.buffer.write(f"Unknown CLI tool: {argv[0]}\n{usage}".encode("utf-8"))
        sys.stderr.buffer.flush()
        return 2

    try:
        targs, err = _parse_cli_args(tool_key, argv[1:])
    except ValueError as exc:
        targs, err = None, f"参数数值格式错误: {exc}"
    if err or targs is None:
        sys.stderr.buffer.write((err + "\n").encode("utf-8"))
        sys.stderr.buffer.flush()
        return 2

    text_out, is_err = fn(targs)
    text_out = _enforce_hard_cap(text_out, MAX_OUTPUT_BYTES)
    out_stream = sys.stderr.buffer if is_err else sys.stdout.buffer
    out_stream.write((text_out + "\n").encode("utf-8", errors="replace"))
    out_stream.flush()
    return 1 if is_err else 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] in ("--cli", "cli"):
        sys.exit(run_cli(sys.argv[2:]))
    serve_stdio()
