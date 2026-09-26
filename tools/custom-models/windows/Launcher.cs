using System;
using System.Diagnostics;
using System.IO;
using System.Text;

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
            start.CreateNoWindow = Array.IndexOf(args, "app-server") >= 0;
            start.WorkingDirectory = Environment.CurrentDirectory;
            start.EnvironmentVariables["PATH"] = Path.Combine(root, "native") + ";" + start.EnvironmentVariables["PATH"];
            using (Process process = Process.Start(start))
            {
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
