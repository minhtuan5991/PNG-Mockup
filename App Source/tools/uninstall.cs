using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Windows.Forms;

internal static class UninstallProgram
{
    [STAThread]
    private static int Main(string[] args)
    {
        if (args.Length > 0 && args[0].Equals("/worker", StringComparison.OrdinalIgnoreCase)) return RunWorker(args);
        string report = null;
        bool interactive = args.Length == 0;
        string shortcutDirectory = null;
        var result = NewResult();
        try
        {
            string mode = interactive ? "/uninstall" : args[0].ToLowerInvariant();
            bool yes = false;
            for (int index = 1; index < args.Length; index++)
            {
                if (args[index].Equals("/yes", StringComparison.OrdinalIgnoreCase) && !yes) yes = true;
                else if (args[index].Equals("/report", StringComparison.OrdinalIgnoreCase) && report == null && index + 1 < args.Length) report = Path.GetFullPath(args[++index]);
                else if (args[index].Equals("/shortcut-directory", StringComparison.OrdinalIgnoreCase) && shortcutDirectory == null && index + 1 < args.Length) shortcutDirectory = Path.GetFullPath(args[++index]);
                else throw new ArgumentException("Usage: Uninstall.exe /preview [/report file.json] OR /uninstall /yes [/report file.json]");
            }
            if (mode != "/preview" && mode != "/uninstall") throw new ArgumentException("Unknown uninstall command.");
            string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
            ProductManifest manifest = ProductSupport.ReadManifest(root);
            if (mode == "/preview")
            {
                result["ok"] = true;
                result["state"] = "preview";
                result["candidate_files"] = new string[] { "all files and folders in the application root except PNG, Mockup and Output", "this installation's %TEMP%/PNGMockup cache", "Control Panel registration", "PNG Mockup desktop shortcut" };
                result["preserved"] = new string[] { "PNG", "Mockup", "Output" };
                if (report != null) ProductSupport.WriteJson(report, result);
                return 0;
            }
            if (!interactive && !yes) throw new InvalidOperationException("Uninstall confirmation is required. Use /uninstall /yes only when you intend to remove PNG Mockup program files.");
            using (ProductSupport.AcquireSessionLock(root)) ProductSupport.AssertNoRunningApp(root);
            if (interactive && MessageBox.Show(
                "Gỡ hoàn toàn PNG Mockup?\n\nChỉ giữ lại ba thư mục PNG, Mockup và Output cùng dữ liệu bên trong. config.json, logs, cache cập nhật, file kỹ thuật và file khác trong thư mục app sẽ bị xóa.\n\nThao tác không gỡ Photoshop.",
                "Gỡ PNG Mockup", MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2) != DialogResult.Yes) return 0;

            string token = Guid.NewGuid().ToString("N");
            string directory = WorkerDirectory(root, token);
            ProductSupport.AssertNoReparse(directory);
            Directory.CreateDirectory(directory);
            string worker = Path.Combine(directory, "worker.exe");
            string requestPath = Path.Combine(directory, "request.json");
            if (report != null && IsInside(report, root)) throw new InvalidDataException("The uninstall report must be stored outside the application folder.");
            File.Copy(Application.ExecutablePath, worker, false);
            using (Process current = Process.GetCurrentProcess())
                ProductSupport.WriteJson(requestPath, new Dictionary<string, object> {
                    { "product_id", ProductSupport.ProductId }, { "root", root }, { "token", token },
                    { "parent_pid", current.Id }, { "parent_started_ticks", current.StartTime.ToUniversalTime().Ticks.ToString() },
                    { "manifest_sha256", ProductSupport.HashFile(Path.Combine(root, "setup_manifest.json")) },
                    { "worker_sha256", ProductSupport.HashFile(worker) }, { "report", report ?? Path.Combine(directory, "result.json") }, { "interactive", interactive }
                    , { "shortcut_directory", shortcutDirectory ?? "" }
                });
            result["ok"] = true;
            result["state"] = "pending";
            result["worker_request"] = requestPath;
            if (report != null) ProductSupport.WriteJson(report, result);
            Process.Start(new ProcessStartInfo { FileName = worker, Arguments = "/worker \"" + requestPath + "\" " + token, WorkingDirectory = directory, UseShellExecute = false, CreateNoWindow = true });
            return 0; // Worker waits for this EXE to exit before deleting it.
        }
        catch (Exception error)
        {
            result["ok"] = false;
            result["state"] = "failed";
            result["error"] = error.Message;
            if (report != null) try { ProductSupport.WriteJson(report, result); } catch { return 3; }
            if (interactive) MessageBox.Show(error.Message, "Không thể gỡ PNG Mockup", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 2;
        }
    }

    private static int RunWorker(string[] args)
    {
        string report = null;
        bool interactive = false;
        string directory = null;
        var result = NewResult();
        try
        {
            if (args.Length != 3 || !System.Text.RegularExpressions.Regex.IsMatch(args[2], "^[a-f0-9]{32}$")) throw new ArgumentException("Invalid uninstall worker request.");
            string requestPath = Path.GetFullPath(args[1]);
            var request = ProductSupport.ReadJson(requestPath);
            if (ProductSupport.StringValue(request, "product_id") != ProductSupport.ProductId || ProductSupport.StringValue(request, "token") != args[2]) throw new InvalidDataException("Uninstall request identity is invalid.");
            string root = ProductSupport.CanonicalRoot(ProductSupport.StringValue(request, "root"));
            directory = WorkerDirectory(root, args[2]);
            if (!Path.GetFullPath(Application.ExecutablePath).Equals(Path.Combine(directory, "worker.exe"), StringComparison.OrdinalIgnoreCase) ||
                !requestPath.Equals(Path.Combine(directory, "request.json"), StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Uninstall worker/request is outside its owned directory.");
            if (ProductSupport.HashFile(Application.ExecutablePath) != ProductSupport.StringValue(request, "worker_sha256")) throw new InvalidDataException("Uninstall worker hash changed.");
            report = Path.GetFullPath(ProductSupport.StringValue(request, "report"));
            interactive = Convert.ToBoolean(request["interactive"]);
            int pid = Convert.ToInt32(request["parent_pid"]);
            if (pid <= 0 || pid == Process.GetCurrentProcess().Id) throw new InvalidDataException("Invalid uninstall parent process.");
            try
            {
                using (Process parent = Process.GetProcessById(pid))
                {
                    if (!parent.HasExited && parent.StartTime.ToUniversalTime().Ticks.ToString() == ProductSupport.StringValue(request, "parent_started_ticks"))
                    {
                        if (!Path.GetFullPath(parent.MainModule.FileName).Equals(Path.Combine(root, "Uninstall.exe"), StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Uninstall parent is not the installed helper.");
                        if (!parent.WaitForExit(60000)) throw new TimeoutException("Uninstall confirmation window did not close. No process was terminated.");
                    }
                }
            }
            catch (ArgumentException) { }
            var deletion = new DeletionReport();
            ProductManifest manifest;
            using (ProductSupport.AcquireSessionLock(root, 1000))
            {
                ProductSupport.AssertNoRunningApp(root);
                manifest = ProductSupport.ReadManifest(root);
                string manifestPath = ProductSupport.SafeDestination(root, "setup_manifest.json");
                if (ProductSupport.HashFile(manifestPath) != ProductSupport.StringValue(request, "manifest_sha256")) throw new InvalidDataException("The installed manifest changed after confirmation. Uninstall was cancelled.");
                // Keep the lock while removing every entry except logs and the three
                // working folders. This deletes the launcher before another session
                // can start. logs is removed immediately after releasing its lock.
                DeleteApplicationRoot(root, deletion, true);
            }
            DeleteNamedRootEntry(root, "logs", deletion);
            EnsureWorkingFolders(root);
            foreach (string name in new string[] { "PNG", "Mockup", "Output" }) ProductSupport.SetHidden(ProductSupport.SafeDestination(root, name), false);
            DeleteOwnedTemp(root, deletion);
            bool registrationRemoved = ProductSupport.RemoveInstalledApplicationRegistration(root);
            result["ok"] = deletion.Warnings.Count == 0;
            result["state"] = result["ok"].Equals(true) ? "uninstalled" : "partial";
            result["version"] = manifest.Version;
            result["deleted_files"] = deletion.Files;
            result["deleted_directories"] = deletion.Directories;
            result["freed_bytes"] = deletion.Bytes;
            result["cleanup_warnings"] = deletion.Warnings.ToArray();
            result["preserved"] = new string[] { "PNG", "Mockup", "Output" };
            result["user_data_preserved"] = true;
            result["root"] = root;
            result["worker_cleanup_scheduled"] = true;
            result["shortcut_result"] = ProductSupport.RemoveOwnShortcut(root, ProductSupport.StringValue(request, "shortcut_directory"));
            result["control_panel_entry_removed"] = registrationRemoved;
            if (!registrationRemoved) result["control_panel_registration_preserved_for_other_install"] = true;
            ProductSupport.WriteJson(report, result);
            if (interactive) MessageBox.Show(deletion.Warnings.Count == 0
                ? "Đã gỡ PNG Mockup và dọn dữ liệu kỹ thuật.\nChỉ giữ lại ba thư mục PNG, Mockup và Output."
                : "Đã gỡ phần lớn PNG Mockup nhưng một số mục đang bị Windows khóa.\nPNG, Mockup và Output vẫn được giữ lại.",
                "PNG Mockup", MessageBoxButtons.OK, deletion.Warnings.Count == 0 ? MessageBoxIcon.Information : MessageBoxIcon.Warning);
            return deletion.Warnings.Count == 0 ? 0 : 2;
        }
        catch (Exception error)
        {
            result["ok"] = false;
            result["state"] = "failed";
            result["error"] = error.Message;
            if (report != null) try { ProductSupport.WriteJson(report, result); } catch { return 3; }
            if (interactive) MessageBox.Show(error.Message + "\nPNG, Mockup và Output không bị xóa. Có thể đóng các file đang mở rồi chạy Uninstall lại.", "Gỡ PNG Mockup chưa hoàn tất", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 2;
        }
        finally
        {
            if (!String.IsNullOrEmpty(directory)) ScheduleWorkerCleanup(directory);
        }
    }

    private sealed class DeletionReport
    {
        public int Files;
        public int Directories;
        public long Bytes;
        public readonly List<string> Warnings = new List<string>();
    }

    private static readonly HashSet<string> PreservedFolders = new HashSet<string>(new string[] { "PNG", "Mockup", "Output" }, StringComparer.OrdinalIgnoreCase);

    private static string WorkerDirectory(string root, string token)
    {
        if (!System.Text.RegularExpressions.Regex.IsMatch(token ?? "", "^[a-f0-9]{32}$")) throw new InvalidDataException("Invalid uninstall worker token.");
        string temp = Path.GetFullPath(Path.GetTempPath()).TrimEnd('\\');
        string directory = Path.GetFullPath(Path.Combine(temp, "PNGMockup-Uninstall", InstallId(root), token));
        if (!directory.StartsWith(temp + "\\", StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Unsafe uninstall worker directory.");
        return directory;
    }

    private static bool IsInside(string path, string root)
    {
        string candidate = Path.GetFullPath(path);
        string prefix = Path.GetFullPath(root).TrimEnd('\\') + "\\";
        return candidate.StartsWith(prefix, StringComparison.OrdinalIgnoreCase);
    }

    private static void EnsureWorkingFolders(string root)
    {
        foreach (string name in PreservedFolders)
        {
            string path = ProductSupport.SafeDestination(root, name);
            if (File.Exists(path)) throw new IOException("A file prevents restoring the working folder: " + name);
            Directory.CreateDirectory(path);
        }
    }

    private static void DeleteApplicationRoot(string root, DeletionReport report, bool keepLockedLogs)
    {
        foreach (string entry in Directory.EnumerateFileSystemEntries(root).ToArray())
        {
            string name = Path.GetFileName(entry);
            if (PreservedFolders.Contains(name) && Directory.Exists(entry)) continue;
            if (keepLockedLogs && name.Equals("logs", StringComparison.OrdinalIgnoreCase)) continue;
            DeleteEntry(entry, root, report);
        }
    }

    private static void DeleteNamedRootEntry(string root, string name, DeletionReport report)
    {
        string path = ProductSupport.SafeDestination(root, name);
        if (File.Exists(path) || Directory.Exists(path)) DeleteEntry(path, root, report);
    }

    private static void DeleteEntry(string path, string allowedRoot, DeletionReport report)
    {
        try
        {
            if (!IsInside(path, allowedRoot)) throw new IOException("Refusing to remove a path outside PNG Mockup: " + path);
            FileAttributes attributes = File.GetAttributes(path);
            bool directory = (attributes & FileAttributes.Directory) != 0;
            bool reparse = (attributes & FileAttributes.ReparsePoint) != 0;
            if (!directory)
            {
                long length = new FileInfo(path).Length;
                File.SetAttributes(path, FileAttributes.Normal);
                File.Delete(path);
                report.Files++;
                report.Bytes += length;
                return;
            }
            if (!reparse)
                foreach (string child in Directory.EnumerateFileSystemEntries(path).ToArray()) DeleteEntry(child, allowedRoot, report);
            File.SetAttributes(path, FileAttributes.Normal);
            Directory.Delete(path, false);
            report.Directories++;
        }
        catch (Exception error) { report.Warnings.Add(path + ": " + error.Message); }
    }

    private static string InstallId(string root)
    {
        string normalized = Path.GetFullPath(root).ToLowerInvariant();
        byte[] digest;
        using (SHA256 sha = SHA256.Create()) digest = sha.ComputeHash(Encoding.UTF8.GetBytes(normalized));
        return BitConverter.ToString(digest).Replace("-", "").ToLowerInvariant().Substring(0, 20);
    }

    private static string InstallTempDirectory(string root)
    {
        return Path.Combine(Path.GetFullPath(Path.GetTempPath()), "PNGMockup", InstallId(root));
    }

    private static void DeleteOwnedTemp(string root, DeletionReport report)
    {
        string parent = Path.Combine(Path.GetFullPath(Path.GetTempPath()), "PNGMockup");
        string owned = InstallTempDirectory(root);
        if (Directory.Exists(owned)) DeleteEntry(owned, parent, report);
        try { if (Directory.Exists(parent) && !Directory.EnumerateFileSystemEntries(parent).Any()) Directory.Delete(parent, false); }
        catch (Exception error) { report.Warnings.Add(parent + ": " + error.Message); }
    }

    private static void ScheduleWorkerCleanup(string directory)
    {
        try
        {
            string temp = Path.GetFullPath(Path.GetTempPath()).TrimEnd('\\');
            string token = Path.GetFileName(directory);
            string script = Path.Combine(temp, "png-mockup-uninstall-cleanup-" + token + ".cmd");
            string worker = Path.Combine(directory, "worker.exe");
            string installWorkerRoot = Path.GetDirectoryName(directory);
            string workerRoot = Path.GetDirectoryName(installWorkerRoot);
            File.WriteAllText(script,
                "@echo off\r\n" +
                ":wait\r\n" +
                "del /f /q \"" + worker + "\" >nul 2>&1\r\n" +
                "if exist \"" + worker + "\" (ping 127.0.0.1 -n 2 >nul & goto wait)\r\n" +
                "cd /d \"" + temp + "\"\r\n" +
                "rd /s /q \"" + directory + "\"\r\n" +
                "rd \"" + installWorkerRoot + "\" >nul 2>&1\r\n" +
                "rd \"" + workerRoot + "\" >nul 2>&1\r\n" +
                "del /f /q \"%~f0\" >nul 2>&1\r\n", new UTF8Encoding(false));
            Process.Start(new ProcessStartInfo { FileName = Environment.GetEnvironmentVariable("ComSpec") ?? "cmd.exe", Arguments = "/d /q /c \"\"" + script + "\"\"", WorkingDirectory = temp, UseShellExecute = false, CreateNoWindow = true });
        }
        catch { /* The worker is tiny; a later uninstall may clean a stale worker folder. */ }
    }

    private static Dictionary<string, object> NewResult() { return new Dictionary<string, object> { { "product_id", ProductSupport.ProductId } }; }
}
