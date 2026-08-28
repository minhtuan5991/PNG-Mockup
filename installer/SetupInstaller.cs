using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal static class SetupProgram
{
    [STAThread]
    private static void Main(string[] args)
    {
        if (args.Length > 0)
        {
            RunHeadless(args);
            return;
        }
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        if (!Environment.Is64BitOperatingSystem)
        {
            MessageBox.Show("PNG Mockup cần Windows 64-bit.", "PNG Mockup Setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
            Environment.ExitCode = 2;
            return;
        }
        Application.Run(new SetupForm());
    }

    private static void RunHeadless(string[] args)
    {
        string report = null;
        var result = new Dictionary<string, object>();
        int exitCode = 0;
        try
        {
            string mode = args[0].ToLowerInvariant();
            int index = mode == "/verify" ? 1 : 2;
            if (!new string[] { "/verify", "/extract", "/install", "/update", "/resolve" }.Contains(mode))
                throw new ArgumentException("Usage: PNG Mockup Setup.exe /verify; /install parent-folder; /extract exact-folder; /update exact-folder /wait-pid PID [/relaunch]. Optional: /report file.json");
            if (mode != "/verify" && args.Length < 2) throw new ArgumentException("The command requires an absolute folder.");
            int waitPid = 0;
            bool relaunch = false;
            string shortcutDirectory = null;
            for (; index < args.Length; index++)
            {
                string flag = args[index].ToLowerInvariant();
                if (flag == "/report" && report == null && index + 1 < args.Length) report = Path.GetFullPath(args[++index]);
                else if (flag == "/wait-pid" && mode == "/update" && waitPid == 0 && index + 1 < args.Length && Int32.TryParse(args[++index], out waitPid) && waitPid > 0) { }
                else if (flag == "/relaunch" && mode == "/update" && !relaunch) relaunch = true;
                else if (flag == "/shortcut-directory" && (mode == "/install" || mode == "/extract") && shortcutDirectory == null && index + 1 < args.Length)
                    shortcutDirectory = Path.GetFullPath(args[++index]);
                else throw new ArgumentException("Invalid command option: " + flag);
            }
            if (mode == "/update" && waitPid == 0) throw new ArgumentException("/update requires /wait-pid with the closing application PID.");
            if (mode == "/update" && report == null)
            {
                string updateRoot = ProductSupport.CanonicalRoot(args[1]);
                ProductSupport.ValidateIdentity(updateRoot);
                report = ProductSupport.SafeDestination(updateRoot, "logs/update-install-result.json");
            }
            if (mode == "/verify") result = InstallEngine.Verify(Application.ExecutablePath);
            else if (mode == "/resolve") result = new Dictionary<string, object> { { "product_id", InstallEngine.ProductId }, { "target", InstallEngine.ResolveParentTarget(args[1]) } };
            else if (mode == "/update") result = InstallEngine.Update(Application.ExecutablePath, args[1], waitPid, relaunch);
            else result = InstallEngine.Extract(Application.ExecutablePath, mode == "/install" ? InstallEngine.ResolveParentTarget(args[1]) : args[1], null);
            if (shortcutDirectory != null)
            {
                try { result["shortcut_result"] = InstallEngine.CreateShortcut((string)result["target"], shortcutDirectory); }
                catch (Exception error)
                {
                    result["shortcut_result"] = "preserved_or_not_created";
                    result["shortcut_warning"] = error.Message;
                }
            }
            result["ok"] = true;
            result["mode"] = mode;
        }
        catch (Exception error)
        {
            exitCode = 2;
            result["ok"] = false;
            result["product_id"] = InstallEngine.ProductId;
            result["error"] = error.Message;
        }
        if (report != null)
        {
            try
            {
                ProductSupport.WriteJson(report, result);
            }
            catch { exitCode = 3; }
        }
        if (exitCode != 0 && result.ContainsKey("error") && args.Length > 1 && args[0].Equals("/update", StringComparison.OrdinalIgnoreCase) && args.Any(arg => arg.Equals("/relaunch", StringComparison.OrdinalIgnoreCase)))
        {
            try
            {
                string root = ProductSupport.CanonicalRoot(args[1]);
                using (ProductSupport.AcquireSessionLock(root))
                {
                    ProductSupport.AssertNoRunningApp(root);
                    ProductManifest old = ProductSupport.ReadManifest(root);
                    foreach (string critical in new string[] { "PNG Mockup.exe", "src/gui.py", "runtime/python/pythonw.exe", "runtime/python/python312.dll" })
                    {
                        ProductFile record = old.Files.SingleOrDefault(file => file.Path == critical);
                        if (record == null || ProductSupport.HashFile(ProductSupport.SafeDestination(root, critical)) != record.Sha256)
                            throw new InvalidDataException("The previous application is incomplete; run PNG Mockup Setup.exe to repair it.");
                    }
                }
                // Record failure before reopening the old app, so its next-start
                // UI can display the failed update rather than lose the result.
                result["previous_app_relaunch_requested"] = true;
                if (report != null) ProductSupport.WriteJson(report, result);
                Process.Start(new ProcessStartInfo { FileName = Path.Combine(root, "PNG Mockup.exe"), WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true });
                result["previous_app_relaunch_started"] = true;
                if (report != null) ProductSupport.WriteJson(report, result);
                MessageBox.Show("Cập nhật PNG Mockup chưa hoàn tất. Đã mở lại phiên bản đang cài.\n\n" + result["error"], "Không thể cập nhật PNG Mockup", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
            catch (Exception recoveryError)
            {
                result["previous_app_relaunch_started"] = false;
                result["recovery_error"] = recoveryError.Message;
                if (report != null) try { ProductSupport.WriteJson(report, result); } catch { }
                MessageBox.Show("Cập nhật PNG Mockup chưa hoàn tất.\n\n" + result["error"] + "\n\n" + recoveryError.Message + "\nCấu hình và dữ liệu người dùng được giữ nguyên.", "Không thể cập nhật PNG Mockup", MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }
        Environment.ExitCode = exitCode;
    }
}

internal sealed class SetupForm : Form
{
    private readonly TextBox targetBox;
    private readonly CheckBox shortcutBox;
    private readonly Button installButton;
    private readonly Button launchButton;
    private readonly Button browseButton;
    private readonly Button closeButton;
    private readonly ProgressBar progress;
    private readonly Label status;
    private readonly TextBox details;
    private readonly Label targetPreview;
    private bool installing;
    private string installedRoot;

    public SetupForm()
    {
        Text = "PNG Mockup Setup";
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { Icon = SystemIcons.Application; }
        Font = new Font("Segoe UI", 10F);
        BackColor = Color.FromArgb(247, 250, 252);
        ClientSize = new Size(760, 600);
        MinimumSize = new Size(700, 560);
        StartPosition = FormStartPosition.CenterScreen;
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(28), ColumnCount = 1, RowCount = 10 };
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        for (int i = 0; i < 9; i++) layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        Controls.Add(layout);
        layout.Controls.Add(new Label { Text = "PNG Mockup", Font = new Font("Segoe UI", 25F, FontStyle.Bold), ForeColor = Color.FromArgb(16, 112, 107), AutoSize = true, Margin = new Padding(0, 0, 0, 6) }, 0, 0);
        layout.Controls.Add(new Label { Text = "Bộ cài ngoại tuyến • Windows 10/11 64-bit", AutoSize = true, ForeColor = Color.FromArgb(85, 101, 118), Margin = new Padding(0, 0, 0, 18) }, 0, 1);
        layout.Controls.Add(new Label { Text = "Đã có Python, Tk và thư viện ảnh trong bộ cài. Máy đích cần cài Photoshop riêng.\nĐóng PNG Mockup trước khi cài lại. Cài đặt và dữ liệu hiện có được giữ nguyên.", AutoSize = true, MaximumSize = new Size(660, 0), Margin = new Padding(0, 0, 0, 20) }, 0, 2);
        layout.Controls.Add(new Label { Text = "Chọn ổ đĩa hoặc thư mục cha", AutoSize = true, Font = new Font("Segoe UI", 10F, FontStyle.Bold), Margin = new Padding(0, 0, 0, 6) }, 0, 3);
        var pathPanel = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 2, Margin = new Padding(0, 0, 0, 12) };
        pathPanel.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        pathPanel.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        targetBox = new TextBox { Dock = DockStyle.Fill, Text = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs") };
        browseButton = new Button { Text = "Chọn thư mục…", AutoSize = true, Margin = new Padding(10, 0, 0, 0) };
        browseButton.Click += delegate
        {
            using (var dialog = new FolderBrowserDialog())
            {
                dialog.Description = "Chọn ổ đĩa hoặc thư mục cha. Bộ cài tự tạo thư mục PNG Mockup bên trong.";
                dialog.SelectedPath = targetBox.Text;
                if (dialog.ShowDialog(this) == DialogResult.OK) targetBox.Text = dialog.SelectedPath;
            }
        };
        pathPanel.Controls.Add(targetBox, 0, 0);
        pathPanel.Controls.Add(browseButton, 1, 0);
        layout.Controls.Add(pathPanel, 0, 4);
        targetPreview = new Label { AutoSize = true, MaximumSize = new Size(690, 0), ForeColor = Color.FromArgb(85, 101, 118), Margin = new Padding(0, 0, 0, 14) };
        layout.Controls.Add(targetPreview, 0, 5);
        targetBox.TextChanged += delegate { RefreshTargetPreview(); };
        RefreshTargetPreview();
        shortcutBox = new CheckBox { Text = "Tạo shortcut PNG Mockup trên Desktop", Checked = true, AutoSize = true, Margin = new Padding(0, 0, 0, 20) };
        layout.Controls.Add(shortcutBox, 0, 6);
        progress = new ProgressBar { Dock = DockStyle.Top, Height = 8, Margin = new Padding(0, 0, 0, 8) };
        layout.Controls.Add(progress, 0, 7);
        status = new Label { Text = "Sẵn sàng cài đặt. Không cần kết nối Internet.", AutoSize = true, Margin = new Padding(0, 0, 0, 14) };
        layout.Controls.Add(status, 0, 8);
        var bottom = new TableLayoutPanel { Dock = DockStyle.Fill, ColumnCount = 1, RowCount = 2 };
        bottom.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        bottom.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        details = new TextBox { Dock = DockStyle.Fill, ReadOnly = true, Multiline = true, ScrollBars = ScrollBars.Vertical, BackColor = Color.White, BorderStyle = BorderStyle.FixedSingle, Text = "Tự tạo PNG Mockup trong thư mục cha đã chọn. Nếu chọn đúng bản cài hiện có, bộ cài dùng lại thư mục đó.\r\nFile kỹ thuật được ẩn riêng trong bản cài; không thay đổi cài đặt Windows." };
        bottom.Controls.Add(details, 0, 0);
        var buttons = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill, FlowDirection = FlowDirection.RightToLeft, Margin = new Padding(0, 18, 0, 0) };
        installButton = new Button { Text = "Cài đặt", AutoSize = true, Padding = new Padding(18, 5, 18, 5), BackColor = Color.FromArgb(16, 112, 107), ForeColor = Color.White, FlatStyle = FlatStyle.Flat };
        launchButton = new Button { Text = "Mở PNG Mockup", AutoSize = true, Padding = new Padding(8, 5, 8, 5), Enabled = false };
        closeButton = new Button { Text = "Đóng", AutoSize = true, Padding = new Padding(8, 5, 8, 5) };
        installButton.Click += BeginInstall;
        closeButton.Click += delegate { Close(); };
        launchButton.Click += delegate { Process.Start(new ProcessStartInfo { FileName = Path.Combine(installedRoot, "PNG Mockup.exe"), WorkingDirectory = installedRoot, UseShellExecute = false }); };
        buttons.Controls.Add(installButton);
        buttons.Controls.Add(launchButton);
        buttons.Controls.Add(closeButton);
        bottom.Controls.Add(buttons, 0, 1);
        layout.Controls.Add(bottom, 0, 9);
        FormClosing += delegate(object sender, FormClosingEventArgs e) { if (installing) e.Cancel = true; };
    }

    private void RefreshTargetPreview()
    {
        try { targetPreview.Text = "Sẽ cài tại: " + InstallEngine.ResolveParentTarget(targetBox.Text.Trim()); }
        catch (Exception error) { targetPreview.Text = "Chọn thư mục hợp lệ: " + error.Message; }
    }

    private void BeginInstall(object sender, EventArgs args)
    {
        string target = targetBox.Text.Trim();
        bool shortcut = shortcutBox.Checked;
        installing = true;
        installButton.Enabled = closeButton.Enabled = browseButton.Enabled = targetBox.Enabled = shortcutBox.Enabled = launchButton.Enabled = false;
        details.Text = "Đang xác minh bộ cài…";
        var worker = new Thread(delegate()
        {
            try
            {
                Dictionary<string, object> result = InstallEngine.Extract(Application.ExecutablePath, InstallEngine.ResolveParentTarget(target), UpdateProgress);
                string root = (string)result["target"];
                string warning = "";
                if (shortcut)
                {
                    try { InstallEngine.CreateShortcut(root); }
                    catch (Exception error) { warning = "\r\nKhông tạo được shortcut: " + error.Message + "\r\nBạn vẫn có thể mở PNG Mockup.exe trong thư mục cài đặt."; }
                }
                BeginInvoke((Action)delegate
                {
                    installedRoot = root;
                    installing = false;
                    progress.Value = 100;
                    status.Text = "Đã cài PNG Mockup thành công.";
                    details.Text = "Thư mục: " + root + "\r\nSHA-256 của gói dữ liệu và từng file đi kèm đã được xác minh.\r\nCài đặt và các thư mục PNG, Mockup, Output, logs được giữ nguyên." + warning;
                    closeButton.Enabled = launchButton.Enabled = true;
                });
            }
            catch (Exception error)
            {
                BeginInvoke((Action)delegate
                {
                    installing = false;
                    status.Text = "Chưa cài được PNG Mockup.";
                    details.Text = error.Message;
                    installButton.Enabled = closeButton.Enabled = browseButton.Enabled = targetBox.Enabled = shortcutBox.Enabled = true;
                });
            }
        });
        worker.IsBackground = true;
        worker.Start();
    }

    private void UpdateProgress(int value, string text)
    {
        BeginInvoke((Action)delegate { progress.Value = Math.Max(0, Math.Min(100, value)); status.Text = text; });
    }
}

internal sealed class FileRecord
{
    public string Path;
    public string Sha256;
    public long Length;
}

internal sealed class AppliedChange
{
    public string Destination;
    public string Backup;
    public bool BackupCreated;
    public bool Installed;
}

internal sealed class VerifiedPayload : IDisposable
{
    public FileStream Executable;
    public ZipArchive Archive;
    public Dictionary<string, ZipArchiveEntry> Entries;
    public List<FileRecord> Files;
    public string Version;
    public string PayloadHash;
    public long ExpandedBytes;
    public Dictionary<string, object> Channel;
    public void Dispose() { if (Archive != null) Archive.Dispose(); if (Executable != null) Executable.Dispose(); }
}

internal static class InstallEngine
{
    public const string ProductId = "png-mockup";
    private const string Marker = "PNGMSP01";
    private const int FooterBytes = 48;
    private const long MaxExpandedBytes = 1024L * 1024L * 1024L;
    private static readonly HashSet<string> RootFiles = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
    {
        "PNG Mockup.exe", "Uninstall.exe", "Show technical files.exe", "config.json", "version.json", "update-channel.json", "README.md", "THIRD_PARTY_NOTICES.md", "setup_manifest.json"
    };

    public static JavaScriptSerializer Serializer()
    {
        return new JavaScriptSerializer { MaxJsonLength = 8 * 1024 * 1024, RecursionLimit = 32 };
    }

    public static Dictionary<string, object> Verify(string executable)
    {
        using (VerifiedPayload payload = OpenVerifiedPayload(executable, null)) { return Summary(payload); }
    }

    public static string ResolveParentTarget(string input)
    {
        string parent = ProductSupport.CanonicalRoot(input, true);
        if (File.Exists(Path.Combine(parent, "version.json")) || File.Exists(Path.Combine(parent, "logs", ".installation.json")))
        {
            ProductSupport.ValidateIdentity(parent, true);
            return ValidateTarget(parent);
        }
        return ValidateTarget(Path.Combine(parent, "PNG Mockup"));
    }

    public static Dictionary<string, object> Update(string executable, string root, int waitPid, bool relaunch)
    {
        root = ValidateTarget(root);
        ProductSupport.ValidateIdentity(root);
        Dictionary<string, object> result;
        using (VerifiedPayload payload = OpenVerifiedPayload(executable, null))
        {
            ValidateUpgrade(payload, root);
            ProductSupport.WaitForAppPid(root, waitPid);
            result = ApplyVerifiedPayload(payload, root, null, 10000, true);
        }
        result["relaunch_requested"] = relaunch;
        if (relaunch)
        {
            try
            {
                Process.Start(new ProcessStartInfo { FileName = Path.Combine(root, "PNG Mockup.exe"), WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true });
                result["relaunch_started"] = true;
            }
            catch (Exception error) { throw new IOException("PNG Mockup was updated, but could not reopen: " + error.Message, error); }
        }
        return result;
    }

    public static Dictionary<string, object> Extract(string executable, string selectedTarget, Action<int, string> progress, int waitLockMs = 0)
    {
        if (!Environment.Is64BitOperatingSystem) throw new InvalidOperationException("PNG Mockup requires Windows 64-bit.");
        using (VerifiedPayload payload = OpenVerifiedPayload(executable, progress))
        {
            return ApplyVerifiedPayload(payload, selectedTarget, progress, waitLockMs, false);
        }
    }

    private static void ValidateUpgrade(VerifiedPayload payload, string root)
    {
        var identity = ProductSupport.ValidateIdentity(root);
        Version installed;
        Version incoming;
        if (!Version.TryParse(ProductSupport.StringValue(identity, "version"), out installed) || !Version.TryParse(payload.Version, out incoming) || incoming <= installed)
            throw new InvalidDataException("An automatic update must be strictly newer than the installed version.");
        var channel = ProductSupport.ReadJson(ProductSupport.SafeDestination(root, "update-channel.json"));
        string incomingRepository = ProductSupport.StringValue(payload.Channel, "repository");
        if (ProductSupport.StringValue(channel, "product_id") != ProductId || Convert.ToInt32(channel["schema_version"]) != 1 ||
            !(channel["enabled"] is bool) || !(bool)channel["enabled"] || !(payload.Channel["enabled"] is bool) || !(bool)payload.Channel["enabled"] ||
            !Regex.IsMatch(incomingRepository, @"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$") ||
            new string[] { "automockup", "autodesignpod", "geminimockup" }.Any(name => incomingRepository.ToLowerInvariant().Contains(name)) ||
            !String.Equals(ProductSupport.StringValue(channel, "repository"), incomingRepository, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Automatic updates require the same enabled PNG Mockup repository in the installed and incoming update channels.");
    }

    private static Dictionary<string, object> ApplyVerifiedPayload(VerifiedPayload payload, string selectedTarget, Action<int, string> progress, int waitLockMs, bool requireUpgrade)
    {
            string target = ValidateTarget(selectedTarget);
            string mutexName = "Local\\PNGMockup-Setup-" + HashText(target.ToUpperInvariant()).Substring(0, 24);
            using (var mutex = new Mutex(false, mutexName))
            {
                bool acquired;
                try { acquired = mutex.WaitOne(0); } catch (AbandonedMutexException) { acquired = true; }
                if (!acquired) throw new IOException("Another installer is using this PNG Mockup folder.");
                try
                {
                    target = ValidateTarget(target);
                    Directory.CreateDirectory(target);
                    using (ProductSupport.AcquireSessionLock(target, waitLockMs))
                    {
                        if (requireUpgrade) ValidateUpgrade(payload, target);
                        ProductSupport.AssertNoRunningApp(target);
                        if (!File.Exists(Path.Combine(target, "version.json"))) ProductSupport.WriteReceipt(target, "incomplete", payload.Version);
                        return ExtractVerified(payload, target, progress);
                    }
                }
                finally { mutex.ReleaseMutex(); }
            }
    }

    private static Dictionary<string, object> ExtractVerified(VerifiedPayload payload, string target, Action<int, string> progress)
    {
        foreach (string name in new string[] { "PNG", "Mockup", "Output", "logs" })
        {
            string userPath = SafeDestination(target, name);
            AssertNoReparse(userPath);
            if (File.Exists(userPath)) throw new IOException("A file conflicts with a user-data folder: " + userPath);
        }
        foreach (FileRecord record in payload.Files)
        {
            string destination = SafeDestination(target, record.Path);
            AssertNoReparse(destination);
            if (Directory.Exists(destination)) throw new IOException("A directory conflicts with a program file: " + destination);
        }
        Directory.CreateDirectory(target);
        string stage = SafeDestination(target, ".png-mockup-install-" + Guid.NewGuid().ToString("N"));
        string filesRoot = Path.Combine(stage, "files");
        string backupRoot = Path.Combine(stage, "backup");
        Directory.CreateDirectory(filesRoot);
        var changes = new List<AppliedChange>();
        var createdDirectories = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        bool preserveConfig = File.Exists(Path.Combine(target, "config.json"));
        bool canClean = true;
        try
        {
            int index = 0;
            foreach (FileRecord record in payload.Files)
            {
                string destination = SafeDestination(filesRoot, record.Path);
                Directory.CreateDirectory(Path.GetDirectoryName(destination));
                using (Stream input = payload.Entries[record.Path].Open())
                using (FileStream output = new FileStream(destination, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                { input.CopyTo(output); output.Flush(true); }
                if (!String.Equals(HashFile(destination), record.Sha256, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("Extracted file hash mismatch: " + record.Path);
                index++;
                if (index % 30 == 0) Report(progress, 25 + index * 40 / payload.Files.Count, "Đang giải nén runtime và ứng dụng…");
            }
            index = 0;
            foreach (FileRecord record in payload.Files.OrderBy(r => r.Path.Equals("version.json", StringComparison.OrdinalIgnoreCase) ? 1 : 0))
            {
                index++;
                if (preserveConfig && record.Path.Equals("config.json", StringComparison.OrdinalIgnoreCase)) continue;
                string destination = SafeDestination(target, record.Path);
                AssertNoReparse(destination);
                CreateInstallDirectory(target, Path.GetDirectoryName(destination), createdDirectories);
                var change = new AppliedChange { Destination = destination, Backup = SafeDestination(backupRoot, record.Path) };
                changes.Add(change);
                if (File.Exists(destination))
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(change.Backup));
                    File.Move(destination, change.Backup);
                    change.BackupCreated = true;
                }
                File.Move(SafeDestination(filesRoot, record.Path), destination);
                change.Installed = true;
                if (index % 30 == 0) Report(progress, 65 + index * 30 / payload.Files.Count, "Đang hoàn thiện cài đặt…");
            }
            foreach (string name in new string[] { "PNG", "Mockup", "Output", "logs" })
            {
                string path = SafeDestination(target, name);
                AssertNoReparse(path);
                CreateInstallDirectory(target, path, createdDirectories);
            }
            ProductSupport.WriteReceipt(target, "installed", payload.Version);
            ProductSupport.SetTechnicalVisibility(target, false);
            Dictionary<string, object> result = Summary(payload);
            result["target"] = target;
            result["config_preserved"] = preserveConfig;
            result["user_data_preserved"] = true;
            result["technical_files_hidden"] = true;
            result["session_lock_used"] = true;
            Report(progress, 100, "Cài đặt hoàn tất.");
            return result;
        }
        catch (Exception error)
        {
            var failures = new List<string>();
            foreach (AppliedChange change in changes.AsEnumerable().Reverse())
            {
                try
                {
                    if (change.Installed && File.Exists(change.Destination)) File.Delete(change.Destination);
                    if (change.BackupCreated) File.Move(change.Backup, change.Destination);
                }
                catch (Exception recoveryError) { failures.Add(recoveryError.Message); }
            }
            if (failures.Count == 0)
            {
                foreach (string directory in createdDirectories.OrderByDescending(path => path.Length))
                {
                    try
                    {
                        AssertNoReparse(directory);
                        if (Directory.Exists(directory) && !Directory.EnumerateFileSystemEntries(directory).Any())
                            Directory.Delete(directory, false);
                    }
                    catch (Exception recoveryError) { failures.Add(recoveryError.Message); }
                }
            }
            if (failures.Count > 0)
            {
                canClean = false;
                throw new IOException("Installation failed: " + error.Message + "\r\nRecovery copies were retained at: " + backupRoot + "\r\n" + String.Join("\r\n", failures));
            }
            throw new IOException("Installation failed; replaced program files were restored. " + error.Message, error);
        }
        finally
        {
            if (canClean) TryCleanOwnedStage(target, stage);
        }
    }

    private static void CreateInstallDirectory(string target, string path, HashSet<string> created)
    {
        string prefix = Path.GetFullPath(target).TrimEnd('\\') + "\\";
        string current = Path.GetFullPath(path);
        if (!current.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) &&
            !current.Equals(prefix.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
            throw new IOException("Program directory escapes the installation target.");
        AssertNoReparse(current);
        while (current.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) && !Directory.Exists(current))
        {
            created.Add(current);
            current = Path.GetDirectoryName(current);
        }
        Directory.CreateDirectory(path);
    }

    private static VerifiedPayload OpenVerifiedPayload(string executable, Action<int, string> progress)
    {
        var payload = new VerifiedPayload();
        try
        {
            Report(progress, 1, "Đang xác minh SHA-256 của gói dữ liệu…");
            payload.Executable = new FileStream(executable, FileMode.Open, FileAccess.Read, FileShare.Read);
            FileStream source = payload.Executable;
            if (source.Length < FooterBytes) throw new InvalidDataException("Installer payload footer is missing.");
            source.Position = source.Length - FooterBytes;
            var reader = new BinaryReader(source, Encoding.UTF8, true);
            long length = reader.ReadInt64();
            byte[] expectedHash = reader.ReadBytes(32);
            string marker = Encoding.ASCII.GetString(reader.ReadBytes(8));
            if (marker != Marker || length <= 0 || length > source.Length - FooterBytes)
                throw new InvalidDataException("Invalid PNG Mockup installer payload.");
            long offset = source.Length - FooterBytes - length;
            using (var data = new SliceStream(source, offset, length))
            using (var sha = SHA256.Create())
            {
                byte[] actual = sha.ComputeHash(data);
                if (!actual.SequenceEqual(expectedHash)) throw new InvalidDataException("Installer payload SHA-256 mismatch.");
                payload.PayloadHash = Hex(actual);
            }
            payload.Archive = new ZipArchive(new SliceStream(source, offset, length), ZipArchiveMode.Read, false, Encoding.UTF8);
            if (payload.Archive.Entries.Count == 0 || payload.Archive.Entries.Count > 20000)
                throw new InvalidDataException("Unexpected number of installer files.");
            payload.Entries = new Dictionary<string, ZipArchiveEntry>(StringComparer.OrdinalIgnoreCase);
            foreach (ZipArchiveEntry entry in payload.Archive.Entries)
            {
                string path = NormalizeRelativePath(entry.FullName);
                if (String.IsNullOrEmpty(entry.Name)) throw new InvalidDataException("Directory entries are not permitted in the payload.");
                ValidateAllowedPath(path);
                if (payload.Entries.ContainsKey(path)) throw new InvalidDataException("Duplicate payload path: " + path);
                payload.ExpandedBytes = checked(payload.ExpandedBytes + entry.Length);
                if (entry.Length < 0 || entry.Length > 256L * 1024L * 1024L || payload.ExpandedBytes > MaxExpandedBytes)
                    throw new InvalidDataException("Installer payload exceeds its safe expanded size.");
                payload.Entries.Add(path, entry);
            }
            Dictionary<string, object> manifest = ReadJsonEntry(payload, "setup_manifest.json");
            if (StringValue(manifest, "product_id") != ProductId || Convert.ToInt32(manifest["format"]) != 1)
                throw new InvalidDataException("Installer manifest belongs to a different product or format.");
            payload.Version = StringValue(manifest, "version");
            if (!Regex.IsMatch(payload.Version, @"^\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.-]+)?$"))
                throw new InvalidDataException("Invalid installer version.");
            object[] records = manifest["files"] as object[];
            if (records == null) throw new InvalidDataException("Installer file manifest is missing.");
            payload.Files = new List<FileRecord>();
            var declared = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (object item in records)
            {
                var row = item as Dictionary<string, object>;
                if (row == null) throw new InvalidDataException("Invalid installer file record.");
                var record = new FileRecord { Path = NormalizeRelativePath(StringValue(row, "path")), Sha256 = StringValue(row, "sha256"), Length = Convert.ToInt64(row["length"]) };
                if (record.Path.Equals("setup_manifest.json", StringComparison.OrdinalIgnoreCase) || !declared.Add(record.Path))
                    throw new InvalidDataException("Invalid duplicate/self-referential manifest record.");
                if (!Regex.IsMatch(record.Sha256, "^[0-9a-fA-F]{64}$") || record.Length < 0)
                    throw new InvalidDataException("Invalid file hash or size: " + record.Path);
                ZipArchiveEntry entry;
                if (!payload.Entries.TryGetValue(record.Path, out entry) || entry.Length != record.Length)
                    throw new InvalidDataException("Payload file differs from its manifest: " + record.Path);
                using (Stream data = entry.Open())
                {
                    if (!String.Equals(HashStream(data, record.Length), record.Sha256, StringComparison.OrdinalIgnoreCase))
                        throw new InvalidDataException("Payload file SHA-256 mismatch: " + record.Path);
                }
                payload.Files.Add(record);
                if (payload.Files.Count % 40 == 0) Report(progress, 5 + payload.Files.Count * 18 / records.Length, "Đang kiểm tra toàn bộ file đi kèm…");
            }
            if (declared.Count != payload.Entries.Count - 1) throw new InvalidDataException("Undeclared files are present in the payload.");
            Dictionary<string, object> version = ReadJsonEntry(payload, "version.json");
            if (StringValue(version, "product_id") != ProductId || StringValue(version, "name") != "PNG Mockup" || StringValue(version, "version") != payload.Version)
                throw new InvalidDataException("Application identity/version differs from the installer.");
            Dictionary<string, object> channel = ReadJsonEntry(payload, "update-channel.json");
            if (StringValue(channel, "product_id") != ProductId || Convert.ToInt32(channel["schema_version"]) != 1 || !channel.ContainsKey("enabled") || !(channel["enabled"] is bool))
                throw new InvalidDataException("Update channel belongs to a different product or format.");
            string repository = StringValue(channel, "repository");
            if ((!String.IsNullOrEmpty(repository) && !Regex.IsMatch(repository, @"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")) ||
                ((bool)channel["enabled"] && String.IsNullOrEmpty(repository)) ||
                new string[] { "automockup", "autodesignpod", "geminimockup" }.Any(name => repository.ToLowerInvariant().Contains(name)))
                throw new InvalidDataException("The update repository is not a valid dedicated PNG Mockup channel.");
            payload.Channel = channel;
            foreach (string required in new string[] { "PNG Mockup.exe", "Uninstall.exe", "Show technical files.exe", "update-channel.json", "config.json", "src/gui.py", "assets/app_icon.ico", "tools/runtime_check.py", "runtime/python/python.exe", "runtime/python/pythonw.exe", "runtime/python/python312.dll", "runtime/python/python312._pth", "runtime/python/Lib/tkinter/__init__.py", "runtime/python/Lib/site-packages/PIL/__init__.py", "runtime/python/tcl/tcl8.6/init.tcl", "runtime/python/tcl/tk8.6/tk.tcl", "tools/oxipng/oxipng.exe", "runtime/python/LICENSE.txt", "tools/oxipng/LICENSE", "THIRD_PARTY_NOTICES.md" })
                if (!declared.Contains(required)) throw new InvalidDataException("Required offline component is missing: " + required);
            ZipArchiveEntry manifestEntry = payload.Entries["setup_manifest.json"];
            using (Stream data = manifestEntry.Open())
                payload.Files.Add(new FileRecord { Path = "setup_manifest.json", Length = manifestEntry.Length, Sha256 = HashStream(data, manifestEntry.Length) });
            return payload;
        }
        catch { payload.Dispose(); throw; }
    }

    private static Dictionary<string, object> ReadJsonEntry(VerifiedPayload payload, string name)
    {
        ZipArchiveEntry entry;
        if (!payload.Entries.TryGetValue(name, out entry) || entry.Length > 8 * 1024 * 1024)
            throw new InvalidDataException("Missing or oversized JSON file: " + name);
        using (var reader = new StreamReader(entry.Open(), Encoding.UTF8, true))
        {
            var result = Serializer().DeserializeObject(reader.ReadToEnd()) as Dictionary<string, object>;
            if (result == null) throw new InvalidDataException("Invalid JSON object: " + name);
            return result;
        }
    }

    private static string StringValue(Dictionary<string, object> data, string key)
    {
        object value;
        if (!data.TryGetValue(key, out value) || !(value is string)) throw new InvalidDataException("Missing string field: " + key);
        return (string)value;
    }

    private static string ValidateTarget(string input)
    {
        input = ProductSupport.CanonicalRoot(input);
        if (String.IsNullOrWhiteSpace(input) || !Path.IsPathRooted(input) || input.StartsWith(@"\\") || Path.GetPathRoot(input).Length < 3)
            throw new InvalidDataException("Choose an absolute folder on a local Windows drive.");
        string target = Path.GetFullPath(input).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        if (!Regex.IsMatch(Path.GetPathRoot(target), @"^[A-Za-z]:\\$"))
            throw new InvalidDataException("Choose an absolute folder on a local Windows drive.");
        if (target.Length < 4 || String.Equals(target, Path.GetPathRoot(target).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Installing into a drive root is not allowed.");
        string windows = Environment.GetFolderPath(Environment.SpecialFolder.Windows).TrimEnd('\\');
        string profile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile).TrimEnd('\\');
        if (target.Equals(profile, StringComparison.OrdinalIgnoreCase) || target.Equals(windows, StringComparison.OrdinalIgnoreCase) || target.StartsWith(windows + "\\", StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Choose a separate PNG Mockup application folder.");
        foreach (string part in target.Split('\\'))
        {
            if (new string[] { "AutoDesignPOD", "GeminiMockupWF", "AutoMockup Setup" }.Any(p => p.Equals(part, StringComparison.OrdinalIgnoreCase)))
                throw new InvalidDataException("The original application's folders must not be used for PNG Mockup.");
        }
        AssertNoReparse(target);
        AssertNotSourceDirectory(target);
        if (File.Exists(target)) throw new IOException("The selected target is a file.");
        if (Directory.Exists(target))
        {
            if (Directory.Exists(Path.Combine(target, "App Source")) || File.Exists(Path.Combine(target, "build_setup.ps1")))
                throw new InvalidDataException("This is the source project folder. Choose a separate installed-app folder.");
            foreach (string oldName in new string[] { "Auto Mockup POD.exe", "Gemini Mockup Workflow.exe", "AutoDesignPOD Updater.exe" })
                if (File.Exists(Path.Combine(target, oldName))) throw new InvalidDataException("The selected folder contains another application.");
            string versionPath = Path.Combine(target, "version.json");
            bool identified = false;
            if (File.Exists(versionPath))
            {
                ProductSupport.ValidateIdentity(target);
                identified = true;
            }
            else if (File.Exists(Path.Combine(target, "logs", ".installation.json")))
            {
                ProductSupport.ValidateIdentity(target, true);
                identified = true;
            }
            if (!identified && Directory.EnumerateFileSystemEntries(target).Any())
                throw new InvalidDataException("The selected folder is not empty and is not an identified PNG Mockup installation.");
        }
        return target;
    }

    private static void AssertNotSourceDirectory(string target)
    {
        string current = target;
        while (!String.IsNullOrEmpty(current))
        {
            string source = Path.Combine(current, "App Source");
            if (File.Exists(Path.Combine(current, "build_setup.ps1")) && Directory.Exists(source))
            {
                if (target.Equals(current, StringComparison.OrdinalIgnoreCase) ||
                    target.Equals(source, StringComparison.OrdinalIgnoreCase) ||
                    target.StartsWith(source + "\\", StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("The application source tree must not be used as an installation target.");
            }
            string parent = Path.GetDirectoryName(current);
            if (parent == current) break;
            current = parent;
        }
    }

    private static string NormalizeRelativePath(string raw)
    {
        if (String.IsNullOrEmpty(raw) || raw.StartsWith("/") || raw.StartsWith("\\") || raw.IndexOf(':') >= 0)
            throw new InvalidDataException("Unsafe archive path.");
        string path = raw.Replace('\\', '/');
        string[] parts = path.Split('/');
        foreach (string part in parts)
        {
            if (part.Length == 0 || part == "." || part == ".." || part.EndsWith(".") || part.EndsWith(" ") || part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
                throw new InvalidDataException("Unsafe archive path: " + raw);
            string stem = part.Split('.')[0];
            if (Regex.IsMatch(stem, @"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", RegexOptions.IgnoreCase))
                throw new InvalidDataException("Reserved Windows filename in payload.");
        }
        return path;
    }

    private static void ValidateAllowedPath(string path)
    {
        string[] parts = path.Split('/');
        if (parts.Length == 1)
        {
            if (!RootFiles.Contains(path)) throw new InvalidDataException("Forbidden root file in payload: " + path);
            return;
        }
        if (parts[0] == "src" || parts[0] == "assets" || parts[0] == "tools") return;
        if (parts.Length > 2 && parts[0] == "runtime" && parts[1] == "python") return;
        throw new InvalidDataException("User data or unsupported directories cannot be shipped: " + path);
    }

    private static string SafeDestination(string root, string relative)
    {
        string normalized = NormalizeRelativePath(relative);
        string prefix = Path.GetFullPath(root).TrimEnd('\\') + "\\";
        string destination = Path.GetFullPath(Path.Combine(prefix, normalized.Replace('/', '\\')));
        if (!destination.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Path escapes the selected directory.");
        return destination;
    }

    private static void AssertNoReparse(string path)
    {
        string current = Path.GetFullPath(path);
        while (!String.IsNullOrEmpty(current))
        {
            try
            {
                if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                    throw new IOException("Symbolic links/junctions are not supported in installation paths: " + current);
            }
            catch (FileNotFoundException) { }
            catch (DirectoryNotFoundException) { }
            string parent = Path.GetDirectoryName(current);
            if (parent == current) break;
            current = parent;
        }
    }

    private static void TryCleanOwnedStage(string target, string stage)
    {
        try
        {
            string expected = SafeDestination(target, Path.GetFileName(stage));
            if (!expected.Equals(Path.GetFullPath(stage), StringComparison.OrdinalIgnoreCase) || !Path.GetFileName(stage).StartsWith(".png-mockup-install-", StringComparison.Ordinal)) return;
            AssertNoReparse(stage);
            foreach (string directory in Directory.EnumerateDirectories(stage, "*", SearchOption.AllDirectories)) AssertNoReparse(directory);
            foreach (string file in Directory.EnumerateFiles(stage, "*", SearchOption.AllDirectories)) AssertNoReparse(file);
            Directory.Delete(stage, true);
        }
        catch { /* A cleanup failure must not delete anything outside this owned stage. */ }
    }

    public static string CreateShortcut(string root, string desktopOverride = null)
    {
        return ProductSupport.CreateShortcut(root, desktopOverride);
    }

    private static Dictionary<string, object> Summary(VerifiedPayload payload)
    {
        return new Dictionary<string, object> { { "product_id", ProductId }, { "version", payload.Version }, { "payload_sha256", payload.PayloadHash }, { "files", payload.Files.Count }, { "expanded_bytes", payload.ExpandedBytes }, { "offline_runtime_included", true } };
    }
    private static void Report(Action<int, string> progress, int value, string text) { if (progress != null) progress(value, text); }
    private static string HashFile(string path) { using (var input = File.OpenRead(path)) using (var sha = SHA256.Create()) return Hex(sha.ComputeHash(input)); }
    private static string HashText(string text) { using (var sha = SHA256.Create()) return Hex(sha.ComputeHash(Encoding.UTF8.GetBytes(text))); }
    private static string Hex(byte[] bytes) { return BitConverter.ToString(bytes).Replace("-", "").ToLowerInvariant(); }
    private static string HashStream(Stream input, long expectedLength)
    {
        using (var sha = SHA256.Create())
        {
            byte[] buffer = new byte[65536];
            long count = 0;
            int read;
            while ((read = input.Read(buffer, 0, buffer.Length)) > 0)
            {
                count = checked(count + read);
                if (count > expectedLength) throw new InvalidDataException("Payload stream exceeds its declared length.");
                sha.TransformBlock(buffer, 0, read, null, 0);
            }
            if (count != expectedLength) throw new InvalidDataException("Payload stream length mismatch.");
            sha.TransformFinalBlock(new byte[0], 0, 0);
            return Hex(sha.Hash);
        }
    }
}

internal sealed class SliceStream : Stream
{
    private readonly Stream source;
    private readonly long start;
    private readonly long length;
    private long position;
    public SliceStream(Stream source, long start, long length) { this.source = source; this.start = start; this.length = length; position = 0; }
    public override bool CanRead { get { return true; } }
    public override bool CanSeek { get { return true; } }
    public override bool CanWrite { get { return false; } }
    public override long Length { get { return length; } }
    public override long Position { get { return position; } set { Seek(value, SeekOrigin.Begin); } }
    public override int Read(byte[] buffer, int offset, int count)
    {
        int allowed = (int)Math.Min(count, length - position);
        if (allowed <= 0) return 0;
        source.Position = start + position;
        int read = source.Read(buffer, offset, allowed);
        position += read;
        return read;
    }
    public override long Seek(long offset, SeekOrigin origin)
    {
        long next = origin == SeekOrigin.Begin ? offset : origin == SeekOrigin.Current ? position + offset : length + offset;
        if (next < 0 || next > length) throw new IOException("Seek outside installer payload.");
        position = next;
        return position;
    }
    public override void Flush() { }
    public override void SetLength(long value) { throw new NotSupportedException(); }
    public override void Write(byte[] buffer, int offset, int count) { throw new NotSupportedException(); }
}
