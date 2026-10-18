using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Automation;
using System.Windows.Automation.Text;
using System.Windows.Forms;

// A single request per process keeps unresponsive third-party UIA providers
// disposable. The Python parent owns this process and its deadline.
internal static class DesktopAgent {
    private static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = Int32.MaxValue, RecursionLimit = 200 };
    private static bool dispatched;
    private static string method = "none";
    private delegate bool EnumCallback(IntPtr window, IntPtr data);
    [StructLayout(LayoutKind.Sequential)] private struct Rect { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] private struct Point { public int X, Y; }
    [StructLayout(LayoutKind.Sequential)] private struct MouseInput { public int X, Y; public uint Data, Flags, Time; public UIntPtr Extra; }
    [StructLayout(LayoutKind.Sequential)] private struct KeyboardInput { public ushort Key, Scan; public uint Flags, Time; public UIntPtr Extra; }
    [StructLayout(LayoutKind.Explicit)] private struct InputUnion { [FieldOffset(0)] public MouseInput Mouse; [FieldOffset(0)] public KeyboardInput Keyboard; }
    [StructLayout(LayoutKind.Sequential)] private struct Input { public uint Type; public InputUnion Value; }
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumCallback callback, IntPtr data);
    [DllImport("user32.dll")] private static extern bool IsWindow(IntPtr window);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr window);
    [DllImport("user32.dll")] private static extern bool IsIconic(IntPtr window);
    [DllImport("user32.dll")] private static extern bool IsZoomed(IntPtr window);
    [DllImport("user32.dll")] private static extern IntPtr GetWindow(IntPtr window, uint command);
    [DllImport("user32.dll")] private static extern IntPtr GetAncestor(IntPtr window, uint flags);
    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetWindowText(IntPtr window, StringBuilder text, int count);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClassName(IntPtr window, StringBuilder text, int count);
    [DllImport("user32.dll")] private static extern bool GetWindowRect(IntPtr window, out Rect rect);
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr window);
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr window, int command);
    [DllImport("user32.dll")] private static extern bool SetWindowPos(IntPtr window, IntPtr after, int x, int y, int width, int height, uint flags);
    [DllImport("user32.dll", SetLastError = true)] private static extern bool PostMessage(IntPtr window, uint message, IntPtr first, IntPtr second);
    [DllImport("user32.dll")] private static extern bool AttachThreadInput(uint source, uint target, bool attach);
    [DllImport("kernel32.dll")] private static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] private static extern IntPtr WindowFromPoint(Point point);
    [DllImport("user32.dll", SetLastError = true)] private static extern uint SendInput(uint count, Input[] inputs, int size);
    [DllImport("user32.dll")] private static extern int GetSystemMetrics(int index);
    [DllImport("user32.dll")] private static extern uint GetDpiForWindow(IntPtr window);
    [DllImport("user32.dll")] private static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] private static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] private static extern bool PrintWindow(IntPtr window, IntPtr dc, uint flags);
    [DllImport("user32.dll", SetLastError = true)] private static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);
    [DllImport("user32.dll")] private static extern bool CloseDesktop(IntPtr desktop);
    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern bool GetUserObjectInformation(IntPtr handle, int index, StringBuilder text, int length, out int needed);
    [DllImport("wtsapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern bool WTSQuerySessionInformation(IntPtr server, int session, int information, out IntPtr buffer, out int bytes);
    [DllImport("wtsapi32.dll")] private static extern void WTSFreeMemory(IntPtr buffer);
    [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
    [DllImport("advapi32.dll", SetLastError = true)] private static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
    [DllImport("advapi32.dll", SetLastError = true)] private static extern bool GetTokenInformation(IntPtr token, int information, IntPtr buffer, int length, out int needed);
    [DllImport("advapi32.dll")] private static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);
    [DllImport("advapi32.dll")] private static extern IntPtr GetSidSubAuthority(IntPtr sid, uint index);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern short VkKeyScan(char character);

    private sealed class Failure : Exception {
        public string Code;
        public Failure(string code, string message) : base(message) { Code = code; }
    }
    private static Dictionary<string, object> Obj(params object[] pairs) {
        var result = new Dictionary<string, object>();
        for (int i = 0; i < pairs.Length; i += 2) result[(string)pairs[i]] = pairs[i + 1];
        return result;
    }
    private static object Get(Dictionary<string, object> args, string name, object fallback = null) { object value; return args.TryGetValue(name, out value) ? value : fallback; }
    private static string Str(Dictionary<string, object> args, string name, string fallback = "") { return Convert.ToString(Get(args, name, fallback), CultureInfo.InvariantCulture); }
    private static int Num(Dictionary<string, object> args, string name, int fallback = 0) { return Convert.ToInt32(Get(args, name, fallback), CultureInfo.InvariantCulture); }
    private static double Number(Dictionary<string, object> args, string name, double fallback = 0) { return Convert.ToDouble(Get(args, name, fallback), CultureInfo.InvariantCulture); }
    private static bool Bool(Dictionary<string, object> args, string name, bool fallback = false) { return Convert.ToBoolean(Get(args, name, fallback), CultureInfo.InvariantCulture); }
    private static Dictionary<string, object> Dict(object value) { return value as Dictionary<string, object>; }
    private static IEnumerable<object> Array(object value) { return value as IEnumerable<object> ?? new object[0]; }
    private static void Dispatch(string used) {
        method = used;
        // Flush before crossing the mutation boundary. A timeout after this
        // receipt has an unknown outcome and must never be replayed blindly.
        Console.Error.WriteLine("{\"phase\":\"dispatching\",\"method\":" + Json.Serialize(used) + "}");
        Console.Error.Flush();
        dispatched = true;
    }
    private static Dictionary<string, object> Success(params object[] pairs) {
        var result = Obj(pairs); result["ok"] = true;
        result["method"] = method; result["outcome"] = dispatched ? "dispatched" : "observed";
        return result;
    }
    private static Rect Bounds(IntPtr window) {
        Rect rect;
        if (!GetWindowRect(window, out rect)) throw new Failure("window_unavailable", "Cannot read the current window bounds.");
        return rect;
    }
    private static Dictionary<string, object> RectJson(Rect rect) { return Obj("left", rect.Left, "top", rect.Top, "width", rect.Right - rect.Left, "height", rect.Bottom - rect.Top); }
    private static int Integrity(Process process) {
        IntPtr token = IntPtr.Zero, buffer = IntPtr.Zero;
        try {
            if (!OpenProcessToken(process.Handle, 8, out token)) return -1;
            int length;
            GetTokenInformation(token, 25, IntPtr.Zero, 0, out length);
            buffer = Marshal.AllocHGlobal(length);
            if (!GetTokenInformation(token, 25, buffer, length, out length)) return -1;
            IntPtr sid = Marshal.ReadIntPtr(buffer);
            byte count = Marshal.ReadByte(GetSidSubAuthorityCount(sid));
            return Marshal.ReadInt32(GetSidSubAuthority(sid, (uint)(count - 1)));
        } catch { return -1; }
        finally { if (buffer != IntPtr.Zero) Marshal.FreeHGlobal(buffer); if (token != IntPtr.Zero) CloseHandle(token); }
    }
    private static Dictionary<string, object> WindowInfo(IntPtr window) {
        uint pid; GetWindowThreadProcessId(window, out pid);
        var title = new StringBuilder(4096); GetWindowText(window, title, title.Capacity);
        var name = new StringBuilder(256); GetClassName(window, name, name.Capacity);
        string start = null, path = null; int session = -1, integrity = -1;
        try { using (var process = Process.GetProcessById((int)pid)) {
            start = process.StartTime.ToUniversalTime().Ticks.ToString(CultureInfo.InvariantCulture);
            session = process.SessionId; integrity = Integrity(process);
            try { path = process.MainModule.FileName; } catch { }
        } } catch { }
        uint dpi = 0; try { dpi = GetDpiForWindow(window); } catch (EntryPointNotFoundException) { }
        return Obj("hwnd", window.ToInt64().ToString(CultureInfo.InvariantCulture), "pid", (int)pid,
                   "processStartTicks", start, "title", title.ToString(), "className", name.ToString(),
                   "executable", path, "sessionId", session, "integrity", integrity,
                   "visible", IsWindowVisible(window), "minimized", IsIconic(window), "maximized", IsZoomed(window),
                   "owner", GetWindow(window, 4).ToInt64().ToString(CultureInfo.InvariantCulture), "bounds", RectJson(Bounds(window)), "dpi", dpi);
    }
    private static IntPtr ResolveWindow(Dictionary<string, object> args, bool required = true) {
        var identity = Dict(Get(args, "window"));
        if (identity == null) { if (required) throw new Failure("invalid_arguments", "A complete returned window identity is required."); return IntPtr.Zero; }
        long native;
        if (!Int64.TryParse(Str(identity, "hwnd"), out native)) throw new Failure("invalid_arguments", "Invalid window handle.");
        var window = new IntPtr(native);
        if (!IsWindow(window)) throw new Failure("stale_window", "The observed window no longer exists.");
        uint pid; GetWindowThreadProcessId(window, out pid);
        if ((int)pid != Num(identity, "pid", -1)) throw new Failure("stale_window", "The window belongs to a different process.");
        try { using (var process = Process.GetProcessById((int)pid)) {
            if (process.StartTime.ToUniversalTime().Ticks.ToString(CultureInfo.InvariantCulture) != Str(identity, "processStartTicks"))
                throw new Failure("stale_window", "The process identity changed since observation.");
        } } catch (Failure) { throw; } catch (Exception error) { throw new Failure("process_identity_unavailable", error.Message); }
        return window;
    }
    private static List<Dictionary<string, object>> Windows() {
        var windows = new List<Dictionary<string, object>>();
        EnumWindows((window, ignored) => {
            try { windows.Add(WindowInfo(window)); } catch { }
            return true;
        }, IntPtr.Zero);
        return windows;
    }
    private static Dictionary<string, object> Session() {
        int session = Process.GetCurrentProcess().SessionId;
        object locked = null; int connection = -1; string queryError = null;
        IntPtr data = IntPtr.Zero; int bytes;
        try {
            if (WTSQuerySessionInformation(IntPtr.Zero, session, 25, out data, out bytes) && bytes >= 20 && Marshal.ReadInt32(data, 0) == 1) {
                connection = Marshal.ReadInt32(data, 12);
                int flags = Marshal.ReadInt32(data, 16);
                if (flags == 0 || flags == 1) locked = flags == 0;
            } else queryError = "WTS session information unavailable: " + Marshal.GetLastWin32Error();
        } finally { if (data != IntPtr.Zero) WTSFreeMemory(data); }
        string desktopName = null; int desktopError = 0;
        IntPtr desktop = OpenInputDesktop(0, false, 1);
        if (desktop != IntPtr.Zero) {
            try { int needed; var name = new StringBuilder(256); if (GetUserObjectInformation(desktop, 2, name, name.Capacity * 2, out needed)) desktopName = name.ToString(); }
            finally { CloseDesktop(desktop); }
        } else desktopError = Marshal.GetLastWin32Error();
        bool interactive = !(locked is bool && (bool)locked) && session != 0 && connection != 4 && desktopName != null && String.Equals(desktopName, "Default", StringComparison.OrdinalIgnoreCase);
        return Obj("sessionId", session, "locked", locked, "connectionState", connection, "inputDesktop", desktopName,
                   "desktopError", desktopError, "queryError", queryError, "canInject", interactive,
                   "integrity", Integrity(Process.GetCurrentProcess()));
    }
    private static void RequireInteractive(IntPtr window) {
        var session = Session();
        if (!Bool(session, "canInject")) throw new Failure("no_interactive_desktop", "Physical input requires an unlocked interactive input desktop. Semantic UIA operations remain available where supported.");
        if (window != IntPtr.Zero) {
            uint pid; GetWindowThreadProcessId(window, out pid);
            using (var process = Process.GetProcessById((int)pid)) {
                if (process.SessionId != Num(session, "sessionId")) throw new Failure("session_mismatch", "The target is in another Windows session.");
                int targetIntegrity = Integrity(process), own = Num(session, "integrity", -1);
                if (own >= 0 && targetIntegrity > own) throw new Failure("integrity_mismatch", "Windows input injection cannot target this higher-integrity process from the current token.");
            }
        }
    }
    private static void Foreground(IntPtr window) {
        RequireInteractive(window);
        if (window == IntPtr.Zero) return;
        if (GetForegroundWindow() == window) return;
        Dispatch("win32.foreground");
        if (IsIconic(window)) ShowWindow(window, 9);
        uint ignored;
        uint own = GetCurrentThreadId(), target = GetWindowThreadProcessId(window, out ignored);
        uint current = GetWindowThreadProcessId(GetForegroundWindow(), out ignored);
        bool attachedCurrent = current != 0 && current != own && AttachThreadInput(own, current, true);
        bool attachedTarget = target != own && target != current && AttachThreadInput(own, target, true);
        try { SetForegroundWindow(window); }
        finally { if (attachedTarget) AttachThreadInput(own, target, false); if (attachedCurrent) AttachThreadInput(own, current, false); }
        for (int attempt = 0; attempt < 10; attempt++) { if (GetForegroundWindow() == window) return; Thread.Sleep(30); }
        throw new Failure("foreground_denied", "Windows did not transfer foreground focus to the selected window.");
    }
    private static AutomationElement Root(IntPtr window) {
        var root = AutomationElement.FromHandle(window);
        if (root == null) throw new Failure("provider_unavailable", "UI Automation did not expose the selected window.");
        return root;
    }
    private static bool SameRuntime(AutomationElement element, int[] identity) {
        try { return element.GetRuntimeId().SequenceEqual(identity); } catch (ElementNotAvailableException) { return false; }
    }
    private static AutomationElement ResolveElement(IntPtr window, Dictionary<string, object> args) {
        var reference = Dict(Get(args, "element"));
        if (reference == null) return Root(window);
        int[] runtime = Array(Get(reference, "runtimeId")).Select(value => Convert.ToInt32(value, CultureInfo.InvariantCulture)).ToArray();
        if (runtime.Length == 0) throw new Failure("stale_element", "Missing observed UI Automation runtime identity.");
        var queue = new Queue<AutomationElement>(); queue.Enqueue(Root(window));
        while (queue.Count > 0) {
            var item = queue.Dequeue();
            if (SameRuntime(item, runtime)) return item;
            for (var child = TreeWalker.RawViewWalker.GetFirstChild(item); child != null; child = TreeWalker.RawViewWalker.GetNextSibling(child)) queue.Enqueue(child);
        }
        throw new Failure("stale_element", "The observed UI Automation element no longer exists. Inspect again before acting.");
    }
    private static T Pattern<T>(AutomationElement element, AutomationPattern pattern) where T : class {
        object value;
        if (!element.TryGetCurrentPattern(pattern, out value)) throw new Failure("unsupported_pattern", "The selected control does not expose " + pattern.ProgrammaticName + ".");
        return (T)value;
    }
    private static Dictionary<string, object> ElementInfo(AutomationElement item, int id, int parent, int depth, int textOffset, int maxTextChars) {
        var current = item.Current;
        var bounds = current.BoundingRectangle;
        int[] runtime = null; try { runtime = item.GetRuntimeId(); } catch { }
        var result = Obj("id", id, "parentId", parent, "depth", depth, "runtimeId", runtime,
                         "name", current.Name, "automationId", current.AutomationId, "className", current.ClassName,
                         "controlType", current.ControlType.ProgrammaticName, "enabled", current.IsEnabled,
                         "offscreen", current.IsOffscreen, "hasFocus", current.HasKeyboardFocus,
                         "bounds", Obj("left", bounds.IsEmpty ? 0 : bounds.Left, "top", bounds.IsEmpty ? 0 : bounds.Top,
                                       "width", bounds.IsEmpty ? 0 : bounds.Width, "height", bounds.IsEmpty ? 0 : bounds.Height),
                         "patterns", item.GetSupportedPatterns().Select(pattern => pattern.ProgrammaticName).ToArray());
        object value;
        if (item.TryGetCurrentPattern(ValuePattern.Pattern, out value)) { result["value"] = ((ValuePattern)value).Current.Value; result["readOnly"] = ((ValuePattern)value).Current.IsReadOnly; }
        if (item.TryGetCurrentPattern(RangeValuePattern.Pattern, out value)) { var range = ((RangeValuePattern)value).Current; result["range"] = Obj("value", range.Value, "minimum", range.Minimum, "maximum", range.Maximum, "readOnly", range.IsReadOnly); }
        if (item.TryGetCurrentPattern(TogglePattern.Pattern, out value)) result["toggleState"] = ((TogglePattern)value).Current.ToggleState.ToString();
        if (item.TryGetCurrentPattern(SelectionItemPattern.Pattern, out value)) result["selected"] = ((SelectionItemPattern)value).Current.IsSelected;
        if (item.TryGetCurrentPattern(ExpandCollapsePattern.Pattern, out value)) result["expandState"] = ((ExpandCollapsePattern)value).Current.ExpandCollapseState.ToString();
        if (item.TryGetCurrentPattern(TextPattern.Pattern, out value)) {
            var text = (TextPattern)value;
            var range = text.DocumentRange.Clone();
            if (textOffset > 0) range.MoveEndpointByUnit(TextPatternRangeEndpoint.Start, TextUnit.Character, textOffset);
            string content = range.GetText(maxTextChars == Int32.MaxValue ? maxTextChars : maxTextChars + 1);
            bool truncated = content.Length > maxTextChars;
            result["text"] = truncated ? content.Substring(0, maxTextChars) : content;
            result["textOffset"] = textOffset;
            result["nextTextOffset"] = truncated ? (object)(textOffset + maxTextChars) : null;
            try { result["selection"] = text.GetSelection().Select(selection => selection.GetText(maxTextChars)).ToArray(); } catch { }
        }
        return result;
    }
    private static Dictionary<string, object> Inspect(Dictionary<string, object> args) {
        var window = ResolveWindow(args);
        int maximum = Num(args, "maxNodes", 500), depth = Num(args, "maxDepth", 8), offset = Num(args, "offset");
        if (maximum < 1 || depth < 0 || offset < 0) throw new Failure("invalid_arguments", "Invalid inspection bounds.");
        var walker = Str(args, "view", "control") == "raw" ? TreeWalker.RawViewWalker : Str(args, "view", "control") == "content" ? TreeWalker.ContentViewWalker : TreeWalker.ControlViewWalker;
        var queue = new Queue<Tuple<AutomationElement, int, int>>(); queue.Enqueue(Tuple.Create(Root(window), -1, 0));
        var elements = new List<Dictionary<string, object>>(); int index = 0; bool limited = false;
        while (queue.Count > 0) {
            var entry = queue.Dequeue(); int id = index++;
            try {
                if (id >= offset) elements.Add(ElementInfo(entry.Item1, id, entry.Item2, entry.Item3, Num(args, "textOffset"), Num(args, "maxTextChars", 16000)));
                if (elements.Count >= maximum) { limited = true; break; }
                if (entry.Item3 < depth) {
                    for (var child = walker.GetFirstChild(entry.Item1); child != null; child = walker.GetNextSibling(child)) queue.Enqueue(Tuple.Create(child, id, entry.Item3 + 1));
                }
            } catch (ElementNotAvailableException) { }
        }
        method = "uia.inspect";
        return Success("window", WindowInfo(window), "elements", elements, "nextOffset", limited ? (object)index : null, "maxDepth", depth, "view", Str(args, "view", "control"));
    }
    private static string Quote(string value) {
        var text = new StringBuilder("\""); int slashes = 0;
        foreach (char ch in value) {
            if (ch == '\\') { slashes++; continue; }
            if (ch == '"') { text.Append('\\', slashes * 2 + 1); text.Append(ch); }
            else { text.Append('\\', slashes); text.Append(ch); }
            slashes = 0;
        }
        text.Append('\\', slashes * 2); text.Append('"'); return text.ToString();
    }
    private static Dictionary<string, object> Launch(Dictionary<string, object> args) {
        string application = Str(args, "application");
        if (String.IsNullOrWhiteSpace(application)) throw new Failure("invalid_arguments", "application is required.");
        var info = new ProcessStartInfo(application, String.Join(" ", Array(Get(args, "arguments")).Select(value => Quote(Convert.ToString(value))).ToArray()));
        info.UseShellExecute = Bool(args, "shell");
        if (!String.IsNullOrEmpty(Str(args, "workingDirectory"))) info.WorkingDirectory = Str(args, "workingDirectory");
        Dispatch("process.start");
        using (var process = Process.Start(info)) {
            int pid = process == null ? 0 : process.Id;
            var result = Success("pid", pid);
            // The dispatch receipt makes an interrupted window wait unknown,
            // never a reason for the wrapper to start a duplicate application.
            if (pid != 0 && Num(args, "waitForWindowMs") > 0) {
                var until = DateTime.UtcNow.AddMilliseconds(Num(args, "waitForWindowMs"));
                while (DateTime.UtcNow < until) {
                    var windows = Windows().Where(item => Num(item, "pid") == pid).ToList();
                    if (windows.Count > 0) { result["windows"] = windows; break; }
                    Thread.Sleep(100);
                }
            }
            return result;
        }
    }
    private static Point Coordinate(Dictionary<string, object> args, IntPtr window, int x, int y) {
        string space = Str(args, "space", window == IntPtr.Zero ? "screen" : "window");
        if (space == "window") { var bounds = Bounds(window); x += bounds.Left; y += bounds.Top; }
        return new Point { X = x, Y = y };
    }
    private static Point ActionPoint(Dictionary<string, object> args, IntPtr window, AutomationElement element) {
        if (Get(args, "element") != null) {
            var rect = element.Current.BoundingRectangle;
            if (rect.IsEmpty || rect.Width < 1 || rect.Height < 1) throw new Failure("element_not_visible", "The selected control has no physical input bounds.");
            return new Point { X = (int)(rect.Left + rect.Width / 2), Y = (int)(rect.Top + rect.Height / 2) };
        }
        if (!args.ContainsKey("x") || !args.ContainsKey("y")) throw new Failure("invalid_arguments", "x and y are required for this mouse action.");
        return Coordinate(args, window, Num(args, "x"), Num(args, "y"));
    }
    private static void Send(params Input[] inputs) {
        if (SendInput((uint)inputs.Length, inputs, Marshal.SizeOf(typeof(Input))) != (uint)inputs.Length)
            throw new Failure("input_incomplete", "Windows did not accept all requested input events; observe the application before continuing.");
    }
    private static Input Keyboard(ushort key, bool up, ushort scan = 0, bool unicode = false) {
        return new Input { Type = 1, Value = new InputUnion { Keyboard = new KeyboardInput { Key = unicode ? (ushort)0 : key, Scan = scan, Flags = (up ? 2u : 0u) | (unicode ? 4u : 0u) | (!unicode && (key == 0x21 || key == 0x22 || key == 0x23 || key == 0x24 || key == 0x25 || key == 0x26 || key == 0x27 || key == 0x28 || key == 0x2D || key == 0x2E || key == 0x5B || key == 0x5C) ? 1u : 0u) } } };
    }
    private static Input Mouse(uint flags, uint data = 0, int x = 0, int y = 0) { return new Input { Type = 0, Value = new InputUnion { Mouse = new MouseInput { Flags = flags, Data = data, X = x, Y = y } } }; }
    private static ushort Key(string input) {
        string key = input.ToLowerInvariant().Replace("_l", "").Replace("_r", "");
        var names = new Dictionary<string, ushort> {
            {"ctrl",17},{"control",17},{"alt",18},{"shift",16},{"win",91},{"windows",91},{"meta",91},{"super",91},
            {"return",13},{"enter",13},{"tab",9},{"escape",27},{"esc",27},{"backspace",8},{"space",32},
            {"delete",46},{"insert",45},{"home",36},{"end",35},{"pageup",33},{"prior",33},{"pagedown",34},{"next",34},
            {"left",37},{"up",38},{"right",39},{"down",40},{"capslock",20},{"numlock",144},{"scrolllock",145},
            {"printscreen",44},{"pause",19},{"apps",93},{"plus",187},{"minus",189},{"period",190},{"comma",188},
            {"slash",191},{"backslash",220},{"semicolon",186},{"apostrophe",222},{"bracketleft",219},{"bracketright",221},{"grave",192},
            {"kp_add",107},{"kp_subtract",109},{"kp_multiply",106},{"kp_divide",111},{"kp_decimal",110},{"kp_enter",13}
        };
        ushort value;
        if (names.TryGetValue(key, out value)) return value;
        int number;
        if (key.Length > 1 && key[0] == 'f' && Int32.TryParse(key.Substring(1), out number) && number >= 1 && number <= 24) return (ushort)(111 + number);
        if (key.StartsWith("kp_") && Int32.TryParse(key.Substring(3), out number) && number >= 0 && number <= 9) return (ushort)(96 + number);
        if (key.Length == 1 && Char.IsLetterOrDigit(key[0])) return (ushort)Char.ToUpperInvariant(key[0]);
        if (key.Length == 1) { short mapped = VkKeyScan(key[0]); if (mapped != -1) return (ushort)(mapped & 255); }
        throw new Failure("invalid_key", "Unknown key name: " + input);
    }
    private static void Chord(string chord, string used) {
        var keys = chord.Split('+').Select(value => Key(value.Trim())).ToArray();
        if (keys.Length == 0) throw new Failure("invalid_arguments", "A nonempty key is required.");
        Dispatch(used);
        var held = new List<ushort>();
        try { foreach (ushort key in keys) { Send(Keyboard(key, false)); held.Add(key); } }
        finally { for (int i = held.Count - 1; i >= 0; i--) Send(Keyboard(held[i], true)); }
    }
    private static void MoveMouse(Point point) {
        int left = GetSystemMetrics(76), top = GetSystemMetrics(77), width = GetSystemMetrics(78), height = GetSystemMetrics(79);
        if (width < 2 || height < 2) throw new Failure("display_unavailable", "Windows did not expose an interactive display surface.");
        int x = (int)Math.Round((point.X - left) * 65535.0 / (width - 1)), y = (int)Math.Round((point.Y - top) * 65535.0 / (height - 1));
        if (x < 0 || x > 65535 || y < 0 || y > 65535) throw new Failure("point_outside_display", "The coordinate is outside the virtual desktop.");
        Send(Mouse(0xC001, 0, x, y));
    }
    private static Input Button(string button, bool up) {
        if (button == "left") return Mouse(up ? 4u : 2u);
        if (button == "right") return Mouse(up ? 16u : 8u);
        if (button == "middle") return Mouse(up ? 64u : 32u);
        if (button == "x1" || button == "x2") return Mouse(up ? 256u : 128u, button == "x1" ? 1u : 2u);
        throw new Failure("invalid_arguments", "Unknown mouse button.");
    }
    private static void CheckPoint(IntPtr window, Point point) {
        if (window != IntPtr.Zero && GetAncestor(WindowFromPoint(point), 2) != GetAncestor(window, 2))
            throw new Failure("target_obscured", "The physical input point is covered by another window. Observe the desktop again.");
    }
    private static Dictionary<string, object> Action(Dictionary<string, object> args) {
        string action = Str(args, "action");
        IntPtr window = ResolveWindow(args, false);
        // Native key/window/coordinate operations must not depend on an app
        // exposing a functioning UIA provider (games and canvases often do not).
        AutomationElement element = window == IntPtr.Zero || Get(args, "element") == null ? null : ResolveElement(window, args);
        var observed = Dict(Get(args, "observedBounds"));
        if (observed != null && window != IntPtr.Zero) {
            var current = Bounds(window);
            if (current.Left != Num(observed, "left") || current.Top != Num(observed, "top") || current.Right - current.Left != Num(observed, "width") || current.Bottom - current.Top != Num(observed, "height"))
                throw new Failure("stale_snapshot", "The window moved or resized after the coordinate observation. Capture it again.");
        }
        if (new[] {"invoke","set_value","set_range_value","select","add_to_selection","remove_from_selection","toggle","toggle_on","toggle_off","expand","collapse","scroll","scroll_into_view","text_select_all"}.Contains(action)) {
            if (element == null) throw new Failure("invalid_arguments", "This UIA action requires an inspected element.");
            if (!element.Current.IsEnabled) throw new Failure("element_disabled", "The selected control is disabled.");
            if (action == "invoke") { var pattern = Pattern<InvokePattern>(element, InvokePattern.Pattern); Dispatch("uia.Invoke"); pattern.Invoke(); }
            else if (action == "set_value") {
                var pattern = Pattern<ValuePattern>(element, ValuePattern.Pattern);
                if (pattern.Current.IsReadOnly) throw new Failure("read_only", "The selected control exposes a read-only Value pattern.");
                Dispatch("uia.Value.SetValue"); pattern.SetValue(Str(args, "value", Str(args, "text")));
            } else if (action == "set_range_value") {
                var pattern = Pattern<RangeValuePattern>(element, RangeValuePattern.Pattern);
                if (pattern.Current.IsReadOnly) throw new Failure("read_only", "The selected range is read-only.");
                double value = Number(args, "number");
                if (value < pattern.Current.Minimum || value > pattern.Current.Maximum) throw new Failure("value_out_of_range", "The requested value is outside the control's range.");
                Dispatch("uia.RangeValue.SetValue"); pattern.SetValue(value);
            } else if (action == "select" || action == "add_to_selection" || action == "remove_from_selection") {
                var pattern = Pattern<SelectionItemPattern>(element, SelectionItemPattern.Pattern); Dispatch("uia.SelectionItem." + action);
                if (action == "select") pattern.Select(); else if (action == "add_to_selection") pattern.AddToSelection(); else pattern.RemoveFromSelection();
            } else if (action.StartsWith("toggle")) {
                var pattern = Pattern<TogglePattern>(element, TogglePattern.Pattern);
                if (action == "toggle") { Dispatch("uia.Toggle"); pattern.Toggle(); }
                else {
                    var desired = action == "toggle_on" ? ToggleState.On : ToggleState.Off;
                    if (pattern.Current.ToggleState != desired) {
                        int transitions = 2; // At most one complete traversal of a three-state toggle.
                        Dispatch("uia.Toggle." + desired);
                        for (int i = 0; i < transitions && pattern.Current.ToggleState != desired; i++) pattern.Toggle();
                        if (pattern.Current.ToggleState != desired) throw new Failure("postcondition_failed", "The toggle did not reach the requested state; inspect before deciding what to do next.");
                    }
                }
            } else if (action == "expand" || action == "collapse") {
                var pattern = Pattern<ExpandCollapsePattern>(element, ExpandCollapsePattern.Pattern); Dispatch("uia.ExpandCollapse." + action);
                if (action == "expand") pattern.Expand(); else pattern.Collapse();
            } else if (action == "scroll_into_view") { var pattern = Pattern<ScrollItemPattern>(element, ScrollItemPattern.Pattern); Dispatch("uia.ScrollItem"); pattern.ScrollIntoView(); }
            else if (action == "scroll") {
                var pattern = Pattern<ScrollPattern>(element, ScrollPattern.Pattern);
                string direction = Str(args, "direction", "down");
                Dispatch("uia.Scroll");
                if (args.ContainsKey("horizontalPercent") || args.ContainsKey("verticalPercent")) pattern.SetScrollPercent(Number(args, "horizontalPercent", -1), Number(args, "verticalPercent", -1));
                else if (direction == "top" || direction == "bottom") pattern.SetScrollPercent(-1, direction == "top" ? 0 : 100);
                else {
                    bool large = Str(args, "amount", "large") == "large";
                    var amount = direction == "up" || direction == "left" ? (large ? ScrollAmount.LargeDecrement : ScrollAmount.SmallDecrement) : (large ? ScrollAmount.LargeIncrement : ScrollAmount.SmallIncrement);
                    pattern.Scroll(direction == "left" || direction == "right" ? amount : ScrollAmount.NoAmount, direction == "up" || direction == "down" ? amount : ScrollAmount.NoAmount);
                }
            } else { var pattern = Pattern<TextPattern>(element, TextPattern.Pattern); Dispatch("uia.Text.SelectAll"); pattern.DocumentRange.Select(); }
        } else if (new[] { "move", "resize", "minimize", "maximize", "restore", "close", "focus" }.Contains(action)) {
            if (window == IntPtr.Zero) throw new Failure("invalid_arguments", "This action requires a window.");
            if (action == "focus") { Foreground(window); if (element != null) { Dispatch("uia.SetFocus"); element.SetFocus(); } }
            else if (action == "close") {
                object supported;
                if (element != null && element.TryGetCurrentPattern(WindowPattern.Pattern, out supported)) { Dispatch("uia.Window.Close"); ((WindowPattern)supported).Close(); }
                else { Dispatch("win32.WM_CLOSE"); if (!PostMessage(window, 0x10, IntPtr.Zero, IntPtr.Zero)) throw new Failure("close_failed", "Windows did not accept the close request."); }
            }
            else if (action == "move" || action == "resize") {
                object supported;
                if (element != null && element.TryGetCurrentPattern(TransformPattern.Pattern, out supported)) {
                    var pattern = (TransformPattern)supported;
                    if (action == "move" && !pattern.Current.CanMove || action == "resize" && !pattern.Current.CanResize) throw new Failure("unsupported_pattern", "This control cannot perform the requested transformation.");
                    Dispatch("uia.Transform." + action);
                    if (action == "move") pattern.Move(Num(args, "left"), Num(args, "top")); else pattern.Resize(Num(args, "width"), Num(args, "height"));
                } else {
                    var bounds = Bounds(window); Dispatch("win32.SetWindowPos");
                    bool changed = SetWindowPos(window, IntPtr.Zero, action == "move" ? Num(args, "left") : bounds.Left,
                        action == "move" ? Num(args, "top") : bounds.Top, action == "resize" ? Num(args, "width") : bounds.Right - bounds.Left,
                        action == "resize" ? Num(args, "height") : bounds.Bottom - bounds.Top, 0x14);
                    if (!changed) throw new Failure("window_transform_failed", "Windows rejected the requested window transformation.");
                }
            } else {
                object supported;
                if (element != null && element.TryGetCurrentPattern(WindowPattern.Pattern, out supported)) { Dispatch("uia.Window.VisualState"); ((WindowPattern)supported).SetWindowVisualState(action == "minimize" ? WindowVisualState.Minimized : action == "maximize" ? WindowVisualState.Maximized : WindowVisualState.Normal); }
                else { Dispatch("win32.ShowWindow"); ShowWindow(window, action == "minimize" ? 6 : action == "maximize" ? 3 : 9); }
            }
        } else if (action == "clipboard_get") { method = "clipboard.get"; return Success("text", Clipboard.ContainsText() ? Clipboard.GetText(TextDataFormat.UnicodeText) : ""); }
        else if (action == "clipboard_set") { Dispatch("clipboard.set"); string text = Str(args, "text"); if (text.Length == 0) Clipboard.Clear(); else Clipboard.SetText(text, TextDataFormat.UnicodeText); }
        else {
            if (action == "key_up" || action == "mouse_up") RequireInteractive(window); else Foreground(window);
            if (action == "key") Chord(Str(args, "key"), "sendinput.key");
            else if (action == "key_down" || action == "key_up") { ushort key = Key(Str(args, "key")); Dispatch("sendinput." + action); Send(Keyboard(key, action == "key_up")); }
            else if (action == "type_text") {
                string text = Str(args, "text"); Dispatch("sendinput.unicode");
                foreach (char ch in text) { if (window != IntPtr.Zero && GetForegroundWindow() != window) throw new Failure("foreground_lost", "The selected window lost focus during text input."); Send(Keyboard(0, false, ch, true), Keyboard(0, true, ch, true)); }
            } else if (action == "drag") {
                var path = Array(Get(args, "path")).Select(Dict).ToArray();
                if (path.Length < 2 || path.Any(item => item == null)) throw new Failure("invalid_arguments", "A drag needs at least two path points.");
                var points = path.Select(item => Coordinate(args, window, Num(item, "x"), Num(item, "y"))).ToArray();
                string button = Str(args, "button", "left"); var down = Button(button, false); var up = Button(button, true);
                var modifiers = Array(Get(args, "modifiers")).Select(value => Key(Convert.ToString(value))).ToArray();
                Dispatch("sendinput.drag"); MoveMouse(points[0]); CheckPoint(window, points[0]);
                var held = new List<ushort>(); bool mouseHeld = false;
                try {
                    foreach (ushort key in modifiers) { Send(Keyboard(key, false)); held.Add(key); }
                    Send(down); mouseHeld = true; Thread.Sleep(Num(args, "holdMs"));
                    int delay = Num(args, "durationMs", 400) / (points.Length - 1);
                    for (int i = 1; i < points.Length; i++) { Thread.Sleep(delay); MoveMouse(points[i]); }
                } finally { if (mouseHeld) Send(up); for (int i = held.Count - 1; i >= 0; i--) Send(Keyboard(held[i], true)); }
            } else if (new[] { "mouse_move", "click", "mouse_down", "mouse_up", "wheel" }.Contains(action)) {
                Point point = action == "mouse_up" && !args.ContainsKey("x") && !args.ContainsKey("y") ? new Point() : ActionPoint(args, window, element); string button = Str(args, "button", "left");
                Input down = Button(button, false), up = Button(button, true);
                Dispatch("sendinput." + action);
                if (action != "mouse_up" || args.ContainsKey("x")) MoveMouse(point);
                if (action != "mouse_up") CheckPoint(window, point);
                if (action == "click") {
                    int count = Num(args, "count", 1); if (count < 1) throw new Failure("invalid_arguments", "Click count must be positive.");
                    for (int i = 0; i < count; i++) { Send(down, up); if (i + 1 < count) Thread.Sleep(40); }
                } else if (action == "mouse_down") Send(down);
                else if (action == "mouse_up") Send(up);
                else if (action == "wheel") { if (Num(args, "deltaY") != 0) Send(Mouse(0x800, unchecked((uint)Num(args, "deltaY")))); if (Num(args, "deltaX") != 0) Send(Mouse(0x1000, unchecked((uint)Num(args, "deltaX")))); }
            } else throw new Failure("invalid_arguments", "Unknown desktop action.");
        }
        return Success("window", window == IntPtr.Zero || !IsWindow(window) ? null : WindowInfo(window));
    }
    private static Dictionary<string, object> Capture(Dictionary<string, object> args) {
        IntPtr window = ResolveWindow(args, false);
        string requested = Str(args, "method", "auto"), output = Str(args, "output");
        if (String.IsNullOrEmpty(output)) throw new Failure("invalid_arguments", "A capture output path is required.");
        Rect rect = window == IntPtr.Zero ? new Rect { Left = GetSystemMetrics(76), Top = GetSystemMetrics(77), Right = GetSystemMetrics(76) + GetSystemMetrics(78), Bottom = GetSystemMetrics(77) + GetSystemMetrics(79) } : Bounds(window);
        int width = rect.Right - rect.Left, height = rect.Bottom - rect.Top;
        if (width < 1 || height < 1) throw new Failure("capture_unavailable", "The selected capture surface has zero size.");
        string used = requested; string printError = null;
        using (var image = new Bitmap(width, height, PixelFormat.Format32bppArgb)) {
            bool captured = false;
            if (window != IntPtr.Zero && requested != "screen") {
                using (var graphics = Graphics.FromImage(image)) {
                    IntPtr dc = graphics.GetHdc();
                    try { captured = PrintWindow(window, dc, 2); } finally { graphics.ReleaseHdc(dc); }
                }
                if (captured) { used = "printwindow"; if (IsUniform(image)) { captured = false; printError = "PrintWindow returned a uniform frame; fresh application pixels could not be established."; } }
                else printError = "PrintWindow did not render the selected window.";
                if (!captured && requested == "printwindow") throw new Failure("capture_unavailable", printError);
            }
            if (!captured) {
                RequireInteractive(window);
                // Screen capture records visible pixels; report that fact and
                // never imply it captured an occluded window's own surface.
                using (var graphics = Graphics.FromImage(image)) graphics.CopyFromScreen(rect.Left, rect.Top, 0, 0, image.Size, CopyPixelOperation.SourceCopy);
                used = "screen";
            }
            image.Save(output, ImageFormat.Png);
        }
        method = used;
        return Success("window", window == IntPtr.Zero ? null : WindowInfo(window),
            "capture", Obj("method", used, "bounds", RectJson(rect), "width", width, "height", height,
                           "coordinateSpace", "physical-screen-pixels", "screenPixelsMayIncludeOtherWindows", used == "screen", "fallbackReason", printError));
    }
    private static bool IsUniform(Bitmap image) {
        int first = image.GetPixel(0, 0).ToArgb();
        for (int y = 0; y < image.Height; y += Math.Max(1, image.Height / 20))
            for (int x = 0; x < image.Width; x += Math.Max(1, image.Width / 20)) if (image.GetPixel(x, y).ToArgb() != first) return false;
        return true;
    }
    [STAThread]
    private static int Main() {
        Console.InputEncoding = new UTF8Encoding(false); Console.OutputEncoding = new UTF8Encoding(false);
        try { try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch (EntryPointNotFoundException) { SetProcessDPIAware(); }
            var args = Json.Deserialize<Dictionary<string, object>>(Console.In.ReadToEnd());
            Dictionary<string, object> result;
            switch (Str(args, "operation")) {
                case "status": method = "win32.status"; result = Success("session", Session(), "foregroundHwnd", GetForegroundWindow().ToInt64().ToString(CultureInfo.InvariantCulture),
                    "monitors", Screen.AllScreens.Select(screen => Obj("name", screen.DeviceName, "primary", screen.Primary, "left", screen.Bounds.Left, "top", screen.Bounds.Top, "width", screen.Bounds.Width, "height", screen.Bounds.Height)).ToArray(),
                    "capabilities", Obj("semanticUIA", true, "physicalInput", Bool(Session(), "canInject"), "captureMethods", new[] { "screen", "printwindow" })); break;
                case "windows": {
                    method = "win32.EnumWindows";
                    var all = Windows().Where(window => Bool(args, "includeHidden") || Bool(window, "visible")).ToList();
                    int offset = Num(args, "offset"), limit = Num(args, "maxNodes", 100);
                    result = Success("windows", all.Skip(offset).Take(limit).ToArray(), "total", all.Count, "nextOffset", offset + limit < all.Count ? (object)(offset + limit) : null);
                    break;
                }
                case "launch": result = Launch(args); break;
                case "inspect": result = Inspect(args); break;
                case "action": result = Action(args); break;
                case "capture": result = Capture(args); break;
                case "cleanup_input": {
                    RequireInteractive(IntPtr.Zero);
                    var keys = Array(Get(args, "keys")).Select(value => Key(Convert.ToString(value))).ToArray();
                    var buttons = Array(Get(args, "buttons")).Select(value => Button(Convert.ToString(value), true)).ToArray();
                    Dispatch("sendinput.release_interrupted_request");
                    foreach (ushort key in keys.Reverse()) Send(Keyboard(key, true));
                    foreach (var button in buttons) Send(button);
                    result = Success("releasedKeys", keys.Length, "releasedButtons", buttons.Length);
                    break;
                }
                default: throw new Failure("invalid_arguments", "Unknown desktop operation.");
            }
            Console.WriteLine(Json.Serialize(result)); return 0;
        } catch (Exception error) {
            string code = error is Failure ? ((Failure)error).Code : error is ElementNotAvailableException ? "stale_element" : error is UnauthorizedAccessException ? "access_denied" : error is InvalidOperationException ? "provider_error" : error is COMException ? "provider_error" : "desktop_error";
            Console.WriteLine(Json.Serialize(Obj("ok", false, "error", Obj("code", code, "message", error.Message), "method", method,
                "outcome", dispatched ? "unknown" : "not_started", "retrySafe", !dispatched)));
            return 1;
        }
    }
}
