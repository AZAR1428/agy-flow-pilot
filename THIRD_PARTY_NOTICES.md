# Third-Party Software Notices

This repository (`agy-flow-pilot`) is licensed under the MIT License (see `LICENSE`),
except for the third-party redistributable binaries listed below, which remain subject
to their respective original licenses.

---

## Microsoft WebView2 SDK

- **Included Files**:
  - `sidecars/pilot/host/Microsoft.Web.WebView2.Core.dll` (Version `1.0.4314.35`)
  - `sidecars/pilot/host/WebView2Loader.dll` (Version `1.0.4314.35`)
- **Copyright**: Copyright (c) Microsoft Corporation. All rights reserved.
- **License**: Microsoft WebView2 SDK License (Microsoft Software License Terms)
- **Official NuGet Source**: `Microsoft.Web.WebView2` (`https://www.nuget.org/packages/Microsoft.Web.WebView2/1.0.4314.35`)
- **License Terms URL**: `https://www.nuget.org/packages/Microsoft.Web.WebView2/1.0.4314.35/license`

### Redistribution Notice

These two binaries are unmodified, Authenticode-signed redistributable components
published by Microsoft Corporation as part of the official `Microsoft.Web.WebView2`
NuGet package (`v1.0.4314.35`), bundled solely to support the Windows WebView2
desktop HUD host (`sidecars/pilot/host/FlowPilotHost.exe`).

Use and redistribution of these binaries are governed by the Microsoft WebView2 SDK
License, NOT the MIT License of this repository. In accordance with the Microsoft
WebView2 SDK License:

1. The redistributable binaries are distributed unmodified in object-code form.
2. Microsoft's name, logos, and trademarks are not used to market this project.
3. The binaries are provided "as-is" without warranty of any kind from Microsoft
   or the maintainers of this repository.
4. Please refer to the official license URL above for the complete, authoritative
   Microsoft WebView2 SDK License terms.
