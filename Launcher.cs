using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Windows.Forms;

static class Launcher {
    [STAThread]
    static void Main(string[] args) {
        string d = AppDomain.CurrentDomain.BaseDirectory;
        string py = Path.Combine(d, "runtime", "python.exe");
        string app = Path.Combine(d, "zaia_pdf.py");
        string log = Path.Combine(d, "start.log");
        if (!File.Exists(py) || !File.Exists(app)) {
            MessageBox.Show("runtime or zaia_pdf.py is missing. Please reinstall ZAIA PDF.", "ZAIA PDF");
            return;
        }
        string a = string.Join(" ", args.Select(x => "\"" + x + "\""));
        // cmd redirects stderr to start.log so a failed start can be shown (no console window)
        var psi = new ProcessStartInfo("cmd.exe",
            "/c \"\"" + py + "\" \"" + app + "\" " + a + " 2>\"" + log + "\"\"") {
            WorkingDirectory = d, UseShellExecute = false, CreateNoWindow = true
        };
        var p = Process.Start(psi);
        if (p.WaitForExit(4000) && p.ExitCode != 0) {
            string t = File.Exists(log) ? File.ReadAllText(log) : "";
            if (t.Length > 800) t = t.Substring(t.Length - 800);
            MessageBox.Show("ZAIA PDF failed to start:\n\n" + t, "ZAIA PDF");
        }
    }
}
