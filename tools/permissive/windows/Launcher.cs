using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
internal static class Launcher {
    private static Thread Pump(Stream input, Stream output, bool closeOutput) {
        var thread = new Thread(delegate() {
            try { var buffer = new byte[8192]; int count; while ((count = input.Read(buffer,0,buffer.Length)) > 0) { output.Write(buffer,0,count); output.Flush(); } }
            catch (IOException) { } catch (ObjectDisposedException) { }
            finally { if (closeOutput) { try { output.Dispose(); } catch (IOException) { } } }
        });
        thread.IsBackground = true; thread.Start(); return thread;
    }
    internal static string Quote(string value) {
        var result = new StringBuilder("\""); int slashes = 0;
        foreach (char c in value) {
            if (c == '\\') { slashes++; continue; }
            result.Append('\\', c == '"' ? slashes * 2 + 1 : slashes); result.Append(c); slashes = 0;
        }
        return result.Append('\\', slashes * 2).Append('"').ToString();
    }
    private static int Main(string[] args) {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        bool browserHost = Path.GetFileName(Environment.GetCommandLineArgs()[0]).Equals("native-host.exe", StringComparison.OrdinalIgnoreCase);
        var command = new StringBuilder(browserHost ? "-B -X utf8 -m permissive.browser_bridge" : "-B -X utf8 -m permissive");
        if (browserHost) args = new string[0];
        foreach (string argument in args) command.Append(' ').Append(Quote(argument));
        if (Array.IndexOf(args, "serve") >= 0) {
            command.Append(" --native ").Append(Quote(Path.Combine(root,"native","codex-app-server.exe")));
            command.Append(" --node ").Append(Quote(Path.Combine(root,"node","node.exe")));
            command.Append(" --browser-worker ").Append(Quote(Path.Combine(root,"browser","worker.mjs")));
        }
        var start = new ProcessStartInfo(Path.Combine(root,"python","python.exe"),command.ToString());
        start.UseShellExecute = false;
        bool stream = browserHost || Array.IndexOf(args, "mcp") >= 0;
        start.CreateNoWindow = stream;
        start.RedirectStandardInput = stream; start.RedirectStandardOutput = true; start.RedirectStandardError = true;
        start.EnvironmentVariables["PATH"] = Path.Combine(root,"native") + ";" + Path.Combine(root,"node") + ";" + Path.Combine(root,"python") + ";" + Path.Combine(root,"powershell") + ";" + start.EnvironmentVariables["PATH"];
        start.EnvironmentVariables["PLAYWRIGHT_BROWSERS_PATH"] = Path.Combine(root,"chromium");
        using (var child = Process.Start(start)) {
            if (stream) Pump(Console.OpenStandardInput(), child.StandardInput.BaseStream, true);
            Thread output = Pump(child.StandardOutput.BaseStream, Console.OpenStandardOutput(), false);
            Thread errors = Pump(child.StandardError.BaseStream, Console.OpenStandardError(), false);
            child.WaitForExit(); output.Join(2000); errors.Join(2000); return child.ExitCode;
        }
    }
}
