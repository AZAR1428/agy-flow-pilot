using System;
using System.Collections.Generic;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;

[assembly: AssemblyTitle("FlowPilotHost")]
[assembly: AssemblyProduct("Flow Pilot")]
[assembly: AssemblyVersion("3.8.3.0")]
[assembly: AssemblyFileVersion("3.8.3.0")]

namespace AgyFlowPilot
{
    internal static class NativeMethods
    {
        public const int WS_EX_TOPMOST = 0x00000008;
        public const int WS_EX_TOOLWINDOW = 0x00000080;
        public const int WS_EX_NOREDIRECTIONBITMAP = 0x00200000;

        public const int WM_MOVE = 0x0003;
        public const int WM_SIZE = 0x0005;
        public const int WM_SETTINGCHANGE = 0x001A;
        public const int WM_GETMINMAXINFO = 0x0024;
        public const int WM_DISPLAYCHANGE = 0x007E;
        public const int WM_NCLBUTTONDOWN = 0x00A1;
        public const int WM_SYSCOMMAND = 0x0112;
        public const int WM_MOVING = 0x0216;
        public const int WM_POWERBROADCAST = 0x0218;
        public const int WM_ENTERSIZEMOVE = 0x0231;
        public const int WM_EXITSIZEMOVE = 0x0232;
        public const int WM_DPICHANGED = 0x02E0;
        public const int WM_DWMCOMPOSITIONCHANGED = 0x031E;

        public const int SC_MINIMIZE = 0xF020;
        public const int SC_MAXIMIZE = 0xF030;
        public const int SC_CLOSE = 0xF060;

        public const int SPI_SETWORKAREA = 0x002F;
        public const int PBT_APMRESUMESUSPEND = 0x0007;
        public const int PBT_APMRESUMEAUTOMATIC = 0x0012;

        public const int HTCAPTION = 0x0002;
        public const int HTLEFT = 10;
        public const int HTRIGHT = 11;
        public const int HTTOP = 12;
        public const int HTTOPLEFT = 13;
        public const int HTTOPRIGHT = 14;
        public const int HTBOTTOM = 15;
        public const int HTBOTTOMLEFT = 16;
        public const int HTBOTTOMRIGHT = 17;

        public static readonly IntPtr HWND_TOPMOST = new IntPtr(-1);
        public static readonly IntPtr HWND_NOTOPMOST = new IntPtr(-2);

        public const uint SWP_NOSIZE = 0x0001;
        public const uint SWP_NOMOVE = 0x0002;
        public const uint SWP_NOZORDER = 0x0004;
        public const uint SWP_NOACTIVATE = 0x0010;
        public const uint SWP_SHOWWINDOW = 0x0040;
        public const uint SWP_NOOWNERZORDER = 0x0200;

        public const uint MONITOR_DEFAULTTONULL = 0x00000000;
        public const uint MONITOR_DEFAULTTOPRIMARY = 0x00000001;
        public const uint MONITOR_DEFAULTTONEAREST = 0x00000002;

        public const uint SYNCHRONIZE = 0x00100000;
        public const uint INFINITE = 0xFFFFFFFF;
        public const uint WAIT_OBJECT_0 = 0x00000000;
        public const uint WAIT_ABANDONED = 0x00000080;
        public const uint WAIT_TIMEOUT = 0x00000102;

        public const int STD_INPUT_HANDLE = -10;
        public const uint FILE_TYPE_DISK = 0x0001;
        public const uint FILE_TYPE_PIPE = 0x0003;

        [StructLayout(LayoutKind.Sequential)]
        public struct POINT { public int X; public int Y; }

        [StructLayout(LayoutKind.Sequential)]
        public struct MINMAXINFO
        {
            public POINT ptReserved;
            public POINT ptMaxSize;
            public POINT ptMaxPosition;
            public POINT ptMinTrackSize;
            public POINT ptMaxTrackSize;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct RECT
        {
            public int Left;
            public int Top;
            public int Right;
            public int Bottom;
            public int Width { get { return Right - Left; } }
            public int Height { get { return Bottom - Top; } }
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Auto)]
        public struct MONITORINFO
        {
            public int cbSize;
            public RECT rcMonitor;
            public RECT rcWork;
            public uint dwFlags;
        }

        public struct MonitorDisplayInfo
        {
            public IntPtr Handle;
            public RECT MonitorRect;
            public RECT WorkRect;
            public uint Dpi;
            public bool IsPrimary;
        }

        public delegate bool MonitorEnumProc(IntPtr hMonitor, IntPtr hdcMonitor, ref RECT lprcMonitor, IntPtr dwData);

        [DllImport("user32.dll", SetLastError = true)]
        public static extern bool SetProcessDpiAwarenessContext(IntPtr dpiFlag);
        [DllImport("shcore.dll", SetLastError = true)]
        public static extern int SetProcessDpiAwareness(int awareness);
        [DllImport("user32.dll", SetLastError = true)]
        public static extern bool SetProcessDPIAware();
        [DllImport("user32.dll")]
        public static extern uint GetDpiForWindow(IntPtr hwnd);
        [DllImport("user32.dll")]
        public static extern uint GetDpiForSystem();
        [DllImport("shcore.dll")]
        public static extern int GetDpiForMonitor(IntPtr hmonitor, int dpiType, out uint dpiX, out uint dpiY);
        [DllImport("user32.dll", SetLastError = true)]
        public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
        [DllImport("user32.dll", SetLastError = true)]
        public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
        [DllImport("user32.dll")]
        public static extern bool IsWindowVisible(IntPtr hWnd);
        [DllImport("user32.dll")]
        public static extern bool ReleaseCapture();
        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        public static extern IntPtr SendMessage(IntPtr hWnd, int Msg, IntPtr wParam, IntPtr lParam);
        [DllImport("user32.dll")]
        public static extern bool EnumDisplayMonitors(IntPtr hdc, IntPtr lprcClip, MonitorEnumProc lpfnEnum, IntPtr dwData);
        [DllImport("user32.dll")]
        public static extern IntPtr MonitorFromWindow(IntPtr hwnd, uint dwFlags);
        [DllImport("user32.dll")]
        public static extern IntPtr MonitorFromPoint(POINT pt, uint dwFlags);
        [DllImport("user32.dll")]
        public static extern bool GetCursorPos(out POINT lpPoint);
        [DllImport("user32.dll", CharSet = CharSet.Auto)]
        public static extern bool GetMonitorInfo(IntPtr hMonitor, ref MONITORINFO lpmi);
        [DllImport("user32.dll")]
        public static extern int SetWindowRgn(IntPtr hWnd, IntPtr hRgn, bool bRedraw);
        [DllImport("gdi32.dll")]
        public static extern IntPtr CreateRoundRectRgn(int nLeft, int nTop, int nRight, int nBottom, int nWidthEllipse, int nHeightEllipse);
        [DllImport("gdi32.dll")]
        public static extern bool DeleteObject(IntPtr hObject);
        [DllImport("dwmapi.dll")]
        public static extern int DwmIsCompositionEnabled(out bool enabled);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern IntPtr CreateMutexW(IntPtr lpMutexAttributes, bool bInitialOwner, string lpName);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool ReleaseMutex(IntPtr hMutex);
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        public static extern IntPtr CreateEventW(IntPtr lpEventAttributes, bool bManualReset, bool bInitialState, string lpName);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool SetEvent(IntPtr hEvent);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr OpenProcess(uint dwDesiredAccess, bool bInheritHandle, uint dwProcessId);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern uint WaitForSingleObject(IntPtr hHandle, uint dwMilliseconds);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern uint WaitForMultipleObjects(uint nCount, IntPtr[] lpHandles, bool bWaitAll, uint dwMilliseconds);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern bool CloseHandle(IntPtr hObject);
        [DllImport("kernel32.dll", SetLastError = true)]
        public static extern IntPtr GetStdHandle(int nStdHandle);
        [DllImport("kernel32.dll")]
        public static extern uint GetFileType(IntPtr hFile);

        public static bool HasRedirectedStdinPipe()
        {
            try
            {
                IntPtr hIn = GetStdHandle(STD_INPUT_HANDLE);
                if (hIn == IntPtr.Zero || hIn == new IntPtr(-1)) return false;
                uint ft = GetFileType(hIn);
                return ft == FILE_TYPE_PIPE || ft == FILE_TYPE_DISK;
            }
            catch { return false; }
        }

        public static void EnablePerMonitorDpiV2()
        {
            try { if (SetProcessDpiAwarenessContext(new IntPtr(-4))) return; } catch { }
            try { if (SetProcessDpiAwareness(2) == 0) return; } catch { }
            try { SetProcessDPIAware(); } catch { }
        }

        public static bool IsDwmActive()
        {
            try { bool en = false; return DwmIsCompositionEnabled(out en) == 0 && en; }
            catch { return false; }
        }

        public static uint GetSafeDpiForWindow(IntPtr hwnd)
        {
            if (hwnd != IntPtr.Zero)
            {
                try
                {
                    uint wdpi = GetDpiForWindow(hwnd);
                    if (wdpi >= 72 && wdpi <= 576) return wdpi;
                }
                catch { }
            }
            return GetSafeSystemDpi();
        }

        public static uint GetSafeDpiForMonitor(IntPtr hMon)
        {
            if (hMon != IntPtr.Zero)
            {
                try
                {
                    uint dx = 96, dy = 96;
                    if (GetDpiForMonitor(hMon, 0, out dx, out dy) == 0 && dx >= 72 && dx <= 576) return dx;
                }
                catch { }
            }
            return GetSafeSystemDpi();
        }

        public static uint GetSafeSystemDpi()
        {
            try
            {
                uint sdpi = GetDpiForSystem();
                if (sdpi >= 72 && sdpi <= 576) return sdpi;
            }
            catch { }
            return 96;
        }

        public static RECT GetWorkAreaForWindow(IntPtr hwnd)
        {
            return GetWorkAreaFromMonitor(MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST));
        }

        public static RECT GetWorkAreaForPoint(int x, int y)
        {
            return GetWorkAreaFromMonitor(MonitorFromPoint(new POINT { X = x, Y = y }, MONITOR_DEFAULTTONEAREST));
        }

        public static RECT GetPrimaryWorkArea(out uint dpi)
        {
            IntPtr hMon = MonitorFromPoint(new POINT { X = 0, Y = 0 }, MONITOR_DEFAULTTOPRIMARY);
            dpi = GetSafeDpiForMonitor(hMon);
            return GetWorkAreaFromMonitor(hMon);
        }

        public static bool TryGetMonitorDisplayInfo(IntPtr hMon, out MonitorDisplayInfo info)
        {
            info = new MonitorDisplayInfo();
            if (hMon == IntPtr.Zero) return false;
            try
            {
                MONITORINFO mi = new MONITORINFO();
                mi.cbSize = Marshal.SizeOf(typeof(MONITORINFO));
                if (GetMonitorInfo(hMon, ref mi) && mi.rcMonitor.Width > 0 && mi.rcMonitor.Height > 0)
                {
                    info.Handle = hMon;
                    info.MonitorRect = mi.rcMonitor;
                    info.WorkRect = (mi.rcWork.Width > 0 && mi.rcWork.Height > 0) ? mi.rcWork : mi.rcMonitor;
                    info.Dpi = GetSafeDpiForMonitor(hMon);
                    info.IsPrimary = (mi.dwFlags & 1u) != 0;
                    return true;
                }
            }
            catch { }
            return false;
        }

        public static MonitorDisplayInfo[] GetAllMonitors()
        {
            List<MonitorDisplayInfo> list = new List<MonitorDisplayInfo>();
            try
            {
                EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, delegate(IntPtr hMon, IntPtr hdc, ref RECT clipRect, IntPtr data)
                {
                    MonitorDisplayInfo info;
                    if (TryGetMonitorDisplayInfo(hMon, out info))
                        list.Add(info);
                    return true;
                }, IntPtr.Zero);
            }
            catch { }

            if (list.Count == 0)
            {
                uint primaryDpi;
                RECT fbWork = GetPrimaryWorkArea(out primaryDpi);
                Rectangle fbBounds = Screen.PrimaryScreen != null
                    ? Screen.PrimaryScreen.Bounds
                    : new Rectangle(fbWork.Left, fbWork.Top, fbWork.Width, fbWork.Height);
                RECT fbMon = new RECT
                {
                    Left = fbBounds.Left,
                    Top = fbBounds.Top,
                    Right = fbBounds.Right,
                    Bottom = fbBounds.Bottom
                };
                list.Add(new MonitorDisplayInfo
                {
                    Handle = IntPtr.Zero,
                    MonitorRect = (fbMon.Width > 0 && fbMon.Height > 0) ? fbMon : fbWork,
                    WorkRect = fbWork,
                    Dpi = primaryDpi,
                    IsPrimary = true
                });
            }
            return list.ToArray();
        }

        public static MonitorDisplayInfo GetCursorOrPrimaryMonitorInfo()
        {
            try
            {
                POINT curPt;
                if (GetCursorPos(out curPt))
                {
                    IntPtr hMon = MonitorFromPoint(curPt, MONITOR_DEFAULTTONEAREST);
                    MonitorDisplayInfo curInfo;
                    if (TryGetMonitorDisplayInfo(hMon, out curInfo))
                        return curInfo;
                }
            }
            catch { }

            IntPtr hPri = MonitorFromPoint(new POINT { X = 0, Y = 0 }, MONITOR_DEFAULTTOPRIMARY);
            MonitorDisplayInfo priInfo;
            if (TryGetMonitorDisplayInfo(hPri, out priInfo))
                return priInfo;

            uint dpi;
            RECT work = GetPrimaryWorkArea(out dpi);
            return new MonitorDisplayInfo
            {
                Handle = IntPtr.Zero,
                MonitorRect = work,
                WorkRect = work,
                Dpi = dpi,
                IsPrimary = true
            };
        }

        public static RECT GetCursorOrPrimaryWorkArea(out uint dpi)
        {
            MonitorDisplayInfo info = GetCursorOrPrimaryMonitorInfo();
            dpi = info.Dpi >= 72 ? info.Dpi : GetSafeSystemDpi();
            return info.WorkRect;
        }

        private static RECT GetWorkAreaFromMonitor(IntPtr hMon)
        {
            if (hMon != IntPtr.Zero)
            {
                MONITORINFO mi = new MONITORINFO();
                mi.cbSize = Marshal.SizeOf(typeof(MONITORINFO));
                if (GetMonitorInfo(hMon, ref mi) && mi.rcWork.Width > 0 && mi.rcWork.Height > 0)
                    return mi.rcWork;
            }
            Rectangle fb = Screen.PrimaryScreen != null ? Screen.PrimaryScreen.WorkingArea : new Rectangle(0, 0, 1920, 1040);
            return new RECT { Left = fb.Left, Top = fb.Top, Right = fb.Right, Bottom = fb.Bottom };
        }
    }

    internal sealed class HostOptions
    {
        public bool SelfTest;
        public int ParentPid;
        public int Port;
        public string TargetUrl;
        public string HudToken;
        public string PluginDataDir;
        public string ViewMode = "pill";
        public bool AlwaysOnTop = true;
        public string AnchorCorner = "top-right";
        public string Theme = "light";
        public int? SavedStackWidth;
        public int? SavedCustomHeight;
        public int? InitialPhysicalX;
        public int? InitialPhysicalY;
        public int? InitialLogicalX;
        public int? InitialLogicalY;
        public int? InitialLogicalW;
        public int? InitialLogicalH;
        public bool ForceRegionClip;
    }

    internal sealed class HudForm : Form
    {
        public const string HostVersion = "3.8.3";
        public const string LightweightBrowserArgs =
            "--disable-features=msSmartScreenProtection,Translate,msWebOOUI,msPdfOOUI,SpareRendererForSitePerProcess --renderer-process-limit=1 --autoplay-policy=no-user-gesture-required --no-first-run --no-default-browser-check";

        public const int DefaultStackLogicalWidth = 388;
        public const int DefaultStackLogicalHeight = 228;
        public const int MinStackLogicalWidth = 320;
        public const int MinStackLogicalHeight = 68;
        public const int DefaultPillLogicalWidth = 388;
        public const int DefaultPillLogicalHeight = 36;
        public const int MinPillLogicalWidth = 320;
        public const int MinPillLogicalHeight = 34;
        public const int BottomWorkMarginPhys = 6;

        private const long MaxLifecycleLogBytes = 256 * 1024;
        private const int TrimKeepLifecycleLogBytes = 128 * 1024;
        private const int MaxNavRetries = 3;
        private static readonly object _logLock = new object();
        private static readonly int _cachedPid = SafeGetCurrentPid();

        private readonly HostOptions _opts;
        private readonly bool _useRegionFallback;
        private readonly JavaScriptSerializer _json;

        private CoreWebView2Environment _env;
        private CoreWebView2Controller _controller;
        private CoreWebView2 _webView;
        private IntPtr _shutdownEvent;
        private Thread _watchdogThread;
        private bool _alwaysOnTop;
        private string _viewMode;
        private string _anchorCorner;
        private int _logicalWidth;
        private int _logicalHeight;
        private uint _currentDpi;
        private int _closingFlag;
        private int _authorizedExit;
        private int _navRetryCount;
        private bool _inInteractiveMove;
        private bool _inInteractiveResize;
        private bool _inExternalSizeMove;
        private bool _dpiChangedInDrag;
        private bool _suppressDpiSuggestedRect;
        private NativeMethods.MonitorDisplayInfo[] _dragMonitors;
        private uint _dpiTransitionTargetDpi;
        private bool _hasDpiTransitionWork;
        private NativeMethods.RECT _dpiTransitionTargetWork;
        private Rectangle _initialPhysicalBounds;
        private uint _initialDpi;
        private NativeMethods.RECT _initialWorkArea;
        private bool _hasInitialPhysicalBounds;
        private NativeMethods.RECT _externalBeforeRect;
        private uint _externalBeforeDpi;
        private DateTime _postUserResizeGuardUntilUtc = DateTime.MinValue;
        private int? _preDrawerNudgeTopY;
        private bool _lastIdleOpen;
        private string _lastEmittedWorkAreaSig = string.Empty;
        private int _lastRgnW;
        private int _lastRgnH;
        private int _lastRgnDiameter;
        private System.Windows.Forms.Timer _tempTopMostTimer;
        private System.Windows.Forms.Timer _visibilityGuardTimer;
        private System.Windows.Forms.Timer _navRetryTimer;

        public HudForm(HostOptions opts)
        {
            _opts = opts;
            _json = new JavaScriptSerializer();
            _shutdownEvent = NativeMethods.CreateEventW(IntPtr.Zero, true, false, null);
            _alwaysOnTop = opts.AlwaysOnTop;
            _viewMode = NormalizeViewMode(opts.ViewMode);
            _anchorCorner = NormalizeAnchor(opts.AnchorCorner);
            _useRegionFallback = opts.ForceRegionClip
                || SystemInformation.TerminalServerSession
                || !NativeMethods.IsDwmActive();

            int savedStackWidth = (opts.SavedStackWidth.HasValue && opts.SavedStackWidth.Value >= MinStackLogicalWidth && opts.SavedStackWidth.Value <= 960)
                ? opts.SavedStackWidth.Value
                : DefaultStackLogicalWidth;

            _logicalWidth = (opts.InitialLogicalW.HasValue && opts.InitialLogicalW.Value >= MinPillLogicalWidth)
                ? Math.Min(960, opts.InitialLogicalW.Value)
                : savedStackWidth;

            if (opts.InitialLogicalH.HasValue && opts.InitialLogicalH.Value >= 28)
                _logicalHeight = opts.InitialLogicalH.Value;
            else if (_viewMode == "pill")
                _logicalHeight = DefaultPillLogicalHeight;
            else if (opts.SavedCustomHeight.HasValue && opts.SavedCustomHeight.Value >= MinStackLogicalHeight && opts.SavedCustomHeight.Value <= 4096)
                _logicalHeight = opts.SavedCustomHeight.Value;
            else
                _logicalHeight = DefaultStackLogicalHeight;

            this.Text = "Flow Pilot HUD";
            this.FormBorderStyle = FormBorderStyle.None;
            this.ShowInTaskbar = false;
            this.StartPosition = FormStartPosition.Manual;
            this.AutoScaleMode = AutoScaleMode.None;
            this.DoubleBuffered = false;
            this.BackColor = Color.FromArgb(236, 235, 230);

            ComputeInitialPhysicalBounds();
        }

        protected override bool ShowWithoutActivation { get { return true; } }

        protected override CreateParams CreateParams
        {
            get
            {
                CreateParams cp = base.CreateParams;
                cp.ExStyle |= NativeMethods.WS_EX_TOOLWINDOW;
                if (!_useRegionFallback) cp.ExStyle |= NativeMethods.WS_EX_NOREDIRECTIONBITMAP;
                if (_alwaysOnTop) cp.ExStyle |= NativeMethods.WS_EX_TOPMOST;
                return cp;
            }
        }

        protected override void OnPaintBackground(PaintEventArgs e)
        {
            if (_useRegionFallback) base.OnPaintBackground(e);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            if (_useRegionFallback) base.OnPaint(e);
        }

        protected override void OnHandleCreated(EventArgs e)
        {
            base.OnHandleCreated(e);
            if (_hasInitialPhysicalBounds)
            {
                NativeMethods.RECT actualRect;
                if (NativeMethods.GetWindowRect(this.Handle, out actualRect)
                    && (actualRect.Left != _initialPhysicalBounds.X
                        || actualRect.Top != _initialPhysicalBounds.Y
                        || actualRect.Width != _initialPhysicalBounds.Width
                        || actualRect.Height != _initialPhysicalBounds.Height))
                {
                    _suppressDpiSuggestedRect = true;
                    _dpiTransitionTargetDpi = _initialDpi;
                    _dpiTransitionTargetWork = _initialWorkArea;
                    _hasDpiTransitionWork = true;
                    try
                    {
                        NativeMethods.SetWindowPos(
                            this.Handle,
                            _alwaysOnTop ? NativeMethods.HWND_TOPMOST : IntPtr.Zero,
                            _initialPhysicalBounds.X, _initialPhysicalBounds.Y,
                            _initialPhysicalBounds.Width, _initialPhysicalBounds.Height,
                            (_alwaysOnTop ? 0u : NativeMethods.SWP_NOZORDER) | NativeMethods.SWP_NOACTIVATE | NativeMethods.SWP_NOOWNERZORDER);
                    }
                    finally
                    {
                        _hasDpiTransitionWork = false;
                        _dpiTransitionTargetDpi = 0;
                        _suppressDpiSuggestedRect = false;
                    }
                }
            }
            _currentDpi = NativeMethods.GetSafeDpiForWindow(this.Handle);
            ApplyRegionClipIfNeeded(this.Width, this.Height);
            ApplyTopMostNative(_alwaysOnTop);
            StartParentWatchdog();
            StartStdinListener();
            StartVisibilityGuardTimer();
            Task ignored = InitializeWebViewAsync();
            if (ignored == null) return;
        }

        private static int SafeGetCurrentPid()
        {
            try { return System.Diagnostics.Process.GetCurrentProcess().Id; }
            catch { return 0; }
        }

        private static string SanitizeLogField(string raw)
        {
            if (string.IsNullOrEmpty(raw)) return string.Empty;
            return raw.Replace('\r', ' ').Replace('\n', ' ').Trim();
        }

        internal static void LogHostEventStatic(string pluginDataDir, string evt, string detail = null)
        {
            try
            {
                if (string.IsNullOrWhiteSpace(pluginDataDir) || string.IsNullOrWhiteSpace(evt)) return;
                string ts = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff", CultureInfo.InvariantCulture);
                string cleanEvt = SanitizeLogField(evt);
                string cleanDetail = SanitizeLogField(detail);
                string line = string.IsNullOrEmpty(cleanDetail)
                    ? string.Format(CultureInfo.InvariantCulture, "[{0}] [host pid={1}] {2}\n", ts, _cachedPid, cleanEvt)
                    : string.Format(CultureInfo.InvariantCulture, "[{0}] [host pid={1}] {2} {3}\n", ts, _cachedPid, cleanEvt, cleanDetail);
                byte[] lineBytes = Encoding.UTF8.GetBytes(line);

                lock (_logLock)
                {
                    Directory.CreateDirectory(pluginDataDir);
                    string logPath = Path.Combine(pluginDataDir, "hud_lifecycle.log");
                    if (File.Exists(logPath))
                    {
                        FileInfo fi = new FileInfo(logPath);
                        if (fi.Length + lineBytes.Length > MaxLifecycleLogBytes)
                        {
                            byte[] tail = null;
                            using (FileStream rfs = new FileStream(logPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
                            {
                                long len = rfs.Length;
                                int keep = (int)Math.Min(len, TrimKeepLifecycleLogBytes);
                                if (keep > 0)
                                {
                                    rfs.Seek(len - keep, SeekOrigin.Begin);
                                    tail = new byte[keep];
                                    int readTotal = 0;
                                    while (readTotal < keep)
                                    {
                                        int n = rfs.Read(tail, readTotal, keep - readTotal);
                                        if (n <= 0) break;
                                        readTotal += n;
                                    }
                                    if (readTotal < keep) Array.Resize(ref tail, readTotal);
                                }
                            }
                            int startOffset = 0;
                            if (tail != null && tail.Length > 0)
                            {
                                int nlIdx = Array.IndexOf(tail, (byte)'\n');
                                if (nlIdx >= 0 && nlIdx + 1 < tail.Length) startOffset = nlIdx + 1;
                            }
                            using (FileStream wfs = new FileStream(logPath, FileMode.Create, FileAccess.Write, FileShare.ReadWrite))
                            {
                                if (tail != null && startOffset < tail.Length)
                                    wfs.Write(tail, startOffset, tail.Length - startOffset);
                                wfs.Write(lineBytes, 0, lineBytes.Length);
                                wfs.Flush();
                            }
                            return;
                        }
                    }

                    using (FileStream afs = new FileStream(logPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite))
                    {
                        afs.Write(lineBytes, 0, lineBytes.Length);
                        afs.Flush();
                    }
                }
            }
            catch { }
        }

        private void LogHostEvent(string evt, string detail = null)
        {
            LogHostEventStatic(_opts != null ? _opts.PluginDataDir : null, evt, detail);
        }

        private static void TryApplySetting(Action setter)
        {
            try { setter(); }
            catch { }
        }

        protected override void OnResize(EventArgs e)
        {
            base.OnResize(e);
            if (IsHandleCreated && !IsDisposed)
            {
                Rectangle client = this.ClientRectangle;
                if (client.Width > 0 && client.Height > 0)
                {
                    if (_useRegionFallback) ApplyRegionClipIfNeeded(client.Width, client.Height);
                    SyncControllerBoundsRect(client);
                }
            }
        }

        private void ComputeInitialPhysicalBounds()
        {
            _suppressDpiSuggestedRect = true;
            try
            {
                NativeMethods.MonitorDisplayInfo[] monitors = NativeMethods.GetAllMonitors();
                NativeMethods.MonitorDisplayInfo cursorOrPrimary = NativeMethods.GetCursorOrPrimaryMonitorInfo();

                int physX, physY, physW, physH;
                uint targetDpi;
                NativeMethods.RECT targetWork;
                string anchor;
                bool usedSaved;
                ResolveInitialPhysicalBounds(
                    _opts.InitialPhysicalX, _opts.InitialPhysicalY,
                    _opts.InitialLogicalX, _opts.InitialLogicalY,
                    _logicalWidth, _logicalHeight,
                    monitors, cursorOrPrimary,
                    out physX, out physY, out physW, out physH,
                    out targetDpi, out targetWork, out anchor, out usedSaved);

                _currentDpi = targetDpi;
                _anchorCorner = anchor;
                _initialDpi = targetDpi;
                _initialWorkArea = targetWork;
                _initialPhysicalBounds = new Rectangle(physX, physY, physW, physH);
                _hasInitialPhysicalBounds = true;
                this.Bounds = _initialPhysicalBounds;
            }
            finally
            {
                _suppressDpiSuggestedRect = false;
            }
        }

        internal static bool ApplyLowMemoryTarget(CoreWebView2 wv)
        {
            if (wv == null) return false;
            try
            {
                wv.MemoryUsageTargetLevel = CoreWebView2MemoryUsageTargetLevel.Low;
                return wv.MemoryUsageTargetLevel == CoreWebView2MemoryUsageTargetLevel.Low;
            }
            catch
            {
                try
                {
                    PropertyInfo prop = wv.GetType().GetProperty("MemoryUsageTargetLevel", BindingFlags.Public | BindingFlags.Instance);
                    if (prop != null && prop.CanWrite)
                    {
                        object lowVal = Enum.ToObject(prop.PropertyType, 1);
                        prop.SetValue(wv, lowVal, null);
                        return true;
                    }
                }
                catch { }
                return false;
            }
        }

        private async Task InitializeWebViewAsync()
        {
            try
            {
                string udfPath = Path.Combine(_opts.PluginDataDir, "webview2_udf");
                Directory.CreateDirectory(udfPath);

                CoreWebView2EnvironmentOptions envOpts = new CoreWebView2EnvironmentOptions(LightweightBrowserArgs);
                TryApplySetting(delegate { envOpts.AreBrowserExtensionsEnabled = false; });
                TryApplySetting(delegate { envOpts.EnableTrackingPrevention = false; });

                _env = await CoreWebView2Environment.CreateAsync(null, udfPath, envOpts);
                if (IsDisposed || !IsHandleCreated || _closingFlag != 0) return;

                CoreWebView2ControllerOptions ctrlOpts = _env.CreateCoreWebView2ControllerOptions();
                TryApplySetting(delegate { ctrlOpts.DefaultBackgroundColor = Color.FromArgb(0, 0, 0, 0); });

                _controller = await _env.CreateCoreWebView2ControllerAsync(this.Handle, ctrlOpts);
                if (IsDisposed || !IsHandleCreated || _closingFlag != 0)
                {
                    SafeCloseController();
                    return;
                }

                TryApplySetting(delegate { _controller.DefaultBackgroundColor = Color.FromArgb(0, 0, 0, 0); });
                TryApplySetting(delegate { _controller.BoundsMode = CoreWebView2BoundsMode.UseRawPixels; });
                TryApplySetting(delegate { _controller.ShouldDetectMonitorScaleChanges = true; });
                TryApplySetting(delegate { _controller.AllowExternalDrop = false; });
                Rectangle initClient = this.ClientRectangle;
                if (initClient.Width > 0 && initClient.Height > 0)
                    _controller.Bounds = initClient;
                _controller.IsVisible = true;

                CoreWebView2 wv = _controller.CoreWebView2;
                _webView = wv;
                ApplyLowMemoryTarget(wv);

                CoreWebView2Settings settings = wv.Settings;
                if (settings != null)
                {
                    TryApplySetting(delegate { settings.IsStatusBarEnabled = false; });
                    TryApplySetting(delegate { settings.AreDefaultContextMenusEnabled = false; });
                    TryApplySetting(delegate { settings.AreDefaultScriptDialogsEnabled = false; });
                    TryApplySetting(delegate { settings.AreDevToolsEnabled = false; });
                    TryApplySetting(delegate { settings.AreHostObjectsAllowed = false; });
                    TryApplySetting(delegate { settings.IsZoomControlEnabled = false; });
                    TryApplySetting(delegate { settings.IsPinchZoomEnabled = false; });
                    TryApplySetting(delegate { settings.IsSwipeNavigationEnabled = false; });
                    TryApplySetting(delegate { settings.IsGeneralAutofillEnabled = false; });
                    TryApplySetting(delegate { settings.IsPasswordAutosaveEnabled = false; });
                    TryApplySetting(delegate { settings.IsReputationCheckingRequired = false; });
                    TryApplySetting(delegate { settings.AreBrowserAcceleratorKeysEnabled = false; });
                    TryApplySetting(delegate { settings.IsWebMessageEnabled = true; });
                }

                string initScript = string.Format(
                    CultureInfo.InvariantCulture,
                    "window.__FP_HUD_TOKEN__ = {0}; window.__FP_NATIVE_HOST__ = true; window.__FP_WORK_AREA__ = {1};",
                    _json.Serialize(_opts.HudToken ?? string.Empty),
                    BuildCurrentWorkAreaObjectJson());
                await wv.AddScriptToExecuteOnDocumentCreatedAsync(initScript);

                if (!Program.IsAllowedLoopbackHttpUrl(_opts.TargetUrl))
                {
                    LogHostEvent("rejected_target_url", "non_loopback_or_invalid");
                    RequestGracefulExit("rejected_target_url");
                    return;
                }

                wv.NavigationStarting += OnNavigationStarting;
                wv.WebMessageReceived += OnWebMessageReceived;
                wv.NavigationCompleted += OnNavigationCompleted;
                wv.ProcessFailed += OnWebViewProcessFailed;

                wv.Navigate(_opts.TargetUrl);
                LogHostEvent("webview_init_ok", string.Format(CultureInfo.InvariantCulture, "url={0}", _opts.TargetUrl));
            }
            catch (Exception ex)
            {
                LogHostEvent("webview_init_error", ex.GetType().Name + ": " + ex.Message);
                RequestGracefulExit("webview_init_error");
            }
        }

        private void OnNavigationStarting(object sender, CoreWebView2NavigationStartingEventArgs e)
        {
            if (e == null) return;
            string uri = e.Uri ?? string.Empty;
            if (string.Equals(uri, "about:blank", StringComparison.OrdinalIgnoreCase)) return;
            if (!Program.IsAllowedLoopbackHttpUrl(uri))
            {
                e.Cancel = true;
                LogHostEvent("blocked_navigation", "non_loopback_or_invalid");
            }
        }

        private void OnNavigationCompleted(object sender, CoreWebView2NavigationCompletedEventArgs e)
        {
            if (e != null && !e.IsSuccess)
            {
                string errStatus = Convert.ToString(e.WebErrorStatus, CultureInfo.InvariantCulture);
                if (_navRetryCount < MaxNavRetries && _closingFlag == 0 && !IsDisposed)
                {
                    _navRetryCount++;
                    LogHostEvent("navigation_failed", string.Format(
                        CultureInfo.InvariantCulture,
                        "status={0} retry={1}/{2}",
                        errStatus, _navRetryCount, MaxNavRetries));
                    ScheduleNavigationRetry();
                }
                else
                {
                    LogHostEvent("navigation_failed", string.Format(
                        CultureInfo.InvariantCulture,
                        "status={0} retries_exhausted={1}",
                        errStatus, _navRetryCount));
                }
                return;
            }

            _navRetryCount = 0;
            StopNavigationRetryTimer();
            EmitWorkAreaIfChanged(true);
        }

        private void ScheduleNavigationRetry()
        {
            if (IsDisposed || !IsHandleCreated || _closingFlag != 0) return;
            if (_navRetryTimer == null)
            {
                _navRetryTimer = new System.Windows.Forms.Timer { Interval = 800 };
                _navRetryTimer.Tick += delegate
                {
                    _navRetryTimer.Stop();
                    if (IsDisposed || !IsHandleCreated || _closingFlag != 0) return;
                    CoreWebView2 wv = _webView;
                    if (wv != null && !string.IsNullOrEmpty(_opts.TargetUrl))
                    {
                        try { wv.Navigate(_opts.TargetUrl); }
                        catch (Exception ex)
                        {
                            LogHostEvent("navigation_failed", "retry_exception=" + ex.GetType().Name + ": " + ex.Message);
                        }
                    }
                };
            }
            _navRetryTimer.Stop();
            _navRetryTimer.Start();
        }

        private void StopNavigationRetryTimer()
        {
            if (_navRetryTimer != null)
            {
                try { _navRetryTimer.Stop(); } catch { }
            }
        }

        private void OnWebViewProcessFailed(object sender, CoreWebView2ProcessFailedEventArgs e)
        {
            string kind = e != null ? Convert.ToString(e.ProcessFailedKind, CultureInfo.InvariantCulture) : "Unknown";
            LogHostEvent("webview_process_failed", "kind=" + kind);
            if (e != null && e.ProcessFailedKind == CoreWebView2ProcessFailedKind.BrowserProcessExited)
            {
                RequestGracefulExit("webview_browser_process_exited");
                return;
            }
            try
            {
                if (_webView != null) _webView.Reload();
            }
            catch (Exception ex)
            {
                LogHostEvent("webview_process_failed", "reload_error=" + ex.GetType().Name + ": " + ex.Message);
                RequestGracefulExit("webview_reload_failed");
            }
        }

        private void OnWebMessageReceived(object sender, CoreWebView2WebMessageReceivedEventArgs e)
        {
            string rawPayload = null;
            try { rawPayload = e.TryGetWebMessageAsString(); }
            catch (ArgumentException) { rawPayload = null; }
            catch { return; }

            if (string.IsNullOrWhiteSpace(rawPayload))
            {
                try { rawPayload = e.WebMessageAsJson; }
                catch { return; }
            }
            if (!string.IsNullOrWhiteSpace(rawPayload))
                HandleCommandString(rawPayload);
        }

        private void HandleCommandString(string raw)
        {
            if (IsDisposed || _closingFlag != 0) return;
            string trimmed = (raw ?? string.Empty).Trim();
            if (trimmed.Length == 0) return;

            Dictionary<string, object> payload = null;
            if (trimmed.StartsWith("{", StringComparison.Ordinal))
            {
                try { payload = _json.Deserialize<Dictionary<string, object>>(trimmed); }
                catch { payload = null; }
            }

            string cmd = payload != null ? GetStringField(payload, "cmd", "action", "type") : trimmed.Trim('"');
            if (string.IsNullOrEmpty(cmd)) return;

            string normalizedCmd = cmd.Trim().ToLowerInvariant();
            switch (normalizedCmd)
            {
                case "dragmove":
                case "drag":
                case "dragstart":
                case "startdrag":
                    this.BeginInvoke(new Action(PerformNativeDrag));
                    break;

                case "startresize":
                case "start_resize":
                case "resizestart":
                case "nativeresize":
                    if (payload != null)
                    {
                        string normEdge;
                        int htCode = MapResizeEdgeToHitTest(GetStringField(payload, "edge", "dir", "direction"), out normEdge);
                        if (htCode != 0 && (_viewMode != "pill" || normEdge == "w" || normEdge == "e"))
                            this.BeginInvoke(new Action(delegate { PerformNativeResize(htCode, normEdge); }));
                    }
                    break;

                case "resize":
                case "setsize":
                case "requestresize":
                    if (payload != null)
                    {
                        ApplyLogicalResize(
                            GetDoubleField(payload, _logicalWidth, "width", "w"),
                            GetDoubleField(payload, _logicalHeight, "height", "h"),
                            GetStringField(payload, "anchorCorner", "anchor"),
                            GetStringField(payload, "viewMode", "mode"),
                            GetBoolField(payload, true, "lockTop", "lock_top"),
                            GetBoolField(payload, _lastIdleOpen, "idleOpen", "idle_open"));
                    }
                    break;

                case "settopmost":
                case "alwaysontop":
                case "topmost":
                    if (payload != null)
                        _alwaysOnTop = GetBoolField(payload, _alwaysOnTop, "alwaysOnTop", "topMost", "pinned", "enabled", "value");
                    else
                        _alwaysOnTop = true;
                    ApplyTopMostNative(_alwaysOnTop);
                    break;

                case "flashlocate":
                case "flash":
                    LogHostEvent("cmd_received", "cmd=" + normalizedCmd);
                    PerformFlashLocate();
                    break;

                case "resetposition":
                case "reset_position":
                    LogHostEvent("cmd_received", "cmd=" + normalizedCmd);
                    PerformResetPosition();
                    break;

                case "resetautoheight":
                case "reset_auto_height":
                    PostJsonToWeb("{\"event\":\"resetAutoHeight\"}");
                    break;

                case "close":
                case "hide":
                case "hidehud":
                case "dock":
                case "shutdown":
                case "exit":
                    {
                        string cmdSrc = payload != null ? GetStringField(payload, "source") : null;
                        LogHostEvent("cmd_received", string.IsNullOrEmpty(cmdSrc)
                            ? ("cmd=" + normalizedCmd)
                            : ("cmd=" + normalizedCmd + " source=" + cmdSrc));
                        RequestGracefulExit("cmd:" + normalizedCmd);
                    }
                    break;
            }
        }

        internal static int MapResizeEdgeToHitTest(string edge, out string normalizedEdge)
        {
            normalizedEdge = string.Empty;
            if (string.IsNullOrEmpty(edge)) return 0;
            switch (edge.Trim().ToLowerInvariant())
            {
                case "w": case "left": normalizedEdge = "w"; return NativeMethods.HTLEFT;
                case "e": case "right": normalizedEdge = "e"; return NativeMethods.HTRIGHT;
                case "n": case "top": normalizedEdge = "n"; return NativeMethods.HTTOP;
                case "nw": case "top-left": case "topleft": normalizedEdge = "nw"; return NativeMethods.HTTOPLEFT;
                case "ne": case "top-right": case "topright": normalizedEdge = "ne"; return NativeMethods.HTTOPRIGHT;
                case "s": case "bottom": normalizedEdge = "s"; return NativeMethods.HTBOTTOM;
                case "sw": case "bottom-left": case "bottomleft": normalizedEdge = "sw"; return NativeMethods.HTBOTTOMLEFT;
                case "se": case "bottom-right": case "bottomright": normalizedEdge = "se"; return NativeMethods.HTBOTTOMRIGHT;
                default: return 0;
            }
        }

        private void PerformNativeDrag()
        {
            if (IsDisposed || !IsHandleCreated || _closingFlag != 0 || _inInteractiveMove || _inInteractiveResize) return;
            NativeMethods.RECT beforeRect;
            NativeMethods.GetWindowRect(this.Handle, out beforeRect);
            uint beforeDpi = NativeMethods.GetSafeDpiForWindow(this.Handle);

            _dragMonitors = NativeMethods.GetAllMonitors();
            _dpiChangedInDrag = false;
            _inInteractiveMove = true;
            try
            {
                NativeMethods.ReleaseCapture();
                NativeMethods.SendMessage(this.Handle, NativeMethods.WM_NCLBUTTONDOWN, new IntPtr(NativeMethods.HTCAPTION), IntPtr.Zero);
            }
            finally
            {
                _inInteractiveMove = false;
                _dragMonitors = null;
            }

            if (IsDisposed || !IsHandleCreated || _closingFlag != 0)
            {
                _dpiChangedInDrag = false;
                return;
            }
            FinalizeDragPlacement(beforeRect, beforeDpi, true);
        }

        private void FinalizeDragPlacement(NativeMethods.RECT beforeRect, uint beforeDpi, bool useCursorTarget)
        {
            bool sawDpiChangeInDrag = _dpiChangedInDrag;
            _dpiChangedInDrag = false;

            NativeMethods.RECT afterRect;
            if (!NativeMethods.GetWindowRect(this.Handle, out afterRect)) return;

            NativeMethods.MonitorDisplayInfo[] monitors = NativeMethods.GetAllMonitors();
            NativeMethods.POINT curPt = new NativeMethods.POINT();
            bool hasCur = useCursorTarget && NativeMethods.GetCursorPos(out curPt);
            int targetIdx = ResolveDropTargetMonitorIndex(afterRect, monitors, hasCur, curPt);
            NativeMethods.MonitorDisplayInfo targetMon = (targetIdx >= 0 && targetIdx < monitors.Length)
                ? monitors[targetIdx]
                : NativeMethods.GetCursorOrPrimaryMonitorInfo();

            uint liveDpiBefore = NativeMethods.GetSafeDpiForWindow(this.Handle);
            uint targetDpi = targetMon.Dpi >= 72 ? targetMon.Dpi : liveDpiBefore;
            NativeMethods.RECT work = targetMon.WorkRect;

            int physX, physY, physW, physH;
            bool dpiChangedDuringDrag;
            ComputeSettledDragRect(
                beforeRect, afterRect,
                beforeDpi, _currentDpi, liveDpiBefore, targetDpi, sawDpiChangeInDrag,
                _logicalWidth, _logicalHeight, work, hasCur, curPt,
                out physX, out physY, out physW, out physH, out dpiChangedDuringDrag);

            if (physX != afterRect.Left || physY != afterRect.Top || physW != afterRect.Width || physH != afterRect.Height)
            {
                _suppressDpiSuggestedRect = true;
                _dpiTransitionTargetDpi = targetDpi;
                _dpiTransitionTargetWork = work;
                _hasDpiTransitionWork = true;
                try
                {
                    ApplyRegionClipIfNeeded(physW, physH);
                    NativeMethods.SetWindowPos(
                        this.Handle, IntPtr.Zero, physX, physY, physW, physH,
                        NativeMethods.SWP_NOZORDER | NativeMethods.SWP_NOACTIVATE | NativeMethods.SWP_NOOWNERZORDER);
                }
                finally
                {
                    _hasDpiTransitionWork = false;
                    _dpiTransitionTargetDpi = 0;
                    _suppressDpiSuggestedRect = false;
                }
                SyncControllerBounds(physW, physH);
            }
            else if (dpiChangedDuringDrag)
            {
                ApplyRegionClipIfNeeded(physW, physH);
                SyncControllerBounds(physW, physH);
            }

            if (Math.Abs(physX - beforeRect.Left) >= 1
                || Math.Abs(physY - beforeRect.Top) >= 1
                || physW != beforeRect.Width
                || physH != beforeRect.Height
                || dpiChangedDuringDrag)
            {
                _preDrawerNudgeTopY = null;
                uint liveDpi = NativeMethods.GetSafeDpiForWindow(this.Handle);
                _currentDpi = (targetDpi >= 72) ? targetDpi : liveDpi;
                _anchorCorner = InferNearestAnchor(physX, physY, physW, physH, work, _currentDpi);
                EmitMovedEvent(physX, physY, _currentDpi, work, _anchorCorner);
            }
            else
            {
                EmitWorkAreaIfChanged(false);
            }
        }

        internal static void ComputeSettledDragRect(
            NativeMethods.RECT beforeRect,
            NativeMethods.RECT afterRect,
            uint beforeDpi,
            uint currentDpi,
            uint liveDpiBefore,
            uint targetDpi,
            bool sawDpiChangeInDrag,
            int logicalWidth,
            int logicalHeight,
            NativeMethods.RECT targetWork,
            bool hasCursor,
            NativeMethods.POINT cursorPt,
            out int outX,
            out int outY,
            out int outW,
            out int outH,
            out bool didReconcileDpi)
        {
            int physX = afterRect.Left, physY = afterRect.Top, physW = afterRect.Width, physH = afterRect.Height;
            uint safeTargetDpi = targetDpi >= 72 ? targetDpi : 96u;
            double targetScale = safeTargetDpi / 96.0;
            didReconcileDpi = sawDpiChangeInDrag
                || (safeTargetDpi != beforeDpi)
                || (safeTargetDpi != currentDpi)
                || (safeTargetDpi != liveDpiBefore)
                || (afterRect.Width != beforeRect.Width)
                || (afterRect.Height != beforeRect.Height);

            if (didReconcileDpi)
            {
                int expectedW = Math.Max(64, (int)Math.Ceiling(logicalWidth * targetScale - 1e-6));
                int expectedH = Math.Max(24, (int)Math.Ceiling(logicalHeight * targetScale - 1e-6));
                if (physW != expectedW)
                {
                    if (hasCursor && cursorPt.X >= physX && cursorPt.X <= physX + physW && physW > 0)
                    {
                        double ratio = (cursorPt.X - physX) / (double)physW;
                        physX = cursorPt.X - (int)Math.Round(ratio * expectedW);
                    }
                    else
                    {
                        physX += (physW - expectedW) / 2;
                    }
                    physW = expectedW;
                }
                physH = expectedH;
            }

            ClampRectToWorkArea(ref physX, ref physY, ref physW, ref physH, targetWork);
            outX = physX;
            outY = physY;
            outW = physW;
            outH = physH;
        }

        private void PerformNativeResize(int htCode, string edge)
        {
            if (IsDisposed || !IsHandleCreated || _closingFlag != 0 || _inInteractiveResize || _inInteractiveMove) return;
            NativeMethods.RECT beforeRect;
            if (!NativeMethods.GetWindowRect(this.Handle, out beforeRect)) return;

            _preDrawerNudgeTopY = null;
            _inInteractiveResize = true;
            try
            {
                NativeMethods.ReleaseCapture();
                NativeMethods.SendMessage(this.Handle, NativeMethods.WM_NCLBUTTONDOWN, new IntPtr(htCode), IntPtr.Zero);
            }
            finally { _inInteractiveResize = false; }

            if (!IsDisposed && IsHandleCreated && _closingFlag == 0)
                FinalizeUserResize(beforeRect, edge);
        }

        private void FinalizeUserResize(NativeMethods.RECT beforeRect, string edge)
        {
            NativeMethods.RECT afterRect;
            if (!NativeMethods.GetWindowRect(this.Handle, out afterRect)) return;

            if (Math.Abs(afterRect.Width - beforeRect.Width) < 3 && Math.Abs(afterRect.Height - beforeRect.Height) < 3)
            {
                PostJsonToWeb("{\"event\":\"resizeCancelled\"}");
                return;
            }

            _currentDpi = NativeMethods.GetSafeDpiForWindow(this.Handle);
            double scale = (_currentDpi >= 72 ? _currentDpi : 96u) / 96.0;
            int minLogW = (_viewMode == "pill") ? MinPillLogicalWidth : MinStackLogicalWidth;
            int minLogH = (_viewMode == "pill") ? MinPillLogicalHeight : MinStackLogicalHeight;
            int maxLogW = (_viewMode == "pill") ? 960 : 2560;
            int minPhysW = Math.Max(64, (int)Math.Round(minLogW * scale));
            int minPhysH = Math.Max(24, (int)Math.Round(minLogH * scale));

            int logicalW = Math.Max(minLogW, Math.Min(maxLogW, (int)Math.Round(afterRect.Width / scale)));
            int logicalH = (_viewMode == "pill")
                ? DefaultPillLogicalHeight
                : Math.Max(minLogH, (int)Math.Round(afterRect.Height / scale));

            int physW = Math.Max(minPhysW, (int)Math.Ceiling(logicalW * scale - 1e-6));
            int physH = Math.Max(minPhysH, (int)Math.Ceiling(logicalH * scale - 1e-6));
            int physX = afterRect.Left, physY = afterRect.Top;

            string eNorm = (edge ?? string.Empty).ToLowerInvariant();
            if (eNorm.IndexOf('w') >= 0) physX = afterRect.Right - physW;
            if (eNorm.IndexOf('n') >= 0) physY = afterRect.Bottom - physH;

            NativeMethods.RECT work = NativeMethods.GetWorkAreaForWindow(this.Handle);
            ClampRectToWorkArea(ref physX, ref physY, ref physW, ref physH, work);

            if (physX != afterRect.Left || physY != afterRect.Top || physW != afterRect.Width || physH != afterRect.Height)
            {
                _suppressDpiSuggestedRect = true;
                _dpiTransitionTargetDpi = _currentDpi;
                _dpiTransitionTargetWork = work;
                _hasDpiTransitionWork = true;
                try
                {
                    ApplyRegionClipIfNeeded(physW, physH);
                    NativeMethods.SetWindowPos(
                        this.Handle, IntPtr.Zero, physX, physY, physW, physH,
                        NativeMethods.SWP_NOZORDER | NativeMethods.SWP_NOACTIVATE | NativeMethods.SWP_NOOWNERZORDER);
                }
                finally
                {
                    _hasDpiTransitionWork = false;
                    _dpiTransitionTargetDpi = 0;
                    _suppressDpiSuggestedRect = false;
                }
            }
            else if (_useRegionFallback)
            {
                ApplyRegionClipIfNeeded(physW, physH);
            }

            SyncControllerBoundsRect(this.ClientRectangle);

            int logicalX = (int)Math.Round(physX / scale);
            int logicalY = (int)Math.Round(physY / scale);
            logicalW = Math.Max(minLogW, Math.Min(maxLogW, (int)Math.Round(physW / scale)));
            logicalH = (_viewMode == "pill")
                ? DefaultPillLogicalHeight
                : Math.Max(minLogH, (int)Math.Round(physH / scale));

            _logicalWidth = logicalW;
            _logicalHeight = logicalH;

            _anchorCorner = InferNearestAnchor(physX, physY, physW, physH, work, _currentDpi);
            _postUserResizeGuardUntilUtc = DateTime.UtcNow.AddMilliseconds(150);

            EmitUserResizedEvent(
                logicalX, logicalY, logicalW, logicalH,
                _anchorCorner, edge ?? string.Empty,
                Math.Abs(physW - beforeRect.Width) >= 1,
                Math.Abs(physH - beforeRect.Height) >= 1);
        }

        private void ApplyLogicalResize(
            double cssWidth, double cssHeight, string anchorOverride, string modeOverride, bool lockTop, bool idleOpen)
        {
            if (IsDisposed || !IsHandleCreated || _closingFlag != 0) return;
            if (_inInteractiveResize || _inInteractiveMove || _inExternalSizeMove) return;
            if (DateTime.UtcNow < _postUserResizeGuardUntilUtc && Math.Abs(cssWidth - _logicalWidth) >= 4.0) return;

            string prevMode = _viewMode;
            _viewMode = !string.IsNullOrEmpty(modeOverride) ? NormalizeViewMode(modeOverride) : ((cssHeight <= 44.0) ? "pill" : "stack");
            if (prevMode != _viewMode)
            {
                _preDrawerNudgeTopY = null;
                if (_viewMode == "pill") idleOpen = false;
            }
            _lastIdleOpen = idleOpen;

            double minCssW = (_viewMode == "pill") ? MinPillLogicalWidth : MinStackLogicalWidth;
            double maxCssW = (_viewMode == "pill") ? 960.0 : 2560.0;
            double minCssH = (_viewMode == "pill") ? MinPillLogicalHeight : MinStackLogicalHeight;
            double maxCssH = (_viewMode == "pill") ? 120.0 : 8192.0;

            double clampedCssW = Math.Max(minCssW, Math.Min(maxCssW, cssWidth));
            double clampedCssH = Math.Max(minCssH, Math.Min(maxCssH, cssHeight));
            _logicalWidth = (int)Math.Ceiling(clampedCssW - 1e-6);
            _logicalHeight = (int)Math.Ceiling(clampedCssH - 1e-6);

            _anchorCorner = NormalizeAnchor(!string.IsNullOrEmpty(anchorOverride) ? anchorOverride : _anchorCorner);
            _currentDpi = NativeMethods.GetSafeDpiForWindow(this.Handle);
            double scale = (_currentDpi >= 72 ? _currentDpi : 96u) / 96.0;
            int newW = Math.Max(64, (int)Math.Ceiling(clampedCssW * scale - 1e-6));
            int newH = Math.Max(24, (int)Math.Ceiling(clampedCssH * scale - 1e-6));

            NativeMethods.RECT oldRect = GetCurrentWindowRectSafe();
            NativeMethods.RECT work = NativeMethods.GetWorkAreaForWindow(this.Handle);
            if (work.Width > 0 && newW > work.Width) newW = work.Width;

            int newX = (_anchorCorner == "top-right") ? (oldRect.Right - newW) : oldRect.Left;
            if (work.Width > 0)
            {
                if (newX + newW > work.Right) newX = work.Right - newW;
                if (newX < work.Left) newX = work.Left;
            }

            int minPhysH = Math.Max(24, (int)Math.Round((_viewMode == "pill" ? MinPillLogicalHeight : MinStackLogicalHeight) * scale));
            int newY;
            ComputeLockedTopVerticalBounds(oldRect, work, scale, lockTop, idleOpen, minPhysH, ref _preDrawerNudgeTopY, out newY, ref newH);

            if (newX == oldRect.Left && newY == oldRect.Top && newW == oldRect.Width && newH == oldRect.Height)
            {
                SyncControllerBounds(newW, newH);
                EmitWorkAreaIfChanged(false);
                return;
            }

            _suppressDpiSuggestedRect = true;
            _dpiTransitionTargetDpi = _currentDpi;
            _dpiTransitionTargetWork = work;
            _hasDpiTransitionWork = true;
            try
            {
                ApplyRegionClipIfNeeded(newW, newH);
                NativeMethods.SetWindowPos(
                    this.Handle, IntPtr.Zero, newX, newY, newW, newH,
                    NativeMethods.SWP_NOZORDER | NativeMethods.SWP_NOACTIVATE | NativeMethods.SWP_NOOWNERZORDER);
            }
            finally
            {
                _hasDpiTransitionWork = false;
                _dpiTransitionTargetDpi = 0;
                _suppressDpiSuggestedRect = false;
            }
            SyncControllerBounds(newW, newH);
            EmitWorkAreaIfChanged(false);
        }

        internal static int ComputeAvailBelowCssPx(int currentTopPhys, NativeMethods.RECT work, double scale)
        {
            if (scale <= 0.1) scale = 1.0;
            if (work.Height <= 0) return 1000;
            int clampedTop = Math.Max(work.Top, currentTopPhys);
            return Math.Max(MinStackLogicalHeight, (int)Math.Floor((work.Bottom - clampedTop) / scale) - 6);
        }

        internal static void ComputeLockedTopVerticalBounds(
            NativeMethods.RECT oldRect, NativeMethods.RECT work, double scale, bool lockTop, bool idleOpen,
            int minPhysH, ref int? preDrawerNudgeTopY, out int outY, ref int ioH)
        {
            int safeBottom = (work.Height > BottomWorkMarginPhys * 4) ? (work.Bottom - BottomWorkMarginPhys) : work.Bottom;
            int maxWorkH = Math.Max(minPhysH, safeBottom - work.Top);
            if (work.Height > 0 && ioH > maxWorkH) ioH = maxWorkH;

            int newY = oldRect.Top;
            if (!idleOpen && preDrawerNudgeTopY.HasValue)
            {
                newY = preDrawerNudgeTopY.Value;
                preDrawerNudgeTopY = null;
            }
            if (work.Height <= 0) { outY = newY; return; }
            if (newY < work.Top) newY = work.Top;

            if (!lockTop)
            {
                if (newY + ioH > safeBottom) newY = Math.Max(work.Top, safeBottom - ioH);
                outY = newY;
                return;
            }

            int nudgeLimit = Math.Max(24, (int)Math.Round(72.0 * scale));
            int oldBottomGap = safeBottom - oldRect.Bottom;

            if (newY + ioH > safeBottom)
            {
                if (idleOpen && (oldBottomGap < nudgeLimit || preDrawerNudgeTopY.HasValue))
                {
                    if (!preDrawerNudgeTopY.HasValue) preDrawerNudgeTopY = oldRect.Top;
                    int baseTop = preDrawerNudgeTopY.Value;
                    int nudgeUp = Math.Max(0, Math.Min(nudgeLimit, (baseTop + ioH) - safeBottom));
                    newY = Math.Max(work.Top, baseTop - nudgeUp);
                    int availAfterNudge = safeBottom - newY;
                    if (ioH > availAfterNudge) ioH = Math.Max(minPhysH, availAfterNudge);
                    if (newY + ioH > safeBottom) newY = Math.Max(work.Top, safeBottom - ioH);
                }
                else
                {
                    int availDown = safeBottom - newY;
                    if (availDown >= minPhysH) ioH = availDown;
                    else newY = Math.Max(work.Top, safeBottom - ioH);
                }
            }
            else if (idleOpen && preDrawerNudgeTopY.HasValue)
            {
                int baseTop = preDrawerNudgeTopY.Value;
                if (baseTop + ioH <= safeBottom)
                {
                    newY = baseTop;
                    preDrawerNudgeTopY = null;
                }
            }
            outY = newY;
        }

        private void PerformFlashLocate()
        {
            if (IsDisposed || !IsHandleCreated || _closingFlag != 0) return;
            NativeMethods.RECT curRect;
            if (NativeMethods.GetWindowRect(this.Handle, out curRect))
            {
                int x = curRect.Left, y = curRect.Top, w = curRect.Width, h = curRect.Height;
                ClampRectToWorkArea(ref x, ref y, ref w, ref h, NativeMethods.GetWorkAreaForWindow(this.Handle));
                NativeMethods.SetWindowPos(this.Handle, NativeMethods.HWND_TOPMOST, x, y, w, h, NativeMethods.SWP_SHOWWINDOW | NativeMethods.SWP_NOACTIVATE);
            }
            else
            {
                ApplyTopMostNative(true);
            }

            if (!_alwaysOnTop)
            {
                if (_tempTopMostTimer == null)
                {
                    _tempTopMostTimer = new System.Windows.Forms.Timer { Interval = 1800 };
                    _tempTopMostTimer.Tick += delegate
                    {
                        _tempTopMostTimer.Stop();
                        if (!IsDisposed && IsHandleCreated && !_alwaysOnTop) ApplyTopMostNative(false);
                    };
                }
                _tempTopMostTimer.Stop();
                _tempTopMostTimer.Start();
            }
            PostJsonToWeb("{\"event\":\"flash\"}");
        }

        private void PerformResetPosition()
        {
            if (IsDisposed || !IsHandleCreated || _closingFlag != 0) return;
            _preDrawerNudgeTopY = null;

            uint targetDpi;
            NativeMethods.RECT targetWork = NativeMethods.GetCursorOrPrimaryWorkArea(out targetDpi);
            _currentDpi = targetDpi;
            double scale = targetDpi / 96.0;

            int margin = (int)Math.Round(16 * scale);
            int physW = Math.Max(64, (int)Math.Ceiling(_logicalWidth * scale - 1e-6));
            int physH = Math.Max(24, (int)Math.Ceiling(_logicalHeight * scale - 1e-6));
            int physX = targetWork.Right - physW - margin;
            int physY = targetWork.Top + margin;
            ClampRectToWorkArea(ref physX, ref physY, ref physW, ref physH, targetWork);

            _anchorCorner = "top-right";
            ApplyRegionClipIfNeeded(physW, physH);
            _suppressDpiSuggestedRect = true;
            _dpiTransitionTargetDpi = targetDpi;
            _dpiTransitionTargetWork = targetWork;
            _hasDpiTransitionWork = true;
            try
            {
                NativeMethods.SetWindowPos(
                    this.Handle, _alwaysOnTop ? NativeMethods.HWND_TOPMOST : IntPtr.Zero,
                    physX, physY, physW, physH,
                    (_alwaysOnTop ? 0u : NativeMethods.SWP_NOZORDER) | NativeMethods.SWP_NOACTIVATE | NativeMethods.SWP_SHOWWINDOW);
            }
            finally
            {
                _hasDpiTransitionWork = false;
                _dpiTransitionTargetDpi = 0;
                _suppressDpiSuggestedRect = false;
            }

            SyncControllerBounds(physW, physH);
            EmitMovedEvent(physX, physY, targetDpi, targetWork, _anchorCorner);
        }

        private NativeMethods.RECT GetCurrentWindowRectSafe()
        {
            NativeMethods.RECT r;
            if (IsHandleCreated && NativeMethods.GetWindowRect(this.Handle, out r)) return r;
            return new NativeMethods.RECT { Left = this.Left, Top = this.Top, Right = this.Right, Bottom = this.Bottom };
        }

        private NativeMethods.RECT GetSafeWindowWorkArea()
        {
            if (IsHandleCreated && this.Handle != IntPtr.Zero) return NativeMethods.GetWorkAreaForWindow(this.Handle);
            uint unused;
            return NativeMethods.GetPrimaryWorkArea(out unused);
        }

        private Dictionary<string, object> BuildWorkAreaDict(int physX, int physY, uint dpi, NativeMethods.RECT work)
        {
            uint safeDpi = dpi >= 72 ? dpi : 96u;
            double scale = safeDpi / 96.0;
            int workLeft = (int)Math.Round(work.Left / scale);
            int workTop = (int)Math.Round(work.Top / scale);
            int workRight = (int)Math.Floor(work.Right / scale);
            int workBottom = (int)Math.Floor(work.Bottom / scale);

            Dictionary<string, object> d = new Dictionary<string, object>();
            d["workLeft"] = workLeft;
            d["workTop"] = workTop;
            d["workRight"] = workRight;
            d["workBottom"] = workBottom;
            d["workWidth"] = Math.Max(320, workRight - workLeft);
            d["workHeight"] = Math.Max(200, workBottom - workTop);
            d["winX"] = (int)Math.Round(physX / scale);
            d["winY"] = (int)Math.Round(physY / scale);
            d["physX"] = physX;
            d["physY"] = physY;
            d["physTop"] = physY;
            d["physWorkBottom"] = work.Bottom;
            d["dpi"] = (int)safeDpi;
            d["scale"] = Math.Round(scale, 4);
            d["availBelowCssPx"] = ComputeAvailBelowCssPx(physY, work, scale);
            return d;
        }

        private string FormatWorkAreaSig(NativeMethods.RECT work, int physTop, uint dpi)
        {
            return string.Format(CultureInfo.InvariantCulture, "{0},{1},{2},{3},{4},{5}",
                work.Left, work.Top, work.Right, work.Bottom, physTop, dpi);
        }

        private string BuildCurrentWorkAreaObjectJson()
        {
            NativeMethods.RECT curRect = GetCurrentWindowRectSafe();
            uint dpi = IsHandleCreated ? NativeMethods.GetSafeDpiForWindow(this.Handle) : (_currentDpi >= 72 ? _currentDpi : NativeMethods.GetSafeSystemDpi());
            _currentDpi = dpi;
            return _json.Serialize(BuildWorkAreaDict(curRect.Left, curRect.Top, dpi, GetSafeWindowWorkArea()));
        }

        private void EmitWorkAreaIfChanged(bool force)
        {
            if (IsDisposed || !IsHandleCreated || _closingFlag != 0) return;
            NativeMethods.RECT curRect = GetCurrentWindowRectSafe();
            _currentDpi = NativeMethods.GetSafeDpiForWindow(this.Handle);
            NativeMethods.RECT work = NativeMethods.GetWorkAreaForWindow(this.Handle);
            string sig = FormatWorkAreaSig(work, curRect.Top, _currentDpi);
            if (!force && sig == _lastEmittedWorkAreaSig) return;
            _lastEmittedWorkAreaSig = sig;

            Dictionary<string, object> dict = BuildWorkAreaDict(curRect.Left, curRect.Top, _currentDpi, work);
            dict["event"] = "workArea";
            PostJsonToWeb(_json.Serialize(dict));
        }

        private void EmitMovedEvent(int physX, int physY, uint dpi, string anchor)
        {
            EmitMovedEvent(physX, physY, dpi, GetSafeWindowWorkArea(), anchor);
        }

        private void EmitMovedEvent(int physX, int physY, uint dpi, NativeMethods.RECT work, string anchor)
        {
            uint safeDpi = dpi >= 72 ? dpi : 96u;
            _lastEmittedWorkAreaSig = FormatWorkAreaSig(work, physY, safeDpi);

            Dictionary<string, object> dict = BuildWorkAreaDict(physX, physY, safeDpi, work);
            dict["event"] = "moved";
            dict["x"] = dict["winX"];
            dict["y"] = dict["winY"];
            dict["physX"] = physX;
            dict["physY"] = physY;
            dict["anchorCorner"] = anchor ?? "top-right";
            PostJsonToWeb(_json.Serialize(dict));
        }

        private void EmitUserResizedEvent(
            int logicalX, int logicalY, int logicalW, int logicalH, string anchor, string edge, bool widthChanged, bool heightChanged)
        {
            NativeMethods.RECT curRect = GetCurrentWindowRectSafe();
            uint dpi = _currentDpi >= 72 ? _currentDpi : 96u;
            NativeMethods.RECT work = GetSafeWindowWorkArea();
            _lastEmittedWorkAreaSig = FormatWorkAreaSig(work, curRect.Top, dpi);

            Dictionary<string, object> dict = BuildWorkAreaDict(curRect.Left, curRect.Top, dpi, work);
            dict["event"] = "userResized";
            dict["x"] = logicalX;
            dict["y"] = logicalY;
            dict["width"] = logicalW;
            dict["height"] = logicalH;
            dict["anchorCorner"] = anchor ?? "top-right";
            dict["edge"] = edge ?? string.Empty;
            dict["widthChanged"] = widthChanged;
            dict["heightChanged"] = heightChanged;
            PostJsonToWeb(_json.Serialize(dict));
        }

        private void PostJsonToWeb(string jsonText)
        {
            try
            {
                CoreWebView2 wv = _webView;
                if (wv != null) wv.PostWebMessageAsJson(jsonText);
            }
            catch { }
        }

        private void ApplyTopMostNative(bool topMost)
        {
            if (!IsHandleCreated || IsDisposed) return;
            NativeMethods.SetWindowPos(
                this.Handle, topMost ? NativeMethods.HWND_TOPMOST : NativeMethods.HWND_NOTOPMOST,
                0, 0, 0, 0,
                NativeMethods.SWP_NOMOVE | NativeMethods.SWP_NOSIZE | NativeMethods.SWP_NOACTIVATE | NativeMethods.SWP_NOOWNERZORDER);
        }

        private void ApplyRegionClipIfNeeded(int physW, int physH)
        {
            if (!_useRegionFallback || !IsHandleCreated || IsDisposed) return;
            double scale = (_currentDpi >= 72 ? _currentDpi : 96u) / 96.0;
            int cssRadius = (_viewMode == "pill" || physH <= (int)Math.Round(44 * scale)) ? 18 : 14;
            int diameter = Math.Max(4, (int)Math.Round(cssRadius * 2.0 * scale));
            if (physW == _lastRgnW && physH == _lastRgnH && diameter == _lastRgnDiameter) return;

            IntPtr hRgn = NativeMethods.CreateRoundRectRgn(0, 0, physW + 1, physH + 1, diameter, diameter);
            if (hRgn != IntPtr.Zero)
            {
                if (NativeMethods.SetWindowRgn(this.Handle, hRgn, true) == 0)
                {
                    NativeMethods.DeleteObject(hRgn);
                }
                else
                {
                    _lastRgnW = physW;
                    _lastRgnH = physH;
                    _lastRgnDiameter = diameter;
                }
            }
        }

        private void SyncControllerBounds(int physW, int physH)
        {
            SyncControllerBoundsRect(new Rectangle(0, 0, Math.Max(1, physW), Math.Max(1, physH)));
        }

        private void SyncControllerBoundsRect(Rectangle clientRect)
        {
            CoreWebView2Controller ctrl = _controller;
            if (ctrl == null) return;
            try
            {
                Rectangle target = new Rectangle(clientRect.X, clientRect.Y, Math.Max(1, clientRect.Width), Math.Max(1, clientRect.Height));
                if (ctrl.Bounds != target) ctrl.Bounds = target;
                ctrl.NotifyParentWindowPositionChanged();
            }
            catch { }
        }

        private void ApplyMinMaxInfo(ref Message m)
        {
            if (m.LParam == IntPtr.Zero) return;
            uint dpi;
            if (_dpiTransitionTargetDpi >= 72 && _dpiTransitionTargetDpi <= 576)
            {
                dpi = _dpiTransitionTargetDpi;
            }
            else if (!IsHandleCreated && _initialDpi >= 72 && _initialDpi <= 576)
            {
                dpi = _initialDpi;
            }
            else if (IsHandleCreated && this.Handle != IntPtr.Zero)
            {
                dpi = NativeMethods.GetSafeDpiForWindow(this.Handle);
                _currentDpi = dpi;
            }
            else
            {
                dpi = (_currentDpi >= 72 ? _currentDpi : NativeMethods.GetSafeSystemDpi());
            }
            double scale = dpi / 96.0;
            NativeMethods.RECT work;
            if (_hasDpiTransitionWork && _dpiTransitionTargetWork.Width > 0 && _dpiTransitionTargetWork.Height > 0)
            {
                work = _dpiTransitionTargetWork;
            }
            else if (!IsHandleCreated && _hasInitialPhysicalBounds && _initialWorkArea.Width > 0 && _initialWorkArea.Height > 0)
            {
                work = _initialWorkArea;
            }
            else
            {
                work = GetSafeWindowWorkArea();
            }

            int maxWorkW = work.Width > 0 ? work.Width : (int)Math.Round(3840 * scale);
            int maxWorkH = work.Height > BottomWorkMarginPhys * 4
                ? (work.Height - BottomWorkMarginPhys)
                : (work.Height > 0 ? work.Height : (int)Math.Round(2160 * scale));

            if ((_inInteractiveMove || _inExternalSizeMove) && _dragMonitors != null && _dragMonitors.Length > 1)
            {
                for (int i = 0; i < _dragMonitors.Length; i++)
                {
                    NativeMethods.RECT mw = _dragMonitors[i].WorkRect;
                    if (mw.Width > maxWorkW) maxWorkW = mw.Width;
                    int mh = (mw.Height > BottomWorkMarginPhys * 4) ? (mw.Height - BottomWorkMarginPhys) : mw.Height;
                    if (mh > maxWorkH) maxWorkH = mh;
                }
            }

            int minPhysW = Math.Max(64, (int)Math.Round(((_viewMode == "pill") ? MinPillLogicalWidth : MinStackLogicalWidth) * scale));
            int minPhysH = (_viewMode == "pill")
                ? Math.Max(24, (int)Math.Round(MinPillLogicalHeight * scale))
                : Math.Max(48, (int)Math.Round(MinStackLogicalHeight * scale));
            int maxPhysW = maxWorkW;
            if (_viewMode == "pill")
            {
                int pillMaxPhysW = Math.Max(minPhysW, (int)Math.Round(960 * scale));
                if (maxPhysW > pillMaxPhysW) maxPhysW = pillMaxPhysW;
            }
            int maxPhysH = maxWorkH;

            if (minPhysW > maxPhysW) minPhysW = maxPhysW;
            if (minPhysH > maxPhysH) minPhysH = maxPhysH;

            NativeMethods.MINMAXINFO mmi = (NativeMethods.MINMAXINFO)Marshal.PtrToStructure(m.LParam, typeof(NativeMethods.MINMAXINFO));
            mmi.ptMinTrackSize.X = minPhysW;
            mmi.ptMinTrackSize.Y = minPhysH;
            mmi.ptMaxTrackSize.X = maxPhysW;
            mmi.ptMaxTrackSize.Y = maxPhysH;
            mmi.ptMaxSize.X = maxPhysW;
            mmi.ptMaxSize.Y = maxPhysH;
            Marshal.StructureToPtr(mmi, m.LParam, false);
            m.Result = IntPtr.Zero;
        }

        internal static bool ShouldBlockSysCommand(int scCommand, bool authorizedExit)
        {
            int masked = scCommand & 0xFFF0;
            if (masked == NativeMethods.SC_MINIMIZE || masked == NativeMethods.SC_MAXIMIZE)
                return true;
            if (masked == NativeMethods.SC_CLOSE && !authorizedExit)
                return true;
            return false;
        }

        internal static bool ShouldCancelFormClosing(bool authorizedExit, CloseReason reason)
        {
            if (authorizedExit) return false;
            if (reason == CloseReason.WindowsShutDown || reason == CloseReason.TaskManagerClosing)
                return false;
            return true;
        }

        internal static bool IsValidLogicalCoord(int v)
        {
            return v > -10000 && v < 30000;
        }

        internal static bool IsValidPhysicalCoord(int v)
        {
            return v > -16000 && v < 32768;
        }

        internal static bool IsRectInvalidOrMinimized(int x, int y, int w, int h)
        {
            return x <= -16000 || y <= -16000 || w < 64 || h < 24;
        }

        internal static void ComputeRecoveredPhysicalRect(
            NativeMethods.RECT currentRect,
            bool hasCurrentRect,
            NativeMethods.RECT windowWorkArea,
            NativeMethods.RECT primaryWorkArea,
            uint primaryDpi,
            int logicalWidth,
            int logicalHeight,
            out int outX,
            out int outY,
            out int outW,
            out int outH,
            out bool usedFallback)
        {
            usedFallback = false;
            int x = currentRect.Left;
            int y = currentRect.Top;
            int w = currentRect.Width;
            int h = currentRect.Height;

            if (!hasCurrentRect || IsRectInvalidOrMinimized(x, y, w, h))
            {
                usedFallback = true;
                uint safeDpi = primaryDpi >= 72 ? primaryDpi : 96u;
                double scale = safeDpi / 96.0;
                int margin = (int)Math.Round(16 * scale);
                int safeLogW = Math.Max(MinPillLogicalWidth, Math.Min(960, logicalWidth > 0 ? logicalWidth : DefaultStackLogicalWidth));
                int safeLogH = Math.Max(MinPillLogicalHeight, logicalHeight > 0 ? logicalHeight : DefaultPillLogicalHeight);
                w = Math.Max(64, (int)Math.Ceiling(safeLogW * scale - 1e-6));
                h = Math.Max(24, (int)Math.Ceiling(safeLogH * scale - 1e-6));
                x = primaryWorkArea.Right - w - margin;
                y = primaryWorkArea.Top + margin;
                ClampRectToWorkArea(ref x, ref y, ref w, ref h, primaryWorkArea);
            }
            else
            {
                NativeMethods.RECT targetWork = (windowWorkArea.Width > 0 && windowWorkArea.Height > 0)
                    ? windowWorkArea
                    : primaryWorkArea;
                ClampRectToWorkArea(ref x, ref y, ref w, ref h, targetWork);
            }

            outX = x;
            outY = y;
            outW = w;
            outH = h;
        }

        private void StartVisibilityGuardTimer()
        {
            if (_visibilityGuardTimer != null) return;
            _visibilityGuardTimer = new System.Windows.Forms.Timer { Interval = 8000 };
            _visibilityGuardTimer.Tick += delegate
            {
                if (IsDisposed || !IsHandleCreated || _closingFlag != 0) return;
                if (_inInteractiveMove || _inInteractiveResize || _inExternalSizeMove) return;
                if (this.WindowState == FormWindowState.Minimized || !this.Visible || !NativeMethods.IsWindowVisible(this.Handle))
                {
                    EnsureVisibleInWorkAreaAndTopMost("guard_restore");
                    return;
                }
                NativeMethods.RECT curRect;
                if (NativeMethods.GetWindowRect(this.Handle, out curRect)
                    && IsRectInvalidOrMinimized(curRect.Left, curRect.Top, curRect.Width, curRect.Height))
                {
                    EnsureVisibleInWorkAreaAndTopMost("guard_restore");
                    return;
                }
                if (_alwaysOnTop)
                {
                    ApplyTopMostNative(true);
                }
            };
            _visibilityGuardTimer.Start();
        }

        private void EnsureVisibleInWorkAreaAndTopMost(string reason)
        {
            if (IsDisposed || !IsHandleCreated || _closingFlag != 0) return;
            try
            {
                if (this.WindowState == FormWindowState.Minimized)
                    this.WindowState = FormWindowState.Normal;
                if (!this.Visible)
                {
                    try { this.Show(); } catch { }
                }

                NativeMethods.RECT curRect;
                bool hasRect = NativeMethods.GetWindowRect(this.Handle, out curRect);
                uint primaryDpi;
                NativeMethods.RECT primaryWork = NativeMethods.GetPrimaryWorkArea(out primaryDpi);
                IntPtr hWinMon = NativeMethods.MonitorFromWindow(this.Handle, NativeMethods.MONITOR_DEFAULTTONEAREST);
                NativeMethods.MonitorDisplayInfo winMonInfo;
                bool hasWinMon = NativeMethods.TryGetMonitorDisplayInfo(hWinMon, out winMonInfo);
                NativeMethods.RECT winWork = hasWinMon ? winMonInfo.WorkRect : NativeMethods.GetWorkAreaForWindow(this.Handle);

                int newX, newY, newW, newH;
                bool usedFallback;
                ComputeRecoveredPhysicalRect(
                    curRect, hasRect, winWork, primaryWork, primaryDpi,
                    _logicalWidth, _logicalHeight,
                    out newX, out newY, out newW, out newH, out usedFallback);

                uint targetDpi = usedFallback ? primaryDpi : (hasWinMon && winMonInfo.Dpi >= 72 ? winMonInfo.Dpi : primaryDpi);
                NativeMethods.RECT targetWork = usedFallback ? primaryWork : ((winWork.Width > 0 && winWork.Height > 0) ? winWork : primaryWork);

                if (usedFallback)
                {
                    _currentDpi = primaryDpi;
                    _anchorCorner = "top-right";
                    _preDrawerNudgeTopY = null;
                }
                else if (hasRect && targetDpi >= 72 && targetDpi != _currentDpi)
                {
                    double targetScale = targetDpi / 96.0;
                    newW = Math.Max(64, (int)Math.Ceiling(_logicalWidth * targetScale - 1e-6));
                    newH = Math.Max(24, (int)Math.Ceiling(_logicalHeight * targetScale - 1e-6));
                    ClampRectToWorkArea(ref newX, ref newY, ref newW, ref newH, targetWork);
                    _currentDpi = targetDpi;
                }

                ApplyRegionClipIfNeeded(newW, newH);
                uint swpFlags = (_alwaysOnTop ? 0u : NativeMethods.SWP_NOZORDER)
                    | NativeMethods.SWP_SHOWWINDOW
                    | NativeMethods.SWP_NOACTIVATE
                    | NativeMethods.SWP_NOOWNERZORDER;
                _suppressDpiSuggestedRect = true;
                _dpiTransitionTargetDpi = targetDpi;
                _dpiTransitionTargetWork = targetWork;
                _hasDpiTransitionWork = true;
                try
                {
                    NativeMethods.SetWindowPos(
                        this.Handle,
                        _alwaysOnTop ? NativeMethods.HWND_TOPMOST : IntPtr.Zero,
                        newX, newY, newW, newH,
                        swpFlags);
                }
                finally
                {
                    _hasDpiTransitionWork = false;
                    _dpiTransitionTargetDpi = 0;
                    _suppressDpiSuggestedRect = false;
                }

                _currentDpi = NativeMethods.GetSafeDpiForWindow(this.Handle);
                CoreWebView2Controller ctrl = _controller;
                if (ctrl != null)
                {
                    try { if (!ctrl.IsVisible) ctrl.IsVisible = true; } catch { }
                }
                SyncControllerBounds(newW, newH);
                EmitWorkAreaIfChanged(true);

                LogHostEvent("display_or_resume_recover", string.Format(
                    CultureInfo.InvariantCulture,
                    "reason={0} fallback={1} before={2},{3},{4}x{5} after={6},{7},{8}x{9} topmost={10}",
                    reason ?? "unspecified", usedFallback,
                    curRect.Left, curRect.Top, curRect.Width, curRect.Height,
                    newX, newY, newW, newH, _alwaysOnTop));
            }
            catch (Exception ex)
            {
                LogHostEvent("display_or_resume_recover", string.Format(
                    CultureInfo.InvariantCulture,
                    "reason={0} error={1}: {2}",
                    reason ?? "unspecified", ex.GetType().Name, ex.Message));
            }
        }

        protected override void WndProc(ref Message m)
        {
            if (m.Msg == NativeMethods.WM_SYSCOMMAND)
            {
                int sc = unchecked((int)(m.WParam.ToInt64() & 0xFFF0));
                bool auth = (Interlocked.CompareExchange(ref _authorizedExit, 0, 0) != 0);
                if (ShouldBlockSysCommand(sc, auth))
                {
                    if (sc == NativeMethods.SC_CLOSE) LogHostEvent("blocked_syscommand", "SC_CLOSE");
                    else if (sc == NativeMethods.SC_MINIMIZE) LogHostEvent("blocked_syscommand", "SC_MINIMIZE");
                    else if (sc == NativeMethods.SC_MAXIMIZE) LogHostEvent("blocked_syscommand", "SC_MAXIMIZE");
                    m.Result = IntPtr.Zero;
                    return;
                }
            }

            if (m.Msg == NativeMethods.WM_GETMINMAXINFO)
            {
                base.WndProc(ref m);
                ApplyMinMaxInfo(ref m);
                return;
            }

            if (m.Msg == NativeMethods.WM_DPICHANGED)
            {
                uint newDpi = (uint)(((long)m.WParam >> 16) & 0xFFFF);
                if (newDpi >= 72 && newDpi <= 576) _currentDpi = newDpi;
                if (_inInteractiveMove || _inExternalSizeMove) _dpiChangedInDrag = true;
                if (!_suppressDpiSuggestedRect && m.LParam != IntPtr.Zero)
                {
                    NativeMethods.RECT suggested = (NativeMethods.RECT)Marshal.PtrToStructure(m.LParam, typeof(NativeMethods.RECT));
                    int x = suggested.Left, y = suggested.Top;
                    int w = Math.Max(64, suggested.Width), h = Math.Max(24, suggested.Height);
                    NativeMethods.RECT work = NativeMethods.GetWorkAreaForPoint(x + w / 2, y + h / 2);
                    if (work.Width <= 0 || work.Height <= 0)
                    {
                        uint unusedDpi;
                        work = NativeMethods.GetPrimaryWorkArea(out unusedDpi);
                    }
                    bool inMove = _inInteractiveMove || _inExternalSizeMove;
                    if (!inMove && !_inInteractiveResize)
                    {
                        ClampRectToWorkArea(ref x, ref y, ref w, ref h, work);
                    }
                    _dpiTransitionTargetDpi = (newDpi >= 72 && newDpi <= 576) ? newDpi : _currentDpi;
                    _dpiTransitionTargetWork = work;
                    _hasDpiTransitionWork = true;
                    try
                    {
                        ApplyRegionClipIfNeeded(w, h);
                        NativeMethods.SetWindowPos(
                            this.Handle,
                            _alwaysOnTop ? NativeMethods.HWND_TOPMOST : IntPtr.Zero,
                            x, y, w, h,
                            (_alwaysOnTop ? 0u : NativeMethods.SWP_NOZORDER) | NativeMethods.SWP_NOACTIVATE | NativeMethods.SWP_NOOWNERZORDER);
                    }
                    finally
                    {
                        _hasDpiTransitionWork = false;
                        _dpiTransitionTargetDpi = 0;
                    }
                    SyncControllerBounds(w, h);
                    if (!inMove && !_inInteractiveResize)
                    {
                        EmitWorkAreaIfChanged(true);
                    }
                }
                m.Result = IntPtr.Zero;
                return;
            }

            if (m.Msg == NativeMethods.WM_ENTERSIZEMOVE && !_inInteractiveMove && !_inInteractiveResize && IsHandleCreated)
            {
                _inExternalSizeMove = true;
                _dpiChangedInDrag = false;
                _dragMonitors = NativeMethods.GetAllMonitors();
                NativeMethods.GetWindowRect(this.Handle, out _externalBeforeRect);
                _externalBeforeDpi = NativeMethods.GetSafeDpiForWindow(this.Handle);
            }

            if (m.Msg == NativeMethods.WM_MOVING && (_inInteractiveMove || _inExternalSizeMove) && m.LParam != IntPtr.Zero)
            {
                NativeMethods.RECT r = (NativeMethods.RECT)Marshal.PtrToStructure(m.LParam, typeof(NativeMethods.RECT));
                NativeMethods.MonitorDisplayInfo[] mons = _dragMonitors;
                if (mons == null || mons.Length == 0)
                {
                    mons = NativeMethods.GetAllMonitors();
                    _dragMonitors = mons;
                }
                NativeMethods.POINT curPt = new NativeMethods.POINT();
                bool hasCur = _inInteractiveMove && NativeMethods.GetCursorPos(out curPt);
                ClampMovingRectMultiMonitor(ref r, mons, hasCur, curPt);
                Marshal.StructureToPtr(r, m.LParam, false);
            }

            base.WndProc(ref m);

            if (m.Msg == NativeMethods.WM_MOVE)
            {
                CoreWebView2Controller ctrl = _controller;
                if (ctrl != null) { try { ctrl.NotifyParentWindowPositionChanged(); } catch { } }
            }
            else if (m.Msg == NativeMethods.WM_SIZE)
            {
                if (IsHandleCreated && !IsDisposed)
                {
                    Rectangle client = this.ClientRectangle;
                    if (client.Width > 0 && client.Height > 0)
                        SyncControllerBoundsRect(client);
                }
            }
            else if (m.Msg == NativeMethods.WM_DISPLAYCHANGE)
            {
                if (_dragMonitors != null && (_inInteractiveMove || _inExternalSizeMove))
                    _dragMonitors = NativeMethods.GetAllMonitors();
                if (!IsDisposed && IsHandleCreated && _closingFlag == 0)
                    EnsureVisibleInWorkAreaAndTopMost("display_change");
            }
            else if (m.Msg == NativeMethods.WM_POWERBROADCAST)
            {
                int pbt = unchecked((int)m.WParam.ToInt64());
                if ((pbt == NativeMethods.PBT_APMRESUMEAUTOMATIC || pbt == NativeMethods.PBT_APMRESUMESUSPEND)
                    && !IsDisposed && IsHandleCreated && _closingFlag == 0)
                {
                    EnsureVisibleInWorkAreaAndTopMost("power_resume_" + pbt.ToString(CultureInfo.InvariantCulture));
                }
            }
            else if (m.Msg == NativeMethods.WM_DWMCOMPOSITIONCHANGED)
            {
                if (!IsDisposed && IsHandleCreated && _closingFlag == 0)
                    EnsureVisibleInWorkAreaAndTopMost("dwm_changed");
            }
            else if (m.Msg == NativeMethods.WM_SETTINGCHANGE)
            {
                if (unchecked((int)m.WParam.ToInt64()) == NativeMethods.SPI_SETWORKAREA
                    && !IsDisposed && IsHandleCreated && _closingFlag == 0
                    && !_inInteractiveMove && !_inInteractiveResize && !_inExternalSizeMove)
                {
                    EnsureVisibleInWorkAreaAndTopMost("workarea_changed");
                }
            }
            else if (m.Msg == NativeMethods.WM_EXITSIZEMOVE && _inExternalSizeMove)
            {
                _inExternalSizeMove = false;
                _dragMonitors = null;
                if (!IsDisposed && IsHandleCreated && _closingFlag == 0)
                {
                    FinalizeDragPlacement(_externalBeforeRect, _externalBeforeDpi, false);
                }
            }
        }

        private void StartParentWatchdog()
        {
            int ppid = _opts.ParentPid;
            if (ppid <= 0) return;

            IntPtr stopHandle = _shutdownEvent;
            _watchdogThread = new Thread(delegate()
            {
                IntPtr hParent = NativeMethods.OpenProcess(NativeMethods.SYNCHRONIZE, false, (uint)ppid);
                if (hParent == IntPtr.Zero)
                {
                    int err = Marshal.GetLastWin32Error();
                    LogHostEvent("parent_exited", string.Format(
                        CultureInfo.InvariantCulture,
                        "ppid={0} reason=open_process_failed win32Err={1}",
                        ppid, err));
                    RequestGracefulExit("parent_exited");
                    return;
                }

                uint waitRc;
                try
                {
                    if (stopHandle != IntPtr.Zero)
                        waitRc = NativeMethods.WaitForMultipleObjects(2, new IntPtr[] { hParent, stopHandle }, false, NativeMethods.INFINITE);
                    else
                        waitRc = NativeMethods.WaitForSingleObject(hParent, NativeMethods.INFINITE);
                }
                finally
                {
                    NativeMethods.CloseHandle(hParent);
                }

                if (waitRc == NativeMethods.WAIT_OBJECT_0)
                {
                    LogHostEvent("parent_exited", string.Format(
                        CultureInfo.InvariantCulture,
                        "ppid={0} reason=wait_signaled",
                        ppid));
                    RequestGracefulExit("parent_exited");
                }
            });
            _watchdogThread.Name = "FlowPilotParentWatchdog";
            _watchdogThread.IsBackground = true;
            _watchdogThread.Start();
        }

        private void StartStdinListener()
        {
            if (!NativeMethods.HasRedirectedStdinPipe()) return;
            Task.Run(async delegate
            {
                try
                {
                    while (_closingFlag == 0)
                    {
                        string line = await Console.In.ReadLineAsync().ConfigureAwait(false);
                        if (line == null) break;
                        string trimmed = line.Trim();
                        if (trimmed.Length == 0) continue;
                        if (!IsDisposed && IsHandleCreated)
                        {
                            try { this.BeginInvoke(new Action<string>(HandleCommandString), trimmed); }
                            catch { }
                        }
                    }
                }
                catch { }
            });
        }

        private void SignalShutdownEvent()
        {
            IntPtr ev = _shutdownEvent;
            if (ev != IntPtr.Zero)
            {
                try { NativeMethods.SetEvent(ev); } catch { }
            }
        }

        private void RequestGracefulExit(string reason)
        {
            Interlocked.Exchange(ref _authorizedExit, 1);
            if (Interlocked.Exchange(ref _closingFlag, 1) != 0) return;
            LogHostEvent("graceful_exit", "reason=" + (string.IsNullOrEmpty(reason) ? "unspecified" : reason));
            SignalShutdownEvent();

            Thread forceThread = new Thread(delegate()
            {
                Thread.Sleep(1500);
                Environment.Exit(0);
            });
            forceThread.IsBackground = true;
            forceThread.Start();

            try
            {
                if (IsHandleCreated && !IsDisposed)
                {
                    this.BeginInvoke(new Action(delegate
                    {
                        SafeCloseController();
                        try { this.Close(); } catch { }
                        Application.ExitThread();
                    }));
                }
                else
                {
                    Environment.Exit(0);
                }
            }
            catch { Environment.Exit(0); }
        }

        private void SafeCloseController()
        {
            CoreWebView2 wv = Interlocked.Exchange(ref _webView, null);
            if (wv != null)
            {
                try { wv.NavigationStarting -= OnNavigationStarting; } catch { }
                try { wv.WebMessageReceived -= OnWebMessageReceived; } catch { }
                try { wv.NavigationCompleted -= OnNavigationCompleted; } catch { }
                try { wv.ProcessFailed -= OnWebViewProcessFailed; } catch { }
            }

            CoreWebView2Controller ctrl = Interlocked.Exchange(ref _controller, null);
            if (ctrl != null)
            {
                try { ctrl.Close(); } catch { }
            }
            _env = null;
        }

        private void DisposeTimers()
        {
            if (_visibilityGuardTimer != null)
            {
                try { _visibilityGuardTimer.Stop(); } catch { }
                try { _visibilityGuardTimer.Dispose(); } catch { }
                _visibilityGuardTimer = null;
            }
            if (_navRetryTimer != null)
            {
                try { _navRetryTimer.Stop(); } catch { }
                try { _navRetryTimer.Dispose(); } catch { }
                _navRetryTimer = null;
            }
            if (_tempTopMostTimer != null)
            {
                try { _tempTopMostTimer.Stop(); } catch { }
                try { _tempTopMostTimer.Dispose(); } catch { }
                _tempTopMostTimer = null;
            }
        }

        protected override void OnFormClosing(FormClosingEventArgs e)
        {
            bool auth = (Interlocked.CompareExchange(ref _authorizedExit, 0, 0) != 0);
            if (ShouldCancelFormClosing(auth, e.CloseReason))
            {
                e.Cancel = true;
                LogHostEvent("blocked_unexpected_close", "closeReason=" + e.CloseReason.ToString());
                EnsureVisibleInWorkAreaAndTopMost("blocked_close");
                return;
            }
            if (!auth)
                LogHostEvent("graceful_exit", "reason=" + e.CloseReason.ToString());

            Interlocked.Exchange(ref _closingFlag, 1);
            SignalShutdownEvent();
            DisposeTimers();
            if (_useRegionFallback && IsHandleCreated && this.Handle != IntPtr.Zero)
            {
                try { NativeMethods.SetWindowRgn(this.Handle, IntPtr.Zero, false); } catch { }
            }
            SafeCloseController();
            base.OnFormClosing(e);
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                DisposeTimers();
            }
            SignalShutdownEvent();
            Thread wt = Interlocked.Exchange(ref _watchdogThread, null);
            if (wt != null && wt.IsAlive)
            {
                try { wt.Join(50); } catch { }
            }
            IntPtr ev = Interlocked.Exchange(ref _shutdownEvent, IntPtr.Zero);
            if (ev != IntPtr.Zero)
            {
                try { NativeMethods.CloseHandle(ev); } catch { }
            }
            base.Dispose(disposing);
        }

        internal static void ClampRectToWorkArea(ref int x, ref int y, ref int w, ref int h, NativeMethods.RECT work)
        {
            if (work.Width > 0 && w > work.Width) w = work.Width;
            if (work.Height > 0)
            {
                int safeBottom = (work.Height > BottomWorkMarginPhys * 4) ? (work.Bottom - BottomWorkMarginPhys) : work.Bottom;
                int maxH = Math.Max(24, safeBottom - work.Top);
                if (h > maxH) h = maxH;
            }
            ClampPositionOnly(ref x, ref y, w, h, work);
        }

        internal static void ClampPositionOnly(ref int x, ref int y, int w, int h, NativeMethods.RECT work)
        {
            if (work.Width > 0)
            {
                if (x + w > work.Right) x = work.Right - w;
                if (x < work.Left) x = work.Left;
            }
            if (work.Height > 0)
            {
                int safeBottom = (work.Height > BottomWorkMarginPhys * 4) ? (work.Bottom - BottomWorkMarginPhys) : work.Bottom;
                if (y + h > safeBottom) y = safeBottom - h;
                if (y < work.Top) y = work.Top;
            }
        }

        internal static int GetSafeWorkBottom(NativeMethods.RECT work)
        {
            return (work.Height > BottomWorkMarginPhys * 4) ? (work.Bottom - BottomWorkMarginPhys) : work.Bottom;
        }

        private static bool RangesOverlap(int a0, int a1, int b0, int b1)
        {
            return a0 < b1 && b0 < a1;
        }

        internal static bool RectsIntersect(NativeMethods.RECT a, NativeMethods.RECT b)
        {
            return a.Left < b.Right && b.Left < a.Right && a.Top < b.Bottom && b.Top < a.Bottom;
        }

        internal static bool RectContainsPoint(NativeMethods.RECT r, int x, int y)
        {
            return x >= r.Left && x < r.Right && y >= r.Top && y < r.Bottom;
        }

        private static long IntersectionArea(NativeMethods.RECT a, NativeMethods.RECT b)
        {
            long ix = Math.Min(a.Right, b.Right) - Math.Max(a.Left, b.Left);
            long iy = Math.Min(a.Bottom, b.Bottom) - Math.Max(a.Top, b.Top);
            return (ix > 0 && iy > 0) ? (ix * iy) : 0L;
        }

        private static long DistanceSqToRect(int x, int y, NativeMethods.RECT r)
        {
            long dx = 0;
            if (x < r.Left) dx = r.Left - x;
            else if (x > r.Right) dx = x - r.Right;
            long dy = 0;
            if (y < r.Top) dy = r.Top - y;
            else if (y > r.Bottom) dy = y - r.Bottom;
            return dx * dx + dy * dy;
        }

        internal static int FindMonitorIndexForPoint(NativeMethods.MonitorDisplayInfo[] monitors, int x, int y, bool requireInside)
        {
            if (monitors == null || monitors.Length == 0) return -1;
            for (int i = 0; i < monitors.Length; i++)
            {
                if (RectContainsPoint(monitors[i].MonitorRect, x, y))
                    return i;
            }
            if (requireInside) return -1;

            int bestIdx = 0;
            long bestDist = long.MaxValue;
            for (int i = 0; i < monitors.Length; i++)
            {
                long d = DistanceSqToRect(x, y, monitors[i].MonitorRect);
                if (d < bestDist)
                {
                    bestDist = d;
                    bestIdx = i;
                }
            }
            return bestIdx;
        }

        internal static int FindMonitorIndexForRect(NativeMethods.MonitorDisplayInfo[] monitors, NativeMethods.RECT rect, bool requireOverlap)
        {
            if (monitors == null || monitors.Length == 0) return -1;
            int bestIdx = -1;
            long bestArea = 0;
            for (int i = 0; i < monitors.Length; i++)
            {
                long area = IntersectionArea(rect, monitors[i].MonitorRect);
                if (area > bestArea)
                {
                    bestArea = area;
                    bestIdx = i;
                }
            }
            if (bestIdx >= 0) return bestIdx;
            if (requireOverlap) return -1;
            return FindMonitorIndexForPoint(monitors, rect.Left + rect.Width / 2, rect.Top + rect.Height / 2, false);
        }

        internal static int ResolveDropTargetMonitorIndex(
            NativeMethods.RECT rect,
            NativeMethods.MonitorDisplayInfo[] monitors,
            bool hasCursor,
            NativeMethods.POINT cursorPt)
        {
            if (monitors == null || monitors.Length == 0) return -1;
            if (monitors.Length == 1) return 0;

            if (hasCursor)
            {
                int curIdx = FindMonitorIndexForPoint(monitors, cursorPt.X, cursorPt.Y, true);
                if (curIdx >= 0)
                {
                    if (RectsIntersect(rect, monitors[curIdx].MonitorRect)
                        || FindMonitorIndexForRect(monitors, rect, true) < 0)
                    {
                        return curIdx;
                    }
                }
            }
            return FindMonitorIndexForRect(monitors, rect, false);
        }

        internal static void ClampMovingRect(ref NativeMethods.RECT r, NativeMethods.RECT work)
        {
            int w = r.Width, h = r.Height;
            if (w <= 0 || h <= 0) return;
            int x = r.Left, y = r.Top;
            ClampPositionOnly(ref x, ref y, w, h, work);
            r.Left = x;
            r.Top = y;
            r.Right = x + w;
            r.Bottom = y + h;
        }

        internal static void ClampMovingRectMultiMonitor(
            ref NativeMethods.RECT r,
            NativeMethods.MonitorDisplayInfo[] monitors,
            bool hasCursor,
            NativeMethods.POINT cursorPt)
        {
            int w = r.Width, h = r.Height;
            if (w <= 0 || h <= 0 || monitors == null || monitors.Length == 0) return;
            if (monitors.Length == 1)
            {
                ClampMovingRect(ref r, monitors[0].WorkRect);
                return;
            }

            int activeIdx = hasCursor ? FindMonitorIndexForPoint(monitors, cursorPt.X, cursorPt.Y, true) : -1;
            if (activeIdx < 0) activeIdx = FindMonitorIndexForRect(monitors, r, false);
            if (activeIdx < 0) activeIdx = 0;

            NativeMethods.MonitorDisplayInfo active = monitors[activeIdx];
            int minLeft = active.WorkRect.Left;
            int maxRight = active.WorkRect.Right;
            int minTop = active.WorkRect.Top;
            int maxSafeBottom = GetSafeWorkBottom(active.WorkRect);

            for (int i = 0; i < monitors.Length; i++)
            {
                if (i == activeIdx) continue;
                NativeMethods.MonitorDisplayInfo other = monitors[i];
                bool horizBand = (hasCursor && cursorPt.Y >= other.MonitorRect.Top && cursorPt.Y < other.MonitorRect.Bottom)
                    || RangesOverlap(r.Top, r.Bottom, other.MonitorRect.Top, other.MonitorRect.Bottom);
                if (horizBand)
                {
                    if (other.MonitorRect.Left < active.MonitorRect.Left)
                        minLeft = Math.Min(minLeft, other.WorkRect.Left);
                    if (other.MonitorRect.Right > active.MonitorRect.Right)
                        maxRight = Math.Max(maxRight, other.WorkRect.Right);
                }

                bool vertBand = (hasCursor && cursorPt.X >= other.MonitorRect.Left && cursorPt.X < other.MonitorRect.Right)
                    || RangesOverlap(r.Left, r.Right, other.MonitorRect.Left, other.MonitorRect.Right);
                if (vertBand)
                {
                    if (other.MonitorRect.Top < active.MonitorRect.Top)
                        minTop = Math.Min(minTop, other.WorkRect.Top);
                    if (other.MonitorRect.Bottom > active.MonitorRect.Bottom)
                        maxSafeBottom = Math.Max(maxSafeBottom, GetSafeWorkBottom(other.WorkRect));
                }

                if (RectsIntersect(r, other.MonitorRect))
                {
                    minTop = Math.Min(minTop, other.WorkRect.Top);
                    maxSafeBottom = Math.Max(maxSafeBottom, GetSafeWorkBottom(other.WorkRect));
                }
            }

            int x = r.Left, y = r.Top;
            if (maxRight > minLeft)
            {
                if (x + w > maxRight) x = maxRight - w;
                if (x < minLeft) x = minLeft;
            }
            if (maxSafeBottom > minTop)
            {
                if (y + h > maxSafeBottom) y = maxSafeBottom - h;
                if (y < minTop) y = minTop;
            }
            r.Left = x;
            r.Top = y;
            r.Right = x + w;
            r.Bottom = y + h;
        }

        internal static void ResolveInitialPhysicalBounds(
            int? initialPhysX, int? initialPhysY,
            int? initialLogX, int? initialLogY,
            int logicalWidth, int logicalHeight,
            NativeMethods.MonitorDisplayInfo[] monitors,
            NativeMethods.MonitorDisplayInfo cursorOrPrimaryMonitor,
            out int outX, out int outY, out int outW, out int outH,
            out uint outDpi, out NativeMethods.RECT outWork, out string outAnchor, out bool usedSavedPosition)
        {
            usedSavedPosition = false;
            NativeMethods.MonitorDisplayInfo[] safeMons = (monitors != null && monitors.Length > 0)
                ? monitors
                : new NativeMethods.MonitorDisplayInfo[] { cursorOrPrimaryMonitor };

            NativeMethods.MonitorDisplayInfo primaryMon = cursorOrPrimaryMonitor;
            for (int i = 0; i < safeMons.Length; i++)
            {
                if (safeMons[i].IsPrimary)
                {
                    primaryMon = safeMons[i];
                    break;
                }
            }
            uint primaryDpi = primaryMon.Dpi >= 72 ? primaryMon.Dpi : 96u;
            double primaryScale = primaryDpi / 96.0;

            if (initialPhysX.HasValue && initialPhysY.HasValue
                && IsValidPhysicalCoord(initialPhysX.Value)
                && IsValidPhysicalCoord(initialPhysY.Value))
            {
                int px = initialPhysX.Value;
                int py = initialPhysY.Value;
                int monIdx = FindMonitorIndexForPoint(safeMons, px, py, true);
                if (monIdx < 0)
                {
                    int probeW = Math.Max(64, (int)Math.Ceiling(logicalWidth * primaryScale - 1e-6));
                    int probeH = Math.Max(24, (int)Math.Ceiling(logicalHeight * primaryScale - 1e-6));
                    NativeMethods.RECT probeRect = new NativeMethods.RECT { Left = px, Top = py, Right = px + probeW, Bottom = py + probeH };
                    monIdx = FindMonitorIndexForRect(safeMons, probeRect, true);
                }
                if (monIdx >= 0)
                {
                    NativeMethods.MonitorDisplayInfo mon = safeMons[monIdx];
                    uint monDpi = mon.Dpi >= 72 ? mon.Dpi : 96u;
                    double scale = monDpi / 96.0;
                    int physW = Math.Max(64, (int)Math.Ceiling(logicalWidth * scale - 1e-6));
                    int physH = Math.Max(24, (int)Math.Ceiling(logicalHeight * scale - 1e-6));
                    int physX = px, physY = py;
                    ClampRectToWorkArea(ref physX, ref physY, ref physW, ref physH, mon.WorkRect);
                    outX = physX;
                    outY = physY;
                    outW = physW;
                    outH = physH;
                    outDpi = monDpi;
                    outWork = mon.WorkRect;
                    outAnchor = InferNearestAnchor(physX, physY, physW, physH, mon.WorkRect, monDpi);
                    usedSavedPosition = true;
                    return;
                }
            }
            else if (initialLogX.HasValue && initialLogY.HasValue
                && IsValidLogicalCoord(initialLogX.Value)
                && IsValidLogicalCoord(initialLogY.Value))
            {
                int lx = initialLogX.Value;
                int ly = initialLogY.Value;
                int guessX = (int)Math.Round(lx * primaryScale);
                int guessY = (int)Math.Round(ly * primaryScale);
                int monIdx = FindMonitorIndexForPoint(safeMons, guessX, guessY, true);
                if (monIdx < 0)
                {
                    for (int i = 0; i < safeMons.Length; i++)
                    {
                        uint mDpi = safeMons[i].Dpi >= 72 ? safeMons[i].Dpi : 96u;
                        double mScale = mDpi / 96.0;
                        int candX = (int)Math.Round(lx * mScale);
                        int candY = (int)Math.Round(ly * mScale);
                        if (RectContainsPoint(safeMons[i].MonitorRect, candX, candY))
                        {
                            monIdx = i;
                            break;
                        }
                    }
                }
                if (monIdx >= 0)
                {
                    NativeMethods.MonitorDisplayInfo mon = safeMons[monIdx];
                    uint monDpi = mon.Dpi >= 72 ? mon.Dpi : 96u;
                    double scale = monDpi / 96.0;
                    int physW = Math.Max(64, (int)Math.Ceiling(logicalWidth * scale - 1e-6));
                    int physH = Math.Max(24, (int)Math.Ceiling(logicalHeight * scale - 1e-6));
                    int physX = (int)Math.Round(lx * scale);
                    int physY = (int)Math.Round(ly * scale);
                    int exactIdx = FindMonitorIndexForPoint(safeMons, physX + physW / 2, physY + physH / 2, true);
                    if (exactIdx >= 0 && exactIdx != monIdx)
                    {
                        mon = safeMons[exactIdx];
                        monDpi = mon.Dpi >= 72 ? mon.Dpi : 96u;
                        scale = monDpi / 96.0;
                        physW = Math.Max(64, (int)Math.Ceiling(logicalWidth * scale - 1e-6));
                        physH = Math.Max(24, (int)Math.Ceiling(logicalHeight * scale - 1e-6));
                    }
                    ClampRectToWorkArea(ref physX, ref physY, ref physW, ref physH, mon.WorkRect);
                    outX = physX;
                    outY = physY;
                    outW = physW;
                    outH = physH;
                    outDpi = monDpi;
                    outWork = mon.WorkRect;
                    outAnchor = InferNearestAnchor(physX, physY, physW, physH, mon.WorkRect, monDpi);
                    usedSavedPosition = true;
                    return;
                }
            }

            uint targetDpi = cursorOrPrimaryMonitor.Dpi >= 72 ? cursorOrPrimaryMonitor.Dpi : primaryDpi;
            NativeMethods.RECT targetWork = (cursorOrPrimaryMonitor.WorkRect.Width > 0 && cursorOrPrimaryMonitor.WorkRect.Height > 0)
                ? cursorOrPrimaryMonitor.WorkRect
                : primaryMon.WorkRect;
            double fbScale = targetDpi / 96.0;
            int margin = (int)Math.Round(16 * fbScale);
            int fbW = Math.Max(64, (int)Math.Ceiling(logicalWidth * fbScale - 1e-6));
            int fbH = Math.Max(24, (int)Math.Ceiling(logicalHeight * fbScale - 1e-6));
            int fbX = targetWork.Right - fbW - margin;
            int fbY = targetWork.Top + margin;
            ClampRectToWorkArea(ref fbX, ref fbY, ref fbW, ref fbH, targetWork);

            outX = fbX;
            outY = fbY;
            outW = fbW;
            outH = fbH;
            outDpi = targetDpi;
            outWork = targetWork;
            outAnchor = "top-right";
            usedSavedPosition = false;
        }

        internal static string InferNearestAnchor(int x, int y, int w, int h, NativeMethods.RECT work, uint dpi)
        {
            int distLeft = Math.Max(0, x - work.Left);
            int distRight = Math.Max(0, work.Right - (x + w));
            return (distRight <= distLeft) ? "top-right" : "top-left";
        }

        internal static string NormalizeAnchor(string raw)
        {
            if (string.IsNullOrEmpty(raw)) return "top-right";
            string s = raw.Trim().ToLowerInvariant();
            return (s == "top-left" || s == "bottom-left" || s == "left") ? "top-left" : "top-right";
        }

        internal static string NormalizeViewMode(string raw)
        {
            return (!string.IsNullOrEmpty(raw) && string.Equals(raw.Trim(), "pill", StringComparison.OrdinalIgnoreCase)) ? "pill" : "stack";
        }

        private static string GetStringField(Dictionary<string, object> dict, params string[] keys)
        {
            foreach (string k in keys)
            {
                object val;
                if (dict.TryGetValue(k, out val) && val != null)
                {
                    string s = Convert.ToString(val, CultureInfo.InvariantCulture);
                    if (!string.IsNullOrEmpty(s)) return s;
                }
            }
            return null;
        }

        private static double GetDoubleField(Dictionary<string, object> dict, double fallback, params string[] keys)
        {
            foreach (string k in keys)
            {
                object val;
                if (dict.TryGetValue(k, out val) && val != null)
                {
                    try
                    {
                        double d = Convert.ToDouble(val, CultureInfo.InvariantCulture);
                        if (!double.IsNaN(d) && !double.IsInfinity(d)) return d;
                    }
                    catch { }
                }
            }
            return fallback;
        }

        private static bool GetBoolField(Dictionary<string, object> dict, bool fallback, params string[] keys)
        {
            foreach (string k in keys)
            {
                object val;
                if (dict.TryGetValue(k, out val) && val != null)
                {
                    if (val is bool) return (bool)val;
                    string s = Convert.ToString(val, CultureInfo.InvariantCulture).Trim().ToLowerInvariant();
                    if (s == "true" || s == "1" || s == "yes" || s == "on") return true;
                    if (s == "false" || s == "0" || s == "no" || s == "off") return false;
                }
            }
            return fallback;
        }
    }

    internal static class Program
    {
        public const string MutexName = @"Local\AgyFlowPilot_DesktopHud_SingleInstance_v2";
        private static IntPtr _mutexHandle = IntPtr.Zero;

        [STAThread]
        public static int Main(string[] args)
        {
            Environment.SetEnvironmentVariable("WEBVIEW2_DEFAULT_BACKGROUND_COLOR", "00000000");
            NativeMethods.EnablePerMonitorDpiV2();

            HostOptions opts = ParseOptions(args);
            if (opts.SelfTest) return RunSelfTest(opts);
            if (!string.IsNullOrEmpty(opts.TargetUrl))
            {
                if (!IsAllowedLoopbackHttpUrl(opts.TargetUrl))
                {
                    HudForm.LogHostEventStatic(opts.PluginDataDir, "rejected_target_url", "non_loopback_or_invalid");
                    return 2;
                }
                opts.TargetUrl = opts.TargetUrl.Trim();
            }
            else
            {
                if (opts.Port <= 0 || opts.Port > 65535) return 2;
                opts.TargetUrl = string.Format(CultureInfo.InvariantCulture, "http://127.0.0.1:{0}/ui.html?mode=hud", opts.Port);
            }

            if (!AcquireSingleInstanceMutex())
            {
                HudForm.LogHostEventStatic(opts.PluginDataDir, "mutex_busy",
                    string.Format(CultureInfo.InvariantCulture, "port={0} ppid={1}", opts.Port, opts.ParentPid));
                return 0;
            }

            try
            {

                HudForm.LogHostEventStatic(opts.PluginDataDir, "startup",
                    string.Format(CultureInfo.InvariantCulture,
                        "version={0} port={1} ppid={2} mode={3} topmost={4} anchor={5}",
                        HudForm.HostVersion, opts.Port, opts.ParentPid, opts.ViewMode, opts.AlwaysOnTop, opts.AnchorCorner));

                try { Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException); } catch { }
                Application.ThreadException += delegate(object sender, ThreadExceptionEventArgs ev)
                {
                    Exception ex = ev != null ? ev.Exception : null;
                    string detail = ex != null
                        ? string.Format(CultureInfo.InvariantCulture, "ui_thread {0}: {1}", ex.GetType().Name, ex.Message)
                        : "ui_thread unknown";
                    HudForm.LogHostEventStatic(opts.PluginDataDir, "unhandled_exception", detail);
                };
                AppDomain.CurrentDomain.UnhandledException += delegate(object sender, UnhandledExceptionEventArgs ev)
                {
                    Exception ex = ev != null ? ev.ExceptionObject as Exception : null;
                    bool term = ev != null && ev.IsTerminating;
                    string detail = ex != null
                        ? string.Format(CultureInfo.InvariantCulture, "appdomain terminating={0} {1}: {2}", term, ex.GetType().Name, ex.Message)
                        : string.Format(CultureInfo.InvariantCulture, "appdomain terminating={0} {1}", term, Convert.ToString(ev != null ? ev.ExceptionObject : null, CultureInfo.InvariantCulture));
                    HudForm.LogHostEventStatic(opts.PluginDataDir, "unhandled_exception", detail);
                };

                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                using (HudForm form = new HudForm(opts))
                {
                    Application.Run(form);
                }
                return 0;
            }
            catch (Exception ex)
            {
                HudForm.LogHostEventStatic(opts.PluginDataDir, "unhandled_exception",
                    string.Format(CultureInfo.InvariantCulture, "main {0}: {1}", ex.GetType().Name, ex.Message));
                return 1;
            }
            finally
            {
                ReleaseSingleInstanceMutex();
            }
        }

        private static bool AcquireSingleInstanceMutex()
        {
            IntPtr hMutex = NativeMethods.CreateMutexW(IntPtr.Zero, false, MutexName);
            if (hMutex == IntPtr.Zero) return false;

            uint waitRc = NativeMethods.WaitForSingleObject(hMutex, 1200);
            if (waitRc == NativeMethods.WAIT_OBJECT_0 || waitRc == NativeMethods.WAIT_ABANDONED)
            {
                _mutexHandle = hMutex;
                return true;
            }
            NativeMethods.CloseHandle(hMutex);
            return false;
        }

        private static void ReleaseSingleInstanceMutex()
        {
            IntPtr h = Interlocked.Exchange(ref _mutexHandle, IntPtr.Zero);
            if (h != IntPtr.Zero)
            {
                NativeMethods.ReleaseMutex(h);
                NativeMethods.CloseHandle(h);
            }
        }

        private static HostOptions ParseOptions(string[] args)
        {
            HostOptions opts = new HostOptions();
            opts.HudToken = (Environment.GetEnvironmentVariable("FLOW_PILOT_HUD_TOKEN") ?? string.Empty).Trim();

            string pluginData = (Environment.GetEnvironmentVariable("PLUGIN_DATA") ?? string.Empty).Trim();
            if (string.IsNullOrEmpty(pluginData))
            {
                string home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
                pluginData = Path.Combine(home, ".gemini", "antigravity", "plugin_data", "agy-flow-pilot");
            }
            opts.PluginDataDir = pluginData;
            LoadSavedHudConfig(opts);

            if (args == null) return opts;
            for (int i = 0; i < args.Length; i++)
            {
                string raw = args[i] ?? string.Empty;
                string key = raw, inlineVal = null;
                int eq = raw.IndexOf('=');
                if (eq > 0) { key = raw.Substring(0, eq); inlineVal = raw.Substring(eq + 1); }

                switch (key.Trim().ToLowerInvariant())
                {
                    case "--self-test": opts.SelfTest = true; break;
                    case "--force-region-clip": opts.ForceRegionClip = true; break;
                    case "--parent-pid": case "--ppid": opts.ParentPid = ParseIntArg(args, ref i, inlineVal, opts.ParentPid); break;
                    case "--port": opts.Port = ParseIntArg(args, ref i, inlineVal, opts.Port); break;
                    case "--url": opts.TargetUrl = NextStringArg(args, ref i, inlineVal); break;
                    case "--x":
                        opts.InitialLogicalX = ParseNullableIntArg(args, ref i, inlineVal, opts.InitialLogicalX);
                        opts.InitialPhysicalX = null;
                        opts.InitialPhysicalY = null;
                        break;
                    case "--y":
                        opts.InitialLogicalY = ParseNullableIntArg(args, ref i, inlineVal, opts.InitialLogicalY);
                        opts.InitialPhysicalX = null;
                        opts.InitialPhysicalY = null;
                        break;
                    case "--phys-x": opts.InitialPhysicalX = ParseNullableIntArg(args, ref i, inlineVal, opts.InitialPhysicalX); break;
                    case "--phys-y": opts.InitialPhysicalY = ParseNullableIntArg(args, ref i, inlineVal, opts.InitialPhysicalY); break;
                    case "--width": case "--w": opts.InitialLogicalW = ParseNullableIntArg(args, ref i, inlineVal, opts.InitialLogicalW); break;
                    case "--height": case "--h": opts.InitialLogicalH = ParseNullableIntArg(args, ref i, inlineVal, opts.InitialLogicalH); break;
                    case "--view-mode": case "--mode":
                        string vm = NextStringArg(args, ref i, inlineVal);
                        if (!string.IsNullOrEmpty(vm)) opts.ViewMode = HudForm.NormalizeViewMode(vm);
                        break;
                    case "--anchor": case "--anchor-corner":
                        string ac = NextStringArg(args, ref i, inlineVal);
                        if (!string.IsNullOrEmpty(ac)) opts.AnchorCorner = HudForm.NormalizeAnchor(ac);
                        break;
                    case "--topmost": case "--always-on-top":
                        string tm = NextStringArg(args, ref i, inlineVal);
                        if (!string.IsNullOrEmpty(tm))
                        {
                            string tml = tm.Trim().ToLowerInvariant();
                            opts.AlwaysOnTop = !(tml == "false" || tml == "0" || tml == "no" || tml == "off");
                        }
                        break;
                }
            }
            return opts;
        }

        private static void LoadSavedHudConfig(HostOptions opts)
        {
            try
            {
                string cfgPath = Path.Combine(opts.PluginDataDir, "config.json");
                if (File.Exists(cfgPath))
                    ApplyHudConfigFromJson(opts, File.ReadAllText(cfgPath, Encoding.UTF8));
            }
            catch { }
        }

        internal static void ApplyHudConfigFromJson(HostOptions opts, string raw)
        {
            if (opts == null || string.IsNullOrWhiteSpace(raw)) return;
            try
            {
                Dictionary<string, object> root = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(raw);
                object hudObj;
                if (root == null || !root.TryGetValue("hud", out hudObj) || hudObj == null) return;
                Dictionary<string, object> hud = hudObj as Dictionary<string, object>;
                if (hud == null) return;

                object valObj;
                if (hud.TryGetValue("viewMode", out valObj) && valObj != null)
                    opts.ViewMode = HudForm.NormalizeViewMode(Convert.ToString(valObj, CultureInfo.InvariantCulture));
                if (hud.TryGetValue("alwaysOnTop", out valObj) && valObj is bool)
                    opts.AlwaysOnTop = (bool)valObj;
                if (hud.TryGetValue("anchorCorner", out valObj) && valObj != null)
                    opts.AnchorCorner = HudForm.NormalizeAnchor(Convert.ToString(valObj, CultureInfo.InvariantCulture));

                int? sw = TryReadCoord(hud, "width");
                if (sw.HasValue && sw.Value >= HudForm.MinStackLogicalWidth && sw.Value <= 960)
                    opts.SavedStackWidth = sw.Value;

                int? sh = TryReadCoord(hud, "customHeight");
                if (sh.HasValue && sh.Value >= HudForm.MinStackLogicalHeight && sh.Value <= 4096)
                    opts.SavedCustomHeight = sh.Value;

                int? sx = TryReadCoord(hud, "x"), sy = TryReadCoord(hud, "y");
                if (sx.HasValue && sy.HasValue
                    && HudForm.IsValidLogicalCoord(sx.Value)
                    && HudForm.IsValidLogicalCoord(sy.Value))
                {
                    opts.InitialLogicalX = sx;
                    opts.InitialLogicalY = sy;
                }

                int? px = TryReadCoord(hud, "physX"), py = TryReadCoord(hud, "physY");
                if (px.HasValue && py.HasValue
                    && HudForm.IsValidPhysicalCoord(px.Value)
                    && HudForm.IsValidPhysicalCoord(py.Value))
                {
                    opts.InitialPhysicalX = px;
                    opts.InitialPhysicalY = py;
                }
            }
            catch { }
        }

        private static int? TryReadCoord(Dictionary<string, object> dict, string key)
        {
            object val;
            if (!dict.TryGetValue(key, out val) || val == null) return null;
            try
            {
                double d = Convert.ToDouble(val, CultureInfo.InvariantCulture);
                if (!double.IsNaN(d) && !double.IsInfinity(d)) return (int)Math.Round(d);
            }
            catch { }
            return null;
        }

        private static string NextStringArg(string[] args, ref int idx, string inlineVal)
        {
            if (inlineVal != null) return inlineVal;
            return (idx + 1 < args.Length) ? args[++idx] : string.Empty;
        }

        private static int ParseIntArg(string[] args, ref int idx, string inlineVal, int fallback)
        {
            int v;
            return int.TryParse(NextStringArg(args, ref idx, inlineVal), NumberStyles.Integer, CultureInfo.InvariantCulture, out v) ? v : fallback;
        }

        private static int? ParseNullableIntArg(string[] args, ref int idx, string inlineVal, int? fallback)
        {
            int v;
            return int.TryParse(NextStringArg(args, ref idx, inlineVal), NumberStyles.Integer, CultureInfo.InvariantCulture, out v) ? (int?)v : fallback;
        }

        private static bool VerifyEdgeHitTestMappings(out Dictionary<string, int> mapOut)
        {
            mapOut = new Dictionary<string, int>();
            string[] edges = new string[] { "w", "e", "n", "nw", "ne", "s", "sw", "se" };
            int[] expected = new int[] { 10, 11, 12, 13, 14, 15, 16, 17 };
            for (int i = 0; i < edges.Length; i++)
            {
                string norm;
                int actual = HudForm.MapResizeEdgeToHitTest(edges[i], out norm);
                mapOut[edges[i]] = actual;
                if (actual != expected[i] || norm != edges[i]) return false;
            }
            string invalidNorm;
            return HudForm.MapResizeEdgeToHitTest("invalid", out invalidNorm) == 0
                && HudForm.MapResizeEdgeToHitTest(string.Empty, out invalidNorm) == 0;
        }

        private static bool VerifyLockTopAndAnchorLogic()
        {
            NativeMethods.RECT work = new NativeMethods.RECT { Left = 0, Top = 0, Right = 3440, Bottom = 1390 };
            int safeBottom = work.Bottom - HudForm.BottomWorkMarginPhys;
            if (HudForm.InferNearestAnchor(2900, 1250, 485, 120, work, 120) != "top-right") return false;
            if (HudForm.InferNearestAnchor(40, 1250, 485, 120, work, 120) != "top-left") return false;
            if (HudForm.NormalizeAnchor("bottom-right") != "top-right" || HudForm.NormalizeAnchor("bottom-left") != "top-left") return false;

            double scale = 1.25;
            int minPhysH = (int)Math.Round(HudForm.MinStackLogicalHeight * scale);
            NativeMethods.RECT normalOld = new NativeMethods.RECT { Left = 2900, Top = 850, Right = 3385, Bottom = 1200 };
            int? nudgeState = null;
            int outY, reqH = 700;
            HudForm.ComputeLockedTopVerticalBounds(normalOld, work, scale, true, true, minPhysH, ref nudgeState, out outY, ref reqH);
            if (outY != 850 || reqH != (safeBottom - 850) || nudgeState.HasValue) return false;

            NativeMethods.RECT dockedOld = new NativeMethods.RECT { Left = 2900, Top = 990, Right = 3385, Bottom = safeBottom };
            int? dockedNudge = null;
            int dockedOutY, dockedReqH = 520;
            HudForm.ComputeLockedTopVerticalBounds(dockedOld, work, scale, true, true, minPhysH, ref dockedNudge, out dockedOutY, ref dockedReqH);
            if (!dockedNudge.HasValue || dockedNudge.Value != 990 || dockedOutY != 900 || dockedReqH != (safeBottom - 900)) return false;

            NativeMethods.RECT afterNudgeOld = new NativeMethods.RECT { Left = 2900, Top = dockedOutY, Right = 3385, Bottom = safeBottom };
            int repeatY, repeatH = 560;
            HudForm.ComputeLockedTopVerticalBounds(afterNudgeOld, work, scale, true, true, minPhysH, ref dockedNudge, out repeatY, ref repeatH);
            if (repeatY != 900 || repeatH != (safeBottom - 900)) return false;

            int restoredY, restoredH = safeBottom - 990;
            HudForm.ComputeLockedTopVerticalBounds(afterNudgeOld, work, scale, true, false, minPhysH, ref dockedNudge, out restoredY, ref restoredH);
            if (!(restoredY == 990 && restoredH == (safeBottom - 990) && !dockedNudge.HasValue)) return false;

            NativeMethods.RECT moveOob = new NativeMethods.RECT { Left = -40, Top = 1320, Right = 348, Bottom = 1480 };
            HudForm.ClampMovingRect(ref moveOob, work);
            return moveOob.Left == 0 && moveOob.Width == 388 && moveOob.Bottom == safeBottom && moveOob.Height == 160;
        }

        private static bool VerifyConfigPreloadLogic()
        {
            HostOptions validStackOpts = new HostOptions();
            ApplyHudConfigFromJson(validStackOpts, "{\"hud\":{\"viewMode\":\"stack\",\"alwaysOnTop\":false,\"anchorCorner\":\"bottom-right\",\"width\":336,\"customHeight\":320,\"x\":2397,\"y\":99}}");
            if (validStackOpts.Theme != "light" || validStackOpts.ViewMode != "stack" || validStackOpts.AlwaysOnTop
                || validStackOpts.AnchorCorner != "top-right" || validStackOpts.SavedStackWidth != 336
                || validStackOpts.SavedCustomHeight != 320 || validStackOpts.InitialLogicalX != 2397 || validStackOpts.InitialLogicalY != 99)
                return false;

            HostOptions minBoundOpts = new HostOptions();
            ApplyHudConfigFromJson(minBoundOpts, "{\"hud\":{\"viewMode\":\"pill\",\"anchorCorner\":\"bottom-left\",\"width\":320,\"customHeight\":68}}");
            if (minBoundOpts.Theme != "light" || minBoundOpts.ViewMode != "pill" || !minBoundOpts.AlwaysOnTop
                || minBoundOpts.AnchorCorner != "top-left" || minBoundOpts.SavedStackWidth != 320 || minBoundOpts.SavedCustomHeight != 68)
                return false;

            HostOptions maxBoundOpts = new HostOptions();
            ApplyHudConfigFromJson(maxBoundOpts, "{\"hud\":{\"width\":960,\"customHeight\":4096}}");
            if (maxBoundOpts.Theme != "light" || maxBoundOpts.SavedStackWidth != 960 || maxBoundOpts.SavedCustomHeight != 4096)
                return false;

            HostOptions oobLowOpts = new HostOptions();
            ApplyHudConfigFromJson(oobLowOpts, "{\"hud\":{\"width\":319,\"customHeight\":67}}");
            if (oobLowOpts.SavedStackWidth.HasValue || oobLowOpts.SavedCustomHeight.HasValue || oobLowOpts.Theme != "light")
                return false;

            HostOptions oobHighOpts = new HostOptions();
            ApplyHudConfigFromJson(oobHighOpts, "{\"hud\":{\"width\":961,\"customHeight\":4097}}");
            return !oobHighOpts.SavedStackWidth.HasValue && !oobHighOpts.SavedCustomHeight.HasValue && oobHighOpts.Theme == "light";
        }

        private static bool VerifyWorkAreaAndDpiCeilingLogic()
        {
            NativeMethods.RECT work = new NativeMethods.RECT { Left = 0, Top = 0, Right = 3440, Bottom = 1390 };
            double scale = 1.25;
            int topPhys = 124;
            int availCss = HudForm.ComputeAvailBelowCssPx(topPhys, work, scale);
            if (availCss != 1006) return false;

            int safeBottom = work.Bottom - HudForm.BottomWorkMarginPhys;
            for (int cssH = HudForm.MinStackLogicalHeight; cssH <= availCss; cssH++)
            {
                int physH = (int)Math.Ceiling(cssH * scale - 1e-6);
                if (physH / scale < cssH - 1e-9 || topPhys + physH > safeBottom) return false;
            }
            return true;
        }

        private static bool VerifyCloseGuardLogic()
        {
            if (!HudForm.ShouldBlockSysCommand(NativeMethods.SC_MINIMIZE, false)) return false;
            if (!HudForm.ShouldBlockSysCommand(NativeMethods.SC_MINIMIZE, true)) return false;
            if (!HudForm.ShouldBlockSysCommand(NativeMethods.SC_MAXIMIZE, false)) return false;
            if (!HudForm.ShouldBlockSysCommand(NativeMethods.SC_MAXIMIZE, true)) return false;
            if (!HudForm.ShouldBlockSysCommand(NativeMethods.SC_CLOSE, false)) return false;
            if (!HudForm.ShouldBlockSysCommand(NativeMethods.SC_CLOSE | 0x000F, false)) return false;
            if (HudForm.ShouldBlockSysCommand(NativeMethods.SC_CLOSE, true)) return false;
            if (HudForm.ShouldBlockSysCommand(0xF010, false)) return false;

            if (!HudForm.ShouldCancelFormClosing(false, CloseReason.UserClosing)) return false;
            if (!HudForm.ShouldCancelFormClosing(false, CloseReason.None)) return false;
            if (!HudForm.ShouldCancelFormClosing(false, CloseReason.ApplicationExitCall)) return false;
            if (HudForm.ShouldCancelFormClosing(false, CloseReason.WindowsShutDown)) return false;
            if (HudForm.ShouldCancelFormClosing(false, CloseReason.TaskManagerClosing)) return false;
            if (HudForm.ShouldCancelFormClosing(true, CloseReason.UserClosing)) return false;
            if (HudForm.ShouldCancelFormClosing(true, CloseReason.None)) return false;
            return true;
        }

        private static bool VerifyWorkAreaRecoveryLogic()
        {
            NativeMethods.RECT primaryWork = new NativeMethods.RECT { Left = 0, Top = 0, Right = 1920, Bottom = 1040 };
            int safeBottom = primaryWork.Bottom - HudForm.BottomWorkMarginPhys;

            // 1) Minimized sentinel rect (-32000, -32000) -> fallback to primary top-right
            NativeMethods.RECT minimizedRect = new NativeMethods.RECT { Left = -32000, Top = -32000, Right = -31840, Bottom = -31972 };
            int rx, ry, rw, rh;
            bool fallback;
            HudForm.ComputeRecoveredPhysicalRect(minimizedRect, true, primaryWork, primaryWork, 96, 388, 36,
                out rx, out ry, out rw, out rh, out fallback);
            if (!fallback || rw != 388 || rh != 36 || rx != (1920 - 388 - 16) || ry != 16)
                return false;

            // 2) Collapsed 0x0 rect -> fallback to primary top-right
            NativeMethods.RECT zeroRect = new NativeMethods.RECT { Left = 100, Top = 100, Right = 100, Bottom = 100 };
            HudForm.ComputeRecoveredPhysicalRect(zeroRect, true, primaryWork, primaryWork, 96, 388, 228,
                out rx, out ry, out rw, out rh, out fallback);
            if (!fallback || rw != 388 || rh != 228 || rx < primaryWork.Left || rx + rw > primaryWork.Right || ry < primaryWork.Top || ry + rh > safeBottom)
                return false;

            // 3) Disconnected secondary monitor rect -> clamped onto remaining primary work area
            NativeMethods.RECT disconnectedRect = new NativeMethods.RECT { Left = 3200, Top = 100, Right = 3588, Bottom = 328 };
            HudForm.ComputeRecoveredPhysicalRect(disconnectedRect, true, primaryWork, primaryWork, 96, 388, 228,
                out rx, out ry, out rw, out rh, out fallback);
            if (fallback || rx != (1920 - 388) || ry != 100 || rw != 388 || rh != 228)
                return false;

            // 4) Off-bottom rect -> clamped above safeBottom
            NativeMethods.RECT offBottomRect = new NativeMethods.RECT { Left = 1200, Top = 1100, Right = 1588, Bottom = 1328 };
            HudForm.ComputeRecoveredPhysicalRect(offBottomRect, true, primaryWork, primaryWork, 96, 388, 228,
                out rx, out ry, out rw, out rh, out fallback);
            if (fallback || rx != 1200 || ry + rh > safeBottom || ry < primaryWork.Top)
                return false;

            // 5) Config preload ignores poisoned sentinel coords (-32000) while preserving valid negative left-monitor coords (-1200)
            HostOptions poisonedOpts = new HostOptions();
            ApplyHudConfigFromJson(poisonedOpts, "{\"hud\":{\"x\":-32000,\"y\":-32000}}");
            if (poisonedOpts.InitialLogicalX.HasValue || poisonedOpts.InitialLogicalY.HasValue)
                return false;

            HostOptions leftMonOpts = new HostOptions();
            ApplyHudConfigFromJson(leftMonOpts, "{\"hud\":{\"x\":-1200,\"y\":80}}");
            if (leftMonOpts.InitialLogicalX != -1200 || leftMonOpts.InitialLogicalY != 80)
                return false;

            return true;
        }

        internal static bool IsAllowedLoopbackHttpUrl(string rawUrl)
        {
            if (string.IsNullOrWhiteSpace(rawUrl)) return false;
            Uri uri;
            if (!Uri.TryCreate(rawUrl.Trim(), UriKind.Absolute, out uri) || uri == null) return false;
            if (!string.Equals(uri.Scheme, Uri.UriSchemeHttp, StringComparison.OrdinalIgnoreCase)) return false;
            if (!string.IsNullOrEmpty(uri.UserInfo)) return false;
            if (uri.Port <= 0 || uri.Port > 65535) return false;
            string host = (uri.Host ?? string.Empty).Trim().ToLowerInvariant();
            if (host == "127.0.0.1" || host == "localhost" || host == "[::1]" || host == "::1")
                return true;
            if (uri.HostNameType == UriHostNameType.IPv6)
            {
                System.Net.IPAddress ip;
                if (System.Net.IPAddress.TryParse(uri.DnsSafeHost, out ip)
                    && ip != null
                    && System.Net.IPAddress.IPv6Loopback.Equals(ip))
                {
                    return true;
                }
            }
            return false;
        }

        private static bool VerifyLoopbackUrlGuardLogic()
        {
            string[] valid = new string[]
            {
                "http://127.0.0.1:21612/ui.html?mode=hud",
                "http://localhost:8080/ui.html?mode=hud",
                "http://[::1]:8080/ui.html?mode=hud",
                "http://127.0.0.1/",
            };
            foreach (string u in valid)
            {
                if (!IsAllowedLoopbackHttpUrl(u)) return false;
            }
            string[] invalid = new string[]
            {
                "",
                "   ",
                "https://127.0.0.1:21612/ui.html",
                "https://localhost:8080/",
                "http://example.com/ui.html",
                "http://198.51.100.10:8080/ui.html",
                "http://127.0.0.1.evil.com/ui.html",
                "http://127.0.0.1@evil.com/ui.html",
                "http://[::ffff:127.0.0.1]:8080/",
                "file:///C:/Windows/win.ini",
                "javascript:alert(1)",
            };
            foreach (string u in invalid)
            {
                if (IsAllowedLoopbackHttpUrl(u)) return false;
            }
            return true;
        }

        private static bool VerifyMultiMonitorDragAndDpiLogic()
        {
            // 1) Mixed-DPI right-secondary topology: Primary 2160x1440 @ 150% + Secondary 1920x1080 @ 100% at X=2160..4080
            NativeMethods.MonitorDisplayInfo pri150 = new NativeMethods.MonitorDisplayInfo
            {
                MonitorRect = new NativeMethods.RECT { Left = 0, Top = 0, Right = 2160, Bottom = 1440 },
                WorkRect = new NativeMethods.RECT { Left = 0, Top = 0, Right = 2160, Bottom = 1380 },
                Dpi = 144,
                IsPrimary = true
            };
            NativeMethods.MonitorDisplayInfo secRight100 = new NativeMethods.MonitorDisplayInfo
            {
                MonitorRect = new NativeMethods.RECT { Left = 2160, Top = 0, Right = 4080, Bottom = 1080 },
                WorkRect = new NativeMethods.RECT { Left = 2160, Top = 0, Right = 4080, Bottom = 1040 },
                Dpi = 96,
                IsPrimary = false
            };
            NativeMethods.MonitorDisplayInfo[] rightDual = new NativeMethods.MonitorDisplayInfo[] { pri150, secRight100 };

            // Dragging right across x=2160 seam: zero invisible wall
            NativeMethods.RECT crossRight = new NativeMethods.RECT { Left = 1950, Top = 100, Right = 2532, Bottom = 154 };
            HudForm.ClampMovingRectMultiMonitor(ref crossRight, rightDual, true, new NativeMethods.POINT { X = 2150, Y = 120 });
            if (crossRight.Left != 1950 || crossRight.Right != 2532 || crossRight.Top != 100) return false;

            // Dragging further onto secondary past outer right edge 4080: clamped cleanly to 4080
            NativeMethods.RECT pastFarRight = new NativeMethods.RECT { Left = 3900, Top = 100, Right = 4288, Bottom = 136 };
            HudForm.ClampMovingRectMultiMonitor(ref pastFarRight, rightDual, true, new NativeMethods.POINT { X = 4070, Y = 120 });
            if (pastFarRight.Right != 4080 || pastFarRight.Left != (4080 - 388)) return false;

            // Dragging back left across x=2160 seam from secondary to primary: zero wall
            NativeMethods.RECT crossBackLeft = new NativeMethods.RECT { Left = 1980, Top = 100, Right = 2368, Bottom = 136 };
            HudForm.ClampMovingRectMultiMonitor(ref crossBackLeft, rightDual, true, new NativeMethods.POINT { X = 2180, Y = 120 });
            if (crossBackLeft.Left != 1980 || crossBackLeft.Right != 2368) return false;

            // Dragging in bottom overhang of Primary (y=1200, below Secondary's 1080): does not poke into dead space
            NativeMethods.RECT overhangDrag = new NativeMethods.RECT { Left = 1900, Top = 1200, Right = 2482, Bottom = 1254 };
            HudForm.ClampMovingRectMultiMonitor(ref overhangDrag, rightDual, true, new NativeMethods.POINT { X = 2140, Y = 1220 });
            if (overhangDrag.Right != 2160 || overhangDrag.Left != (2160 - 582)) return false;

            // Tall stack straddling seam at y=850 (Bottom=1250 > secRight100 safeBottom 1034): no sudden vertical lurch while straddling
            NativeMethods.RECT tallStraddle = new NativeMethods.RECT { Left = 2000, Top = 850, Right = 2388, Bottom = 1250 };
            HudForm.ClampMovingRectMultiMonitor(ref tallStraddle, rightDual, true, new NativeMethods.POINT { X = 2190, Y = 865 });
            if (tallStraddle.Top != 850 || tallStraddle.Bottom != 1250) return false;

            // Once wholly inside secondary, tall stack lifts above secondary taskbar safeBottom (1034)
            NativeMethods.RECT tallInSec = new NativeMethods.RECT { Left = 2200, Top = 850, Right = 2588, Bottom = 1250 };
            HudForm.ClampMovingRectMultiMonitor(ref tallInSec, rightDual, true, new NativeMethods.POINT { X = 2300, Y = 865 });
            if (tallInSec.Bottom != (1040 - HudForm.BottomWorkMarginPhys) || tallInSec.Height != 400) return false;

            // Drop target selection: straddling window follows cursor onto secondary monitor (no snap-back to primary)
            NativeMethods.RECT straddleDrop = new NativeMethods.RECT { Left = 1750, Top = 100, Right = 2332, Bottom = 154 };
            if (HudForm.ResolveDropTargetMonitorIndex(straddleDrop, rightDual, true, new NativeMethods.POINT { X = 2220, Y = 120 }) != 1) return false;
            if (HudForm.ResolveDropTargetMonitorIndex(straddleDrop, rightDual, true, new NativeMethods.POINT { X = 2120, Y = 120 }) != 0) return false;

            // 2) Negative-X left-secondary topology: Secondary (-1920..0 @ 100%) + Primary (0..2160 @ 150%)
            NativeMethods.MonitorDisplayInfo secLeft100 = new NativeMethods.MonitorDisplayInfo
            {
                MonitorRect = new NativeMethods.RECT { Left = -1920, Top = 0, Right = 0, Bottom = 1080 },
                WorkRect = new NativeMethods.RECT { Left = -1920, Top = 0, Right = 0, Bottom = 1040 },
                Dpi = 96,
                IsPrimary = false
            };
            NativeMethods.MonitorDisplayInfo[] leftDual = new NativeMethods.MonitorDisplayInfo[] { pri150, secLeft100 };

            NativeMethods.RECT crossNegX = new NativeMethods.RECT { Left = -240, Top = 80, Right = 342, Bottom = 134 };
            HudForm.ClampMovingRectMultiMonitor(ref crossNegX, leftDual, true, new NativeMethods.POINT { X = -20, Y = 100 });
            if (crossNegX.Left != -240 || crossNegX.Right != 342) return false;

            NativeMethods.RECT pastNegLeft = new NativeMethods.RECT { Left = -2050, Top = 80, Right = -1662, Bottom = 116 };
            HudForm.ClampMovingRectMultiMonitor(ref pastNegLeft, leftDual, true, new NativeMethods.POINT { X = -1910, Y = 100 });
            if (pastNegLeft.Left != -1920 || pastNegLeft.Width != 388) return false;

            // 3) Negative-Y top-secondary topology: Secondary (0..-1080 @ 125%) + Primary (0..1440 @ 150%)
            NativeMethods.MonitorDisplayInfo secTop125 = new NativeMethods.MonitorDisplayInfo
            {
                MonitorRect = new NativeMethods.RECT { Left = 0, Top = -1080, Right = 1920, Bottom = 0 },
                WorkRect = new NativeMethods.RECT { Left = 0, Top = -1080, Right = 1920, Bottom = -40 },
                Dpi = 120,
                IsPrimary = false
            };
            NativeMethods.MonitorDisplayInfo[] topDual = new NativeMethods.MonitorDisplayInfo[] { pri150, secTop125 };

            NativeMethods.RECT crossNegY = new NativeMethods.RECT { Left = 400, Top = -200, Right = 885, Bottom = -155 };
            HudForm.ClampMovingRectMultiMonitor(ref crossNegY, topDual, true, new NativeMethods.POINT { X = 600, Y = -180 });
            if (crossNegY.Top != -200 || crossNegY.Bottom != -155) return false;

            NativeMethods.RECT crossDownFromTop = new NativeMethods.RECT { Left = 400, Top = -20, Right = 885, Bottom = 25 };
            HudForm.ClampMovingRectMultiMonitor(ref crossDownFromTop, topDual, true, new NativeMethods.POINT { X = 600, Y = -5 });
            if (crossDownFromTop.Top != -20 || crossDownFromTop.Bottom != 25) return false;

            // 4) Bottom-secondary topology: Primary (0..1080 @ 100%) + Secondary (0..1080..2160 @ 100%)
            NativeMethods.MonitorDisplayInfo pri100 = new NativeMethods.MonitorDisplayInfo
            {
                MonitorRect = new NativeMethods.RECT { Left = 0, Top = 0, Right = 1920, Bottom = 1080 },
                WorkRect = new NativeMethods.RECT { Left = 0, Top = 0, Right = 1920, Bottom = 1040 },
                Dpi = 96,
                IsPrimary = true
            };
            NativeMethods.MonitorDisplayInfo secBottom100 = new NativeMethods.MonitorDisplayInfo
            {
                MonitorRect = new NativeMethods.RECT { Left = 0, Top = 1080, Right = 1920, Bottom = 2160 },
                WorkRect = new NativeMethods.RECT { Left = 0, Top = 1080, Right = 1920, Bottom = 2120 },
                Dpi = 96,
                IsPrimary = false
            };
            NativeMethods.MonitorDisplayInfo[] bottomDual = new NativeMethods.MonitorDisplayInfo[] { pri100, secBottom100 };

            NativeMethods.RECT crossBottom = new NativeMethods.RECT { Left = 500, Top = 1060, Right = 888, Bottom = 1096 };
            HudForm.ClampMovingRectMultiMonitor(ref crossBottom, bottomDual, true, new NativeMethods.POINT { X = 650, Y = 1070 });
            if (crossBottom.Top != 1060 || crossBottom.Bottom != 1096) return false;

            // 5) Reverse mixed-DPI startup restoration (Primary 100% 0..1920 + Secondary 150% 1920..4080)
            //    Physical coords (2100, 100) must restore on Secondary @ 150% (width=582), never colliding with Primary 1400!
            NativeMethods.MonitorDisplayInfo secRight150 = new NativeMethods.MonitorDisplayInfo
            {
                MonitorRect = new NativeMethods.RECT { Left = 1920, Top = 0, Right = 4080, Bottom = 1440 },
                WorkRect = new NativeMethods.RECT { Left = 1920, Top = 0, Right = 4080, Bottom = 1380 },
                Dpi = 144,
                IsPrimary = false
            };
            NativeMethods.MonitorDisplayInfo[] revMixed = new NativeMethods.MonitorDisplayInfo[] { pri100, secRight150 };
            int ix, iy, iw, ih;
            uint idpi;
            NativeMethods.RECT iwork;
            string ianchor;
            bool usedSaved;
            HudForm.ResolveInitialPhysicalBounds(
                2100, 100, 1400, 67, 388, 36, revMixed, pri100,
                out ix, out iy, out iw, out ih, out idpi, out iwork, out ianchor, out usedSaved);
            if (!usedSaved || ix != 2100 || iy != 100 || iw != 582 || ih != 54 || idpi != 144 || iwork.Left != 1920) return false;

            // 6) Startup restoration on negative-X left monitor & negative-Y top monitor
            HudForm.ResolveInitialPhysicalBounds(
                -1200, 80, -1200, 80, 388, 36, leftDual, pri150,
                out ix, out iy, out iw, out ih, out idpi, out iwork, out ianchor, out usedSaved);
            if (!usedSaved || ix != -1200 || iy != 80 || iw != 388 || ih != 36 || idpi != 96 || iwork.Left != -1920) return false;

            HudForm.ResolveInitialPhysicalBounds(
                400, -800, 320, -640, 388, 36, topDual, pri150,
                out ix, out iy, out iw, out ih, out idpi, out iwork, out ianchor, out usedSaved);
            if (!usedSaved || ix != 400 || iy != -800 || iw != 485 || ih != 45 || idpi != 120 || iwork.Top != -1080) return false;

            // 7) Legacy logical config restoration on Primary 150% + Secondary 100% (x=3600 would overshoot 4080 if multiplied by 1.5!)
            HudForm.ResolveInitialPhysicalBounds(
                null, null, 3600, 20, 388, 36, rightDual, pri150,
                out ix, out iy, out iw, out ih, out idpi, out iwork, out ianchor, out usedSaved);
            if (!usedSaved || ix != 3600 || iy != 20 || iw != 388 || ih != 36 || idpi != 96 || iwork.Left != 2160) return false;

            // 8) Disconnected secondary monitor -> falls back cleanly to cursor/primary monitor top-right
            NativeMethods.MonitorDisplayInfo[] singleOnly = new NativeMethods.MonitorDisplayInfo[] { pri150 };
            HudForm.ResolveInitialPhysicalBounds(
                3600, 20, 3600, 20, 388, 36, singleOnly, pri150,
                out ix, out iy, out iw, out ih, out idpi, out iwork, out ianchor, out usedSaved);
            if (usedSaved || idpi != 144 || iw != 582 || ih != 54 || ix != (2160 - 582 - 24) || iy != 24 || ianchor != "top-right") return false;

            // 9) Config preload parses physX/physY (including deep negative coords) and rejects minimized sentinel -32000
            HostOptions physOpts = new HostOptions();
            ApplyHudConfigFromJson(physOpts, "{\"hud\":{\"x\":-1200,\"y\":80,\"physX\":-12000,\"physY\":-800}}");
            if (physOpts.InitialPhysicalX != -12000 || physOpts.InitialPhysicalY != -800) return false;

            HostOptions sentinelPhys = new HostOptions();
            ApplyHudConfigFromJson(sentinelPhys, "{\"hud\":{\"physX\":-32000,\"physY\":-32000}}");
            if (sentinelPhys.InitialPhysicalX.HasValue || sentinelPhys.InitialPhysicalY.HasValue) return false;

            // 10) Expand/collapse on negative-Y secondary monitor stays inside that monitor's work area
            NativeMethods.RECT negOld = new NativeMethods.RECT { Left = 400, Top = -900, Right = 885, Bottom = -855 };
            int? negNudge = null;
            int negOutY, negReqH = 300;
            HudForm.ComputeLockedTopVerticalBounds(negOld, secTop125.WorkRect, 1.25, true, false, 85, ref negNudge, out negOutY, ref negReqH);
            if (negOutY != -900 || negReqH != 300 || (negOutY + negReqH) > HudForm.GetSafeWorkBottom(secTop125.WorkRect)) return false;

            // 11) Legacy mixed-DPI startup near seam (x=1800 on Pri 100% 0..1920 whose center 1994 lands on Sec 150% 1920..4080):
            //     must recompute physW/physH at Sec's 144 DPI (582x54), not leave 388x36 paired with outDpi=144!
            HudForm.ResolveInitialPhysicalBounds(
                null, null, 1800, 100, 388, 36, revMixed, pri100,
                out ix, out iy, out iw, out ih, out idpi, out iwork, out ianchor, out usedSaved);
            if (!usedSaved || idpi != 144 || iw != 582 || ih != 54 || ix != 1920 || iy != 100 || iwork.Left != 1920) return false;

            // 12) Round-trip cross-DPI drag (125% -> 100% -> 125%) with +-2px rounding drift must reconcile back to exact 485x45
            NativeMethods.RECT rtBefore = new NativeMethods.RECT { Left = 400, Top = -900, Right = 885, Bottom = -855 };
            NativeMethods.RECT rtAfterDrifted = new NativeMethods.RECT { Left = 420, Top = -880, Right = 903, Bottom = -836 };
            int sxOut, syOut, swOut, shOut;
            bool didReconcile;
            HudForm.ComputeSettledDragRect(
                rtBefore, rtAfterDrifted, 120, 120, 120, 120, true, 388, 36, secTop125.WorkRect,
                true, new NativeMethods.POINT { X = 660, Y = -860 },
                out sxOut, out syOut, out swOut, out shOut, out didReconcile);
            if (!didReconcile || swOut != 485 || shOut != 45 || sxOut < secTop125.WorkRect.Left || (sxOut + swOut) > secTop125.WorkRect.Right) return false;

            // Also test round-trip drift detection even if sawDpiChangeInDrag were false (via width/height mismatch vs beforeRect)
            HudForm.ComputeSettledDragRect(
                rtBefore, rtAfterDrifted, 120, 120, 120, 120, false, 388, 36, secTop125.WorkRect,
                false, new NativeMethods.POINT(),
                out sxOut, out syOut, out swOut, out shOut, out didReconcile);
            if (!didReconcile || swOut != 485 || shOut != 45) return false;

            return true;
        }

        private static int RunSelfTest(HostOptions opts)
        {
            try
            {
                string wvVer = CoreWebView2Environment.GetAvailableBrowserVersionString();
                uint primaryDpi;
                NativeMethods.RECT work = NativeMethods.GetPrimaryWorkArea(out primaryDpi);
                uint cursorDpi;
                NativeMethods.RECT cursorWork = NativeMethods.GetCursorOrPrimaryWorkArea(out cursorDpi);
                bool cursorWorkAreaOk = cursorWork.Width > 0 && cursorWork.Height > 0 && cursorDpi >= 72 && cursorDpi <= 576;
                NativeMethods.MonitorDisplayInfo[] liveMonitors = NativeMethods.GetAllMonitors();
                bool liveMonitorsOk = liveMonitors != null && liveMonitors.Length >= 1;
                bool dwm = NativeMethods.IsDwmActive();

                Dictionary<string, int> edgeMap;
                bool edgeOk = VerifyEdgeHitTestMappings(out edgeMap);
                bool lockTopOk = VerifyLockTopAndAnchorLogic();
                bool configPreloadOk = VerifyConfigPreloadLogic();
                bool workAreaDpiOk = VerifyWorkAreaAndDpiCeilingLogic();
                bool closeGuardOk = VerifyCloseGuardLogic();
                bool workAreaRecoveryOk = VerifyWorkAreaRecoveryLogic();
                bool loopbackUrlGuardOk = VerifyLoopbackUrlGuardLogic();
                bool multiMonitorDragOk = VerifyMultiMonitorDragAndDpiLogic();
                int mmiSize = Marshal.SizeOf(typeof(NativeMethods.MINMAXINFO));
                bool mmiOk = (mmiSize == 40);

                bool lowMemEnumOk = ((int)CoreWebView2MemoryUsageTargetLevel.Low == 1);
                bool equalWidthOk = string.Equals(HudForm.HostVersion, "3.8.3", StringComparison.Ordinal)
                    && (HudForm.DefaultPillLogicalWidth == HudForm.DefaultStackLogicalWidth)
                    && (HudForm.DefaultPillLogicalWidth == 388)
                    && (HudForm.MinPillLogicalWidth == HudForm.MinStackLogicalWidth)
                    && (HudForm.MinPillLogicalWidth == 320)
                    && (HudForm.DefaultPillLogicalHeight == 36)
                    && (HudForm.MinPillLogicalHeight == 34)
                    && (HudForm.MinStackLogicalHeight == 68)
                    && string.Equals(opts.Theme, "light", StringComparison.Ordinal);
                bool argsOk = HudForm.LightweightBrowserArgs.IndexOf("SpareRendererForSitePerProcess", StringComparison.Ordinal) >= 0
                    && HudForm.LightweightBrowserArgs.IndexOf("--renderer-process-limit=1", StringComparison.Ordinal) >= 0
                    && HudForm.LightweightBrowserArgs.IndexOf("disable-" + "gpu", StringComparison.OrdinalIgnoreCase) < 0;

                string testMutexName = MutexName + "_SelfTest_" + System.Diagnostics.Process.GetCurrentProcess().Id.ToString(CultureInfo.InvariantCulture);
                bool mutexOk = false, secondBlocked = false;
                IntPtr h1 = NativeMethods.CreateMutexW(IntPtr.Zero, false, testMutexName);
                if (h1 != IntPtr.Zero)
                {
                    try
                    {
                        uint waitRc = NativeMethods.WaitForSingleObject(h1, 0);
                        mutexOk = (waitRc == NativeMethods.WAIT_OBJECT_0 || waitRc == NativeMethods.WAIT_ABANDONED);
                        if (mutexOk)
                        {
                            try
                            {
                                IntPtr h2 = NativeMethods.CreateMutexW(IntPtr.Zero, false, testMutexName);
                                if (h2 != IntPtr.Zero)
                                {
                                    try
                                    {
                                        bool existsFlag = (Marshal.GetLastWin32Error() == 183);
                                        uint crossThreadWait = 0;
                                        Thread contender = new Thread(delegate() { crossThreadWait = NativeMethods.WaitForSingleObject(h2, 0); });
                                        contender.IsBackground = true;
                                        contender.Start();
                                        contender.Join(1000);
                                        secondBlocked = existsFlag && (crossThreadWait == NativeMethods.WAIT_TIMEOUT);
                                    }
                                    finally { NativeMethods.CloseHandle(h2); }
                                }
                            }
                            finally { NativeMethods.ReleaseMutex(h1); }
                        }
                    }
                    finally { NativeMethods.CloseHandle(h1); }
                }

                bool prodMutexAlreadyHeld = false;
                IntPtr hProd = NativeMethods.CreateMutexW(IntPtr.Zero, false, MutexName);
                if (hProd != IntPtr.Zero)
                {
                    prodMutexAlreadyHeld = (Marshal.GetLastWin32Error() == 183);
                    NativeMethods.CloseHandle(hProd);
                }

                bool allOk = !string.IsNullOrEmpty(wvVer)
                    && mutexOk && secondBlocked && edgeOk && lockTopOk
                    && configPreloadOk && workAreaDpiOk && mmiOk && lowMemEnumOk && equalWidthOk && argsOk
                    && closeGuardOk && workAreaRecoveryOk && cursorWorkAreaOk && loopbackUrlGuardOk
                    && liveMonitorsOk && multiMonitorDragOk;

                int resolvedStackWidth = (opts.SavedStackWidth.HasValue && opts.SavedStackWidth.Value >= HudForm.MinStackLogicalWidth && opts.SavedStackWidth.Value <= 960)
                    ? opts.SavedStackWidth.Value
                    : HudForm.DefaultStackLogicalWidth;
                double curScale = (primaryDpi >= 72 ? primaryDpi : 96u) / 96.0;
                int curTopPhys = opts.InitialLogicalY.HasValue
                    ? (int)Math.Round(opts.InitialLogicalY.Value * curScale)
                    : (work.Top + (int)Math.Round(16 * curScale));

                Dictionary<string, object> info = new Dictionary<string, object>();
                info["ok"] = allOk;
                info["version"] = HudForm.HostVersion;
                info["webview2Version"] = wvVer;
                info["memoryUsageTargetLevel"] = lowMemEnumOk ? "Low" : "Unknown";
                info["lightweightBrowserArgsOk"] = argsOk;
                info["mutexName"] = MutexName;
                info["mutexAcquired"] = mutexOk;
                info["duplicateDetected"] = secondBlocked;
                info["productionHudCurrentlyActive"] = prodMutexAlreadyHeld;
                info["edgeHitTestOk"] = edgeOk;
                info["edgeHitTestMap"] = edgeMap;
                info["lockTopAnchorOk"] = lockTopOk;
                info["configPreloadOk"] = configPreloadOk;
                info["workAreaDpiOk"] = workAreaDpiOk;
                info["closeGuardOk"] = closeGuardOk;
                info["workAreaRecoveryOk"] = workAreaRecoveryOk;
                info["loopbackUrlGuardOk"] = loopbackUrlGuardOk;
                info["cursorWorkAreaOk"] = cursorWorkAreaOk;
                info["liveMonitorCount"] = liveMonitors != null ? liveMonitors.Length : 0;
                info["multiMonitorDragOk"] = multiMonitorDragOk;
                info["equalWidthOk"] = equalWidthOk;
                info["minMaxInfoSize"] = mmiSize;
                info["defaultStackWidth"] = HudForm.DefaultStackLogicalWidth;
                info["defaultPillWidth"] = HudForm.DefaultPillLogicalWidth;
                info["defaultPillHeight"] = HudForm.DefaultPillLogicalHeight;
                info["resolvedStackWidth"] = resolvedStackWidth;
                info["resolvedCustomHeight"] = opts.SavedCustomHeight;
                info["resolvedAnchor"] = opts.AnchorCorner;
                info["resolvedTheme"] = opts.Theme;
                info["minStackSize"] = new int[] { HudForm.MinStackLogicalWidth, HudForm.MinStackLogicalHeight };
                info["minPillSize"] = new int[] { HudForm.MinPillLogicalWidth, HudForm.MinPillLogicalHeight };
                info["dwmEnabled"] = dwm;
                info["primaryDpi"] = primaryDpi;
                info["workArea"] = new int[] { work.Left, work.Top, work.Right, work.Bottom };
                info["bottomWorkMarginPhys"] = HudForm.BottomWorkMarginPhys;
                info["availBelowCssPxAtSavedY"] = HudForm.ComputeAvailBelowCssPx(curTopPhys, work, curScale);
                info["is64BitProcess"] = Environment.Is64BitProcess;
                info["tokenPresent"] = !string.IsNullOrEmpty(opts.HudToken);
                Console.WriteLine(new JavaScriptSerializer().Serialize(info));
                return allOk ? 0 : 1;
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine("SELF_TEST_ERROR: " + ex.Message);
                return 1;
            }
        }
    }
}
