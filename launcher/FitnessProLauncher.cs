using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Threading;
using System.Windows.Forms;

internal static class Program
{
    private const string Root = @"D:\FitnessPro";
    private const string Url = "http://127.0.0.1:8000";
    private static Process _server;
    private static Mutex _mutex;

    [STAThread]
    private static void Main()
    {
        bool created;
        _mutex = new Mutex(true, "FitnessProDesktopLauncher", out created);
        if (!created)
        {
            OpenBrowser();
            return;
        }

        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new LauncherForm());
        if (_server != null && !_server.HasExited)
        {
            try { _server.Kill(); } catch { }
        }
        _mutex.ReleaseMutex();
    }

    internal static bool IsHealthy()
    {
        try
        {
            var request = (HttpWebRequest)WebRequest.Create(Url + "/health");
            request.Timeout = 1500;
            request.ReadWriteTimeout = 1500;
            using (var response = (HttpWebResponse)request.GetResponse())
            {
                return response.StatusCode == HttpStatusCode.OK;
            }
        }
        catch
        {
            return false;
        }
    }

    internal static void OpenBrowser()
    {
        Process.Start(new ProcessStartInfo(Url) { UseShellExecute = true });
    }

    internal static string StartServer()
    {
        string python = Path.Combine(Root, ".venv", "Scripts", "python.exe");
        if (!File.Exists(python))
        {
            return "找不到 Python 环境：" + python;
        }

        var start = new ProcessStartInfo
        {
            FileName = python,
            Arguments = "-m uvicorn app.main:app --host 127.0.0.1 --port 8000",
            WorkingDirectory = Root,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden
        };
        _server = Process.Start(start);
        if (_server == null)
        {
            return "服务进程没有启动。";
        }

        for (int i = 0; i < 60; i++)
        {
            if (_server.HasExited)
            {
                return "服务启动后立即退出，代码 " + _server.ExitCode + "。";
            }
            if (IsHealthy())
            {
                return null;
            }
            Thread.Sleep(500);
        }
        return "服务在 30 秒内没有就绪。";
    }
}

internal sealed class LauncherForm : Form
{
    private readonly Label _status = new Label();
    private readonly Button _open = new Button();
    private readonly Button _stop = new Button();

    public LauncherForm()
    {
        Text = "FitnessPro";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = true;
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(440, 168);
        Font = new Font("Microsoft YaHei UI", 9F);

        _status.SetBounds(20, 20, 400, 52);
        _status.Text = "正在启动 FitnessPro…";
        Controls.Add(_status);

        _open.Text = "在浏览器中打开";
        _open.SetBounds(20, 96, 180, 36);
        _open.Enabled = false;
        _open.Click += delegate { Program.OpenBrowser(); };
        Controls.Add(_open);

        _stop.Text = "停止并退出";
        _stop.SetBounds(220, 96, 180, 36);
        _stop.Click += delegate { Close(); };
        Controls.Add(_stop);

        Shown += delegate { ThreadPool.QueueUserWorkItem(delegate { Boot(); }); };
    }

    private void Boot()
    {
        if (Program.IsHealthy())
        {
            SetReady("服务已在运行。");
            Program.OpenBrowser();
            return;
        }

        string error = Program.StartServer();
        if (error != null)
        {
            BeginInvoke((Action)delegate
            {
                _status.Text = error;
                MessageBox.Show(this, error, "FitnessPro", MessageBoxButtons.OK, MessageBoxIcon.Error);
            });
            return;
        }

        SetReady("已启动。关闭此窗口会停止服务。");
        Program.OpenBrowser();
    }

    private void SetReady(string text)
    {
        BeginInvoke((Action)delegate
        {
            _status.Text = text;
            _open.Enabled = true;
        });
    }
}
