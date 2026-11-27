// phoenixmp3.exe: starts the app with the bundled Python. Built by
// build-release.ps1 with the C# compiler that comes with Windows.
// Opens the new window (app.pyw) when it and pywebview are there, otherwise
// the classic window (gui.pyw). "phoenixmp3.exe --classic" always opens the classic one.
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Windows.Forms;

static class Launcher
{
    [STAThread]
    static void Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string pipeline = Path.Combine(root, "pipeline");
        string pythonw = Path.Combine(pipeline, "python", "pythonw.exe");
        string gui = Path.Combine(pipeline, "gui.pyw");
        string app = Path.Combine(pipeline, "app.pyw");
        bool classic = args.Any(a => a.Equals("--classic", StringComparison.OrdinalIgnoreCase));
        bool hasNew = File.Exists(app) && Directory.Exists(Path.Combine(pipeline, "libs", "webview"));

        if (!File.Exists(pythonw) || !File.Exists(gui))
        {
            MessageBox.Show(
                "This copy is missing the bundled programs (Python, ffmpeg and so on).\n\n" +
                "You probably downloaded the \"Source code\" zip. Go to the project's GitHub page, " +
                "open Releases on the right, and download the big .zip under Assets instead.",
                "phoenixmp3", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }

        string script = hasNew && !classic ? app : gui;
        var start = new ProcessStartInfo(pythonw, "\"" + script + "\"");
        start.WorkingDirectory = root;
        start.UseShellExecute = false;
        try
        {
            Process.Start(start);
        }
        catch (Exception e)
        {
            MessageBox.Show("Couldn't start phoenixmp3:\n\n" + e.Message, "phoenixmp3",
                            MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
