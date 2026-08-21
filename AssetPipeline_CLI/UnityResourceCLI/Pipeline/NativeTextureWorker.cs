using AssetsTools.NET.Texture;
using StbImageSharp;
using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using System.Threading.Tasks;

namespace UnityResourceCLI
{
    internal sealed class NativeTextureEncodingResult
    {
        public int Width { get; init; }
        public int Height { get; init; }
        public byte[][] Mips { get; init; } = [];
    }

    internal static class NativeTextureWorker
    {
        private const int OutputMagic = 0x31584554; // TEX1

        public static int Run(string[] args)
        {
            if (args.Length != 5)
            {
                Console.Error.WriteLine(
                    "encode-texture-worker requires: <input> <output> <format> <mips> <quality>");
                return 2;
            }

            string inputPath = Path.GetFullPath(args[0]);
            string outputPath = Path.GetFullPath(args[1]);
            TextureFormat format = (TextureFormat)int.Parse(args[2], CultureInfo.InvariantCulture);
            int mipCount = int.Parse(args[3], CultureInfo.InvariantCulture);
            int quality = int.Parse(args[4], CultureInfo.InvariantCulture);

            if (!TextureEncoderWrapper.NativeLibrariesSupported())
                throw new InvalidOperationException(
                    "textureencoder.dll/cuttlefish.dll is unavailable in the native texture worker.");

            using FileStream input = File.OpenRead(inputPath);
            ImageResult image = ImageResult.FromStream(input, ColorComponents.RedGreenBlueAlpha);
            byte[] bgraData = (byte[])image.Data.Clone();
            TextureOperations.SwapRBComponentsInplace(bgraData);
            byte[][] encodedMips = TextureEncoderWrapper.ConvertImage(
                bgraData,
                mipCount,
                format,
                image.Width,
                image.Height,
                quality
            );
            if (encodedMips == null || encodedMips.Length == 0)
                throw new InvalidOperationException("Native texture encoder returned no mip data.");

            Directory.CreateDirectory(Path.GetDirectoryName(outputPath)!);
            using FileStream output = new FileStream(outputPath, FileMode.Create, FileAccess.Write, FileShare.None);
            using var writer = new BinaryWriter(output, Encoding.UTF8, leaveOpen: false);
            writer.Write(OutputMagic);
            writer.Write(image.Width);
            writer.Write(image.Height);
            writer.Write(encodedMips.Length);
            foreach (byte[] mip in encodedMips)
            {
                writer.Write(mip.Length);
                writer.Write(mip);
            }
            return 0;
        }

        public static NativeTextureEncodingResult EncodeIsolated(
            string inputPath,
            TextureFormat format,
            int mipCount,
            int quality)
        {
            string outputPath = Path.Combine(
                Path.GetTempPath(),
                $"UnityResourceCLI_texture_{Guid.NewGuid():N}.bin"
            );
            try
            {
                string assemblyPath = typeof(NativeTextureWorker).Assembly.Location;
                string dotnetHost = Environment.GetEnvironmentVariable("DOTNET_HOST_PATH") ?? "dotnet";
                var startInfo = new ProcessStartInfo
                {
                    FileName = dotnetHost,
                    WorkingDirectory = AppContext.BaseDirectory,
                    UseShellExecute = false,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    CreateNoWindow = true,
                };
                startInfo.ArgumentList.Add(assemblyPath);
                startInfo.ArgumentList.Add("encode-texture-worker");
                startInfo.ArgumentList.Add(Path.GetFullPath(inputPath));
                startInfo.ArgumentList.Add(outputPath);
                startInfo.ArgumentList.Add(((int)format).ToString(CultureInfo.InvariantCulture));
                startInfo.ArgumentList.Add(mipCount.ToString(CultureInfo.InvariantCulture));
                startInfo.ArgumentList.Add(quality.ToString(CultureInfo.InvariantCulture));

                using Process process = Process.Start(startInfo)
                    ?? throw new InvalidOperationException("Failed to start native texture worker.");
                Task<string> stdoutTask = process.StandardOutput.ReadToEndAsync();
                Task<string> stderrTask = process.StandardError.ReadToEndAsync();
                process.WaitForExit();
                string stdout = stdoutTask.GetAwaiter().GetResult().Trim();
                string stderr = stderrTask.GetAwaiter().GetResult().Trim();
                if (process.ExitCode != 0)
                {
                    throw new InvalidOperationException(
                        $"Native texture worker exited with code {process.ExitCode}." +
                        FormatWorkerOutput(stdout, stderr));
                }
                if (!File.Exists(outputPath))
                {
                    throw new InvalidOperationException(
                        "Native texture worker succeeded but did not create its output." +
                        FormatWorkerOutput(stdout, stderr));
                }

                return ReadResult(outputPath);
            }
            finally
            {
                try
                {
                    if (File.Exists(outputPath))
                        File.Delete(outputPath);
                }
                catch
                {
                    // Temporary worker output is best-effort cleanup only.
                }
            }
        }

        private static NativeTextureEncodingResult ReadResult(string outputPath)
        {
            using FileStream input = File.OpenRead(outputPath);
            using var reader = new BinaryReader(input, Encoding.UTF8, leaveOpen: false);
            if (reader.ReadInt32() != OutputMagic)
                throw new InvalidDataException("Native texture worker output has an invalid header.");

            int width = reader.ReadInt32();
            int height = reader.ReadInt32();
            int mipCount = reader.ReadInt32();
            if (width <= 0 || height <= 0 || mipCount <= 0 || mipCount > 64)
                throw new InvalidDataException("Native texture worker output metadata is invalid.");

            byte[][] mips = new byte[mipCount][];
            for (int i = 0; i < mipCount; i++)
            {
                int size = reader.ReadInt32();
                if (size <= 0 || size > input.Length - input.Position)
                    throw new InvalidDataException($"Native texture worker mip {i} is truncated.");
                byte[] data = reader.ReadBytes(size);
                if (data.Length != size)
                    throw new InvalidDataException($"Native texture worker mip {i} is truncated.");
                mips[i] = data;
            }
            if (input.Position != input.Length)
                throw new InvalidDataException("Native texture worker output contains trailing data.");

            return new NativeTextureEncodingResult
            {
                Width = width,
                Height = height,
                Mips = mips,
            };
        }

        private static string FormatWorkerOutput(string stdout, string stderr)
        {
            var details = new StringBuilder();
            if (!string.IsNullOrWhiteSpace(stderr))
                details.Append($" stderr: {stderr}");
            if (!string.IsNullOrWhiteSpace(stdout))
                details.Append($" stdout: {stdout}");
            return details.ToString();
        }
    }
}
