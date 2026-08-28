using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class PortableLauncher
{
    private const string ProductId = "png-mockup";

    [STAThread]
    private static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        bool selfTest = args.Length > 0 && args[0].Equals("/self-test", StringComparison.OrdinalIgnoreCase);
        string report = null;
        int holdMs = 0;
        try
        {
            if (selfTest)
            {
                for (int index = 1; index < args.Length; index++)
                {
                    if (args[index].Equals("/report", StringComparison.OrdinalIgnoreCase) && report == null && index + 1 < args.Length) report = Path.GetFullPath(args[++index]);
                    else if (args[index].Equals("/hold-ms", StringComparison.OrdinalIgnoreCase) && holdMs == 0 && index + 1 < args.Length && Int32.TryParse(args[++index], out holdMs) && holdMs > 0 && holdMs <= 15000) { }
                    else throw new ArgumentException("Usage: PNG Mockup.exe /self-test [/hold-ms 1..15000] [/report file.json]");
                }
            }
            else if (args.Length != 0)
                throw new ArgumentException("Unknown PNG Mockup argument.");

            string runtime = Path.Combine(root, "runtime", "python");
            string gui = Path.Combine(root, "src", "gui.py");
            string python = Path.Combine(runtime, selfTest ? "python.exe" : "pythonw.exe");
            if (!Environment.Is64BitOperatingSystem || !File.Exists(python) || !File.Exists(gui))
                throw new InvalidOperationException(
                    "PNG Mockup cần Windows 64-bit và bộ runtime riêng đi kèm. " +
                    "Hãy chạy PNG Mockup Setup.exe để cài lại các file còn thiếu. Dữ liệu và cài đặt hiện có được giữ nguyên.");
            var serializer = new JavaScriptSerializer();
            ProductSupport.ValidateIdentity(root, false, true);

            using (ProductSupport.AcquireSessionLock(root))
            {
            ProductSupport.AssertNoRunningApp(root);
            string script = selfTest ? Path.Combine(root, "tools", "runtime_check.py") : gui;
            ProcessStartInfo info = CreateProcess(root, runtime, python, script);
            if (selfTest && holdMs > 0) info.Arguments += " --hold-ms " + holdMs;
            if (!selfTest)
            {
                using (Process application = Process.Start(info))
                {
                    application.WaitForExit();
                    return application.ExitCode;
                }
            }
            info.RedirectStandardOutput = info.RedirectStandardError = true;
            info.StandardOutputEncoding = info.StandardErrorEncoding = new UTF8Encoding(false);
            using (Process process = Process.Start(info))
            {
                var stdout = process.StandardOutput.ReadToEndAsync();
                var stderr = process.StandardError.ReadToEndAsync();
                if (!process.WaitForExit(30000))
                {
                    process.Kill();
                    throw new TimeoutException("The private runtime check timed out.");
                }
                if (process.ExitCode != 0)
                    throw new InvalidOperationException("The private runtime check failed: " + stderr.Result);
                var result = serializer.DeserializeObject(stdout.Result) as Dictionary<string, object>;
                if (result == null || !result.ContainsKey("product_id") || !ProductId.Equals(result["product_id"]))
                    throw new InvalidOperationException("The runtime returned an invalid product identity.");
                result["ok"] = true;
                result["launcher_verified"] = true;
                result["session_lock_used"] = true;
                WriteReport(report, result);
                return 0;
            }
            }
        }
        catch (Exception error)
        {
            if (selfTest)
            {
                try { WriteReport(report, new Dictionary<string, object> { { "ok", false }, { "product_id", ProductId }, { "error", error.Message } }); }
                catch { return 3; }
            }
            else MessageBox.Show(error.Message, "Không thể mở PNG Mockup", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 2;
        }
    }

    private static ProcessStartInfo CreateProcess(string root, string runtime, string python, string script)
    {
        var info = new ProcessStartInfo
        {
            FileName = python,
            Arguments = "-I -B -X utf8 \"" + script + "\"",
            WorkingDirectory = root,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        // Change this launcher process only. Accessing .NET Framework's
        // EnvironmentVariables dictionary throws when a caller supplied both
        // Path and PATH; native child inheritance also handles that environment.
        // Nothing is written to the user's or machine's environment settings.
        Environment.SetEnvironmentVariable("PYTHONHOME", null, EnvironmentVariableTarget.Process);
        Environment.SetEnvironmentVariable("PYTHONPATH", null, EnvironmentVariableTarget.Process);
        Environment.SetEnvironmentVariable("PYTHONNOUSERSITE", "1", EnvironmentVariableTarget.Process);
        Environment.SetEnvironmentVariable("PYTHONDONTWRITEBYTECODE", "1", EnvironmentVariableTarget.Process);
        Environment.SetEnvironmentVariable("PYTHONUTF8", "1", EnvironmentVariableTarget.Process);
        Environment.SetEnvironmentVariable("TCL_LIBRARY", Path.Combine(runtime, "tcl", "tcl8.6"), EnvironmentVariableTarget.Process);
        Environment.SetEnvironmentVariable("TK_LIBRARY", Path.Combine(runtime, "tcl", "tk8.6"), EnvironmentVariableTarget.Process);
        return info;
    }

    private static void WriteReport(string path, Dictionary<string, object> result)
    {
        if (path == null) return;
        string parent = Path.GetDirectoryName(path);
        if (!Directory.Exists(parent)) Directory.CreateDirectory(parent);
        File.WriteAllText(path, new JavaScriptSerializer().Serialize(result), new UTF8Encoding(false));
    }
}
