$ErrorActionPreference = 'Stop'
$srcDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$destDir = Join-Path $env:USERPROFILE '.gemini\config\plugins\agy-flow-pilot'

function Find-RealPython {
    $candidates = New-Object System.Collections.Generic.List[string]
    try {
        $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($pyLauncher) {
            $p = (& $pyLauncher.Source -3 -c "import sys; print(sys.executable)" 2>$null | Select-Object -First 1)
            if ($p) { $candidates.Add($p.Trim()) }
        }
    } catch {}
    foreach ($glob in @(
        "$env:APPDATA\uv\python\cpython-3*\python.exe",
        "$env:LOCALAPPDATA\uv\python\cpython-3*\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "$env:USERPROFILE\scoop\apps\python*\current\python.exe",
        "$env:USERPROFILE\miniconda3\python.exe",
        "$env:USERPROFILE\anaconda3\python.exe",
        "$env:ProgramFiles\Python3*\python.exe"
    )) {
        Get-Item $glob -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object { $candidates.Add($_.FullName) }
    }
    Get-Command python.exe -All -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_.Source -and $_.Source -notlike '*\WindowsApps\*') { $candidates.Add($_.Source) }
    }
    foreach ($c in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $c)) { continue }
        if ((Get-Item -LiteralPath $c).Length -le 0) { continue }
        try {
            $ok = (& $c -S -c "import sys; print(int(sys.version_info >= (3, 10)))" 2>$null | Select-Object -First 1)
            if ($ok -and $ok.Trim() -eq '1') { return $c }
        } catch {}
    }
    throw "未检测到可用的 Python 3.10+ 解释器（已自动跳过 WindowsApps 空占位），请先安装 Python 3.10+ 或 uv Python。"
}

function Stop-FlowPilotRuntime {
    param([string]$PluginDir)
    $normPlugin = [System.IO.Path]::GetFullPath($PluginDir).TrimEnd('\')
    $hostProcs = @(Get-CimInstance Win32_Process -Filter "Name = 'FlowPilotHost.exe'" -ErrorAction SilentlyContinue | Where-Object {
        ($_.ExecutablePath -and $_.ExecutablePath -like "$normPlugin*") -or
        ($_.CommandLine -and $_.CommandLine -like "*agy-flow-pilot*")
    })
    $serverPids = New-Object System.Collections.Generic.HashSet[int]
    foreach ($hp in $hostProcs) {
        if ($hp.ParentProcessId -gt 0) { [void]$serverPids.Add([int]$hp.ParentProcessId) }
    }
    $nodesDirs = New-Object System.Collections.Generic.List[string]
    $nodesDirs.Add((Join-Path $env:USERPROFILE '.gemini\antigravity\plugin_data\agy-flow-pilot\nodes'))
    if ($env:ANTIGRAVITY_APP_DATA_DIR) {
        $nodesDirs.Add((Join-Path $env:ANTIGRAVITY_APP_DATA_DIR 'plugin_data\agy-flow-pilot\nodes'))
    }
    foreach ($nodesDir in ($nodesDirs | Select-Object -Unique)) {
        if ($nodesDir -and (Test-Path -LiteralPath $nodesDir)) {
            Get-ChildItem -LiteralPath $nodesDir -Filter '*.json' -ErrorAction SilentlyContinue | ForEach-Object {
                try {
                    $nodeObj = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
                    if ($nodeObj -and $nodeObj.pid) { [void]$serverPids.Add([int]$nodeObj.pid) }
                } catch {}
            }
        }
    }
    $pyServers = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe' OR Name LIKE 'python3%'" -ErrorAction SilentlyContinue | Where-Object {
        $cmd = [string]$_.CommandLine
        ($cmd -match 'agy-flow-pilot[/\\]sidecars[/\\]pilot[/\\]server\.py') -or
        ($cmd -match '(?i)\bserver\.py\b' -and $serverPids.Contains([int]$_.ProcessId))
    })
    $stoppedPids = New-Object System.Collections.Generic.List[int]
    foreach ($sp in $pyServers) {
        try {
            Stop-Process -Id ([int]$sp.ProcessId) -Force -ErrorAction SilentlyContinue
            $stoppedPids.Add([int]$sp.ProcessId)
        } catch {}
    }
    foreach ($hp in $hostProcs) {
        try {
            Stop-Process -Id ([int]$hp.ProcessId) -Force -ErrorAction SilentlyContinue
            $stoppedPids.Add([int]$hp.ProcessId)
        } catch {}
    }
    if ($stoppedPids.Count -gt 0) {
        foreach ($wpid in $stoppedPids) {
            try { Wait-Process -Id $wpid -Timeout 5 -ErrorAction SilentlyContinue } catch {}
        }
        Start-Sleep -Milliseconds 150
    }
}

function Copy-AllowlistedTree {
    param(
        [string]$SourceRoot,
        [string]$DestRoot,
        [string]$RelativeDir
    )
    $srcSub = Join-Path $SourceRoot $RelativeDir
    if (-not (Test-Path -LiteralPath $srcSub)) {
        throw "缺少必需的运行时目录: $RelativeDir"
    }
    $excludedDirNames = @('__pycache__', '.git', 'bin', 'obj', 'edge_hud_udf', 'nodes', 'hook_state')
    $excludedFilePatterns = @('*.pyc', '*.pyo', '*.pyd', '*.tmp', '*.bak', '*.log', '*.pdb', 'Thumbs.db', '.DS_Store')
    $stack = New-Object System.Collections.Generic.Stack[string]
    $stack.Push($RelativeDir)
    while ($stack.Count -gt 0) {
        $rel = $stack.Pop()
        $curSrc = Join-Path $SourceRoot $rel
        $curDst = Join-Path $DestRoot $rel
        New-Item -ItemType Directory -Path $curDst -Force | Out-Null
        foreach ($entry in (Get-ChildItem -LiteralPath $curSrc -Force)) {
            if ($entry.PSIsContainer) {
                $name = $entry.Name
                if (($excludedDirNames -contains $name) -or ($name -like '*.WebView2')) {
                    continue
                }
                $stack.Push((Join-Path $rel $name))
            } else {
                $skip = $false
                foreach ($pat in $excludedFilePatterns) {
                    if ($entry.Name -like $pat) {
                        $skip = $true
                        break
                    }
                }
                if (-not $skip) {
                    Copy-Item -LiteralPath $entry.FullName -Destination (Join-Path $curDst $entry.Name) -Force
                }
            }
        }
    }
}

$pyExe = Find-RealPython
if ([System.IO.Path]::GetFullPath($srcDir).TrimEnd('\') -ne [System.IO.Path]::GetFullPath($destDir).TrimEnd('\')) {
    Stop-FlowPilotRuntime -PluginDir $destDir
    New-Item -ItemType Directory -Path $destDir -Force | Out-Null

    $allowFiles = @(
        'plugin.json',
        'mcp_config.json',
        'hooks.json',
        'LICENSE',
        'THIRD_PARTY_NOTICES.md',
        'scripts\fast_io_mcp.py',
        'scripts\hook_entry.py'
    )
    foreach ($relFile in $allowFiles) {
        $srcFile = Join-Path $srcDir $relFile
        if (-not (Test-Path -LiteralPath $srcFile -PathType Leaf)) {
            throw "缺少必需的运行时文件: $relFile"
        }
        $dstFile = Join-Path $destDir $relFile
        $dstParent = Split-Path -Parent $dstFile
        if ($dstParent -and (-not (Test-Path -LiteralPath $dstParent))) {
            New-Item -ItemType Directory -Path $dstParent -Force | Out-Null
        }
        Copy-Item -LiteralPath $srcFile -Destination $dstFile -Force
    }

    $allowDirs = @('assets', 'rules', 'sidecars')
    foreach ($relDir in $allowDirs) {
        Copy-AllowlistedTree -SourceRoot $srcDir -DestRoot $destDir -RelativeDir $relDir
    }
}

$globalHooksPath = Join-Path $env:USERPROFILE '.gemini\config\hooks.json'

@'
import json, os, pathlib, sys
dest = pathlib.Path(sys.argv[1])
py = os.path.realpath(sys.argv[2])
global_hooks_p = pathlib.Path(sys.argv[3])
qpy = f'"{py}"' if ' ' in py else py
hooks_p = dest / 'hooks.json'
h = json.loads(hooks_p.read_text('utf-8'))
for grp in h.values():
    for ev_list in grp.values():
        for item in ev_list:
            if 'command' in item:
                item['command'] = f'{qpy} -S scripts/hook_entry.py'
hooks_p.write_text(json.dumps(h, ensure_ascii=False, indent=2) + '\n', 'utf-8')

old_gate = dest / 'scripts' / 'fast_pretool_gate.py'
if old_gate.exists() or old_gate.is_symlink():
    try:
        old_gate.unlink()
    except OSError:
        pass

if global_hooks_p.is_file():
    try:
        raw_gh = global_hooks_p.read_text('utf-8-sig').strip()
        loaded_gh = json.loads(raw_gh) if raw_gh else {}
    except Exception:
        loaded_gh = None
    if isinstance(loaded_gh, dict) and 'flow-pilot-swarm-wait-guard' in loaded_gh:
        loaded_gh.pop('flow-pilot-swarm-wait-guard', None)
        if loaded_gh:
            global_hooks_p.write_text(json.dumps(loaded_gh, ensure_ascii=False, indent=2) + '\n', 'utf-8')
        else:
            try:
                global_hooks_p.unlink()
            except FileNotFoundError:
                pass

mcp_p = dest / 'mcp_config.json'
try:
    m = json.loads(mcp_p.read_text('utf-8-sig')) if mcp_p.is_file() else {}
except Exception:
    m = {}
if not isinstance(m, dict):
    m = {}
servers = m.get('mcpServers')
if not isinstance(servers, dict):
    servers = {}
servers.pop('agy-flow-pilot-fast-io', None)
servers['fast_io'] = {
    'command': py,
    'args': ['-S', '-B', '-u', str(dest / 'scripts' / 'fast_io_mcp.py')],
    'tools': {
        'fast_io_batch_read': {'eager': True},
        'fast_io_grep': {'eager': True},
        'fast_io_glob_tree': {'eager': True},
        'fast_io_outline': {'eager': True},
    }
}
m['mcpServers'] = servers
mcp_p.write_text(json.dumps(m, ensure_ascii=False, indent=2) + '\n', 'utf-8')

sc_p = dest / 'sidecars' / 'pilot' / 'sidecar.json'
sc = json.loads(sc_p.read_text('utf-8'))
sc['command'] = py
sc_p.write_text(json.dumps(sc, ensure_ascii=False, indent=2) + '\n', 'utf-8')
'@ | & $pyExe -S - $destDir $pyExe $globalHooksPath
if ($LASTEXITCODE -ne 0) { throw "Flow Pilot 配置写入失败 (exit $LASTEXITCODE)" }

Write-Host "[Flow Pilot v3.8.2] 安装完成: $destDir" -ForegroundColor Green
Write-Host "[Flow Pilot v3.8.2] 已绑定 Python: $pyExe" -ForegroundColor Cyan
