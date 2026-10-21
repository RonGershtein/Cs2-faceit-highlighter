// CS2 Highlighter.exe: double-click launcher for friends.
//   1. checks what is missing (Steam + CS2) and shows a popup with download links;
//   2. first run (or after uv.lock changed): downloads uv, which installs its own Python 3.12 and the locked
//      dependencies into .venv (nothing system-wide), then radars and the Desktop icon;
//   3. starts run.py with no console window, opens the browser when the server answers,
//      and sits in the tray (Open / Show log / Quit).
// Built with the C# compiler that ships with Windows (.NET Framework 4.x), see scripts\build_launcher.bat.
// Plain C# 5: that compiler knows nothing newer.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Management;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: System.Reflection.AssemblyTitle("CS2 Highlighter")]
[assembly: System.Reflection.AssemblyProduct("CS2 Highlighter")]
[assembly: System.Reflection.AssemblyDescription("Starts the CS2 Highlighter app in your browser")]
[assembly: System.Reflection.AssemblyVersion("1.0.0.0")]

namespace Cs2Highlighter
{
    static class Theme
    {
        public static readonly Color Bg = ColorTranslator.FromHtml("#12151b");
        public static readonly Color Bg3 = ColorTranslator.FromHtml("#191d25");
        public static readonly Color Bg4 = ColorTranslator.FromHtml("#222733");
        public static readonly Color Text = ColorTranslator.FromHtml("#f2f3f5");
        public static readonly Color Muted = ColorTranslator.FromHtml("#939bab");
        public static readonly Color Accent = ColorTranslator.FromHtml("#f5a623");
        public static readonly Color AccentInk = ColorTranslator.FromHtml("#171004");
        public static readonly Color Ok = ColorTranslator.FromHtml("#3ddc97");
        public static readonly Color Warn = ColorTranslator.FromHtml("#ffd166");
        public static readonly Color Danger = ColorTranslator.FromHtml("#ff6680");
        public static float Scale = 1f;

        public static int S(int px) { return (int)Math.Round(px * Scale); }
        public static Font Font(float pt, FontStyle style) { return new Font("Segoe UI", pt, style); }
        public static Font Font(float pt) { return Font(pt, FontStyle.Regular); }

        public static Button Button(string text, bool primary)
        {
            var b = new Button();
            b.Text = text;
            b.FlatStyle = FlatStyle.Flat;
            b.FlatAppearance.BorderSize = 0;
            b.BackColor = primary ? Accent : Bg4;
            b.ForeColor = primary ? AccentInk : Text;
            b.FlatAppearance.MouseOverBackColor = primary ? ColorTranslator.FromHtml("#ffb940") : ColorTranslator.FromHtml("#2b3140");
            b.Font = Font(10f, primary ? FontStyle.Bold : FontStyle.Regular);
            b.Cursor = Cursors.Hand;
            b.AutoSize = true;
            b.AutoSizeMode = AutoSizeMode.GrowAndShrink;
            b.Padding = new Padding(S(14), S(6), S(14), S(6));
            b.UseVisualStyleBackColor = false;
            return b;
        }

        public static Label Label(string text, Font font, Color color)
        {
            var l = new Label();
            l.Text = text;
            l.Font = font;
            l.ForeColor = color;
            l.BackColor = Color.Transparent;
            l.AutoSize = true;
            return l;
        }

        public static void Form(Form f, string title)
        {
            f.Text = title;
            f.BackColor = Bg;
            f.ForeColor = Text;
            f.Font = Font(10f);
            f.StartPosition = FormStartPosition.CenterScreen;
            f.FormBorderStyle = FormBorderStyle.FixedDialog;
            f.MaximizeBox = false;
            f.MinimizeBox = false;
            f.AutoScaleMode = AutoScaleMode.None;
            try { f.Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        }
    }

    static class Paths
    {
        public static string Root;
        public static string Venv { get { return Path.Combine(Root, ".venv"); } }
        public static string VenvPython { get { return Path.Combine(Root, @".venv\Scripts\python.exe"); } }
        public static string DataDir { get { return Path.Combine(Root, "data"); } }
        public static string SetupLog { get { return Path.Combine(DataDir, "setup.log"); } }
        public static string ServerLog { get { return Path.Combine(DataDir, "server.log"); } }
        public static string Tools { get { return Path.Combine(Root, "tools"); } }
        public static string UvDir { get { return Path.Combine(Tools, "uv"); } }
        public static string UvExe { get { return Path.Combine(UvDir, "uv.exe"); } }
        public static string InstalledStamp { get { return Path.Combine(Venv, ".cs2h-requirements"); } }
        public static string SkipCs2Marker { get { return Path.Combine(DataDir, ".launcher-no-cs2-ok"); } }
    }

    static class Config
    {
        static string Read()
        {
            try { return File.ReadAllText(Path.Combine(Paths.Root, "config.toml")); } catch { return ""; }
        }

        public static int Port()
        {
            var m = Regex.Match(Read(), @"^\s*port\s*=\s*(\d+)", RegexOptions.Multiline);
            int p;
            return m.Success && int.TryParse(m.Groups[1].Value, out p) ? p : 8000;
        }

        public static string Cs2Path()
        {
            var m = Regex.Match(Read(), "^\\s*cs2_path\\s*=\\s*[\"'](.*?)[\"']", RegexOptions.Multiline);
            return m.Success ? m.Groups[1].Value.Replace(@"\\", @"\") : "";
        }
    }

    static class Proc
    {
        // Runs a program with no window, appending its output to log; returns the exit code (-1 if it could not start).
        public static int Run(string exe, string args, string log, Func<Process, bool> keepGoing)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(log));
            using (var w = new StreamWriter(log, true, new UTF8Encoding(false)))
            {
                w.AutoFlush = true;
                w.WriteLine();
                w.WriteLine("> " + exe + " " + args + "   (" + DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + ")");
                var psi = new ProcessStartInfo(exe, args);
                psi.WorkingDirectory = Paths.Root;
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.RedirectStandardOutput = true;
                psi.RedirectStandardError = true;
                psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
                psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";
                psi.EnvironmentVariables["PIP_DISABLE_PIP_VERSION_CHECK"] = "1";
                var gate = new object();
                DataReceivedEventHandler h = delegate(object s, DataReceivedEventArgs e) { if (e.Data != null) lock (gate) try { w.WriteLine(e.Data); } catch { } };
                Process p;
                try
                {
                    p = new Process();
                    p.StartInfo = psi;
                    p.OutputDataReceived += h;
                    p.ErrorDataReceived += h;
                    p.Start();
                }
                catch (Exception ex)
                {
                    w.WriteLine("could not start: " + ex.Message);
                    return -1;
                }
                p.BeginOutputReadLine();
                p.BeginErrorReadLine();
                while (!p.WaitForExit(250))
                {
                    if (keepGoing != null && !keepGoing(p))
                    {
                        KillTree(p.Id, false);
                        p.WaitForExit(5000);
                        lock (gate) try { w.WriteLine("cancelled"); } catch { }
                        return -2;
                    }
                }
                p.WaitForExit();
                lock (gate) try { w.WriteLine("exit code " + p.ExitCode); } catch { }
                return p.ExitCode;
            }
        }

        // First line a command prints (hidden window), or null.
        public static string FirstLine(string exe, string args, int timeoutMs)
        {
            try
            {
                var psi = new ProcessStartInfo(exe, args);
                psi.WorkingDirectory = Paths.Root;
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.RedirectStandardOutput = true;
                psi.RedirectStandardError = true;
                using (var p = Process.Start(psi))
                {
                    p.ErrorDataReceived += delegate { };
                    p.BeginErrorReadLine();
                    var task = p.StandardOutput.ReadToEndAsync();
                    if (!p.WaitForExit(timeoutMs)) { try { p.Kill(); } catch { } return null; }
                    if (p.ExitCode != 0) return null;
                    foreach (var line in task.Result.Split('\n'))
                        if (line.Trim().Length > 0) return line.Trim();
                }
            }
            catch { }
            return null;
        }

        // Kills a process and its children. With spareGame, CS2 and HLAE (and whatever they started) are left
        // running: someone watching a demo in the game keeps it when they quit the app, like with the old console.
        public static void KillTree(int pid, bool spareGame)
        {
            try
            {
                using (var q = new ManagementObjectSearcher("SELECT ProcessId, Name FROM Win32_Process WHERE ParentProcessId=" + pid))
                    foreach (ManagementObject mo in q.Get())
                    {
                        var name = (Convert.ToString(mo["Name"]) ?? "").ToLowerInvariant();
                        if (spareGame && (name == "cs2.exe" || name == "hlae.exe" || name == "steam.exe")) continue;
                        KillTree(Convert.ToInt32(mo["ProcessId"]), spareGame);
                    }
            }
            catch { }
            try { Process.GetProcessById(pid).Kill(); } catch { }
        }

        public static void Open(string target)
        {
            try { Process.Start(target); }
            catch (Exception ex) { MessageBox.Show("Could not open " + target + "\n\n" + ex.Message, "CS2 Highlighter"); }
        }

        public static void OpenLog(string path)
        {
            if (File.Exists(path)) Open(path);
            else MessageBox.Show("There is no log yet.", "CS2 Highlighter");
        }

        public static string Tail(string path, int lines)
        {
            try
            {
                // the server log is still open for writing, so read it with sharing allowed
                var all = new List<string>();
                using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                using (var r = new StreamReader(fs, Encoding.UTF8))
                {
                    string line;
                    while ((line = r.ReadLine()) != null) all.Add(line);
                }
                var start = Math.Max(0, all.Count - lines);
                return string.Join("\n", all.GetRange(start, all.Count - start));
            }
            catch { return ""; }
        }
    }

    // ---------------------------------------------------------------- what the PC needs
    class Requirement
    {
        public string Name, Why, Hint, Url, ButtonText;
        public bool Required, Found;
    }

    static class Checks
    {
        static string SteamDir()
        {
            foreach (var key in new[] { @"HKEY_CURRENT_USER\Software\Valve\Steam", @"HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Valve\Steam" })
            {
                var v = Registry.GetValue(key, key.StartsWith("HKEY_CURRENT_USER") ? "SteamPath" : "InstallPath", null) as string;
                if (!string.IsNullOrEmpty(v) && Directory.Exists(v)) return v.Replace('/', '\\');
            }
            var pf = Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86);
            var def = Path.Combine(pf, "Steam");
            return File.Exists(Path.Combine(def, "steam.exe")) ? def : null;
        }

        static bool Cs2Installed(string steam)
        {
            var cfg = Config.Cs2Path();
            if (cfg.Length > 0 && (File.Exists(cfg) || Directory.Exists(cfg))) return true;
            if (steam == null) return false;
            var libs = new List<string> { steam };
            try
            {
                var vdf = File.ReadAllText(Path.Combine(steam, @"steamapps\libraryfolders.vdf"));
                foreach (Match m in Regex.Matches(vdf, "\"path\"\\s+\"([^\"]+)\""))
                    libs.Add(m.Groups[1].Value.Replace(@"\\", @"\"));
            }
            catch { }
            foreach (var lib in libs)
                if (File.Exists(Path.Combine(lib, @"steamapps\common\Counter-Strike Global Offensive\game\bin\win64\cs2.exe")))
                    return true;
            return false;
        }

        // Python is not on the list: uv brings its own (see SetupForm).
        public static List<Requirement> Run()
        {
            var list = new List<Requirement>();
            var steam = SteamDir();
            var cs2 = new Requirement();
            cs2.Name = steam == null ? "Steam + Counter-Strike 2" : "Counter-Strike 2";
            cs2.Why = "Needed to record clips and open demos in the game. The replay viewer works without it.";
            cs2.Hint = steam == null ? "Install Steam, log in, then install Counter-Strike 2 (free)." : "Install it from your Steam library (free).";
            cs2.Url = steam == null ? "https://store.steampowered.com/about/" : "steam://install/730";
            cs2.ButtonText = steam == null ? "Download Steam" : "Install CS2";
            cs2.Required = false;
            cs2.Found = Cs2Installed(steam);
            list.Add(cs2);
            return list;
        }
    }

    class RequirementsForm : Form
    {
        readonly FlowLayoutPanel rows;
        readonly Button go, recheck;
        List<Requirement> reqs;

        public RequirementsForm(List<Requirement> initial)
        {
            Theme.Form(this, "CS2 Highlighter: almost ready");
            AutoSize = true;
            AutoSizeMode = AutoSizeMode.GrowAndShrink;
            Padding = new Padding(Theme.S(24), Theme.S(20), Theme.S(24), Theme.S(18));

            var col = new FlowLayoutPanel();
            col.FlowDirection = FlowDirection.TopDown;
            col.WrapContents = false;
            col.AutoSize = true;
            col.AutoSizeMode = AutoSizeMode.GrowAndShrink;
            col.Location = new Point(Padding.Left, Padding.Top);
            Controls.Add(col);

            col.Controls.Add(Theme.Label("Almost ready", Theme.Font(16f, FontStyle.Bold), Theme.Text));
            var sub = Theme.Label("Install what is missing below, then click Check again.", Theme.Font(10f), Theme.Muted);
            sub.Margin = new Padding(0, Theme.S(2), 0, Theme.S(14));
            col.Controls.Add(sub);

            rows = new FlowLayoutPanel();
            rows.FlowDirection = FlowDirection.TopDown;
            rows.WrapContents = false;
            rows.AutoSize = true;
            rows.AutoSizeMode = AutoSizeMode.GrowAndShrink;
            rows.Margin = Padding.Empty;
            col.Controls.Add(rows);

            var note = Theme.Label("Python and the app's components install by themselves in the next step. HLAE and FFmpeg (for recording) are downloaded from inside the app.",
                                   Theme.Font(9f), Theme.Muted);
            note.MaximumSize = new Size(Theme.S(560), 0);
            note.Margin = new Padding(0, Theme.S(6), 0, Theme.S(14));
            col.Controls.Add(note);

            var bar = new FlowLayoutPanel();
            bar.FlowDirection = FlowDirection.RightToLeft;
            bar.AutoSize = true;
            bar.AutoSizeMode = AutoSizeMode.GrowAndShrink;
            bar.MinimumSize = new Size(Theme.S(560), 0);
            bar.Margin = Padding.Empty;
            go = Theme.Button("Continue", true);
            go.Click += delegate
            {
                if (!reqs.TrueForAll(x => x.Found))
                    try { Directory.CreateDirectory(Paths.DataDir); File.WriteAllText(Paths.SkipCs2Marker, "ok"); } catch { }
                DialogResult = DialogResult.OK;
            };
            recheck = Theme.Button("Check again", false);
            recheck.Click += delegate { Recheck(); };
            var close = Theme.Button("Close", false);
            close.Click += delegate { DialogResult = DialogResult.Cancel; };
            bar.Controls.Add(go);
            bar.Controls.Add(recheck);
            bar.Controls.Add(close);
            col.Controls.Add(bar);

            AcceptButton = recheck;
            CancelButton = close;
            Fill(initial);
        }

        void Recheck()
        {
            Cursor = Cursors.WaitCursor;
            recheck.Enabled = false;
            recheck.Text = "Checking...";
            var t = new Thread(delegate()
            {
                var r = Checks.Run();
                BeginInvoke((MethodInvoker)delegate
                {
                    Cursor = Cursors.Default;
                    recheck.Enabled = true;
                    recheck.Text = "Check again";
                    Fill(r);
                    if (r.TrueForAll(x => x.Found)) DialogResult = DialogResult.OK;
                });
            });
            t.IsBackground = true;
            t.Start();
        }

        void Fill(List<Requirement> list)
        {
            reqs = list;
            rows.SuspendLayout();
            rows.Controls.Clear();
            foreach (var r in list) rows.Controls.Add(Row(r));
            rows.ResumeLayout();
            bool requiredOk = list.TrueForAll(x => !x.Required || x.Found);
            go.Enabled = requiredOk;
            go.Text = list.TrueForAll(x => x.Found) ? "Continue" : "Continue without CS2";
            go.Visible = requiredOk;
        }

        Control Row(Requirement r)
        {
            var p = new TableLayoutPanel();
            p.BackColor = Theme.Bg3;
            p.ColumnCount = 3;
            p.RowCount = 3;
            p.AutoSize = true;
            p.AutoSizeMode = AutoSizeMode.GrowAndShrink;
            p.MinimumSize = new Size(Theme.S(560), 0);
            p.MaximumSize = new Size(Theme.S(560), 0);
            p.Padding = new Padding(Theme.S(14), Theme.S(12), Theme.S(14), Theme.S(12));
            p.Margin = new Padding(0, 0, 0, Theme.S(8));
            p.ColumnStyles.Add(new ColumnStyle(SizeType.Absolute, Theme.S(34)));
            p.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
            p.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));

            Color c = r.Found ? Theme.Ok : r.Required ? Theme.Danger : Theme.Warn;
            var mark = Theme.Label(r.Found ? "\u2714" : r.Required ? "\u2716" : "!", Theme.Font(14f, FontStyle.Bold), c);
            mark.Margin = new Padding(0, Theme.S(1), 0, 0);
            p.Controls.Add(mark, 0, 0);
            p.SetRowSpan(mark, 3);

            var head = new FlowLayoutPanel();
            head.AutoSize = true;
            head.WrapContents = false;
            head.Margin = Padding.Empty;
            head.Controls.Add(Theme.Label(r.Name, Theme.Font(11f, FontStyle.Bold), Theme.Text));
            var tag = Theme.Label(r.Found ? "installed" : r.Required ? "required" : "recommended", Theme.Font(8.5f, FontStyle.Bold), c);
            tag.Margin = new Padding(Theme.S(6), Theme.S(4), 0, 0);
            head.Controls.Add(tag);
            p.Controls.Add(head, 1, 0);

            var why = Theme.Label(r.Why, Theme.Font(9.5f), Theme.Muted);
            why.MaximumSize = new Size(Theme.S(360), 0);
            why.Margin = new Padding(Theme.S(3), Theme.S(2), 0, 0);
            p.Controls.Add(why, 1, 1);

            if (!r.Found)
            {
                var hint = Theme.Label(r.Hint, Theme.Font(9.5f, FontStyle.Bold), Theme.Accent);
                hint.MaximumSize = new Size(Theme.S(360), 0);
                hint.Margin = new Padding(Theme.S(3), Theme.S(6), 0, 0);
                p.Controls.Add(hint, 1, 2);

                var b = Theme.Button(r.ButtonText, true);
                b.Anchor = AnchorStyles.Right;
                var url = r.Url;
                b.Click += delegate { Proc.Open(url); };
                p.Controls.Add(b, 2, 0);
                p.SetRowSpan(b, 3);
            }
            return p;
        }
    }

    // ---------------------------------------------------------------- first-run setup
    static class Setup
    {
        // Keep in step with scripts\ensure_uv.bat.
        public const string UvVersion = "0.13.0";

        // uv keeps its Python and download cache inside tools\, so deleting the folder removes everything.
        public static void UvEnvironment()
        {
            Environment.SetEnvironmentVariable("UV_PYTHON_INSTALL_DIR", Path.Combine(Paths.Tools, "python"));
            Environment.SetEnvironmentVariable("UV_CACHE_DIR", Path.Combine(Paths.Tools, "uv-cache"));
            Environment.SetEnvironmentVariable("UV_PYTHON_PREFERENCE", "only-managed");
            Environment.SetEnvironmentVariable("UV_NO_PROGRESS", "1");
        }

        // What the venv was built from: the lock file and the Python version.
        public static string LockHash()
        {
            try
            {
                using (var sha = SHA1.Create())
                {
                    var bytes = new List<byte>(File.ReadAllBytes(Path.Combine(Paths.Root, "uv.lock")));
                    var pv = Path.Combine(Paths.Root, ".python-version");
                    if (File.Exists(pv)) bytes.AddRange(File.ReadAllBytes(pv));
                    return BitConverter.ToString(sha.ComputeHash(bytes.ToArray())).Replace("-", "");
                }
            }
            catch { return ""; }
        }

        public static string Installed()
        {
            try { return File.ReadAllText(Paths.InstalledStamp).Trim(); } catch { return ""; }
        }
    }

    class SetupForm : Form
    {
        readonly bool freshVenv;
        readonly Label step;
        volatile bool cancelled;
        public bool Succeeded;

        public SetupForm(bool freshVenv)
        {
            this.freshVenv = freshVenv;
            Theme.Form(this, "CS2 Highlighter: setting up");
            AutoSize = true;
            AutoSizeMode = AutoSizeMode.GrowAndShrink;
            Padding = new Padding(Theme.S(24), Theme.S(20), Theme.S(24), Theme.S(18));

            var col = new FlowLayoutPanel();
            col.FlowDirection = FlowDirection.TopDown;
            col.WrapContents = false;
            col.AutoSize = true;
            col.Location = new Point(Padding.Left, Padding.Top);
            Controls.Add(col);

            col.Controls.Add(Theme.Label(freshVenv ? "Setting up CS2 Highlighter" : "Updating CS2 Highlighter", Theme.Font(16f, FontStyle.Bold), Theme.Text));
            var sub = Theme.Label(freshVenv ? "This happens once and takes 1 to 2 minutes. Keep your internet on."
                                            : "Installing the new components of this version. Usually under a minute.",
                                  Theme.Font(10f), Theme.Muted);
            sub.Margin = new Padding(0, Theme.S(2), 0, Theme.S(16));
            col.Controls.Add(sub);

            step = Theme.Label("Starting...", Theme.Font(10f, FontStyle.Bold), Theme.Text);
            col.Controls.Add(step);

            var bar = new ProgressBar();
            bar.Style = ProgressBarStyle.Marquee;
            bar.MarqueeAnimationSpeed = 30;
            bar.Size = new Size(Theme.S(480), Theme.S(8));
            bar.Margin = new Padding(0, Theme.S(8), 0, Theme.S(16));
            col.Controls.Add(bar);

            var buttons = new FlowLayoutPanel();
            buttons.FlowDirection = FlowDirection.RightToLeft;
            buttons.AutoSize = true;
            buttons.MinimumSize = new Size(Theme.S(480), 0);
            buttons.Margin = Padding.Empty;
            var cancel = Theme.Button("Cancel", false);
            cancel.Click += delegate { cancelled = true; Close(); };
            var details = Theme.Button("Show details", false);
            details.Click += delegate { Proc.OpenLog(Paths.SetupLog); };
            buttons.Controls.Add(cancel);
            buttons.Controls.Add(details);
            col.Controls.Add(buttons);

            FormClosing += delegate { if (!Succeeded) cancelled = true; };
            Shown += delegate
            {
                var t = new Thread(Work);
                t.IsBackground = true;
                t.Start();
            };
        }

        void Say(string text)
        {
            try { BeginInvoke((MethodInvoker)delegate { step.Text = text; }); } catch { }
        }

        bool Step(string text, string exe, string args)
        {
            Say(text);
            if (cancelled) return false;
            return Proc.Run(exe, args, Paths.SetupLog, p => !cancelled) == 0;
        }

        void DownloadUv()
        {
            var url = "https://github.com/astral-sh/uv/releases/download/" + Setup.UvVersion + "/uv-x86_64-pc-windows-msvc.zip";
            var zip = Path.Combine(Path.GetTempPath(), "cs2h-uv-" + Setup.UvVersion + ".zip");
            ServicePointManager.SecurityProtocol |= (SecurityProtocolType)3072;   // TLS 1.2 (GitHub refuses older)
            using (var wc = new WebClient())
            {
                var done = new ManualResetEvent(false);
                Exception err = null;
                wc.DownloadProgressChanged += delegate(object s, DownloadProgressChangedEventArgs e)
                {
                    Say("Downloading uv (installs Python for the app)... " + e.ProgressPercentage + "%");
                    if (cancelled) wc.CancelAsync();
                };
                wc.DownloadFileCompleted += delegate(object s, System.ComponentModel.AsyncCompletedEventArgs e)
                {
                    err = e.Cancelled ? new OperationCanceledException() : e.Error;
                    done.Set();
                };
                wc.DownloadFileAsync(new Uri(url), zip);
                done.WaitOne();
                if (err != null) throw err;
            }
            if (Directory.Exists(Paths.UvDir)) Directory.Delete(Paths.UvDir, true);
            ZipFile.ExtractToDirectory(zip, Paths.UvDir);
            try { File.Delete(zip); } catch { }
        }

        void Work()
        {
            string fail = null;
            try
            {
                var py = Paths.VenvPython;
                if (!File.Exists(Paths.UvExe))
                {
                    Say("Downloading uv (installs Python for the app)...");
                    try { DownloadUv(); }
                    catch (Exception ex)
                    {
                        if (!cancelled) fail = "Could not download uv from GitHub. Check your internet connection and try again.\n(" + ex.Message + ")";
                    }
                }
                // uv sync: installs Python 3.12 if needed and makes .venv match uv.lock exactly
                // (an old pip-made venv with another Python is rebuilt by itself)
                if (fail == null && !cancelled &&
                    !Step(freshVenv ? "Installing Python and the app's components (the slow part)..." : "Updating the app's components...",
                          Paths.UvExe, "sync --frozen --no-dev"))
                    fail = "Installing the components failed. Check your internet connection and try again.";
                if (fail == null && !cancelled && !Step("Downloading the map radar images...", py, "scripts\\fetch_radars.py"))
                    fail = "Downloading the radar images failed. Check your internet connection and try again.";
                if (fail == null && !cancelled && freshVenv)
                    Step("Adding the Desktop icon...", "powershell.exe", "-NoProfile -ExecutionPolicy Bypass -File scripts\\make_shortcut.ps1");
                if (fail == null && !cancelled) File.WriteAllText(Paths.InstalledStamp, Setup.LockHash());
            }
            catch (Exception ex) { fail = ex.Message; }

            if (cancelled) return;
            BeginInvoke((MethodInvoker)delegate
            {
                if (fail == null) { Succeeded = true; Close(); return; }
                var r = MessageBox.Show(this, fail + "\n\nLast lines of the setup log:\n\n" + Proc.Tail(Paths.SetupLog, 8)
                                        + "\n\nOpen the full log?", "CS2 Highlighter setup failed",
                                        MessageBoxButtons.YesNo, MessageBoxIcon.Error);
                if (r == DialogResult.Yes) Proc.OpenLog(Paths.SetupLog);
                Close();
            });
        }
    }

    // ---------------------------------------------------------------- running the app
    class Splash : Form
    {
        public Splash()
        {
            Theme.Form(this, "CS2 Highlighter");
            FormBorderStyle = FormBorderStyle.None;
            ShowInTaskbar = true;
            TopMost = true;
            Size = new Size(Theme.S(380), Theme.S(110));
            var title = Theme.Label("Starting CS2 Highlighter...", Theme.Font(13f, FontStyle.Bold), Theme.Text);
            title.Location = new Point(Theme.S(24), Theme.S(22));
            Controls.Add(title);
            var sub = Theme.Label("Your browser opens in a moment.", Theme.Font(9.5f), Theme.Muted);
            sub.Location = new Point(Theme.S(24), Theme.S(52));
            Controls.Add(sub);
            var bar = new ProgressBar();
            bar.Style = ProgressBarStyle.Marquee;
            bar.MarqueeAnimationSpeed = 30;
            bar.Bounds = new Rectangle(Theme.S(24), Theme.S(82), Theme.S(332), Theme.S(6));
            Controls.Add(bar);
            Paint += delegate(object s, PaintEventArgs e)
            {
                using (var pen = new Pen(Theme.Accent, Theme.S(2)))
                    e.Graphics.DrawRectangle(pen, 0, 0, Width - 1, Height - 1);
            };
        }
    }

    class TrayApp : ApplicationContext
    {
        readonly int port;
        readonly string url;
        readonly NotifyIcon tray;
        readonly System.Windows.Forms.Timer poll;
        readonly Splash splash;
        readonly Control invoker = new Control();   // marshals the server Exited event to the UI thread
        readonly DateTime started = DateTime.Now;
        Process server;
        StreamWriter log;
        bool ready, quitting;

        public TrayApp(int port)
        {
            this.port = port;
            url = "http://localhost:" + port;

            var menu = new ContextMenuStrip();
            var open = menu.Items.Add("Open CS2 Highlighter", null, delegate { OpenBrowser(); });
            open.Font = new Font(menu.Font, FontStyle.Bold);
            menu.Items.Add("Show log", null, delegate { Proc.OpenLog(Paths.ServerLog); });
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("Quit CS2 Highlighter", null, delegate { Quit(); });

            tray = new NotifyIcon();
            try { tray.Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { tray.Icon = SystemIcons.Application; }
            tray.Text = "CS2 Highlighter (starting...)";
            tray.ContextMenuStrip = menu;
            tray.DoubleClick += delegate { OpenBrowser(); };
            tray.Visible = true;

            invoker.CreateControl();
            splash = new Splash();
            splash.Show();

            if (!StartServer()) return;
            poll = new System.Windows.Forms.Timer();
            poll.Interval = 100;
            poll.Tick += delegate { Tick(); };
            poll.Start();
            Application.ApplicationExit += delegate { StopServer(); };
        }

        bool StartServer()
        {
            try
            {
                Directory.CreateDirectory(Paths.DataDir);
                if (File.Exists(Paths.ServerLog)) File.Copy(Paths.ServerLog, Paths.ServerLog + ".old", true);
                log = new StreamWriter(Paths.ServerLog, false, new UTF8Encoding(false));
                log.AutoFlush = true;
                var psi = new ProcessStartInfo(Paths.VenvPython, "run.py --no-browser");
                psi.WorkingDirectory = Paths.Root;
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;   // children (ffmpeg, powershell) share this hidden console, so no windows flash up
                psi.RedirectStandardOutput = true;
                psi.RedirectStandardError = true;
                psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
                psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";
                server = new Process();
                server.StartInfo = psi;
                server.EnableRaisingEvents = true;
                DataReceivedEventHandler h = delegate(object s, DataReceivedEventArgs e)
                {
                    if (e.Data == null) return;
                    lock (log) { try { log.WriteLine(e.Data); } catch { } }
                };
                server.OutputDataReceived += h;
                server.ErrorDataReceived += h;
                server.Exited += delegate { try { invoker.BeginInvoke((MethodInvoker)ServerExited); } catch { } };
                server.Start();
                server.BeginOutputReadLine();
                server.BeginErrorReadLine();
                return true;
            }
            catch (Exception ex)
            {
                splash.Close();
                MessageBox.Show("Could not start the app:\n\n" + ex.Message, "CS2 Highlighter", MessageBoxButtons.OK, MessageBoxIcon.Error);
                tray.Visible = false;
                ExitThreadCore();
                return false;
            }
        }

        void Tick()
        {
            if (ready) return;
            if (Program.PortOpen(port))
            {
                ready = true;
                poll.Stop();
                var ms = (int)(DateTime.Now - Process.GetCurrentProcess().StartTime).TotalMilliseconds;
                lock (log) { try { log.WriteLine("[launcher] app ready " + ms + " ms after the exe started"); } catch { } }
                tray.Text = "CS2 Highlighter is running";
                splash.Close();
                OpenBrowser();
                tray.ShowBalloonTip(6000, "CS2 Highlighter is running",
                                    "It keeps running down here. Right-click this icon to open it again or to quit.", ToolTipIcon.Info);
            }
            else if ((DateTime.Now - started).TotalSeconds > 120)
            {
                poll.Stop();
                splash.Close();
                var r = MessageBox.Show("The app is taking unusually long to start. Keep waiting?\n\n(No = quit and open the log)",
                                        "CS2 Highlighter", MessageBoxButtons.YesNo, MessageBoxIcon.Warning);
                if (r == DialogResult.Yes) poll.Start();
                else { Proc.OpenLog(Paths.ServerLog); Quit(); }
            }
        }

        void ServerExited()
        {
            if (quitting) return;
            if (poll != null) poll.Stop();
            if (!splash.IsDisposed) splash.Close();
            tray.Visible = false;
            var r = MessageBox.Show("CS2 Highlighter stopped unexpectedly.\n\nLast lines of the log:\n\n" + Proc.Tail(Paths.ServerLog, 10)
                                    + "\n\nOpen the full log?", "CS2 Highlighter", MessageBoxButtons.YesNo, MessageBoxIcon.Error);
            if (r == DialogResult.Yes) Proc.OpenLog(Paths.ServerLog);
            ExitThreadCore();
        }

        void OpenBrowser()
        {
            if (!ready)
            {
                tray.ShowBalloonTip(3000, "CS2 Highlighter", "Still starting, the browser opens by itself in a moment.", ToolTipIcon.Info);
                return;
            }
            Proc.Open(url);
        }

        void StopServer()
        {
            quitting = true;
            if (server != null) { try { if (!server.HasExited) Proc.KillTree(server.Id, true); } catch { } }
        }

        void Quit()
        {
            StopServer();
            tray.Visible = false;
            if (!splash.IsDisposed) splash.Close();
            ExitThreadCore();
        }
    }

    static class Program
    {
        [DllImport("user32.dll")] static extern bool SetProcessDPIAware();

        public static bool PortOpen(int port)
        {
            try
            {
                using (var c = new TcpClient())
                {
                    var ar = c.BeginConnect("127.0.0.1", port, null, null);
                    if (!ar.AsyncWaitHandle.WaitOne(250)) return false;
                    c.EndConnect(ar);
                    return true;
                }
            }
            catch { return false; }
        }

        static void Fatal(string msg)
        {
            MessageBox.Show(msg, "CS2 Highlighter", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }

        [STAThread]
        static int Main()
        {
            try { SetProcessDPIAware(); } catch { }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            using (var g = Graphics.FromHwnd(IntPtr.Zero)) Theme.Scale = g.DpiX / 96f;

            Paths.Root = Path.GetDirectoryName(Application.ExecutablePath);
            if (Paths.Root.StartsWith(@"\\"))
            {
                Fatal("This folder is on a network path:\n" + Paths.Root + "\n\nCopy the CS2 Highlighter folder to a normal place such as C:\\cs2-highlighter and run it from there.");
                return 1;
            }
            if (!File.Exists(Path.Combine(Paths.Root, "run.py")))
            {
                Fatal("CS2 Highlighter.exe must stay inside the CS2 Highlighter folder (next to run.py).\n\nTo get an icon somewhere else, use the Desktop shortcut it creates.");
                return 1;
            }

            int port = Config.Port();
            bool first;
            using (var mutex = new Mutex(true, @"Local\CS2HighlighterLauncher", out first))
            {
                if (!first || PortOpen(port))
                {
                    // already running (this launcher, or start.bat): just bring up the page
                    if (PortOpen(port)) Proc.Open("http://localhost:" + port);
                    return 0;
                }

                Setup.UvEnvironment();
                // fast path: setup finished before for this uv.lock, so there is nothing to install or check
                bool stampOk = File.Exists(Paths.VenvPython) && Setup.Installed() == Setup.LockHash();
                bool freshVenv = !File.Exists(Paths.VenvPython);
                var reqs = Checks.Run();
                if (!reqs.TrueForAll(x => x.Found) && !File.Exists(Paths.SkipCs2Marker))
                {
                    using (var f = new RequirementsForm(reqs))
                        if (f.ShowDialog() != DialogResult.OK) return 0;
                }

                if (!stampOk)
                {
                    var s = new SetupForm(freshVenv);
                    Application.Run(s);
                    if (!s.Succeeded) return 1;
                }

                Application.Run(new TrayApp(port));
                GC.KeepAlive(mutex);
            }
            return 0;
        }
    }
}
