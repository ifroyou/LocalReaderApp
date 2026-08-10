using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Windows.Forms;

internal static class Program
{
    private const string LauncherMutexName = @"Local\LocalReaderApp.Launcher.v3";
    private const int SetupTimeoutMilliseconds = 30 * 60 * 1000;
    private const int OpenTimeoutMilliseconds = 150 * 1000;

    private static bool RuntimeReady(string python)
    {
        if (String.IsNullOrWhiteSpace(python) || !File.Exists(python))
        {
            return false;
        }

        try
        {
            ProcessStartInfo check = new ProcessStartInfo();
            check.FileName = python;
            // CPU/ONNX is a complete, supported runtime even on a machine that
            // also has an NVIDIA GPU. CUDA remains an optional acceleration path.
            check.Arguments = "-c \"import fitz,pytesseract,PIL,vieneu,onnxruntime\"";
            check.UseShellExecute = false;
            check.CreateNoWindow = true;
            check.WindowStyle = ProcessWindowStyle.Hidden;
            check.RedirectStandardOutput = true;
            check.RedirectStandardError = true;
            using (Process process = Process.Start(check))
            {
                if (process == null || !process.WaitForExit(45000))
                {
                    try { if (process != null) process.Kill(); } catch { }
                    return false;
                }
                return process.ExitCode == 0;
            }
        }
        catch
        {
            return false;
        }
    }

    private static string FindBootstrapPython(string appDir, string runtimePython)
    {
        string[] candidates = new string[]
        {
            Path.Combine(appDir, "python", "python.exe"),
            Path.Combine(appDir, ".vieneu_test", "Scripts", "python.exe"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), @"anaconda3\envs\localreader\python.exe"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), @"anaconda3\python.exe"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), @"Programs\Python\Python312\python.exe"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), @"Programs\Python\Python311\python.exe"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), @"Programs\Python\Python310\python.exe"),
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), @"Programs\Python\Python313\python.exe"),
            runtimePython
        };

        foreach (string candidate in candidates)
        {
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }
        return null;
    }

    private static string ReadLogTail(string logPath, int maxLines, int maxCharacters)
    {
        try
        {
            if (!File.Exists(logPath))
            {
                return "";
            }

            string[] lines = File.ReadAllLines(logPath, Encoding.UTF8);
            int first = Math.Max(0, lines.Length - maxLines);
            string tail = String.Join(Environment.NewLine, lines, first, lines.Length - first).Trim();
            if (tail.Length > maxCharacters)
            {
                tail = tail.Substring(tail.Length - maxCharacters);
            }
            return tail;
        }
        catch
        {
            return "";
        }
    }

    private static void ShowLaunchFailure(string reason, string stderr, string logPath)
    {
        StringBuilder message = new StringBuilder();
        message.AppendLine("Local Reader chưa mở được.");
        message.AppendLine(reason);

        string details = (stderr ?? "").Trim();
        if (details.Length > 1800)
        {
            details = details.Substring(details.Length - 1800);
        }
        if (details.Length == 0)
        {
            details = ReadLogTail(logPath, 14, 2200);
        }
        if (details.Length > 0)
        {
            message.AppendLine();
            message.AppendLine("Chi tiết cuối:");
            message.AppendLine(details);
        }
        message.AppendLine();
        message.Append("Nhật ký: ").Append(logPath);

        MessageBox.Show(
            message.ToString(),
            "Local Reader",
            MessageBoxButtons.OK,
            MessageBoxIcon.Error
        );
    }

    private static int RunLauncher()
    {
        string appDir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        string script = Path.Combine(appDir, "open_reader.py");
        string setupScript = Path.Combine(appDir, "setup_local_reader.py");
        string runtimeDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "LocalReaderApp");
        string runtimePython = Path.Combine(runtimeDir, @".vieneu_test\Scripts\python.exe");
        string logPath = Path.Combine(runtimeDir, "reader_server.log");

        if (!File.Exists(script))
        {
            MessageBox.Show("Không tìm thấy open_reader.py trong thư mục ứng dụng.", "Local Reader", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }

        string bootstrapPython = FindBootstrapPython(appDir, runtimePython);
        if (bootstrapPython == null)
        {
            MessageBox.Show("Không tìm thấy Python để cài hoặc mở Local Reader.", "Local Reader", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }

        if (!RuntimeReady(runtimePython))
        {
            if (!File.Exists(setupScript))
            {
                MessageBox.Show("Không tìm thấy bộ cài đặt Local Reader.", "Local Reader", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            }

            MessageBox.Show(
                "Lần đầu mở trên máy này, Local Reader sẽ cài bộ xử lý phù hợp. CPU/ONNX luôn dùng được; nếu CUDA tương thích thì ứng dụng sẽ tự tăng tốc bằng NVIDIA. Quá trình có thể mất vài phút.",
                "Local Reader - Cài đặt lần đầu",
                MessageBoxButtons.OK,
                MessageBoxIcon.Information
            );

            ProcessStartInfo setupPsi = new ProcessStartInfo();
            setupPsi.FileName = bootstrapPython;
            setupPsi.Arguments = "\"" + setupScript + "\"";
            setupPsi.WorkingDirectory = appDir;
            setupPsi.UseShellExecute = true;
            setupPsi.WindowStyle = ProcessWindowStyle.Normal;
            using (Process setupProcess = Process.Start(setupPsi))
            {
                if (setupProcess == null || !setupProcess.WaitForExit(SetupTimeoutMilliseconds))
                {
                    try { if (setupProcess != null) setupProcess.Kill(); } catch { }
                    MessageBox.Show("Cài đặt Local Reader quá thời gian cho phép. Hãy mở lại ứng dụng để thử tiếp.", "Local Reader", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return 1;
                }
                if (setupProcess.ExitCode != 0)
                {
                    MessageBox.Show("Cài đặt Local Reader chưa thành công. Hãy xem cửa sổ cài đặt để biết lỗi.", "Local Reader", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return 1;
                }
            }

            // A setup process can exit successfully even when a dependency was
            // skipped or failed. Verify the actual runtime before launching.
            if (!RuntimeReady(runtimePython))
            {
                MessageBox.Show(
                    "Bộ xử lý chưa sẵn sàng sau khi cài. Cần đủ fitz, pytesseract, PIL, vieneu và onnxruntime. Hãy mở lại ứng dụng sau khi kiểm tra kết nối.",
                    "Local Reader",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error
                );
                return 1;
            }
        }

        ProcessStartInfo psi = new ProcessStartInfo();
        psi.FileName = runtimePython;
        psi.Arguments = "-B \"" + script + "\"";
        psi.WorkingDirectory = appDir;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.WindowStyle = ProcessWindowStyle.Hidden;
        psi.RedirectStandardOutput = true;
        psi.RedirectStandardError = true;
        psi.StandardOutputEncoding = Encoding.UTF8;
        psi.StandardErrorEncoding = Encoding.UTF8;
        psi.EnvironmentVariables["LOCAL_READER_RUNTIME_DIR"] = runtimeDir;
        psi.EnvironmentVariables["LOCAL_READER_VIENEU_PORTS"] = "8766";
        psi.EnvironmentVariables["LOCAL_READER_VIENEU_ENABLED"] = "1";
        psi.EnvironmentVariables["LOCAL_READER_BACKGROUND_WORKERS"] = "1";
        psi.EnvironmentVariables["LOCAL_READER_TTS_MODEL_VERSION"] = "vieneu-tts-v3-turbo-48khz";
        psi.EnvironmentVariables["LOCAL_READER_VIENEU_MODEL_VERSION"] = "vieneu-tts-v3-turbo-48khz";
        psi.EnvironmentVariables["LOCAL_READER_VIENEU_MODEL_FORMAT"] = "v3-turbo";
        psi.EnvironmentVariables["LOCAL_READER_VIENEU_MODE"] = "v3turbo";
        psi.EnvironmentVariables["LOCAL_READER_VIENEU_BACKBONE_DEVICE"] = "auto";
        psi.EnvironmentVariables["LOCAL_READER_VIENEU_BACKEND"] = "auto";
        psi.EnvironmentVariables["LOCAL_READER_VIENEU_BACKBONE_REPO"] = "pnnbao-ump/VieNeu-TTS-v3-Turbo";
        psi.EnvironmentVariables["LOCAL_READER_VIENEU_VOICE"] = "Trúc Ly";
        psi.EnvironmentVariables["LOCAL_READER_VIENEU_VOICE_LABEL"] = "Trúc Ly [VieNeu v3]";
        psi.EnvironmentVariables["LOCAL_READER_VIENEU_GGUF_FILENAME"] = "";
        psi.EnvironmentVariables["HF_HUB_DISABLE_XET"] = "1";

        using (Process process = new Process())
        {
            StringBuilder stderrCapture = new StringBuilder();
            object stderrLock = new object();
            process.StartInfo = psi;
            process.OutputDataReceived += delegate(object sender, DataReceivedEventArgs args)
            {
                // Drain stdout asynchronously so the hidden Python process can
                // never block on a full redirected pipe.
            };
            process.ErrorDataReceived += delegate(object sender, DataReceivedEventArgs args)
            {
                if (args.Data == null) return;
                lock (stderrLock)
                {
                    stderrCapture.AppendLine(args.Data);
                    if (stderrCapture.Length > 12000)
                    {
                        stderrCapture.Remove(0, stderrCapture.Length - 9000);
                    }
                }
            };

            if (!process.Start())
            {
                ShowLaunchFailure("Không tạo được tiến trình mở ứng dụng.", "", logPath);
                return 1;
            }
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();

            if (!process.WaitForExit(OpenTimeoutMilliseconds))
            {
                try { process.Kill(); } catch { }
                try { process.WaitForExit(5000); } catch { }
                string timeoutError;
                lock (stderrLock) { timeoutError = stderrCapture.ToString(); }
                ShowLaunchFailure("Ứng dụng phản hồi quá chậm (quá 150 giây).", timeoutError, logPath);
                return 1;
            }

            // Complete pending asynchronous read callbacks before inspecting
            // the captured error text.
            process.WaitForExit();
            string stderr;
            lock (stderrLock) { stderr = stderrCapture.ToString(); }
            if (process.ExitCode != 0)
            {
                ShowLaunchFailure("Tiến trình mở ứng dụng đã dừng với mã " + process.ExitCode + ".", stderr, logPath);
                return process.ExitCode;
            }
        }
        return 0;
    }

    [STAThread]
    private static int Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);

        bool ownsMutex = false;
        Mutex launcherMutex = null;
        try
        {
            launcherMutex = new Mutex(true, LauncherMutexName, out ownsMutex);
            if (!ownsMutex)
            {
                // The first click is already starting/focusing the app.
                return 0;
            }
            return RunLauncher();
        }
        catch (Exception ex)
        {
            MessageBox.Show("Không mở được Local Reader.\r\n\r\n" + ex.Message, "Local Reader", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        finally
        {
            if (launcherMutex != null)
            {
                if (ownsMutex)
                {
                    try { launcherMutex.ReleaseMutex(); } catch { }
                }
                launcherMutex.Dispose();
            }
        }
    }
}
