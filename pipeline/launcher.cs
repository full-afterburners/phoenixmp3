// phoenixmp3.exe: starts the app with the bundled Python. Built by
// build-release.ps1 with the C# compiler that comes with Windows.
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

static class Launcher
{
    [STAThread]
    static void Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        string pipeline = Path.Combine(root, "pipeline");
        string pythonw = Path.Combine(pipeline, "python", "pythonw.exe");
        string gui = Path.Combine(pipeline, "gui.pyw");

        if (!File.Exists(pythonw) || !File.Exists(gui))
        {
            MessageBox.Show(
                "This copy is missing the bundled programs (Python, ffmpeg and so on).\n\n" +
                "You probably downloaded the \"Source code\" zip. Go to the project's GitHub page, " +
                "open Releases on the right, and download the big .zip under Assets instead.",
                "phoenixmp3", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return;
        }

        var start = new ProcessStartInfo(pythonw, "\"" + gui + "\"");
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
