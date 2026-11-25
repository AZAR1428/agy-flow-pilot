#!/usr/bin/env python3
"""Pre-publish security, privacy, PE binary, and git hygiene audit for agy-flow-pilot.

Zero third-party dependencies. Runs cleanly under:
    python -S -B scripts/pre_publish_audit.py

Exit codes:
    0 -> 0 findings (ready to publish)
    1 -> 1 or more findings detected (must fix before push/release)
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import os
import pathlib
import re
import shutil
import struct
import subprocess
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

# Official Microsoft-signed WebView2 redistributable DLLs (documented in THIRD_PARTY_NOTICES.md).
ALLOWLISTED_PE_BASENAMES: Set[str] = {
    "webview2loader.dll",
    "microsoft.web.webview2.core.dll",
}

BINARY_SKIP_SUFFIXES: Set[str] = {
    ".exe",
    ".dll",
    ".pyd",
    ".so",
    ".dylib",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".zip",
    ".gz",
    ".7z",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".pdf",
    ".sqlite",
    ".db",
}

ALWAYS_SKIP_DIRS: Set[str] = {
    ".git",
    "__pycache__",
    ".vscode",
    ".idea",
    "bin",
    "obj",
    "edge_hud_udf",
    "nodes",
    "hook_state",
}

ALWAYS_SKIP_FILES: Set[str] = {
    ".audit-denylist.local",
    ".env",
    ".DS_Store",
    "Thumbs.db",
}

PLACEHOLDER_USERNAMES: Set[str] = {
    "user",
    "username",
    "yourname",
    "your_name",
    "name",
    "public",
    "default",
    "shared",
    "example",
    "runner",
    "ubuntu",
    "admin",
    "administrator",
}

ALLOWED_EMAIL_DOMAINS: Tuple[str, ...] = (
    "users.noreply.github.com",
    "example.com",
    "example.org",
    "example.net",
    "localhost",
    "invalid",
)

PLACEHOLDER_SECRET_WORDS: Tuple[str, ...] = (
    "example",
    "placeholder",
    "changeme",
    "your_token",
    "your-token",
    "your_key",
    "your-key",
    "replace_me",
    "replace-me",
    "dummy",
    "sample",
    "fake_token",
    "test_token",
)

# ---------------------------------------------------------------------------
# Compiled structural detection patterns (no literal NDA/internal codenames)
# ---------------------------------------------------------------------------

RE_SECRET_PATTERNS: List[Tuple[str, re.Pattern[str], str]] = [
    (
        "SECRET_GOOGLE_API_KEY",
        re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z\-_]{20,}"),
        "Potential Google API key literal",
    ),
    (
        "SECRET_SK_API_KEY",
        re.compile(
            r"(?<![A-Za-z0-9_-])sk-(?:proj-|live-|test-|ant-api\d*-)?[A-Za-z0-9\-_]{20,}"
        ),
        "Potential sk-* secret API key literal",
    ),
    (
        "SECRET_GITHUB_TOKEN",
        re.compile(
            r"(?<![A-Za-z0-9_])(?:ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|"
            r"ghu_[A-Za-z0-9]{20,}|ghs_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"
        ),
        "Potential GitHub personal/OAuth access token",
    ),
    (
        "SECRET_SLACK_TOKEN",
        re.compile(r"(?<![A-Za-z0-9_])xox[baprs]-[A-Za-z0-9\-]{10,}"),
        "Potential Slack token literal",
    ),
    (
        "SECRET_AWS_KEY",
        re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])"),
        "Potential AWS access key ID",
    ),
    (
        "SECRET_JWT",
        re.compile(
            r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\."
            r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?![A-Za-z0-9_-])"
        ),
        "Potential hardcoded JWT token",
    ),
    (
        "SECRET_PRIVATE_KEY",
        re.compile("-----" + r"BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY" + "-----"),
        "Private key block header detected",
    ),
]

RE_BEARER_LITERAL = re.compile(r"(?i)\bBearer\s+([A-Za-z0-9._\-+/=]{16,})")

RE_CRED_ASSIGN = re.compile(
    r"(?i)(?:\b(?:x-codeium-csrf-token|csrf[_-]?token|api[_-]?key|access[_-]?token|"
    r"auth[_-]?token|refresh[_-]?token|secret[_-]?key|client[_-]?secret|cookie|"
    r"password|passwd|webhook_url)\b)[\"']?\s*[:=]\s*[\"']([^\"'\r\n]{16,})[\"']"
)

RE_HIGH_ENTROPY_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-]{24,}")
RE_URL_SPAN = re.compile(r"(?:https?|wss?|sidecar|file)://[^\s\"'<>`]+", re.IGNORECASE)
RE_DATA_URI_SPAN = re.compile(r"data:[a-z0-9.+-]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]+", re.IGNORECASE)

RE_WIN_HOME = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:(?:\\{1,2}|/)Users(?:\\{1,2}|/)([A-Za-z0-9._-]+)")
RE_UNIX_HOME = re.compile(r"(?<![A-Za-z0-9_.-])/(?:Users|home)/([A-Za-z0-9._-]+)")
RE_EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+\-/])([A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,}))(?![A-Za-z0-9._%+\-])"
)
RE_LAN_IP = re.compile(
    r"(?<![\d.vV])(?:"
    r"10\.(?:25[0-5]|2[0-4]\d|1?\d?\d)\.(?:25[0-5]|2[0-4]\d|1?\d?\d)\.(?:25[0-5]|2[0-4]\d|1?\d?\d)|"
    r"172\.(?:1[6-9]|2\d|3[01])\.(?:25[0-5]|2[0-4]\d|1?\d?\d)\.(?:25[0-5]|2[0-4]\d|1?\d?\d)|"
    r"192\.168\.(?:25[0-5]|2[0-4]\d|1?\d?\d)\.(?:25[0-5]|2[0-4]\d|1?\d?\d)"
    r")(?![\d.])"
)
RE_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

RE_MODEL_PLACEHOLDER = re.compile(r"(?i)\bMODEL_PLACEHOLDER_M\d+\b")
RE_CLIENT_VERSION_PIN = re.compile(
    r"(?i)\bAntigravity\b[^\r\n]{0,24}\bv?\d+\.\d+(?:\.\d+)?\+?"
)

RE_PE_PDB_ASCII = re.compile(rb"\.pdb(?![A-Za-z0-9_])", re.IGNORECASE)
RE_PE_PDB_UTF16 = re.compile(rb"\.\x00p\x00d\x00b\x00", re.IGNORECASE)
RE_PE_USERS_ASCII = re.compile(rb"users[\\/]", re.IGNORECASE)
RE_PE_USERS_UTF16 = re.compile(rb"u\x00s\x00e\x00r\x00s\x00[\\/]\x00", re.IGNORECASE)

RE_REFLOG_EMAIL = re.compile(r"<([^>\r\n]+)>")


def shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    counts: Dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    length = len(text)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def is_safe_charset_or_identifier(token: str) -> bool:
    low = token.lower()
    if "abcdefghij" in low or "0123456789abcdef" in low:
        return True
    # Pure sequential alphabet / base64 charset constants
    if "ABCDEFGHIJKLMNOPQRSTUVWXYZ" in token and "abcdefghijklmnopqrstuvwxyz" in token:
        return True
    # Common repeated-char separators
    if len(set(token)) <= 4:
        return True
    # Plain uppercase/lowercase snake_case identifier without digits (e.g. long constant names)
    if re.fullmatch(r"[A-Z]+(?:_[A-Z]+){2,}", token) or re.fullmatch(r"[a-z]+(?:_[a-z]+){2,}", token):
        return True
    return False


def is_placeholder_secret(val: str) -> bool:
    low = val.lower()
    if any(w in low for w in PLACEHOLDER_SECRET_WORDS):
        return True
    if any(ch in val for ch in (" ", "\t", "{", "}", "$", "<", ">")):
        return True
    if re.fullmatch(r"[A-Z0-9_]+", val):
        return True
    if re.fullmatch(r"[a-z]+(?:[_-][a-z]+)+", val):
        return True
    return False


def find_git_executable(explicit_git: Optional[str]) -> Optional[str]:
    if explicit_git:
        if os.path.isfile(explicit_git):
            return explicit_git
        return None
    env_git = os.environ.get("GIT_BIN", "").strip()
    if env_git and os.path.isfile(env_git):
        return env_git
    found = shutil.which("git")
    if found:
        return found
    if os.name == "nt":
        candidates = [
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "cmd", "git.exe"),
            os.path.join(
                os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                "Git",
                "cmd",
                "git.exe",
            ),
            os.path.join(
                os.environ.get("LOCALAPPDATA", ""),
                "Programs",
                "Git",
                "cmd",
                "git.exe",
            ),
        ]
        for cand in candidates:
            if cand and os.path.isfile(cand):
                return cand
    return None


def load_gitignore_patterns(root: pathlib.Path) -> List[str]:
    gi = root / ".gitignore"
    patterns: List[str] = []
    if not gi.is_file():
        return patterns
    for raw in gi.read_text("utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        patterns.append(line)
    return patterns


def matches_gitignore(rel_posix: str, is_dir: bool, patterns: Sequence[str]) -> bool:
    name = rel_posix.rsplit("/", 1)[-1]
    for pat in patterns:
        dir_only = pat.endswith("/")
        clean = pat.rstrip("/")
        if dir_only and not is_dir:
            parts = rel_posix.split("/")
            if any(fnmatch.fnmatch(p, clean) for p in parts[:-1]):
                return True
            continue
        if "/" in clean:
            if fnmatch.fnmatch(rel_posix, clean):
                return True
        else:
            if fnmatch.fnmatch(name, clean):
                return True
    return False


def discover_files(
    root: pathlib.Path,
    git_exe: Optional[str],
    tracked_only: bool,
    excludes: Sequence[str],
) -> Tuple[List[pathlib.Path], str]:
    files: List[pathlib.Path] = []
    mode = "walk"

    if git_exe and (root / ".git").exists():
        cmd = [git_exe, "-C", str(root), "ls-files", "-z"]
        if not tracked_only:
            cmd.extend(["--cached", "--others", "--exclude-standard"])
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=15,
            )
            if proc.returncode == 0:
                mode = "git-tracked" if tracked_only else "git-ls-files"
                seen: Set[str] = set()
                for entry in proc.stdout.split(b"\x00"):
                    if not entry:
                        continue
                    rel = entry.decode("utf-8", errors="surrogateescape")
                    if rel in seen:
                        continue
                    seen.add(rel)
                    full = root / rel
                    if full.is_file():
                        files.append(full)
        except OSError:
            mode = "walk"

    if mode == "walk":
        gi_patterns = load_gitignore_patterns(root)
        for dirpath, dirnames, filenames in os.walk(root):
            cur = pathlib.Path(dirpath)
            rel_dir = cur.relative_to(root).as_posix()
            kept_dirs: List[str] = []
            for d in sorted(dirnames):
                if d in ALWAYS_SKIP_DIRS or d.endswith(".WebView2"):
                    continue
                d_rel = d if rel_dir == "." else f"{rel_dir}/{d}"
                if matches_gitignore(d_rel, True, gi_patterns):
                    continue
                kept_dirs.append(d)
            dirnames[:] = kept_dirs
            for fn in sorted(filenames):
                if fn in ALWAYS_SKIP_FILES or fn.startswith(".env."):
                    continue
                f_rel = fn if rel_dir == "." else f"{rel_dir}/{fn}"
                if matches_gitignore(f_rel, False, gi_patterns):
                    continue
                files.append(cur / fn)

    filtered: List[pathlib.Path] = []
    for p in sorted(set(files)):
        rel = p.relative_to(root).as_posix()
        if any(fnmatch.fnmatch(rel, pat) for pat in excludes):
            continue
        filtered.append(p)
    return filtered, mode


def load_local_denylist(path: pathlib.Path) -> List[Tuple[int, re.Pattern[str]]]:
    rules: List[Tuple[int, re.Pattern[str]]] = []
    if not path.is_file():
        return rules
    for idx, raw in enumerate(path.read_text("utf-8", errors="replace").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            expr = line[3:].strip()
            if expr:
                rules.append((idx, re.compile(expr, re.IGNORECASE)))
        else:
            rules.append((idx, re.compile(re.escape(line), re.IGNORECASE)))
    return rules


def add_finding(
    findings: List[Dict[str, object]],
    rel_path: str,
    line: int,
    rule_id: str,
    message: str,
) -> None:
    findings.append(
        {
            "file": rel_path,
            "line": line,
            "rule": rule_id,
            "message": message,
        }
    )


def scan_text_file(
    full_path: pathlib.Path,
    rel_path: str,
    denylist: Sequence[Tuple[int, re.Pattern[str]]],
    findings: List[Dict[str, object]],
) -> bool:
    try:
        raw = full_path.read_bytes()
    except OSError as exc:
        add_finding(findings, rel_path, 0, "IO_ERROR", f"Unable to read file: {exc}")
        return False

    if b"\x00" in raw[:4096]:
        return False

    text = raw.decode("utf-8", errors="replace")
    is_svg = full_path.suffix.lower() == ".svg"

    for lineno, line in enumerate(text.splitlines(), 1):
        # 1. Explicit secret patterns
        for rule_id, rx, desc in RE_SECRET_PATTERNS:
            if rx.search(line):
                add_finding(findings, rel_path, lineno, rule_id, desc)

        for m in RE_BEARER_LITERAL.finditer(line):
            tok = m.group(1)
            if not is_placeholder_secret(tok):
                add_finding(
                    findings,
                    rel_path,
                    lineno,
                    "SECRET_HARDCODED_BEARER",
                    "Hardcoded Bearer credential literal",
                )

        for m in RE_CRED_ASSIGN.finditer(line):
            val = m.group(1)
            if not is_placeholder_secret(val):
                add_finding(
                    findings,
                    rel_path,
                    lineno,
                    "SECRET_HARDCODED_ASSIGNMENT",
                    "Hardcoded credential/token/cookie assignment",
                )

        # High-entropy literals (>= 24 chars, Shannon entropy >= 4.6)
        if not is_svg:
            masked = RE_URL_SPAN.sub(" ", line)
            masked = RE_DATA_URI_SPAN.sub(" ", masked)
            for m in RE_HIGH_ENTROPY_TOKEN.finditer(masked):
                tok = m.group(0)
                if is_safe_charset_or_identifier(tok):
                    continue
                ent = shannon_entropy(tok)
                if ent >= 4.6:
                    add_finding(
                        findings,
                        rel_path,
                        lineno,
                        "SECRET_HIGH_ENTROPY",
                        f"High-entropy literal (len={len(tok)}, H={ent:.2f}): {tok[:12]}...",
                    )

        # 2. PII & Local Environment Paths
        for m in RE_WIN_HOME.finditer(line):
            user = m.group(1).lower()
            if user not in PLACEHOLDER_USERNAMES:
                add_finding(
                    findings,
                    rel_path,
                    lineno,
                    "PII_LOCAL_HOME_PATH",
                    "Windows local user home path detected",
                )

        for m in RE_UNIX_HOME.finditer(line):
            user = m.group(1).lower()
            if user not in PLACEHOLDER_USERNAMES:
                add_finding(
                    findings,
                    rel_path,
                    lineno,
                    "PII_LOCAL_HOME_PATH",
                    "macOS/Linux local user home path detected",
                )

        for m in RE_EMAIL.finditer(line):
            domain = m.group(2).lower()
            if not any(
                domain == d or domain.endswith("." + d) for d in ALLOWED_EMAIL_DOMAINS
            ):
                add_finding(
                    findings,
                    rel_path,
                    lineno,
                    "PII_PERSONAL_EMAIL",
                    f"Non-noreply email domain detected (@{domain})",
                )

        if RE_LAN_IP.search(line):
            add_finding(
                findings,
                rel_path,
                lineno,
                "PII_LAN_IP",
                "Non-loopback RFC1918 private LAN IP address detected",
            )

        for m in RE_UUID.finditer(line):
            if m.group(0) != "00000000-0000-0000-0000-000000000000":
                add_finding(
                    findings,
                    rel_path,
                    lineno,
                    "PII_CONVERSATION_UUID",
                    f"Unexpected hardcoded UUID literal ({m.group(0)[:8]}...)",
                )

        # 3. Platform-Internal Structural Patterns + local denylist
        if RE_MODEL_PLACEHOLDER.search(line):
            add_finding(
                findings,
                rel_path,
                lineno,
                "INTERNAL_MODEL_PLACEHOLDER",
                "Hardcoded platform internal model placeholder ID detected",
            )

        if RE_CLIENT_VERSION_PIN.search(line):
            add_finding(
                findings,
                rel_path,
                lineno,
                "INTERNAL_CLIENT_VERSION_PIN",
                "Hardcoded client version pin next to product name",
            )

        for rule_line, drx in denylist:
            if drx.search(line):
                add_finding(
                    findings,
                    rel_path,
                    lineno,
                    "LOCAL_DENYLIST",
                    f"Matched .audit-denylist.local rule #{rule_line}",
                )

    return True


def parse_pe_debug_directory(data: bytes) -> Tuple[Optional[Tuple[int, int]], Optional[str]]:
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None, "Not a valid MZ/PE binary"
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if e_lfanew + 24 > len(data) or data[e_lfanew : e_lfanew + 4] != b"PE\x00\x00":
        return None, "Missing PE signature"
    opt_size = struct.unpack_from("<H", data, e_lfanew + 20)[0]
    opt_off = e_lfanew + 24
    if opt_off + opt_size > len(data) or opt_size < 2:
        return None, "Truncated PE optional header"
    magic = struct.unpack_from("<H", data, opt_off)[0]
    if magic == 0x10B:  # PE32
        dd_off = opt_off + 96
        num_rva_off = opt_off + 92
    elif magic == 0x20B:  # PE32+
        dd_off = opt_off + 112
        num_rva_off = opt_off + 108
    else:
        return None, f"Unsupported PE optional header magic 0x{magic:x}"
    if num_rva_off + 4 > len(data):
        return None, "Truncated PE DataDirectory count"
    num_rva = struct.unpack_from("<I", data, num_rva_off)[0]
    if num_rva <= 6:
        return (0, 0), None
    dbg_off = dd_off + 6 * 8
    if dbg_off + 8 > len(data):
        return None, "Truncated PE IMAGE_DEBUG_DIRECTORY entry"
    va, size = struct.unpack_from("<II", data, dbg_off)
    return (va, size), None


def scan_pe_binary(
    full_path: pathlib.Path,
    rel_path: str,
    findings: List[Dict[str, object]],
) -> str:
    base = full_path.name.lower()
    if base in ALLOWLISTED_PE_BASENAMES:
        return "allowlisted"
    try:
        data = full_path.read_bytes()
    except OSError as exc:
        add_finding(findings, rel_path, 0, "PE_IO_ERROR", f"Unable to read PE binary: {exc}")
        return "error"

    dbg, err = parse_pe_debug_directory(data)
    if err is not None:
        add_finding(findings, rel_path, 0, "PE_PARSE_ERROR", err)
    elif dbg != (0, 0):
        add_finding(
            findings,
            rel_path,
            0,
            "PE_DEBUG_DIRECTORY",
            f"Non-zero IMAGE_DEBUG_DIRECTORY (RVA=0x{dbg[0]:x}, Size={dbg[1]})",
        )

    if b"RSDS" in data or b"NB10" in data:
        add_finding(
            findings,
            rel_path,
            0,
            "PE_CODEVIEW_SIGNATURE",
            "CodeView RSDS/NB10 debug signature embedded in PE binary",
        )
    if RE_PE_PDB_ASCII.search(data) or RE_PE_PDB_UTF16.search(data):
        add_finding(
            findings,
            rel_path,
            0,
            "PE_PDB_STRING",
            ".pdb symbol reference embedded in PE binary",
        )
    if RE_PE_USERS_ASCII.search(data) or RE_PE_USERS_UTF16.search(data):
        add_finding(
            findings,
            rel_path,
            0,
            "PE_USER_PATH_STRING",
            "Local Windows Users\\ path string embedded in PE binary",
        )
    return "checked"


def is_noreply_github_email(email: str) -> bool:
    low = email.strip().lower()
    return low.endswith("@users.noreply.github.com") and len(low) > len("@users.noreply.github.com")


def check_git_commit_emails(
    root: pathlib.Path,
    git_exe: Optional[str],
    max_commits: int,
    findings: List[Dict[str, object]],
) -> Tuple[int, str]:
    if not (root / ".git").exists():
        return 0, "no-git-dir"

    if git_exe:
        try:
            proc = subprocess.run(
                [
                    git_exe,
                    "-C",
                    str(root),
                    "log",
                    f"-n{max_commits}",
                    "--format=%h\t%ae\t%ce",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=15,
            )
            if proc.returncode == 0:
                lines = [
                    ln
                    for ln in proc.stdout.decode("utf-8", errors="replace").splitlines()
                    if ln.strip()
                ]
                for ln in lines:
                    parts = ln.split("\t")
                    if len(parts) < 3:
                        continue
                    sha, ae, ce = parts[0], parts[1], parts[2]
                    if not is_noreply_github_email(ae):
                        add_finding(
                            findings,
                            ".git",
                            0,
                            "GIT_COMMIT_EMAIL",
                            f"Commit {sha} author email is not users.noreply.github.com ({ae})",
                        )
                    if not is_noreply_github_email(ce):
                        add_finding(
                            findings,
                            ".git",
                            0,
                            "GIT_COMMIT_EMAIL",
                            f"Commit {sha} committer email is not users.noreply.github.com ({ce})",
                        )
                return len(lines), "git-log"
        except OSError:
            pass

    # Pure-Python fallback when git binary is unavailable on PATH: inspect .git/logs/HEAD and .git/config
    checked = 0
    seen_bad: Set[str] = set()
    reflog = root / ".git" / "logs" / "HEAD"
    if reflog.is_file():
        entries = [
            ln
            for ln in reflog.read_text("utf-8", errors="replace").splitlines()
            if ln.strip()
        ]
        for entry in entries[-max_commits:]:
            m = RE_REFLOG_EMAIL.search(entry)
            if not m:
                continue
            checked += 1
            email = m.group(1).strip()
            if not is_noreply_github_email(email) and email not in seen_bad:
                seen_bad.add(email)
                add_finding(
                    findings,
                    ".git/logs/HEAD",
                    0,
                    "GIT_COMMIT_EMAIL",
                    f"Reflog email is not users.noreply.github.com ({email})",
                )

    git_cfg = root / ".git" / "config"
    if git_cfg.is_file():
        in_user = False
        for lineno, raw in enumerate(git_cfg.read_text("utf-8", errors="replace").splitlines(), 1):
            stripped = raw.strip()
            if stripped.startswith("["):
                in_user = stripped.lower() == "[user]"
                continue
            if in_user and "=" in stripped:
                k, v = stripped.split("=", 1)
                if k.strip().lower() == "email":
                    email = v.strip().strip("\"'")
                    if email and not is_noreply_github_email(email):
                        add_finding(
                            findings,
                            ".git/config",
                            lineno,
                            "GIT_COMMIT_EMAIL",
                            f"Local git user.email is not users.noreply.github.com ({email})",
                        )

    return checked, "git-reflog-fallback"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pre-publish security, privacy, PE binary, and git hygiene audit."
    )
    parser.add_argument(
        "--root",
        default=str(pathlib.Path(__file__).resolve().parent.parent),
        help="Repository root directory (default: parent of scripts/).",
    )
    parser.add_argument(
        "--tracked-only",
        action="store_true",
        help="Scan only git-tracked files (when git is available).",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Glob pattern (POSIX relative path) to exclude; repeatable.",
    )
    parser.add_argument(
        "--denylist",
        default="",
        help="Path to optional local denylist file (default: <root>/.audit-denylist.local).",
    )
    parser.add_argument(
        "--max-commits",
        type=int,
        default=50,
        help="Number of recent git commits to check for noreply emails (default: 50).",
    )
    parser.add_argument(
        "--git",
        default="",
        help="Optional explicit path to git executable.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON summary.",
    )
    args = parser.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    git_exe = find_git_executable(args.git or None)
    denylist_path = (
        pathlib.Path(args.denylist).resolve()
        if args.denylist
        else (root / ".audit-denylist.local")
    )
    denylist = load_local_denylist(denylist_path)
    files, file_mode = discover_files(root, git_exe, args.tracked_only, args.exclude)

    findings: List[Dict[str, object]] = []
    text_scanned = 0
    pe_checked = 0
    pe_allowlisted = 0

    for full in files:
        rel = full.relative_to(root).as_posix()
        suffix = full.suffix.lower()
        if suffix in (".exe", ".dll"):
            status = scan_pe_binary(full, rel, findings)
            if status == "allowlisted":
                pe_allowlisted += 1
            else:
                pe_checked += 1
            continue
        if suffix in BINARY_SKIP_SUFFIXES:
            continue
        if scan_text_file(full, rel, denylist, findings):
            text_scanned += 1

    commits_checked, git_mode = check_git_commit_emails(
        root, git_exe, max(1, args.max_commits), findings
    )

    summary = {
        "ok": len(findings) == 0,
        "file_discovery_mode": file_mode,
        "text_files_scanned": text_scanned,
        "pe_binaries_checked": pe_checked,
        "pe_binaries_allowlisted": pe_allowlisted,
        "local_denylist_rules": len(denylist),
        "git_commits_checked": commits_checked,
        "git_email_check_mode": git_mode,
        "finding_count": len(findings),
        "findings": findings,
    }

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(
            f"[pre-publish-audit] mode={file_mode} text_files={text_scanned} "
            f"pe_checked={pe_checked} pe_allowlisted={pe_allowlisted} "
            f"denylist_rules={len(denylist)} git_commits={commits_checked} ({git_mode})"
        )
        for item in findings:
            loc = f"{item['file']}:{item['line']}" if item["line"] else str(item["file"])
            print(f"  [FAIL] {loc} [{item['rule']}] {item['message']}")
        if findings:
            print(f"[pre-publish-audit] FAIL: {len(findings)} finding(s) detected.")
        else:
            print("[pre-publish-audit] PASS: 0 findings.")

    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
