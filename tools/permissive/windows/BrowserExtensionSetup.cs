using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Automation;
using System.Windows.Forms;

// Uses the browser's normal extension controls. Personal profile files are never edited.
internal static class BrowserExtensionSetup {
    [DllImport("user32.dll")] private static extern bool SetForegroundWindow(IntPtr handle);
    [DllImport("user32.dll")] private static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr handle, out uint process);
    [DllImport("kernel32.dll")] private static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] private static extern bool AttachThreadInput(uint source, uint target, bool attach);
    [DllImport("user32.dll")] private static extern bool ShowWindow(IntPtr handle, int command);
    private delegate bool EnumWindowCallback(IntPtr handle, IntPtr context);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumWindowCallback callback, IntPtr context);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern int GetClassName(IntPtr handle, StringBuilder text, int capacity);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr handle);
    [StructLayout(LayoutKind.Sequential)] private struct NativePoint { public int X; public int Y; }
    [DllImport("user32.dll")] private static extern bool GetCursorPos(out NativePoint point);
    [DllImport("user32.dll")] private static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] private static extern void mouse_event(uint flags, uint x, uint y, uint data, UIntPtr extra);
    [DllImport("user32.dll")] private static extern IntPtr WindowFromPoint(NativePoint point);
    [DllImport("user32.dll")] private static extern IntPtr GetAncestor(IntPtr window, uint flags);
    [DllImport("user32.dll")] private static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] private static extern bool SetWindowPos(IntPtr window, IntPtr after, int x, int y, int width, int height, uint flags);
    [DllImport("user32.dll")] private static extern IntPtr GetDlgItem(IntPtr dialog, int id);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr SendMessage(IntPtr window, uint message, IntPtr value, string text);
    [DllImport("user32.dll")] private static extern IntPtr SendMessage(IntPtr window, uint message, IntPtr value, IntPtr parameter);
    private static AutomationElement window;
    private static IntPtr handle;
    private static string stage = "Open browser profile";

    private static string Quote(string value) {
        return "\"" + value.Replace("\"", "\\\"").TrimEnd('\\') + "\"";
    }
    private static IntPtr[] Windows(string executable, bool dialogs = false) {
        var found = new List<IntPtr>();
        EnumWindows((native, ignored) => {
            var name = new StringBuilder(128); GetClassName(native, name, name.Capacity);
            if (IsWindowVisible(native) && name.ToString() == (dialogs ? "#32770" : "Chrome_WidgetWin_1")) {
                uint process; GetWindowThreadProcessId(native, out process);
                try {
                    if (String.Equals(Process.GetProcessById((int)process).MainModule.FileName, executable, StringComparison.OrdinalIgnoreCase)) found.Add(native);
                } catch { }
            }
            return true;
        }, IntPtr.Zero);
        return found.ToArray();
    }
    private static AutomationElement Wait(Func<AutomationElement> find, string description, int seconds = 12) {
        stage = description;
        Console.Error.WriteLine(description);
        var until = DateTime.UtcNow.AddSeconds(seconds);
        while (DateTime.UtcNow < until) {
            try { var result = find(); if (result != null) return result; }
            catch (ElementNotAvailableException) { }
            Thread.Sleep(150);
        }
        throw new InvalidOperationException(description + " timed out. Browser policy, an unexpected dialog, or another window may be preventing setup.");
    }
    private static AutomationElement Control(string name, AutomationPattern pattern) {
        var condition = new PropertyCondition(AutomationElement.NameProperty, name);
        foreach (AutomationElement item in window.FindAll(TreeScope.Descendants, condition)) {
            object value;
            if (item.Current.IsEnabled && !item.Current.IsOffscreen && item.TryGetCurrentPattern(pattern, out value)) return item;
        }
        return null;
    }
    private static void FocusWindow() {
        for (int attempt = 0; attempt < 15; attempt++) {
            ShowWindow(handle, 9);
            uint ignored;
            uint foreground = GetWindowThreadProcessId(GetForegroundWindow(), out ignored);
            uint own = GetCurrentThreadId();
            bool attached = foreground != own && AttachThreadInput(own, foreground, true);
            try { SetForegroundWindow(handle); } finally { if (attached) AttachThreadInput(own, foreground, false); }
            Thread.Sleep(150);
            if (GetForegroundWindow() == handle) return;
            if (attempt == 2) {
                // Activate only the new setup window through a normal title-bar click.
                SetWindowPos(handle, IntPtr.Zero, 0, 0, 0, 0, 0x13);
                var bounds = window.Current.BoundingRectangle;
                var title = new NativePoint { X = (int)(bounds.Left + bounds.Width / 2), Y = (int)bounds.Top + 20 };
                if (GetAncestor(WindowFromPoint(title), 2) == handle) {
                    NativePoint previous; GetCursorPos(out previous); SetCursorPos(title.X, title.Y);
                    mouse_event(2, 0, 0, 0, UIntPtr.Zero); mouse_event(4, 0, 0, 0, UIntPtr.Zero);
                    Thread.Sleep(100); SetCursorPos(previous.X, previous.Y);
                }
            }
        }
        throw new InvalidOperationException("Keep the browser setup window in the foreground while setup completes.");
    }
    private static void Invoke(AutomationElement control) {
        FocusWindow();
        var bounds = control.Current.BoundingRectangle;
        if (bounds.IsEmpty || bounds.Width < 1 || bounds.Height < 1) throw new InvalidOperationException("The browser setup control is not visible.");
        var target = new NativePoint { X = (int)(bounds.Left + bounds.Width / 2), Y = (int)(bounds.Top + bounds.Height / 2) };
        if (GetAncestor(WindowFromPoint(target), 2) != handle) throw new InvalidOperationException("Another window is covering the browser setup control.");
        NativePoint previous; GetCursorPos(out previous);
        SetCursorPos(target.X, target.Y);
        mouse_event(2, 0, 0, 0, UIntPtr.Zero); mouse_event(4, 0, 0, 0, UIntPtr.Zero);
        Thread.Sleep(100); SetCursorPos(previous.X, previous.Y);
    }
    private static void Navigate(string url) {
        var address = Wait(() => Control("Address and search bar", ValuePattern.Pattern), "Find browser address bar");
        FocusWindow();
        address.SetFocus();
        ((ValuePattern)address.GetCurrentPattern(ValuePattern.Pattern)).SetValue(url);
        if (GetForegroundWindow() != handle) throw new InvalidOperationException("Browser setup lost focus before navigation.");
        SendKeys.SendWait("{ENTER}");
        Thread.Sleep(500);
    }
    private static string Json(string value) {
        return "\"" + value.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "\\r").Replace("\n", "\\n") + "\"";
    }
    [STAThread]
    private static int Main(string[] args) {
        try {
            SetProcessDPIAware();
            Console.OutputEncoding = new UTF8Encoding(false);
            if (args.Length < 5 || args.Length > 6) throw new ArgumentException("Expected browser executable, profile directory, extension directory, profile ID and label.");
            string executable = Path.GetFullPath(args[0]);
            string extension = Path.GetFullPath(args[2]);
            if (!File.Exists(executable) || !File.Exists(Path.Combine(extension, "manifest.json"))) throw new InvalidOperationException("Browser or extension files are unavailable.");
            string scheme = Path.GetFileName(executable).Equals("msedge.exe", StringComparison.OrdinalIgnoreCase) ? "edge" : "chrome";
            var previous = new HashSet<IntPtr>(Windows(executable));
            string arguments = "--new-window --profile-directory=" + Quote(args[1]) + " " + scheme + "://extensions/";
            // Optional isolated data directory is used by the native acceptance test only.
            if (args.Length == 6) arguments = "--user-data-dir=" + Quote(Path.GetFullPath(args[5])) + " --no-first-run --disable-sync " + arguments;
            Process.Start(new ProcessStartInfo(executable, arguments) { UseShellExecute = false });
            window = Wait(() => { var candidate = Windows(executable).FirstOrDefault(item => !previous.Contains(item)); return candidate == IntPtr.Zero ? null : AutomationElement.FromHandle(candidate); }, "Open selected profile window");
            handle = new IntPtr(window.Current.NativeWindowHandle);
            Navigate(scheme + "://extensions/");
            var load = Control("Load unpacked", InvokePattern.Pattern);
            if (load == null) {
                var toggle = Control("Developer mode", TogglePattern.Pattern);
                if (toggle == null) {
                    var menu = Control("Extensions menu", InvokePattern.Pattern);
                    if (menu != null) Invoke(menu);
                    toggle = Wait(() => Control("Developer mode", TogglePattern.Pattern), "Find Developer mode");
                }
                var pattern = (TogglePattern)toggle.GetCurrentPattern(TogglePattern.Pattern);
                if (pattern.Current.ToggleState != ToggleState.On) Invoke(toggle);
                Navigate(scheme + "://extensions/");
                load = Wait(() => Control("Load unpacked", InvokePattern.Pattern), "Enable Developer mode");
            }
            Invoke(load);
            var picker = Wait(() => {
                var candidate = Windows(executable, true).FirstOrDefault(item => GetAncestor(item, 3) == handle);
                return candidate == IntPtr.Zero ? null : AutomationElement.FromHandle(candidate);
            }, "Open extension folder picker");
            var pickerHandle = new IntPtr(picker.Current.NativeWindowHandle);
            var folder = GetDlgItem(pickerHandle, 1152);
            var choose = GetDlgItem(pickerHandle, 1);
            if (folder == IntPtr.Zero || choose == IntPtr.Zero) throw new InvalidOperationException("The browser's folder picker did not expose its folder controls.");
            SetForegroundWindow(pickerHandle);
            SendMessage(folder, 12, IntPtr.Zero, extension);
            SendMessage(choose, 245, IntPtr.Zero, IntPtr.Zero);
            Wait(() => Control("Turn off Codex Permissive Browser", TogglePattern.Pattern) ??
                Control("Turn on Codex Permissive Browser", TogglePattern.Pattern) ??
                window.FindFirst(TreeScope.Descendants, new PropertyCondition(AutomationElement.NameProperty, "Codex Permissive Browser")), "Load Codex Permissive extension");
            string link = "chrome-extension://mnldpjghemdjnoefihnifnpfhlaihamk/options.html#localProfileId=" + Uri.EscapeDataString(args[3]) + "&profileName=" + Uri.EscapeDataString(args[4]);
            Navigate(link);
            Invoke(Wait(() => Control("Save and connect", InvokePattern.Pattern), "Pair selected browser profile"));
            Wait(() => window.FindFirst(TreeScope.Descendants, new PropertyCondition(AutomationElement.NameProperty, "Profile connected. Refresh browser profiles in Codex Permissive.")), "Confirm profile pairing");
            Console.WriteLine("{\"enabledDeveloperMode\":true,\"extensionLoaded\":true,\"paired\":true}");
            return 0;
        } catch (Exception error) {
            Console.WriteLine("{\"error\":" + Json(stage + ": " + error.Message) + "}");
            return 1;
        }
    }
}
