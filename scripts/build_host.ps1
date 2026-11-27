<#
.SYNOPSIS
    Reproducible clean x64 build, self-test & PE privacy verification for FlowPilotHost.exe.
.DESCRIPTION
    1. Compiles a temporary FlowPilotHost.selftest.exe with /define:SELF_TEST and executes --self-test.
    2. Compiles the lean production sidecars\pilot\host\FlowPilotHost.exe WITHOUT /define:SELF_TEST
       using /nologo /target:winexe /platform:x64 /optimize+ /debug-.
    3. Verifies PE32+ x64 headers, IMAGE_DEBUG_DIRECTORY == (0, 0), version metadata matching plugin.json,
       zero RSDS/NB10/.pdb/Users\ path strings across ASCII and even/odd UTF-16LE encodings,
       and confirms negative --url exits with code 2 on the production binary.
#>
[CmdletBinding()]
param(
    [switch]$SkipSelfTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$manifestPath = Join-Path $repoRoot "plugin.json"
if (-not (Test-Path -LiteralPath $manifestPath)) {
    throw "Plugin manifest not found: $manifestPath"
}
$expectedVersion = [string]((Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json).version)
if (-not $expectedVersion) {
    throw "Failed to read version from $manifestPath"
}
$expectedFileVersion = "$expectedVersion.0"
$hostDir = Join-Path $repoRoot "sidecars\pilot\host"
$srcPath = Join-Path $hostDir "FlowPilotHost.cs"
$outPath = Join-Path $hostDir "FlowPilotHost.exe"
$wv2CoreDll = Join-Path $hostDir "Microsoft.Web.WebView2.Core.dll"
$cscExe = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"

if (-not (Test-Path -LiteralPath $srcPath)) {
    throw "Source file not found: $srcPath"
}
if (-not (Test-Path -LiteralPath $wv2CoreDll)) {
    throw "Required WebView2 reference not found: $wv2CoreDll"
}
if (-not (Test-Path -LiteralPath $cscExe)) {
    throw "C# compiler not found: $cscExe"
}

function Invoke-HostCompile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$OutputExe,
        [string[]]$ExtraArgs = @()
    )

    $cscArgs = @(
        "/nologo",
        "/target:winexe",
        "/platform:x64",
        "/optimize+",
        "/debug-",
        "/out:$OutputExe",
        "/r:System.dll",
        "/r:System.Drawing.dll",
        "/r:System.Windows.Forms.dll",
        "/r:System.Web.Extensions.dll",
        "/r:$wv2CoreDll"
    )
    if ($ExtraArgs -and $ExtraArgs.Count -gt 0) {
        $cscArgs += $ExtraArgs
    }
    $cscArgs += $srcPath

    $cscOut = & $cscExe @cscArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        if ($cscOut) { $cscOut | ForEach-Object { Write-Error $_ } }
        throw "csc.exe failed with exit code $LASTEXITCODE"
    }
    if ($cscOut) {
        $cscOut | ForEach-Object { Write-Host $_ }
    }
}

function Test-CleanPeBinary {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ExePath
    )

    [byte[]]$bytes = [System.IO.File]::ReadAllBytes($ExePath)
    if ($bytes.Length -lt 256) {
        throw "PE file too small: $($bytes.Length) bytes"
    }
    if ($bytes[0] -ne 0x4D -or $bytes[1] -ne 0x5A) {
        throw "Invalid DOS header (missing MZ signature)"
    }

    $peOffset = [System.BitConverter]::ToInt32($bytes, 0x3C)
    if ($peOffset -lt 0 -or ($peOffset + 200) -gt $bytes.Length) {
        throw "Invalid e_lfanew PE offset: $peOffset"
    }
    if ($bytes[$peOffset] -ne 0x50 -or $bytes[$peOffset + 1] -ne 0x45 -or $bytes[$peOffset + 2] -ne 0x00 -or $bytes[$peOffset + 3] -ne 0x00) {
        throw "Invalid PE signature at offset $peOffset"
    }

    $machine = [System.BitConverter]::ToUInt16($bytes, $peOffset + 4)
    if ($machine -ne 0x8664) {
        throw ("Unexpected COFF machine 0x{0:X4} (expected 0x8664 x64)" -f $machine)
    }

    $optOffset = $peOffset + 24
    $optMagic = [System.BitConverter]::ToUInt16($bytes, $optOffset)
    if ($optMagic -ne 0x020B) {
        throw ("Unexpected OptionalHeader magic 0x{0:X4} (expected 0x020B PE32+)" -f $optMagic)
    }

    $numRvaAndSizes = [System.BitConverter]::ToUInt32($bytes, $optOffset + 108)
    if ($numRvaAndSizes -lt 7) {
        throw "PE DataDirectory count too small ($numRvaAndSizes)"
    }

    # PE32+: DataDirectory starts at optOffset + 112; entry 6 is IMAGE_DIRECTORY_ENTRY_DEBUG
    $debugDirOffset = $optOffset + 112 + (6 * 8)
    $debugRva = [System.BitConverter]::ToUInt32($bytes, $debugDirOffset)
    $debugSize = [System.BitConverter]::ToUInt32($bytes, $debugDirOffset + 4)
    if ($debugRva -ne 0 -or $debugSize -ne 0) {
        throw "Non-zero IMAGE_DEBUG_DIRECTORY detected: RVA=$debugRva Size=$debugSize"
    }

    $latin1 = [System.Text.Encoding]::GetEncoding(28591).GetString($bytes)
    $evenLen = $bytes.Length - ($bytes.Length % 2)
    $utf16Even = if ($evenLen -ge 2) { [System.Text.Encoding]::Unicode.GetString($bytes, 0, $evenLen) } else { "" }
    $oddRaw = $bytes.Length - 1
    $oddLen = $oddRaw - ($oddRaw % 2)
    $utf16Odd = if ($oddLen -ge 2) { [System.Text.Encoding]::Unicode.GetString($bytes, 1, $oddLen) } else { "" }

    $forbiddenExact = @("RSDS", "NB10")
    foreach ($tok in $forbiddenExact) {
        if ($latin1.IndexOf($tok, [System.StringComparison]::Ordinal) -ge 0) {
            throw "Forbidden ASCII debug marker '$tok' found in PE bytes"
        }
        if ($utf16Even.IndexOf($tok, [System.StringComparison]::Ordinal) -ge 0 -or $utf16Odd.IndexOf($tok, [System.StringComparison]::Ordinal) -ge 0) {
            throw "Forbidden UTF-16LE debug marker '$tok' found in PE bytes"
        }
    }

    $forbiddenCi = @(".pdb", "Users\", "Users/")
    foreach ($tok in $forbiddenCi) {
        if ($latin1.IndexOf($tok, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
            throw "Forbidden ASCII string '$tok' found in PE bytes"
        }
        if ($utf16Even.IndexOf($tok, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
            throw "Forbidden even-aligned UTF-16LE string '$tok' found in PE bytes"
        }
        if ($utf16Odd.IndexOf($tok, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
            throw "Forbidden odd-aligned UTF-16LE string '$tok' found in PE bytes"
        }
    }

    $verInfo = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($ExePath)
    if ($verInfo.FileVersion -ne $expectedFileVersion) {
        throw "Unexpected FileVersion '$($verInfo.FileVersion)' (expected '$expectedFileVersion')"
    }

    return [pscustomobject]@{
        SizeBytes = $bytes.Length
        MachineHex = ("0x{0:X4}" -f $machine)
        OptMagicHex = ("0x{0:X4}" -f $optMagic)
        DebugDirectory = "($debugRva, $debugSize)"
        FileVersion = $verInfo.FileVersion
        ProductName = $verInfo.ProductName
        FileDescription = $verInfo.FileDescription
    }
}

$tmpDir = Join-Path ([System.IO.Path]::GetTempPath()) ("fp_host_build_" + [System.Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null

try {
    if (-not $SkipSelfTest) {
        Get-ChildItem -LiteralPath $hostDir -Filter "*.dll" -File | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $tmpDir $_.Name) -Force
        }

        $selfTestExe = Join-Path $tmpDir "FlowPilotHost.selftest.exe"
        Write-Host "[build_host] Compiling temporary FlowPilotHost.selftest.exe (/define:SELF_TEST)..."
        Invoke-HostCompile -OutputExe $selfTestExe -ExtraArgs @("/define:SELF_TEST")

        Write-Host "[build_host] Running FlowPilotHost.selftest.exe --self-test..."
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $selfTestExe
        $psi.Arguments = "--self-test"
        $psi.WorkingDirectory = $tmpDir
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.EnvironmentVariables["PLUGIN_DATA"] = $tmpDir
        $proc = [System.Diagnostics.Process]::Start($psi)
        try {
            $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
            $stderrTask = $proc.StandardError.ReadToEndAsync()
            if (-not $proc.WaitForExit(15000)) {
                try { $proc.Kill() } catch {}
                throw "FlowPilotHost.selftest.exe --self-test timed out after 15s"
            }
            $selfJson = $stdoutTask.GetAwaiter().GetResult().Trim()
            $selfErr = $stderrTask.GetAwaiter().GetResult().Trim()
            $selfRc = $proc.ExitCode
        }
        finally {
            $proc.Dispose()
        }
        if ($selfRc -ne 0) {
            throw "FlowPilotHost.selftest.exe --self-test failed (exit $selfRc): $selfErr $selfJson"
        }
        if (-not $selfJson) {
            throw "FlowPilotHost.selftest.exe --self-test returned empty stdout (stderr: $selfErr)"
        }
        $parsed = $selfJson | ConvertFrom-Json
        if (-not $parsed.ok) {
            throw "FlowPilotHost.selftest.exe --self-test reported ok=false: $selfJson"
        }
        if ($parsed.version -ne $expectedVersion) {
            throw "FlowPilotHost.selftest.exe --self-test reported version='$($parsed.version)' (expected '$expectedVersion')"
        }
        if (-not $parsed.multiMonitorDragOk) {
            throw "FlowPilotHost.selftest.exe --self-test reported multiMonitorDragOk=false"
        }
        if (-not $parsed.loopbackUrlGuardOk) {
            throw "FlowPilotHost.selftest.exe --self-test reported loopbackUrlGuardOk=false"
        }
        Write-Host ("[build_host] Self-test PASSED: ok={0} version={1} is64Bit={2} multiMonitorDragOk={3} loopbackUrlGuardOk={4}" -f `
            $parsed.ok, $parsed.version, $parsed.is64BitProcess, $parsed.multiMonitorDragOk, $parsed.loopbackUrlGuardOk)
    }

    $stagedExe = Join-Path $tmpDir "FlowPilotHost.exe"
    Write-Host "[build_host] Compiling production FlowPilotHost.exe (x64, /optimize+, /debug-, stripped SELF_TEST)..."
    Invoke-HostCompile -OutputExe $stagedExe

    $peCheck = Test-CleanPeBinary -ExePath $stagedExe
    Write-Host ("[build_host] PE privacy check PASSED: size={0} B machine={1} magic={2} debugDir={3} version={4} title={5}" -f `
        $peCheck.SizeBytes, $peCheck.MachineHex, $peCheck.OptMagicHex, $peCheck.DebugDirectory, $peCheck.FileVersion, $peCheck.FileDescription)

    Copy-Item -LiteralPath $stagedExe -Destination $outPath -Force

    $finalCheck = Test-CleanPeBinary -ExePath $outPath
    $sha256 = (Get-FileHash -LiteralPath $outPath -Algorithm SHA256).Hash
    Write-Host ("[build_host] Installed {0} (SHA256={1}, size={2} B, version={3}, debugDir={4})" -f `
        $outPath, $sha256, $finalCheck.SizeBytes, $finalCheck.FileVersion, $finalCheck.DebugDirectory)

    Write-Host "[build_host] Verifying negative --url guard on production FlowPilotHost.exe..."
    $negPsi = New-Object System.Diagnostics.ProcessStartInfo
    $negPsi.FileName = $outPath
    $negPsi.Arguments = "--url http://example.com/ui.html"
    $negPsi.WorkingDirectory = $hostDir
    $negPsi.UseShellExecute = $false
    $negPsi.CreateNoWindow = $true
    $negPsi.EnvironmentVariables["PLUGIN_DATA"] = $tmpDir
    $negProc = [System.Diagnostics.Process]::Start($negPsi)
    try {
        if (-not $negProc.WaitForExit(10000)) {
            try { $negProc.Kill() } catch {}
            throw "Negative --url check timed out after 10s"
        }
        $negRc = $negProc.ExitCode
    }
    finally {
        $negProc.Dispose()
    }
    if ($negRc -ne 2) {
        throw "Negative --url check failed: expected exit code 2, got $negRc"
    }
    Write-Host "[build_host] Negative --url guard PASSED (exit=2)"
}
finally {
    if (Test-Path -LiteralPath $tmpDir) {
        Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
