#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ultra-lightweight HTTP Sidecar Gateway, Peer Bus & Desktop HUD Supervisor for Flow Pilot v3.8.3."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import datetime
import hmac
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

sys.dont_write_bytecode = True

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

from engine import FlowPilotEngine, _stat_mtime_ns  # noqa: E402

APP_VERSION = "3.8.3"
HUD_MUTEX_NAME = "Local\\AgyFlowPilot_DesktopHud_SingleInstance_v2"
CREATE_NO_WINDOW = 0x08000000
_FALLBACK_HTML = (
    b"<!doctype html><html><head><meta charset='utf-8'><title>Flow Pilot</title></head>"
    b"<body><p>Flow Pilot UI initializing...</p></body></html>"
)


class _Win32Helper:
    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_LIMITED_INFORMATION = 0x00001000
    WAIT_OBJECT_0 = 0x00000000
    WAIT_FAILED = 0xFFFFFFFF
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5

    def __init__(self) -> None:
        self.available = False
        self.ctypes: Any = None
        self.wintypes: Any = None
        self.kernel32: Any = None
        self.filetime_cls: Any = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            class _FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k32.WaitForSingleObject.restype = wintypes.DWORD
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.CloseHandle.restype = wintypes.BOOL
            k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k32.GetExitCodeProcess.restype = wintypes.BOOL
            k32.GetProcessTimes.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
                ctypes.POINTER(_FILETIME), ctypes.POINTER(_FILETIME),
            ]
            k32.GetProcessTimes.restype = wintypes.BOOL
            k32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
            k32.OpenMutexW.restype = wintypes.HANDLE
            self.ctypes, self.wintypes, self.kernel32, self.filetime_cls = ctypes, wintypes, k32, _FILETIME
            self.available = True
        except Exception:
            self.available = False

    def is_mutex_held(self, mutex_name: str = HUD_MUTEX_NAME) -> bool:
        if not self.available or self.kernel32 is None or self.ctypes is None:
            return False
        h = self.kernel32.OpenMutexW(self.SYNCHRONIZE, False, mutex_name)
        if h:
            self.kernel32.CloseHandle(h)
            return True
        return int(self.ctypes.get_last_error()) == self.ERROR_ACCESS_DENIED

    def _read_ctime_handle(self, h: Any) -> Optional[int]:
        if self.filetime_cls is None:
            return None
        ct, et, kt, ut = self.filetime_cls(), self.filetime_cls(), self.filetime_cls(), self.filetime_cls()
        if self.kernel32.GetProcessTimes(h, self.ctypes.byref(ct), self.ctypes.byref(et), self.ctypes.byref(kt), self.ctypes.byref(ut)):
            return (int(ct.dwHighDateTime) << 32) | int(ct.dwLowDateTime)
        return None

    def get_process_creation_time(self, pid: int) -> Optional[int]:
        if not self.available or pid <= 0:
            return None
        h = self.kernel32.OpenProcess(self.SYNCHRONIZE | self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            return None if self.kernel32.WaitForSingleObject(h, 0) == self.WAIT_OBJECT_0 else self._read_ctime_handle(h)
        finally:
            self.kernel32.CloseHandle(h)

    def is_pid_alive(self, pid: int, expected_ctime: Optional[int] = None) -> bool:
        if pid <= 0:
            return False
        if not self.available:
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

        h = self.kernel32.OpenProcess(self.SYNCHRONIZE | self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            if int(self.ctypes.get_last_error()) != self.ERROR_ACCESS_DENIED:
                return False
            hq = self.kernel32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not hq:
                return int(self.ctypes.get_last_error()) == self.ERROR_ACCESS_DENIED
            try:
                code = self.wintypes.DWORD(0)
                return (int(code.value) == self.STILL_ACTIVE) if self.kernel32.GetExitCodeProcess(hq, self.ctypes.byref(code)) else True
            finally:
                self.kernel32.CloseHandle(hq)

        try:
            wait_rc = self.kernel32.WaitForSingleObject(h, 0)
            if wait_rc == self.WAIT_OBJECT_0:
                return False
            if wait_rc == self.WAIT_FAILED:
                code = self.wintypes.DWORD(0)
                if self.kernel32.GetExitCodeProcess(h, self.ctypes.byref(code)) and int(code.value) != self.STILL_ACTIVE:
                    return False
            if expected_ctime is not None and (cur_ctime := self._read_ctime_handle(h)) is not None and cur_ctime != expected_ctime:
                return False
            return True
        finally:
            self.kernel32.CloseHandle(h)


WIN32 = _Win32Helper()


def _find_edge_executable() -> Optional[Path]:
    if os.name != "nt":
        return None
    for r in (os.environ.get("ProgramFiles(x86)", ""), os.environ.get("ProgramFiles", ""), os.environ.get("LocalAppData", "")):
        if r and (p := Path(r) / "Microsoft" / "Edge" / "Application" / "msedge.exe").is_file():
            return p
    return None


class _PeerNodeCoordinator:
    """Maintains <PLUGIN_DATA>/nodes/<lsInstanceId>.json (15s cadence / 45s TTL) with zero-read single-node fast path."""

    HEARTBEAT_INTERVAL_SEC = 15.0
    NODE_ALIVE_TTL_SEC = 45.0
    STALE_CLEANUP_TTL_SEC = 50.0

    def __init__(self, engine: FlowPilotEngine, bound_port: int) -> None:
        self.engine = engine
        self.sidecar_addr = f"127.0.0.1:{bound_port}"
        self.ls_instance_id = engine.ls_instance_id
        self.pid, self.ppid = os.getpid(), os.getppid()
        self.peer_token = secrets.token_hex(24)
        self.node_file = engine.nodes_dir / f"{self.ls_instance_id}.json"

        self._lock = threading.Lock()
        self._owns_hud = False
        self._stop_event = threading.Event()
        self._self_node: dict[str, Any] = self._make_self_payload(False, time.time())
        self._remote_peers: list[dict[str, Any]] = []
        self._nodes_dir_mtime_ns = -1
        self._last_scandir_at = self._peers_verified_at = 0.0
        self._cluster_rev = 0
        self._last_cluster_calm_fp: Any = None
        self._thread: Optional[threading.Thread] = None
        self._pool: Optional[ThreadPoolExecutor] = None

    def _make_self_payload(self, owns_hud: bool, now: float) -> dict[str, Any]:
        return {
            "lsInstanceId": self.ls_instance_id, "pid": self.pid, "ppid": self.ppid,
            "sidecarAddr": self.sidecar_addr, "lsAddr": self.engine.ls_address,
            "ownsHud": owns_hud, "peerToken": self.peer_token, "updatedAt": round(now, 3),
        }

    def _get_pool(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pilot-peer")
            return self._pool

    def start(self) -> None:
        self.write_heartbeat(owns_hud=False)
        self._thread = threading.Thread(target=self._heartbeat_loop, name="flow-pilot-node-reg", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self.node_file.unlink(missing_ok=True)
        except OSError:
            pass
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

    def set_owns_hud(self, owns: bool) -> None:
        with self._lock:
            if self._owns_hud == owns:
                return
            self._owns_hud = owns
        self.write_heartbeat(owns_hud=owns)

    def write_heartbeat(self, owns_hud: Optional[bool] = None, cleanup_stale: bool = False) -> None:
        now = time.time()
        with self._lock:
            if owns_hud is not None:
                self._owns_hud = owns_hud
            payload = self._make_self_payload(self._owns_hud, now)
            self._self_node = payload
        self.engine._write_json_atomic(self.node_file, payload)
        self._scan_remote_nodes_if_needed(force=True, now=now, cleanup_stale=cleanup_stale)

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(self.HEARTBEAT_INTERVAL_SEC):
            try:
                self.write_heartbeat(cleanup_stale=True)
            except Exception:
                pass

    def _scan_remote_nodes_if_needed(self, force: bool = False, now: Optional[float] = None, cleanup_stale: bool = False) -> list[dict[str, Any]]:
        t_now = time.time() if now is None else now
        dir_str = str(self.engine.nodes_dir)
        dir_ns = _stat_mtime_ns(dir_str)
        if dir_ns <= 0:
            with self._lock:
                self._remote_peers = []
                self._nodes_dir_mtime_ns = 0
                self._last_scandir_at = self._peers_verified_at = t_now
            return []

        with self._lock:
            if not force and not cleanup_stale and dir_ns == self._nodes_dir_mtime_ns and (t_now - self._last_scandir_at) >= 0.05:
                if not self._remote_peers:
                    return []
                if (t_now - self._peers_verified_at) < 2.0:
                    return list(self._remote_peers)

        self_name = self.node_file.name
        peer_entries: list[str] = []
        try:
            with os.scandir(dir_str) as it:
                for e in it:
                    if e.name != self_name and e.name.endswith(".json") and not e.name.startswith("."):
                        peer_entries.append(e.path)
        except OSError:
            peer_entries = []

        remotes: list[dict[str, Any]] = []
        seen_ids: set[str] = {self.ls_instance_id}
        for fpath in peer_entries:
            try:
                p_obj = Path(fpath)
                raw = json.loads(p_obj.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    continue
                nid = str(raw.get("lsInstanceId") or "").strip()
                p_pid = int(raw.get("pid") or 0)
                addr = str(raw.get("sidecarAddr") or "").strip()
                upd = float(raw.get("updatedAt") or 0.0)
                age = t_now - upd
                if cleanup_stale and age > self.STALE_CLEANUP_TTL_SEC and not WIN32.is_pid_alive(p_pid):
                    p_obj.unlink(missing_ok=True)
                    continue
                if not nid or not addr or nid in seen_ids or addr == self.sidecar_addr:
                    continue
                if age <= self.NODE_ALIVE_TTL_SEC and WIN32.is_pid_alive(p_pid):
                    seen_ids.add(nid)
                    remotes.append(raw)
            except Exception:
                continue

        post_ns = _stat_mtime_ns(dir_str)
        with self._lock:
            prev_had = bool(self._remote_peers)
            if post_ns != self._nodes_dir_mtime_ns:
                self._nodes_dir_mtime_ns = post_ns
                self._last_scandir_at = t_now
            self._remote_peers = remotes
            self._peers_verified_at = t_now
        if bool(remotes) != prev_had:
            self.engine.bump_rev()
        return list(remotes)

    def has_remote_peers(self) -> bool:
        return bool(self._scan_remote_nodes_if_needed(force=False))

    def list_alive_nodes(self, include_self: bool = False, force: bool = False) -> list[dict[str, Any]]:
        remotes = self._scan_remote_nodes_if_needed(force=force)
        if not include_self:
            return remotes
        with self._lock:
            return [dict(self._self_node), *remotes]

    def is_election_leader(self) -> bool:
        nodes = self.list_alive_nodes(include_self=True, force=True)
        if len(nodes) <= 1:
            return True
        nodes.sort(key=lambda n: (int(n.get("pid") or 999999999), str(n.get("lsInstanceId") or "")))
        return (int(nodes[0].get("pid") or 0), str(nodes[0].get("lsInstanceId") or "")) == (self.pid, self.ls_instance_id)

    def request_peer(self, peer: dict[str, Any], method: str, path: str, body: Optional[dict[str, Any]] = None, timeout: float = 0.65) -> Optional[dict[str, Any]]:
        addr, tok = str(peer.get("sidecarAddr") or "").strip(), str(peer.get("peerToken") or "").strip()
        if not addr or addr == self.sidecar_addr:
            return None
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json", "X-Peer-Token": tok}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"http://{addr}{path}", data=data, headers=headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                parsed = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
                return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None

    _BUCKET_MERGE_RANK: dict[str, int] = {
        "IDLE": 0,
        "RECENT": 1,
        "RUNNING": 2,
        "ALERT": 2,
    }

    @classmethod
    def _task_merge_priority(cls, task: dict[str, Any]) -> tuple[int, int, int, float]:
        bucket_rank = cls._BUCKET_MERGE_RANK.get(str(task.get("bucket") or "").upper(), 0)
        hb = task.get("heartbeat")
        try:
            hb_step = int(hb.get("stepIndex") or 0) if isinstance(hb, dict) else 0
        except (ValueError, TypeError):
            hb_step = 0
        raw_step = task.get("stepCount")
        try:
            step_cnt = int(raw_step) if raw_step is not None else hb_step
        except (ValueError, TypeError):
            step_cnt = hb_step
        step_cnt = max(step_cnt, hb_step)
        stream_connected = 1 if task.get("streamConnected") else 0
        try:
            last_mod = float(task.get("lastModifiedAt") or 0.0)
        except (ValueError, TypeError):
            last_mod = 0.0
        return (bucket_rank, step_cnt, stream_connected, last_mod)

    def aggregate_all_tasks(self, hint_conversation_id: str = "") -> dict[str, Any]:
        local_payload = self.engine.get_local_tasks_payload(hint_conversation_id=hint_conversation_id)
        remote_peers = self.list_alive_nodes(include_self=False, force=False)
        if not remote_peers:
            return local_payload

        pool = self._get_pool()
        futures = [pool.submit(self.request_peer, peer, "GET", "/api/node-tasks", None, 1.2) for peer in remote_peers]
        by_cid: dict[str, dict[str, Any]] = {
            str(t["conversationId"]): t for t in (local_payload.get("tasks") or []) if isinstance(t, dict) and t.get("conversationId")
        }
        peer_revs: list[int] = [int(local_payload.get("rev") or 0)]
        for fut in futures:
            try:
                peer_res = fut.result(timeout=1.35)
            except Exception:
                peer_res = None
            if not isinstance(peer_res, dict) or not peer_res.get("ok"):
                continue
            peer_revs.append(int(peer_res.get("rev") or 0))
            for pt in peer_res.get("tasks") or []:
                if not isinstance(pt, dict) or not (cid := str(pt.get("conversationId") or "")) or self.engine.is_cid_known_removed(cid):
                    continue
                if (existing := by_cid.get(cid)) is None:
                    by_cid[cid] = pt
                elif self._task_merge_priority(pt) > self._task_merge_priority(existing):
                    by_cid[cid] = pt

        merged_tasks = list(by_cid.values())
        summary_block = FlowPilotEngine.finalize_tasks_and_summary(merged_tasks)
        local_payload["tasks"] = merged_tasks
        local_payload["summary"] = summary_block

        calm_fp = (
            tuple(peer_revs),
            tuple(
                (
                    t.get("taskKey"),
                    t.get("bucket"),
                    t.get("status"),
                    (t.get("heartbeat") or {}).get("state"),
                    bool((t.get("heartbeat") or {}).get("isStalled")),
                    (t.get("heartbeat") or {}).get("runningStepIndex"),
                )
                for t in merged_tasks
            ),
        )
        with self._lock:
            base_rev = max(self._cluster_rev, int(local_payload.get("rev") or 1))
            if (
                summary_block["runningCount"] > 0
                or summary_block["anyWaitingTtft"]
                or summary_block.get("anyGenerating")
                or summary_block["totalLiveTps"] > 0
            ):
                self._cluster_rev, self._last_cluster_calm_fp = base_rev + 1, None
            elif calm_fp != self._last_cluster_calm_fp or self._cluster_rev <= 0:
                self._last_cluster_calm_fp, self._cluster_rev = calm_fp, base_rev + 1
            local_payload["rev"] = self._cluster_rev
        return local_payload

    def route_post_to_owner(self, target_ls_id: str, cid: str, route: str, body: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not (remote_peers := self.list_alive_nodes(include_self=False, force=False)):
            return None
        if target_ls_id and target_ls_id != self.ls_instance_id:
            for p in remote_peers:
                if str(p.get("lsInstanceId") or "") == target_ls_id and (res := self.request_peer(p, "POST", f"{route}?localOnly=1", body, timeout=4.5)) is not None:
                    return res
        if cid and not self.engine.owns_conversation(cid):
            for p in remote_peers:
                if isinstance(res := self.request_peer(p, "POST", f"{route}?localOnly=1", body, timeout=4.5), dict) and (
                    res.get("ok") or res.get("error") == "already_running"
                ):
                    return res
        return None


class _DesktopHudManager:
    """Supervises FlowPilotHost.exe with singleton mutex election, clean pipe release, lifecycle audit & rev notifications."""

    HUD_LOG_MAX_BYTES = 256 * 1024
    HUD_LOG_TRIM_BYTES = 128 * 1024

    def __init__(self, engine: FlowPilotEngine, peer_coord: _PeerNodeCoordinator, bound_port: int) -> None:
        self.engine = engine
        self.peer_coord = peer_coord
        self.bound_port = bound_port
        self.hud_token = secrets.token_hex(24)
        self.disable_spawn = os.environ.get("FLOW_PILOT_DISABLE_HUD_SPAWN", "").strip() == "1"

        self._lock = threading.RLock()
        self._log_lock = threading.Lock()
        self._hud_log_path = self.engine.plugin_data_dir / "hud_lifecycle.log"
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._proc: Optional[subprocess.Popen[bytes]] = None
        self._host_kind = "none"
        self._last_spawn_at = self._next_retry_at = 0.0
        self._last_exit_code: Optional[int] = None
        self._crash_count = 0
        self._last_reported_running: Optional[bool] = None
        self._last_enabled: Optional[bool] = None
        self._logged_disable_spawn = False
        self._unexpected_exit_block_pending = False
        self._edge_bootstraps: dict[str, float] = {}
        self._thread: Optional[threading.Thread] = None

    def create_edge_bootstrap_code(self, ttl_sec: float = 30.0) -> str:
        now = time.monotonic()
        code = secrets.token_hex(16)
        with self._lock:
            self._edge_bootstraps = {k: exp for k, exp in self._edge_bootstraps.items() if exp > now}
            if len(self._edge_bootstraps) >= 16:
                self._edge_bootstraps.clear()
            self._edge_bootstraps[code] = now + ttl_sec
        return code

    def consume_edge_bootstrap_code(self, code: str) -> bool:
        c = (code or "").strip()
        if not c:
            return False
        now = time.monotonic()
        with self._lock:
            self._edge_bootstraps = {k: exp for k, exp in self._edge_bootstraps.items() if exp > now}
            for existing in list(self._edge_bootstraps.keys()):
                if hmac.compare_digest(existing, c):
                    exp = self._edge_bootstraps.pop(existing, 0.0)
                    return exp > now
        return False

    @staticmethod
    def _format_log_value(val: Any) -> str:
        if val is None:
            return "none"
        if isinstance(val, bool):
            return "true" if val else "false"
        if isinstance(val, float):
            return f"{val:.3f}".rstrip("0").rstrip(".") or "0"
        s = str(val).replace("\r", " ").replace("\n", " ").strip()
        if len(s) > 240:
            s = s[:237] + "..."
        if not s:
            return '""'
        if any(ch.isspace() for ch in s):
            return '"' + s.replace('"', "'") + '"'
        return s

    def _log_hud_event(self, event: str, **fields: Any) -> None:
        try:
            now_dt = datetime.datetime.now()
            ts = now_dt.strftime("%Y-%m-%d %H:%M:%S") + f".{now_dt.microsecond // 1000:03d}"
            kv = " ".join(f"{k}={self._format_log_value(v)}" for k, v in fields.items())
            suffix = f" {kv}" if kv else ""
            file_line = f"[{ts}] [server pid={os.getpid()}] {event}{suffix}\n"
            stderr_line = f"[agy-flow-pilot] hud {event}{suffix}\n"
        except Exception:
            return

        try:
            sys.stderr.write(stderr_line)
            sys.stderr.flush()
        except Exception:
            pass

        try:
            line_bytes = file_line.encode("utf-8", errors="replace")
            with self._log_lock:
                self._hud_log_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    cur_size = self._hud_log_path.stat().st_size if self._hud_log_path.exists() else 0
                except OSError:
                    cur_size = 0
                if cur_size + len(line_bytes) > self.HUD_LOG_MAX_BYTES:
                    trimmed = False
                    try:
                        with open(self._hud_log_path, "rb") as rf:
                            seek_pos = max(0, cur_size - self.HUD_LOG_TRIM_BYTES)
                            rf.seek(seek_pos)
                            tail = rf.read()
                        if seek_pos > 0:
                            nl_idx = tail.find(b"\n")
                            if nl_idx >= 0 and nl_idx + 1 < len(tail):
                                tail = tail[nl_idx + 1 :]
                        with open(self._hud_log_path, "wb") as wf:
                            wf.write(tail)
                            wf.write(line_bytes)
                        trimmed = True
                    except Exception:
                        trimmed = False
                    if not trimmed:
                        with open(self._hud_log_path, "ab") as af:
                            af.write(line_bytes)
                else:
                    with open(self._hud_log_path, "ab") as af:
                        af.write(line_bytes)
        except Exception:
            pass

    def start(self) -> None:
        self._thread = threading.Thread(target=self._supervisor_loop, name="flow-pilot-hud-mgr", daemon=True)
        self._thread.start()
        self._wake_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        self._terminate_local_child(reason="sidecar_stop")
        self.peer_coord.set_owns_hud(False)

    @staticmethod
    def _close_proc_stdin(proc: Optional[subprocess.Popen[bytes]]) -> None:
        if proc is not None and proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass

    def _send_stdin_cmd(self, payload: dict[str, Any]) -> bool:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None or proc.stdin is None:
                return False
            try:
                proc.stdin.write((json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                proc.stdin.flush()
                return True
            except Exception:
                return False

    def _terminate_local_child(self, reason: str = "unspecified") -> None:
        with self._lock:
            proc, self._proc, self._host_kind = self._proc, None, "none"
        if proc is None:
            return
        pid = int(proc.pid)
        waited_rc: Optional[int] = proc.poll()
        forced = False
        if waited_rc is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.write(b'{"cmd":"close"}\n')
                    proc.stdin.flush()
            except Exception:
                pass
            try:
                waited_rc = proc.wait(timeout=1.0)
            except Exception:
                forced = True
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    waited_rc = proc.wait(timeout=1.5)
                except Exception:
                    try:
                        proc.kill()
                        waited_rc = proc.wait(timeout=0.5)
                    except Exception:
                        waited_rc = proc.poll()
        self._close_proc_stdin(proc)
        if waited_rc is not None:
            with self._lock:
                self._last_exit_code = int(waited_rc)
        self._log_hud_event("terminate_child", reason=reason, pid=pid, waited_rc=waited_rc, forced=forced)

    def _spawn_hud_child_once(self) -> bool:
        if self._stop_event.is_set():
            return False
        if self.disable_spawn:
            if not self._logged_disable_spawn:
                self._logged_disable_spawn = True
                self._log_hud_event("spawn_blocked", reason="disable_spawn")
            return False
        if WIN32.is_mutex_held(HUD_MUTEX_NAME):
            if self._unexpected_exit_block_pending:
                self._unexpected_exit_block_pending = False
                self._log_hud_event("spawn_blocked", reason="mutex_held_after_unexpected_exit", last_exit_code=self._last_exit_code)
            return False
        host_exe = THIS_DIR / "host" / "FlowPilotHost.exe"
        env = {
            **os.environ,
            "FLOW_PILOT_HUD_TOKEN": self.hud_token,
            "PLUGIN_DATA": str(self.engine.plugin_data_dir),
            "WEBVIEW2_DEFAULT_BACKGROUND_COLOR": "00000000",
        }
        popen_kwargs: dict[str, Any] = {"stdin": subprocess.PIPE, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "env": env}
        if os.name == "nt":
            popen_kwargs["creationflags"] = CREATE_NO_WINDOW

        if host_exe.is_file():
            cmd = [str(host_exe), "--parent-pid", str(os.getpid()), "--port", str(self.bound_port), "--view-mode", "pill"]
            popen_kwargs["cwd"] = str(host_exe.parent)
            kind = "webview2_native"
        else:
            if (edge_exe := _find_edge_executable()) is None:
                self._log_hud_event("spawn_failed", error="no_host_exe_or_edge_found")
                return False
            udf_dir = self.engine.plugin_data_dir / "edge_hud_udf"
            udf_dir.mkdir(parents=True, exist_ok=True)
            bootstrap_code = self.create_edge_bootstrap_code()
            hud_url = f"http://127.0.0.1:{self.bound_port}/hud-bootstrap?mode=hud&code={bootstrap_code}"
            cmd = [str(edge_exe), f"--app={hud_url}", f"--user-data-dir={udf_dir}", "--window-size=388,36", "--no-first-run", "--no-default-browser-check", "--disable-extensions"]
            kind = "edge_app_fallback"

        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
            with self._lock:
                self._proc, self._host_kind, self._last_spawn_at, self._last_reported_running = proc, kind, time.time(), True
                self._logged_disable_spawn = False
                self._unexpected_exit_block_pending = False
            self.peer_coord.set_owns_hud(True)
            self.engine.bump_rev()
            self._log_hud_event("spawn_ok", pid=proc.pid, hostKind=kind, port=self.bound_port)
            return True
        except Exception as exc:
            self._log_hud_event("spawn_failed", error=f"{type(exc).__name__}:{exc}", hostKind=kind)
            return False

    def _supervisor_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                enabled = bool((self.engine.get_config_snapshot().get("hud") or {}).get("enabled", True))
                now = time.time()
                exited_proc: Optional[subprocess.Popen[bytes]] = None
                exited_pid = 0
                exited_rc: Optional[int] = None
                uptime_sec = 0.0
                exit_crash_count = 0
                retry_delay_sec = 0.0
                alive_pid = 0
                prev_enabled = self._last_enabled
                self._last_enabled = enabled

                with self._lock:
                    proc = self._proc
                    rc = proc.poll() if proc is not None else None
                    local_alive = proc is not None and rc is None
                    if local_alive and proc is not None:
                        alive_pid = int(proc.pid)
                    if proc is not None and not local_alive:
                        exited_proc, self._proc, self._host_kind = proc, None, "none"
                        exited_pid = int(proc.pid)
                        exited_rc = int(rc) if rc is not None else None
                        uptime_sec = round(max(0.0, now - self._last_spawn_at), 3) if self._last_spawn_at > 0 else 0.0
                        self._last_exit_code = exited_rc
                        if enabled:
                            if (now - self._last_spawn_at) >= 15.0:
                                self._crash_count = 0
                            self._crash_count += 1
                            retry_delay_sec = min(10.0, 1.5 * (2 ** min(3, self._crash_count - 1)))
                            self._next_retry_at = now + retry_delay_sec
                            self._unexpected_exit_block_pending = True
                        exit_crash_count = self._crash_count

                if exited_proc is not None:
                    self._close_proc_stdin(exited_proc)
                    self._log_hud_event(
                        "child_exited",
                        pid=exited_pid,
                        returncode=exited_rc,
                        uptime_sec=uptime_sec,
                        enabled=enabled,
                        crash_count=exit_crash_count,
                        retry_delay_sec=retry_delay_sec,
                    )
                    self.engine.bump_rev()

                if not enabled:
                    self._unexpected_exit_block_pending = False
                    self._logged_disable_spawn = False
                    if local_alive:
                        if prev_enabled is not False:
                            self._log_hud_event("config_disabled", pid=alive_pid)
                        self._terminate_local_child(reason="config_disabled")
                        self.engine.bump_rev()
                    self.peer_coord.set_owns_hud(False)
                elif local_alive:
                    self._unexpected_exit_block_pending = False
                    self.peer_coord.set_owns_hud(True)
                else:
                    self.peer_coord.set_owns_hud(False)
                    mutex_held = WIN32.is_mutex_held(HUD_MUTEX_NAME)
                    if self.disable_spawn:
                        if not self._logged_disable_spawn:
                            self._logged_disable_spawn = True
                            self._log_hud_event("spawn_blocked", reason="disable_spawn", mutex_held=mutex_held)
                    elif mutex_held:
                        if self._unexpected_exit_block_pending:
                            self._unexpected_exit_block_pending = False
                            self._log_hud_event("spawn_blocked", reason="mutex_held_after_unexpected_exit", last_exit_code=self._last_exit_code)
                    elif now >= self._next_retry_at:
                        if not self.peer_coord.is_election_leader():
                            if self._stop_event.wait(0.6):
                                break
                            mutex_held = WIN32.is_mutex_held(HUD_MUTEX_NAME)
                        if not mutex_held and bool((self.engine.get_config_snapshot().get("hud") or {}).get("enabled", True)):
                            self._spawn_hud_child_once()
                        elif mutex_held and self._unexpected_exit_block_pending:
                            self._unexpected_exit_block_pending = False
                            self._log_hud_event("spawn_blocked", reason="mutex_held_after_unexpected_exit", last_exit_code=self._last_exit_code)
            except Exception:
                pass
            self._wake_event.wait(timeout=2.0)
            self._wake_event.clear()

    def _compute_running_fast(self) -> tuple[bool, bool, int, str]:
        with self._lock:
            proc = self._proc
            local_alive = proc is not None and proc.poll() is None
            pid = int(proc.pid) if (local_alive and proc is not None) else 0
            kind = self._host_kind if local_alive else "none"
        mutex_held = False if local_alive else WIN32.is_mutex_held(HUD_MUTEX_NAME)
        peer_owns = bool(not local_alive and not mutex_held and self.peer_coord.has_remote_peers() and any(bool(p.get("ownsHud")) for p in self.peer_coord.list_alive_nodes(include_self=False)))
        is_running = bool(local_alive or mutex_held or peer_owns)
        return is_running, local_alive, pid, ("remote_peer_hud" if (not local_alive and is_running and kind == "none") else kind)

    def sync_running_rev(self) -> None:
        is_running, _, _, _ = self._compute_running_fast()
        with self._lock:
            changed = self._last_reported_running is not None and self._last_reported_running != is_running
            self._last_reported_running = is_running
        if changed:
            self.engine.bump_rev()

    def get_status(self) -> dict[str, Any]:
        hud_cfg = self.engine.get_config_snapshot().get("hud") or {}
        is_running, local_alive, pid, kind = self._compute_running_fast()
        with self._lock:
            last_exit_code = self._last_exit_code
            self._last_reported_running = is_running
        return {
            **hud_cfg, "running": is_running, "visible": is_running, "ownsHud": local_alive,
            "pid": pid, "hostKind": kind, "lastExitCode": last_exit_code,
        }

    def _forward_to_hud_owner(self, body: dict[str, Any]) -> None:
        for p in self.peer_coord.list_alive_nodes(include_self=False, force=True):
            if bool(p.get("ownsHud")):
                self.peer_coord.request_peer(p, "POST", "/api/hud?localOnly=1", body, timeout=1.2)
                return

    def _wake_remote_hud_peer_for_open(self, source: str) -> None:
        nodes = self.peer_coord.list_alive_nodes(include_self=True, force=True)
        if len(nodes) <= 1:
            return
        target: Optional[dict[str, Any]] = None
        for n in nodes:
            if str(n.get("lsInstanceId") or "") != self.peer_coord.ls_instance_id and bool(n.get("ownsHud")):
                target = n
                break
        if target is None:
            nodes.sort(key=lambda n: (int(n.get("pid") or 999999999), str(n.get("lsInstanceId") or "")))
            if str(nodes[0].get("lsInstanceId") or "") != self.peer_coord.ls_instance_id:
                target = nodes[0]
        if target is not None:
            try:
                self.peer_coord._get_pool().submit(
                    self.peer_coord.request_peer,
                    target,
                    "POST",
                    "/api/hud?localOnly=1",
                    {"action": "open", "enabled": True, "source": f"peer:{source}"},
                    1.0,
                )
            except Exception:
                pass

    def _cfg_resp(self, action: str, cfg_res: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "action": action, "config": cfg_res["config"], "fastIoConfig": cfg_res["config"].get("fastIo"), "hudWindow": self.get_status()}

    def handle_action(self, body: dict[str, Any], local_only: bool = False) -> dict[str, Any]:
        action = str(body.get("action") or "update_prefs").strip().lower()
        source = str(body.get("source") or "api").strip() or "api"
        enabled_before = bool((self.engine.get_config_snapshot().get("hud") or {}).get("enabled", True))
        if action == "dismiss_recent":
            self.engine.dismiss_recent(str(body.get("conversationId") or "").strip())
            return {"ok": True, "action": action, "hudWindow": self.get_status()}

        if action == "open":
            patch = {**body, "enabled": True}
            if isinstance(patch.get("prefs"), dict):
                patch["prefs"] = {**patch["prefs"], "enabled": True}
            cfg_res = self.engine.update_config(patch)
            enabled_after = bool((cfg_res.get("config", {}).get("hud") or {}).get("enabled", True))
            self._log_hud_event(
                "action_open",
                source=source,
                local_only=local_only,
                enabled_before=enabled_before,
                enabled_after=enabled_after,
            )
            with self._lock:
                self._crash_count, self._next_retry_at = 0, 0.0
                self._unexpected_exit_block_pending = False
            self._wake_event.set()
            if not local_only and self.peer_coord.has_remote_peers():
                self._wake_remote_hud_peer_for_open(source)
            if not self.disable_spawn:
                deadline = time.time() + 0.8
                while time.time() < deadline:
                    if WIN32.is_mutex_held(HUD_MUTEX_NAME) or (self._proc is not None and self._proc.poll() is None):
                        break
                    time.sleep(0.04)
            return self._cfg_resp("open", cfg_res)

        if action == "close":
            patch = {**body, "enabled": False}
            if isinstance(patch.get("prefs"), dict):
                patch["prefs"] = {**patch["prefs"], "enabled": False}
            cfg_res = self.engine.update_config(patch)
            enabled_after = bool((cfg_res.get("config", {}).get("hud") or {}).get("enabled", False))
            self._log_hud_event(
                "action_close",
                source=source,
                local_only=local_only,
                enabled_before=enabled_before,
                enabled_after=enabled_after,
            )
            self._terminate_local_child(reason=f"action_close:{source}")
            self.peer_coord.set_owns_hud(False)
            self._wake_event.set()
            if not local_only and self.peer_coord.has_remote_peers():
                self._forward_to_hud_owner({"action": "close", "enabled": False, "source": f"peer:{source}"})
            return self._cfg_resp("close", cfg_res)

        if action == "flash":
            self._log_hud_event(
                "action_flash",
                source=source,
                local_only=local_only,
                enabled_before=enabled_before,
                enabled_after=enabled_before,
            )
            self.engine.bump_rev()
            if not self._send_stdin_cmd({"cmd": "flashLocate"}) and not local_only and self.peer_coord.has_remote_peers():
                self._forward_to_hud_owner({"action": "flash", "source": f"peer:{source}"})
            return {"ok": True, "action": "flash", "hudWindow": self.get_status()}

        if action == "reset_position":
            cfg_res = self.engine.update_config({"x": None, "y": None, "physX": None, "physY": None, "anchorCorner": "top-right", "width": 388, "customHeight": None})
            enabled_after = bool((cfg_res.get("config", {}).get("hud") or {}).get("enabled", True))
            self._log_hud_event(
                "action_reset_position",
                source=source,
                local_only=local_only,
                enabled_before=enabled_before,
                enabled_after=enabled_after,
            )
            if not self._send_stdin_cmd({"cmd": "resetPosition"}) and not local_only and self.peer_coord.has_remote_peers():
                self._forward_to_hud_owner({"action": "reset_position", "source": f"peer:{source}"})
            return self._cfg_resp("reset_position", cfg_res)

        if action == "reset_auto_height":
            cfg_res = self.engine.update_config({"customHeight": None})
            if not self._send_stdin_cmd({"cmd": "resetAutoHeight"}) and not local_only and self.peer_coord.has_remote_peers():
                self._forward_to_hud_owner({"action": "reset_auto_height"})
            return self._cfg_resp("reset_auto_height", cfg_res)

        cfg_res = self.engine.update_config(body)
        prefs_dict = body.get("prefs") if isinstance(body.get("prefs"), dict) else body
        if "alwaysOnTop" in prefs_dict or "alwaysOnTop" in body:
            top_val = bool(cfg_res["config"]["hud"].get("alwaysOnTop", True))
            if not self._send_stdin_cmd({"cmd": "setTopMost", "alwaysOnTop": top_val}) and not local_only and self.peer_coord.has_remote_peers():
                self._forward_to_hud_owner({"action": "update_prefs", "alwaysOnTop": top_val})
        if "enabled" in prefs_dict or "enabled" in body:
            self._wake_event.set()
        return self._cfg_resp(action, cfg_res)


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(
    engine: FlowPilotEngine,
    ui_token: str,
    peer_coord_ref: list[Optional[_PeerNodeCoordinator]],
    hud_mgr_ref: list[Optional[_DesktopHudManager]],
):
    ui_html_path = THIS_DIR / "ui.html"
    ui_cache_lock = threading.Lock()
    ui_cache: list[Any] = [0, b""]

    def _get_ui_html_bytes() -> bytes:
        if (mtime_ns := _stat_mtime_ns(str(ui_html_path))) <= 0:
            return _FALLBACK_HTML
        with ui_cache_lock:
            if mtime_ns == ui_cache[0] and ui_cache[1]:
                return ui_cache[1]
        try:
            raw = ui_html_path.read_bytes()
        except OSError:
            return _FALLBACK_HTML
        with ui_cache_lock:
            ui_cache[0], ui_cache[1] = mtime_ns, raw
        return raw

    def _inject_head_script(html_bytes: bytes, script_bytes: bytes) -> bytes:
        idx = html_bytes.find(b"<head>")
        if idx >= 0:
            pos = idx + len(b"<head>")
            return html_bytes[:pos] + script_bytes + html_bytes[pos:]
        return script_bytes + html_bytes

    class FlowPilotHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 15

        def setup(self) -> None:
            super().setup()
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass

        def handle(self) -> None:
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError, socket.timeout):
                pass

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_unchanged(self, rev: int) -> None:
            body = f'{{"ok":true,"unchanged":true,"rev":{rev}}}'.encode("ascii")
            pkt = (
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\nCache-Control: no-store\r\n"
                b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body
            )
            try:
                self.wfile.write(pkt)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                self.close_connection = True

        def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                self.close_connection = True

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            self._send_bytes(status, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), "application/json; charset=utf-8")

        def _check_token(self, query_params: dict[str, list[str]]) -> bool:
            peer_coord, hud_mgr = peer_coord_ref[0], hud_mgr_ref[0]
            if peer_hdr := (self.headers.get("X-Peer-Token") or "").strip():
                return bool(peer_coord and hmac.compare_digest(peer_hdr, peer_coord.peer_token))
            hud_token = hud_mgr.hud_token if hud_mgr is not None else ""
            provided_candidates: list[str] = []
            for hdr_name in ("X-Flow-Pilot-Token", "X-Sidecar-Token"):
                if val := (self.headers.get(hdr_name) or "").strip():
                    provided_candidates.append(val)
            if not provided_candidates and (tok_list := query_params.get("token")):
                if val := (tok_list[0] or "").strip():
                    provided_candidates.append(val)
            if provided_candidates:
                for provided in provided_candidates:
                    if ui_token and hmac.compare_digest(provided, ui_token):
                        return True
                    if hud_token and hmac.compare_digest(provided, hud_token):
                        return True
                return False
            return not (bool(ui_token) or bool(hud_token))

        def _read_json_body(self) -> dict[str, Any]:
            try:
                length = int((self.headers.get("Content-Length") or "0").strip())
            except ValueError:
                length = 0
            if length <= 0:
                return {}
            if length > 2 * 1024 * 1024:
                raise ValueError("Request payload too large")
            if not (raw := self.rfile.read(length)):
                return {}
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
            if not isinstance(parsed, dict):
                raise ValueError("JSON body must be an object")
            return parsed

        def do_GET(self) -> None:
            raw_path = self.path
            q_idx = raw_path.find("?")
            route = (raw_path[:q_idx] if q_idx >= 0 else raw_path) or "/"
            query_str = raw_path[q_idx + 1 :] if q_idx >= 0 else ""

            if route == "/api/tasks":
                qs = urllib.parse.parse_qs(query_str, keep_blank_values=True) if query_str else {}
                if not self._check_token(qs):
                    self._send_json(401, {"ok": False, "error": "unauthorized"})
                    return
                hint_cid = (qs["hintConversationId"][0] or "").strip() if "hintConversationId" in qs else ""
                since_rev = 0
                if "sinceRev" in qs:
                    try:
                        since_rev = int(qs["sinceRev"][0])
                    except (ValueError, TypeError, IndexError):
                        since_rev = 0

                peer_coord, hud_mgr = peer_coord_ref[0], hud_mgr_ref[0]
                if since_rev > 0:
                    if hud_mgr is not None:
                        hud_mgr.sync_running_rev()
                    if (peer_coord is None or not peer_coord.has_remote_peers()) and engine.can_short_circuit_tasks(since_rev, hint_cid):
                        self._send_unchanged(since_rev)
                        return

                try:
                    payload = peer_coord.aggregate_all_tasks(hint_conversation_id=hint_cid) if peer_coord is not None else engine.get_local_tasks_payload(hint_conversation_id=hint_cid)
                    payload["hudWindow"] = hud_mgr.get_status() if hud_mgr is not None else {"running": False, "visible": False}
                    self._send_json(200, payload)
                except Exception as exc:
                    self._send_json(500, {"ok": False, "error": str(exc)})
                return

            if route == "/hud-bootstrap":
                qs = urllib.parse.parse_qs(query_str, keep_blank_values=True) if query_str else {}
                code = (qs.get("code", [""])[0] or "").strip()
                hud_mgr = hud_mgr_ref[0]
                if hud_mgr is None or not hud_mgr.consume_edge_bootstrap_code(code):
                    self._send_json(403, {"ok": False, "error": "invalid_or_expired_bootstrap"})
                    return
                tok_js = json.dumps(hud_mgr.hud_token)
                inject = (
                    f"<script>(function(){{window.__FP_HUD_TOKEN__={tok_js};"
                    f"try{{sessionStorage.setItem('__fp_hud_token__',{tok_js});"
                    f"history.replaceState(null,'','/ui.html?mode=hud');}}catch(_){{}}}})();</script>"
                ).encode("utf-8")
                self._send_bytes(200, _inject_head_script(_get_ui_html_bytes(), inject), "text/html; charset=utf-8")
                return

            if route in ("/", "/ui.html"):
                html_bytes = _get_ui_html_bytes()
                if route == "/":
                    qs = urllib.parse.parse_qs(query_str, keep_blank_values=True) if query_str else {}
                    hud_mgr = hud_mgr_ref[0]
                    sec_site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
                    origin = (self.headers.get("Origin") or "").strip().lower()
                    is_cross_site = (sec_site == "cross-site") or bool(
                        origin and not (
                            origin.startswith("http://127.0.0.1")
                            or origin.startswith("http://localhost")
                            or origin.startswith("http://[::1]")
                            or origin.startswith("sidecar://")
                        )
                    )
                    embed_tok = ""
                    if not is_cross_site:
                        if ui_token:
                            if self._check_token(qs):
                                embed_tok = ui_token
                        elif hud_mgr is not None:
                            embed_tok = hud_mgr.hud_token
                    if embed_tok:
                        tok_js = json.dumps(embed_tok)
                        inject = f"<script>window.__FP_HUD_TOKEN__={tok_js};</script>".encode("utf-8")
                        html_bytes = _inject_head_script(html_bytes, inject)
                self._send_bytes(200, html_bytes, "text/html; charset=utf-8")
                return
            if route == "/healthz":
                self._send_json(200, {"ok": True, "service": "agy-flow-pilot", "version": APP_VERSION})
                return

            qs = urllib.parse.parse_qs(query_str, keep_blank_values=True) if query_str else {}
            if route == "/api/node-tasks":
                if not self._check_token(qs):
                    self._send_json(401, {"ok": False, "error": "unauthorized"})
                    return
                try:
                    self._send_json(200, engine.get_local_tasks_payload())
                except Exception as exc:
                    self._send_json(500, {"ok": False, "error": str(exc)})
                return

            self._send_json(404, {"ok": False, "error": f"Not found: {route}"})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            route = parsed.path or "/"
            qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            local_only = (qs.get("localOnly", ["0"])[0] or "0").strip() == "1"

            if not self._check_token(qs):
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return
            try:
                body = self._read_json_body()
            except Exception as exc:
                self._send_json(400, {"ok": False, "error": f"Invalid JSON body: {exc}"})
                return

            peer_coord, hud_mgr = peer_coord_ref[0], hud_mgr_ref[0]
            try:
                if route == "/api/heal":
                    target_ls = str(body.get("lsInstanceId") or "").strip()
                    cid = str(body.get("conversationId") or qs.get("conversationId", [""])[0] or "").strip()
                    if not local_only and peer_coord is not None and (forwarded := peer_coord.route_post_to_owner(target_ls, cid, "/api/heal", body)) is not None:
                        self._send_json(200, forwarded)
                        return
                    raw_idx = body.get("stepIndex")
                    step_idx = int(raw_idx) if isinstance(raw_idx, (int, float)) and not isinstance(raw_idx, bool) and int(raw_idx) >= 0 else None
                    heal_res = engine.heal(
                        conversation_id=cid,
                        mode=str(body.get("mode") or "wake").strip(),
                        step_index=step_idx,
                        text=str(body.get("text") or "").strip(),
                        kill_background_tasks=bool(body.get("killBackgroundTasks", False)),
                    )
                    if not isinstance(heal_res, dict):
                        heal_res = {"ok": False, "error": "invalid_heal_response"}
                    self._send_json(200, heal_res)
                    return

                if route == "/api/config":
                    self._send_json(200, engine.update_config(body))
                    return
                if route == "/api/hud":
                    if hud_mgr is None:
                        self._send_json(503, {"ok": False, "error": "HUD manager not initialized"})
                        return
                    self._send_json(200, hud_mgr.handle_action(body, local_only=local_only))
                    return
                self._send_json(404, {"ok": False, "error": f"Unknown API endpoint: {route}"})
            except ValueError as exc:
                self._send_json(400, {"ok": False, "error": str(exc)})
            except Exception as exc:
                sys.stderr.write(f"[flow-pilot] POST {route} error: {exc}\n")
                sys.stderr.flush()
                self._send_json(500, {"ok": False, "error": str(exc)})

    return FlowPilotHandler


def _parse_ls_port(raw_addr: str) -> int | None:
    if not (addr := (raw_addr or "").strip()):
        return None
    if addr.isdigit():
        port = int(addr)
        return port if 1 <= port <= 65535 else None
    try:
        port = urllib.parse.urlsplit(addr if "://" in addr else f"http://{addr}").port
        if port is not None and 1 <= port <= 65535:
            return int(port)
    except ValueError:
        pass
    if ":" in addr and (tail := addr.rsplit(":", 1)[1].rstrip("/")).isdigit():
        port = int(tail)
        if 1 <= port <= 65535:
            return port
    return None


def _start_parent_watchdog(shutdown_fn: Callable[[], None], stop_event: threading.Event | None = None) -> threading.Thread:
    ppid = os.getppid()
    ls_port = _parse_ls_port(os.environ.get("ANTIGRAVITY_LS_ADDRESS", ""))
    ev = stop_event if stop_event is not None else threading.Event()
    initial_parent_ctime = WIN32.get_process_creation_time(ppid) if ppid > 0 else None
    parent_dead_at_init = ppid > 0 and WIN32.available and not WIN32.is_pid_alive(ppid)

    def _is_parent_alive() -> bool:
        if ppid <= 0:
            return True
        if WIN32.available:
            return WIN32.is_pid_alive(ppid, expected_ctime=initial_parent_ctime)
        cur_ppid = os.getppid()
        return cur_ppid == ppid and cur_ppid != 1

    def _trigger_exit(reason: str) -> None:
        threading.Thread(target=lambda: (time.sleep(2.0), os._exit(0)), name="flow-pilot-force-exit", daemon=True).start()
        try:
            print(f"[agy-flow-pilot] parent watchdog shutting down ({reason})", flush=True)
        except Exception:
            pass
        try:
            shutdown_fn()
        except Exception:
            os._exit(0)

    def _watchdog_loop() -> None:
        if parent_dead_at_init:
            _trigger_exit(f"ppid_exited:{ppid}")
            return
        ls_fail_count = 0
        while not ev.wait(3.0):
            if not _is_parent_alive():
                _trigger_exit(f"ppid_exited:{ppid}")
                return
            if ls_port is not None:
                try:
                    with socket.create_connection(("127.0.0.1", ls_port), timeout=0.25):
                        pass
                    ls_fail_count = 0
                except OSError:
                    ls_fail_count += 1
                    if ls_fail_count >= 4:
                        _trigger_exit(f"ls_port_unreachable:127.0.0.1:{ls_port}")
                        return

    t = threading.Thread(target=_watchdog_loop, name="flow-pilot-parent-watchdog", daemon=True)
    t.start()
    return t


def main() -> None:
    port_raw = os.environ.get("ANTIGRAVITY_SIDECAR_WEB_PORT", "").strip()
    port = int(port_raw) if port_raw else 0
    ui_token = os.environ.get("ANTIGRAVITY_SIDECAR_UI_TOKEN", "").strip()

    peer_coord_ref: list[Optional[_PeerNodeCoordinator]] = [None]
    hud_mgr_ref: list[Optional[_DesktopHudManager]] = [None]

    engine = FlowPilotEngine()
    httpd = _ReusableThreadingHTTPServer(("127.0.0.1", port), make_handler(engine, ui_token, peer_coord_ref, hud_mgr_ref))
    bound_port = int(httpd.server_address[1])

    peer_coord = _PeerNodeCoordinator(engine, bound_port)
    hud_mgr = _DesktopHudManager(engine, peer_coord, bound_port)
    peer_coord_ref[0], hud_mgr_ref[0] = peer_coord, hud_mgr

    engine.start()
    peer_coord.start()
    hud_mgr.start()
    print(f"[agy-flow-pilot] sidecar v{APP_VERSION} listening on 127.0.0.1:{bound_port}", flush=True)

    stop_watchdog = threading.Event()
    shutdown_lock = threading.Lock()
    shutting_down = False

    def _shutdown(*_args: Any) -> None:
        nonlocal shutting_down
        with shutdown_lock:
            if shutting_down:
                return
            shutting_down = True
        stop_watchdog.set()
        hud_mgr.stop()
        peer_coord.stop()
        engine.stop()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except Exception:
            pass

    _start_parent_watchdog(_shutdown, stop_event=stop_watchdog)
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        stop_watchdog.set()
        hud_mgr.stop()
        peer_coord.stop()
        engine.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
