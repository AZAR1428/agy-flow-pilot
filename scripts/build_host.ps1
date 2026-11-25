<#
.SYNOPSIS
    Reproducible clean x64 build & PE privacy verification for FlowPilotHost.exe.
.DESCRIPTION
    Compiles sidecars\pilot\host\FlowPilotHost.cs with .NET Framework 4.x csc.exe
    using /target:winexe /platform:x64 /optimize+ /debug- and verifies that the
    resulting PE32+ executable has IMAGE_DEBUG_DIRECTORY == (0, 0), zero RSDS/NB10/
    .pdb/Users\ path strings, valid version metadata, and passes --self-test.
#>
[CmdletBinding()]
param(
    [switch]$SkipSelfTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
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
    $utf16 = [System.Text.Encoding]::Unicode.GetString($bytes)

    $forbiddenExact = @("RSDS", "NB10")
    foreach ($tok in $forbiddenExact) {
        if ($latin1.IndexOf($tok, [System.StringComparison]::Ordinal) -ge 0) {
            throw "Forbidden debug marker '$tok' found in PE bytes"
        }
    }

    $forbiddenCi = @(".pdb", "Users\", "Users/")
    foreach ($tok in $forbiddenCi) {
        if ($latin1.IndexOf($tok, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
            throw "Forbidden ASCII string '$tok' found in PE bytes"
        }
        if ($utf16.IndexOf($tok, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
            throw "Forbidden UTF-16LE string '$tok' found in PE bytes"
        }
    }

    $verInfo = [System.Diagnostics.FileVersionInfo]::GetVersionInfo($ExePath)
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
    $stagedExe = Join-Path $tmpDir "FlowPilotHost.exe"
    $cscArgs = @(
        "/nologo",
        "/target:winexe",
        "/platform:x64",
        "/optimize+",
        "/debug-",
        "/out:$stagedExe",
        "/r:System.dll",
        "/r:System.Drawing.dll",
        "/r:System.Windows.Forms.dll",
        "/r:System.Web.Extensions.dll",
        "/r:$wv2CoreDll",
        $srcPath
    )

    Write-Host "[build_host] Compiling FlowPilotHost.cs (x64, /optimize+, /debug-)..."
    $cscOut = & $cscExe @cscArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        if ($cscOut) { $cscOut | ForEach-Object { Write-Error $_ } }
        throw "csc.exe failed with exit code $LASTEXITCODE"
    }
    if ($cscOut) {
        $cscOut | ForEach-Object { Write-Host $_ }
    }

    $peCheck = Test-CleanPeBinary -ExePath $stagedExe
    Write-Host ("[build_host] PE privacy check PASSED: size={0} B machine={1} magic={2} debugDir={3} version={4} title={5}" -f `
        $peCheck.SizeBytes, $peCheck.MachineHex, $peCheck.OptMagicHex, $peCheck.DebugDirectory, $peCheck.FileVersion, $peCheck.FileDescription)

    Copy-Item -LiteralPath $stagedExe -Destination $outPath -Force

    $finalCheck = Test-CleanPeBinary -ExePath $outPath
    $sha256 = (Get-FileHash -LiteralPath $outPath -Algorithm SHA256).Hash
    Write-Host ("[build_host] Installed {0} (SHA256={1}, size={2} B, debugDir={3})" -f $outPath, $sha256, $finalCheck.SizeBytes, $finalCheck.DebugDirectory)

    if (-not $SkipSelfTest) {
        Write-Host "[build_host] Running FlowPilotHost.exe --self-test..."
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $outPath
        $psi.Arguments = "--self-test"
        $psi.WorkingDirectory = $hostDir
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $proc = [System.Diagnostics.Process]::Start($psi)
        try {
            $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
            $stderrTask = $proc.StandardError.ReadToEndAsync()
            if (-not $proc.WaitForExit(15000)) {
                try { $proc.Kill() } catch {}
                throw "FlowPilotHost.exe --self-test timed out after 15s"
            }
            $selfJson = $stdoutTask.GetAwaiter().GetResult().Trim()
            $selfErr = $stderrTask.GetAwaiter().GetResult().Trim()
            $selfRc = $proc.ExitCode
        }
        finally {
            $proc.Dispose()
        }
        if ($selfRc -ne 0) {
            throw "FlowPilotHost.exe --self-test failed (exit $selfRc): $selfErr $selfJson"
        }
        if (-not $selfJson) {
            throw "FlowPilotHost.exe --self-test returned empty stdout (stderr: $selfErr)"
        }
        $parsed = $selfJson | ConvertFrom-Json
        if (-not $parsed.ok) {
            throw "FlowPilotHost.exe --self-test reported ok=false: $selfJson"
        }
        Write-Host ("[build_host] Self-test PASSED: ok={0} version={1} is64Bit={2} loopbackUrlGuardOk={3}" -f `
            $parsed.ok, $parsed.version, $parsed.is64BitProcess, $parsed.loopbackUrlGuardOk)
    }
}
finally {
    if (Test-Path -LiteralPath $tmpDir) {
        Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
