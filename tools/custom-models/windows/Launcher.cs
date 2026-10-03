using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;

internal static class Launcher
{
    private static string Quote(string value)
    {
        var quoted = new StringBuilder("\"");
        int slashes = 0;
        foreach (char character in value)
        {
            if (character == '\\') { slashes++; continue; }
            if (character == '"') { quoted.Append('\\', slashes * 2 + 1); quoted.Append('"'); }
            else { quoted.Append('\\', slashes); quoted.Append(character); }
            slashes = 0;
        }
        quoted.Append('\\', slashes * 2);
        return quoted.Append('"').ToString();
    }

    private static string Setting(string name, string fallback)
    {
        string value = Environment.GetEnvironmentVariable(name);
        return String.IsNullOrEmpty(value) ? fallback : value;
    }

    private static Thread Pump(Stream input, Stream output, bool closeOutput)
    {
        var thread = new Thread(delegate()
        {
            try
            {
                var buffer = new byte[8192];
                int count;
                while ((count = input.Read(buffer, 0, buffer.Length)) > 0)
                {
                    output.Write(buffer, 0, count);
                    output.Flush();
                }
            }
            catch (IOException) { }
            catch (ObjectDisposedException) { }
            finally
            {
                if (closeOutput) { try { output.Dispose(); } catch (IOException) { } }
            }
        });
        thread.IsBackground = true;
        thread.Start();
        return thread;
    }

    private static int Main(string[] args)
    {
        try
        {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string python = Path.Combine(root, "python", "python.exe");
            string native = Setting("CUSTOM_CODEX_NATIVE", Path.Combine(root, "native", "codex.exe"));
            string registry = Setting("CUSTOM_CODEX_REGISTRY", Path.Combine(root, "registry.json"));
            string home = Setting("CUSTOM_CODEX_HOME", Path.Combine(root, ".codex-custom"));
            var command = new StringBuilder("-B -X utf8 -m custom_models --registry ");
            command.Append(Quote(registry)).Append(" --home ").Append(Quote(home));
            command.Append(" --native ").Append(Quote(native));
            foreach (string argument in args) { command.Append(' ').Append(Quote(argument)); }
            var start = new ProcessStartInfo(python, command.ToString());
            start.UseShellExecute = false;
            bool appServer = Console.IsInputRedirected && Array.IndexOf(args, "app-server") >= 0;
            start.CreateNoWindow = appServer;
            start.RedirectStandardInput = appServer;
            start.RedirectStandardOutput = appServer;
            start.RedirectStandardError = appServer;
            start.WorkingDirectory = Environment.CurrentDirectory;
            start.EnvironmentVariables["PATH"] = Path.Combine(root, "native") + ";" + start.EnvironmentVariables["PATH"];
            using (Process process = Process.Start(start))
            {
                if (appServer)
                {
                    // Keep stdin independent: a child may exit while its caller
                    // still owns an open input pipe. Its background pump must
                    // not delay exit propagation.
                    Pump(Console.OpenStandardInput(), process.StandardInput.BaseStream, true);
                    Thread output = Pump(process.StandardOutput.BaseStream, Console.OpenStandardOutput(), false);
                    Thread errors = Pump(process.StandardError.BaseStream, Console.OpenStandardError(), false);
                    process.WaitForExit();
                    // A helper may inherit these pipes after the app-server
                    // exits. Give buffered output time to drain, then return
                    // the exited app-server's status.
                    output.Join(2000);
                    errors.Join(2000);
                    return process.ExitCode;
                }
                process.WaitForExit();
                return process.ExitCode;
            }
        }
        catch (Exception error)
        {
            Console.Error.WriteLine("codex-custom: " + error.Message);
            return 1;
        }
    }
}
