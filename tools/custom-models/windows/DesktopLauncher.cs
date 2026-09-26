using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

internal static class DesktopLauncher
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

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            var command = new StringBuilder("-NoProfile -ExecutionPolicy Bypass -File ");
            command.Append(Quote(Path.Combine(root, "windows", "launch-desktop.ps1")));
            foreach (string argument in args) { command.Append(' ').Append(Quote(argument)); }
            var start = new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell", "v1.0", "powershell.exe"), command.ToString());
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.RedirectStandardError = true;
            start.RedirectStandardOutput = true;
            using (Process process = Process.Start(start))
            {
                string errors = process.StandardError.ReadToEnd();
                string output = process.StandardOutput.ReadToEnd();
                process.WaitForExit();
                if (process.ExitCode != 0) { MessageBox.Show(errors + output, "Codex custom profile", MessageBoxButtons.OK, MessageBoxIcon.Error); }
                return process.ExitCode;
            }
        }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "Codex custom profile", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
