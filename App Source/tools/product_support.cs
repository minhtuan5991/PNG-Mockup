using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;

// Shared by the launcher, installer and the two maintenance helpers.
internal sealed class ProductFile
{
    public string Path;
    public string Sha256;
    public long Length;
}

internal sealed class ProductManifest
{
    public string Version;
    public List<ProductFile> Files;
}

internal static class ProductSupport
{
    public const string ProductId = "png-mockup";
    public static readonly string[] VisibleItems = { "PNG Mockup.exe", "Uninstall.exe", "Show technical files.exe", "PNG", "Mockup", "Output" };
    private static readonly HashSet<string> RootFiles = new HashSet<string>(StringComparer.OrdinalIgnoreCase)
    {
        "PNG Mockup.exe", "Uninstall.exe", "Show technical files.exe", "config.json", "version.json",
        "update-channel.json", "README.md", "THIRD_PARTY_NOTICES.md", "setup_manifest.json"
    };

    public static JavaScriptSerializer Serializer() { return new JavaScriptSerializer { MaxJsonLength = 8 * 1024 * 1024, RecursionLimit = 32 }; }
    public static string StringValue(Dictionary<string, object> data, string key)
    {
        object value;
        if (!data.TryGetValue(key, out value) || !(value is string)) throw new InvalidDataException("Missing string field: " + key);
        return (string)value;
    }
    public static Dictionary<string, object> ReadJson(string path)
    {
        AssertNoReparse(path);
        if (!File.Exists(path) || new FileInfo(path).Length > 8 * 1024 * 1024) throw new InvalidDataException("Missing or oversized JSON file: " + path);
        var result = Serializer().DeserializeObject(File.ReadAllText(path, Encoding.UTF8)) as Dictionary<string, object>;
        if (result == null) throw new InvalidDataException("Invalid JSON object: " + path);
        return result;
    }
    public static void WriteJson(string path, object data)
    {
        AssertNoReparse(path);
        Directory.CreateDirectory(Path.GetDirectoryName(path));
        byte[] content = new UTF8Encoding(false).GetBytes(Serializer().Serialize(data));
        // OpenOrCreate keeps Hidden/System flags intact and works with hidden files.
        using (var output = new FileStream(path, FileMode.OpenOrCreate, FileAccess.Write, FileShare.Read))
        {
            output.SetLength(0);
            output.Write(content, 0, content.Length);
            output.Flush(true);
        }
    }

    public static string CanonicalRoot(string input, bool allowDriveRoot = false, bool allowSource = false)
    {
        if (String.IsNullOrWhiteSpace(input) || !Regex.IsMatch(input, @"^[A-Za-z]:[\\/]"))
            throw new InvalidDataException("Choose an absolute folder on a local Windows drive.");
        string root = Path.GetFullPath(input).TrimEnd('\\', '/');
        if (root.Length == 2) root += "\\";
        if (!Regex.IsMatch(Path.GetPathRoot(root), @"^[A-Za-z]:\\$"))
            throw new InvalidDataException("Choose an absolute folder on a local Windows drive.");
        if (!allowDriveRoot && root.Length <= 3) throw new InvalidDataException("A drive root cannot be used as the application folder.");
        string windows = Environment.GetFolderPath(Environment.SpecialFolder.Windows).TrimEnd('\\');
        string profile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile).TrimEnd('\\');
        if (root.Equals(windows, StringComparison.OrdinalIgnoreCase) || root.StartsWith(windows + "\\", StringComparison.OrdinalIgnoreCase) || (!allowDriveRoot && root.Equals(profile, StringComparison.OrdinalIgnoreCase)))
            throw new InvalidDataException("Choose a separate PNG Mockup application folder.");
        foreach (string part in root.Split('\\'))
            if (new string[] { "AutoDesignPOD", "GeminiMockupWF", "AutoMockup Setup" }.Any(name => name.Equals(part, StringComparison.OrdinalIgnoreCase)))
                throw new InvalidDataException("The original application's folders must not be used for PNG Mockup.");
        AssertNoReparse(root);
        if (!allowSource) AssertNotSource(root);
        foreach (string name in new string[] { "Auto Mockup POD.exe", "Gemini Mockup Workflow.exe", "AutoDesignPOD Updater.exe" })
            if (File.Exists(Path.Combine(root, name))) throw new InvalidDataException("The selected folder contains another application.");
        if (File.Exists(root)) throw new IOException("The selected target is a file.");
        return root;
    }

    public static void AssertNotSource(string root)
    {
        string current = Path.GetFullPath(root);
        while (!String.IsNullOrEmpty(current))
        {
            string source = Path.Combine(current, "App Source");
            if (File.Exists(Path.Combine(current, "build_setup.ps1")) && Directory.Exists(source))
                if (root.Equals(current, StringComparison.OrdinalIgnoreCase) || root.Equals(source, StringComparison.OrdinalIgnoreCase) || root.StartsWith(source + "\\", StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("The application source tree must not be used as an installation or maintenance target.");
            string parent = Path.GetDirectoryName(current);
            if (parent == current) break;
            current = parent;
        }
    }

    public static Dictionary<string, object> ValidateIdentity(string root, bool allowUninstalled = false, bool allowSource = false)
    {
        root = CanonicalRoot(root, false, allowSource);
        string identity = SafeDestination(root, "version.json");
        if (!File.Exists(identity) && allowUninstalled) identity = SafeDestination(root, "logs/.installation.json");
        var data = ReadJson(identity);
        if (StringValue(data, "product_id") != ProductId || !Regex.IsMatch(StringValue(data, "version"), @"^\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.-]+)?$"))
            throw new InvalidDataException("This folder is not an identified PNG Mockup installation.");
        if (data.ContainsKey("name") && StringValue(data, "name") != "PNG Mockup")
            throw new InvalidDataException("This folder belongs to a different product.");
        if (identity.EndsWith(".installation.json", StringComparison.OrdinalIgnoreCase) &&
            !new string[] { "installed", "uninstalled", "incomplete" }.Contains(StringValue(data, "state")))
            throw new InvalidDataException("Invalid PNG Mockup installation receipt.");
        return data;
    }

    public static ProductManifest ReadManifest(string root)
    {
        var identity = ValidateIdentity(root);
        var data = ReadJson(SafeDestination(root, "setup_manifest.json"));
        if (StringValue(data, "product_id") != ProductId || Convert.ToInt32(data["format"]) != 1 || StringValue(data, "version") != StringValue(identity, "version"))
            throw new InvalidDataException("Installed manifest belongs to a different product or version.");
        var rows = data["files"] as object[];
        if (rows == null || rows.Length < 1 || rows.Length > 20000) throw new InvalidDataException("Invalid installed file manifest.");
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var manifest = new ProductManifest { Version = StringValue(data, "version"), Files = new List<ProductFile>() };
        foreach (object item in rows)
        {
            var row = item as Dictionary<string, object>;
            if (row == null) throw new InvalidDataException("Invalid installed file record.");
            var file = new ProductFile { Path = NormalizeRelativePath(StringValue(row, "path")), Sha256 = StringValue(row, "sha256"), Length = Convert.ToInt64(row["length"]) };
            ValidateAllowedPath(file.Path);
            if (file.Path.Equals("setup_manifest.json", StringComparison.OrdinalIgnoreCase) || !seen.Add(file.Path) || !Regex.IsMatch(file.Sha256, "^[a-fA-F0-9]{64}$") || file.Length < 0)
                throw new InvalidDataException("Invalid installed hash/path record: " + file.Path);
            AssertNoReparse(SafeDestination(root, file.Path));
            manifest.Files.Add(file);
        }
        return manifest;
    }

    public static string NormalizeRelativePath(string raw)
    {
        if (String.IsNullOrEmpty(raw) || raw.StartsWith("/") || raw.StartsWith("\\") || raw.IndexOf(':') >= 0) throw new InvalidDataException("Unsafe archive path.");
        string path = raw.Replace('\\', '/');
        foreach (string part in path.Split('/'))
        {
            if (part.Length == 0 || part == "." || part == ".." || part.EndsWith(".") || part.EndsWith(" ") || part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
                throw new InvalidDataException("Unsafe archive path: " + raw);
            if (Regex.IsMatch(part.Split('.')[0], @"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", RegexOptions.IgnoreCase))
                throw new InvalidDataException("Reserved Windows filename in payload.");
        }
        return path;
    }
    public static void ValidateAllowedPath(string path)
    {
        string[] parts = path.Split('/');
        if (parts.Length == 1 && RootFiles.Contains(path)) return;
        if (parts.Length > 1 && (parts[0] == "src" || parts[0] == "assets" || parts[0] == "tools")) return;
        if (parts.Length > 2 && parts[0] == "runtime" && parts[1] == "python") return;
        throw new InvalidDataException("User data or unsupported paths cannot be owned by the application: " + path);
    }
    public static string SafeDestination(string root, string relative)
    {
        string normalized = NormalizeRelativePath(relative);
        string prefix = Path.GetFullPath(root).TrimEnd('\\') + "\\";
        string destination = Path.GetFullPath(Path.Combine(prefix, normalized.Replace('/', '\\')));
        if (!destination.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Path escapes the selected folder.");
        return destination;
    }
    public static void AssertNoReparse(string path)
    {
        string current = Path.GetFullPath(path);
        while (!String.IsNullOrEmpty(current))
        {
            try
            {
                if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0)
                    throw new IOException("Symbolic links/junctions are not supported in application paths: " + current);
            }
            catch (FileNotFoundException) { }
            catch (DirectoryNotFoundException) { }
            string parent = Path.GetDirectoryName(current);
            if (parent == current) break;
            current = parent;
        }
    }
    public static string HashFile(string path)
    {
        using (var input = File.OpenRead(path)) using (var sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(input)).Replace("-", "").ToLowerInvariant();
    }

    public static FileStream AcquireSessionLock(string root, int waitMs = 0)
    {
        string logs = SafeDestination(root, "logs");
        AssertNoReparse(logs);
        Directory.CreateDirectory(logs);
        string path = SafeDestination(root, "logs/.app-session.lock");
        var timer = Stopwatch.StartNew();
        while (true)
        {
            AssertNoReparse(path);
            try { return new FileStream(path, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None); }
            catch (IOException error)
            {
                if (timer.ElapsedMilliseconds >= waitMs)
                    throw new IOException("PNG Mockup is running or another installation/maintenance operation is active. Close the app and try again.", error);
                Thread.Sleep(150);
            }
        }
    }
    public static bool IsAppProcess(string root, Process process)
    {
        string path = Path.GetFullPath(process.MainModule.FileName);
        return new string[] { "PNG Mockup.exe", "runtime/python/python.exe", "runtime/python/pythonw.exe" }
            .Any(relative => SafeDestination(root, relative).Equals(path, StringComparison.OrdinalIgnoreCase));
    }
    public static void AssertNoRunningApp(string root)
    {
        foreach (string name in new string[] { "PNG Mockup", "python", "pythonw" })
            foreach (Process process in Process.GetProcessesByName(name))
                using (process)
                {
                    if (process.Id == Process.GetCurrentProcess().Id) continue;
                    try { if (!process.HasExited && IsAppProcess(root, process)) throw new IOException("PNG Mockup is still running. Close the app before installing or uninstalling."); }
                    catch (System.ComponentModel.Win32Exception) { }
                    catch (InvalidOperationException) { }
                }
    }
    public static void WaitForAppPid(string root, int pid)
    {
        if (pid <= 0 || pid == Process.GetCurrentProcess().Id) throw new ArgumentException("Invalid application PID for update.");
        Process process;
        try { process = Process.GetProcessById(pid); }
        catch (ArgumentException) { return; } // GUI already exited before setup attached.
        using (process)
        {
            if (process.HasExited) return;
            bool own;
            try { own = IsAppProcess(root, process); }
            catch (InvalidOperationException) { if (process.HasExited) return; throw; }
            catch (System.ComponentModel.Win32Exception) { if (process.HasExited) return; throw; }
            if (!own)
            {
                if (process.HasExited) return;
                throw new InvalidOperationException("The supplied PID does not belong to this PNG Mockup installation.");
            }
            if (!process.WaitForExit(60000)) throw new TimeoutException("PNG Mockup did not exit within 60 seconds. No process was terminated and no program files were replaced.");
        }
    }
    public static void WriteReceipt(string root, string state, string version)
    {
        string path = SafeDestination(root, "logs/.installation.json");
        WriteJson(path, new Dictionary<string, object> { { "product_id", ProductId }, { "name", "PNG Mockup" }, { "version", version }, { "state", state } });
        SetHidden(path, true);
    }
    public static void SetHidden(string path, bool hidden)
    {
        AssertNoReparse(path);
        if (!File.Exists(path) && !Directory.Exists(path)) return;
        FileAttributes attributes = File.GetAttributes(path);
        File.SetAttributes(path, hidden ? attributes | FileAttributes.Hidden : attributes & ~FileAttributes.Hidden);
    }
    public static List<string> TechnicalItems(string root)
    {
        var manifest = ReadManifest(root);
        var names = new HashSet<string>(manifest.Files.Select(file => file.Path.Split('/')[0]), StringComparer.OrdinalIgnoreCase);
        names.Add("setup_manifest.json");
        names.Add("logs");
        names.ExceptWith(VisibleItems);
        var paths = names.Select(name => SafeDestination(root, name)).OrderBy(path => path, StringComparer.OrdinalIgnoreCase).ToList();
        foreach (string path in paths) AssertNoReparse(path);
        return paths;
    }
    public static List<string> SetTechnicalVisibility(string root, bool show)
    {
        List<string> paths = TechnicalItems(root);
        foreach (string name in VisibleItems) AssertNoReparse(SafeDestination(root, name));
        var changed = new List<string>();
        foreach (string path in paths)
        {
            if (!File.Exists(path) && !Directory.Exists(path)) continue;
            bool wasHidden = (File.GetAttributes(path) & FileAttributes.Hidden) != 0;
            SetHidden(path, !show);
            if (wasHidden == show) changed.Add(path);
        }
        foreach (string name in VisibleItems) SetHidden(SafeDestination(root, name), false);
        return changed;
    }
    public static string CreateShortcut(string root, string desktopOverride)
    {
        root = CanonicalRoot(root, false, true);
        ValidateIdentity(root, false, true);
        string launcher = SafeDestination(root, "PNG Mockup.exe");
        AssertNoReparse(launcher);
        if (!File.Exists(launcher)) throw new FileNotFoundException("PNG Mockup.exe is missing.");
        string desktop = String.IsNullOrWhiteSpace(desktopOverride) ? Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory) : Path.GetFullPath(desktopOverride);
        AssertNoReparse(desktop);
        if (!Directory.Exists(desktop)) throw new DirectoryNotFoundException("The shortcut directory does not exist: " + desktop);
        string shortcut = Path.Combine(desktop, "PNG Mockup.lnk");
        AssertNoReparse(shortcut);
        if (Directory.Exists(shortcut)) throw new IOException("A folder named PNG Mockup.lnk already exists. It was preserved.");
        bool updating = File.Exists(shortcut);
        string before = updating ? HashFile(shortcut) : null;
        string temporary = null;
        object shell = null;
        object link = null;
        try
        {
            Type shellType = Type.GetTypeFromProgID("WScript.Shell", true);
            shell = Activator.CreateInstance(shellType);
            if (updating)
            {
                link = shellType.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { shortcut });
                string target = (string)link.GetType().InvokeMember("TargetPath", BindingFlags.GetProperty, null, link, null);
                if (String.IsNullOrWhiteSpace(target) || !Path.GetFullPath(target).Equals(launcher, StringComparison.OrdinalIgnoreCase))
                    throw new IOException("The existing PNG Mockup.lnk does not point to this installation. It was preserved.");
                Marshal.FinalReleaseComObject(link);
                link = null;
            }
            // Save to an owned temporary name, then commit only after checking
            // the original link again. Never edit an unreadable or foreign link.
            string candidate = Path.Combine(desktop, ".png-mockup-shortcut-" + Guid.NewGuid().ToString("N") + ".lnk");
            AssertNoReparse(candidate);
            if (File.Exists(candidate) || Directory.Exists(candidate)) throw new IOException("A temporary shortcut path is already in use.");
            temporary = candidate;
            link = shellType.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { temporary });
            Type type = link.GetType();
            type.InvokeMember("TargetPath", BindingFlags.SetProperty, null, link, new object[] { launcher });
            type.InvokeMember("Arguments", BindingFlags.SetProperty, null, link, new object[] { "" });
            type.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, link, new object[] { root });
            type.InvokeMember("IconLocation", BindingFlags.SetProperty, null, link, new object[] { launcher + ",0" });
            type.InvokeMember("Description", BindingFlags.SetProperty, null, link, new object[] { "PNG Mockup" });
            type.InvokeMember("Save", BindingFlags.InvokeMethod, null, link, null);
            Marshal.FinalReleaseComObject(link);
            link = null;
            AssertNoReparse(shortcut);
            AssertNoReparse(temporary);
            if (updating)
            {
                if (!File.Exists(shortcut) || HashFile(shortcut) != before)
                    throw new IOException("The existing PNG Mockup.lnk changed while installing. It was preserved.");
                File.Replace(temporary, shortcut, null);
            }
            else File.Move(temporary, shortcut); // Refuses a newly created collision.
            temporary = null;
            return updating ? "updated_matching_shortcut" : "created_shortcut";
        }
        finally
        {
            if (link != null) Marshal.FinalReleaseComObject(link);
            if (shell != null) Marshal.FinalReleaseComObject(shell);
            if (temporary != null)
                try { AssertNoReparse(temporary); if (File.Exists(temporary)) File.Delete(temporary); } catch { }
        }
    }

    public static string RemoveOwnShortcut(string root, string desktopOverride)
    {
        string desktop = String.IsNullOrEmpty(desktopOverride) ? Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory) : Path.GetFullPath(desktopOverride);
        string path = Path.Combine(desktop, "PNG Mockup.lnk");
        object shell = null;
        object link = null;
        try
        {
            AssertNoReparse(path);
            if (!File.Exists(path)) return "not_present";
            string before = HashFile(path);
            Type shellType = Type.GetTypeFromProgID("WScript.Shell", true);
            shell = Activator.CreateInstance(shellType);
            link = shellType.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { path });
            string target = (string)link.GetType().InvokeMember("TargetPath", BindingFlags.GetProperty, null, link, null);
            if (String.IsNullOrWhiteSpace(target) || !Path.GetFullPath(target).Equals(SafeDestination(root, "PNG Mockup.exe"), StringComparison.OrdinalIgnoreCase)) return "different_target_preserved";
            AssertNoReparse(path);
            if (HashFile(path) != before) return "changed_shortcut_preserved";
            File.Delete(path);
            return "removed_matching_shortcut";
        }
        catch (Exception error) { return "shortcut_preserved: " + error.Message; }
        finally
        {
            if (link != null) Marshal.FinalReleaseComObject(link);
            if (shell != null) Marshal.FinalReleaseComObject(shell);
        }
    }
}
