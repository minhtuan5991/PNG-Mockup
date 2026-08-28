using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Windows.Forms;

internal static class TechnicalVisibility
{
    [STAThread]
    private static int Main(string[] args)
    {
        string report = null;
        bool interactive = args.Length == 0;
        var result = new Dictionary<string, object> { { "product_id", ProductSupport.ProductId } };
        int exitCode = 0;
        try
        {
            string mode = interactive ? "/toggle" : args[0].ToLowerInvariant();
            if (args.Length == 3 && args[1].Equals("/report", StringComparison.OrdinalIgnoreCase)) report = Path.GetFullPath(args[2]);
            else if (!interactive && args.Length != 1) throw new ArgumentException("Usage: Show technical files.exe [/toggle|/show|/hide|/status] [/report absolute.json]");
            if (!new string[] { "/toggle", "/show", "/hide", "/status" }.Contains(mode)) throw new ArgumentException("Unknown visibility command.");
            string root = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
            List<string> paths = ProductSupport.TechnicalItems(root);
            bool visible = paths.Where(path => File.Exists(path) || Directory.Exists(path))
                .All(path => (File.GetAttributes(path) & FileAttributes.Hidden) == 0);
            var changed = new List<string>();
            if (mode != "/status")
            {
                visible = mode == "/show" || (mode == "/toggle" && !visible);
                changed = ProductSupport.SetTechnicalVisibility(root, visible);
            }
            result["ok"] = true;
            result["visible"] = visible;
            result["state"] = visible ? "shown" : "hidden";
            result["changed"] = changed.ToArray();
            result["technical_items"] = paths.ToArray();
            result["global_explorer_settings_changed"] = false;
            if (interactive)
                MessageBox.Show(visible ? "Đã hiện file kỹ thuật của PNG Mockup. Bấm lại để ẩn.\nKhông thay đổi tùy chọn hiển thị của Windows." : "Đã ẩn file kỹ thuật của PNG Mockup.\nPNG, Mockup và Output vẫn hiển thị.", "PNG Mockup", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }
        catch (Exception error)
        {
            result["ok"] = false;
            result["error"] = error.Message;
            exitCode = 2;
            if (interactive) MessageBox.Show(error.Message, "PNG Mockup", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        if (report != null)
            try { ProductSupport.WriteJson(report, result); } catch { return 3; }
        return exitCode;
    }
}
