using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
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
                result["candidate_files"] = manifest.Files.Where(file => !file.Path.Equals("config.json", StringComparison.OrdinalIgnoreCase)).Select(file => file.Path).ToArray();
                result["preserved"] = new string[] { "config.json", "PNG", "Mockup", "Output", "logs", "unknown or modified files" };
                if (report != null) ProductSupport.WriteJson(report, result);
                return 0;
            }
            if (!interactive && !yes) throw new InvalidOperationException("Uninstall confirmation is required. Use /uninstall /yes only when you intend to remove PNG Mockup program files.");
            using (ProductSupport.AcquireSessionLock(root)) ProductSupport.AssertNoRunningApp(root);
            if (interactive && MessageBox.Show(
                "Gỡ các file chương trình của PNG Mockup?\n\nGiữ nguyên config.json, PNG, Mockup, Output, logs và file do bạn thêm hoặc chỉnh sửa.\nKhông gỡ Photoshop hay thay đổi Windows.",
                "Gỡ PNG Mockup", MessageBoxButtons.YesNo, MessageBoxIcon.Question, MessageBoxDefaultButton.Button2) != DialogResult.Yes) return 0;

            string token = Guid.NewGuid().ToString("N");
            string directory = ProductSupport.SafeDestination(root, "logs/.uninstall/" + token);
            ProductSupport.AssertNoReparse(directory);
            Directory.CreateDirectory(directory);
            ProductSupport.SetHidden(ProductSupport.SafeDestination(root, "logs"), true);
            string worker = Path.Combine(directory, "worker.exe");
            string requestPath = Path.Combine(directory, "request.json");
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
            Process.Start(new ProcessStartInfo { FileName = worker, Arguments = "/worker \"" + requestPath + "\" " + token, WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true });
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
        var result = NewResult();
        try
        {
            if (args.Length != 3 || !System.Text.RegularExpressions.Regex.IsMatch(args[2], "^[a-f0-9]{32}$")) throw new ArgumentException("Invalid uninstall worker request.");
            string requestPath = Path.GetFullPath(args[1]);
            var request = ProductSupport.ReadJson(requestPath);
            if (ProductSupport.StringValue(request, "product_id") != ProductSupport.ProductId || ProductSupport.StringValue(request, "token") != args[2]) throw new InvalidDataException("Uninstall request identity is invalid.");
            string root = ProductSupport.CanonicalRoot(ProductSupport.StringValue(request, "root"));
            string directory = ProductSupport.SafeDestination(root, "logs/.uninstall/" + args[2]);
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
            using (ProductSupport.AcquireSessionLock(root, 1000))
            {
                ProductSupport.AssertNoRunningApp(root);
                ProductManifest manifest = ProductSupport.ReadManifest(root);
                string manifestPath = ProductSupport.SafeDestination(root, "setup_manifest.json");
                if (ProductSupport.HashFile(manifestPath) != ProductSupport.StringValue(request, "manifest_sha256")) throw new InvalidDataException("The installed manifest changed after confirmation. Uninstall was cancelled.");
                var remove = new List<ProductFile>();
                var modified = new List<string>();
                foreach (ProductFile file in manifest.Files)
                {
                    if (file.Path.Equals("config.json", StringComparison.OrdinalIgnoreCase)) continue;
                    string path = ProductSupport.SafeDestination(root, file.Path);
                    ProductSupport.AssertNoReparse(path);
                    if (Directory.Exists(path)) throw new InvalidDataException("A directory conflicts with an installed program file: " + file.Path);
                    if (!File.Exists(path)) continue;
                    if ((File.GetAttributes(path) & FileAttributes.ReadOnly) != 0 || new FileInfo(path).Length != file.Length || ProductSupport.HashFile(path) != file.Sha256.ToLowerInvariant()) modified.Add(file.Path);
                    else remove.Add(file);
                }
                var directories = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
                int deleted = 0;
                foreach (ProductFile file in remove.OrderBy(file => file.Path.Equals("version.json", StringComparison.OrdinalIgnoreCase) ? 1 : 0))
                {
                    string path = ProductSupport.SafeDestination(root, file.Path);
                    ProductSupport.AssertNoReparse(path);
                    // Hash again immediately before deletion: user edits are never removed.
                    if (ProductSupport.HashFile(path) != file.Sha256.ToLowerInvariant()) { modified.Add(file.Path); continue; }
                    File.Delete(path);
                    deleted++;
                    string directoryPath = Path.GetDirectoryName(path);
                    while (!directoryPath.Equals(root, StringComparison.OrdinalIgnoreCase))
                    {
                        directories.Add(directoryPath);
                        directoryPath = Path.GetDirectoryName(directoryPath);
                    }
                }
                if (ProductSupport.HashFile(manifestPath) != ProductSupport.StringValue(request, "manifest_sha256")) throw new InvalidDataException("The installed manifest changed during removal.");
                File.Delete(manifestPath);
                foreach (string empty in directories.OrderByDescending(path => path.Length))
                {
                    ProductSupport.AssertNoReparse(empty);
                    if (Directory.Exists(empty) && !Directory.EnumerateFileSystemEntries(empty).Any()) Directory.Delete(empty, false);
                }
                ProductSupport.WriteReceipt(root, "uninstalled", manifest.Version);
                foreach (string name in new string[] { "PNG", "Mockup", "Output" }) ProductSupport.SetHidden(ProductSupport.SafeDestination(root, name), false);
                bool registrationRemoved = ProductSupport.RemoveInstalledApplicationRegistration(root);
                result["ok"] = true;
                result["state"] = "uninstalled";
                result["deleted_program_files"] = deleted;
                result["modified_files_preserved"] = modified.ToArray();
                result["user_data_preserved"] = true;
                result["root"] = root;
                result["worker_retained_in_logs"] = Application.ExecutablePath;
                result["shortcut_result"] = ProductSupport.RemoveOwnShortcut(root, ProductSupport.StringValue(request, "shortcut_directory"));
                result["control_panel_entry_removed"] = registrationRemoved;
                if (!registrationRemoved) result["control_panel_registration_preserved_for_other_install"] = true;
            }
            ProductSupport.WriteJson(report, result);
            if (interactive) MessageBox.Show("Đã gỡ chương trình PNG Mockup.\nCấu hình, PNG, Mockup, Output, logs và file riêng của bạn được giữ nguyên.", "PNG Mockup", MessageBoxButtons.OK, MessageBoxIcon.Information);
            return 0;
        }
        catch (Exception error)
        {
            result["ok"] = false;
            result["state"] = "failed";
            result["error"] = error.Message;
            if (report != null) try { ProductSupport.WriteJson(report, result); } catch { return 3; }
            if (interactive) MessageBox.Show(error.Message + "\nDữ liệu người dùng không bị xóa. Có thể chạy lại bộ cài để phục hồi chương trình.", "Gỡ PNG Mockup chưa hoàn tất", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 2;
        }
    }
    private static Dictionary<string, object> NewResult() { return new Dictionary<string, object> { { "product_id", ProductSupport.ProductId } }; }
}
