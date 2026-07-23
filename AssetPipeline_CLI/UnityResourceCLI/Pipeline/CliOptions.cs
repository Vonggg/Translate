using System;
using System.Collections.Generic;
using System.IO;

namespace UnityResourceCLI
{
    internal sealed class CliOptions
    {
        public string Command { get; init; } = "";
        public string SourceRoot { get; init; } = "";
        public string WorkRoot { get; init; } = "";
        public string ManagedRoot { get; init; } = "";
        public string ReplacementRoot { get; init; } = "";
        public string ResultRoot { get; init; } = "";
        public string DumpFormat { get; init; } = "json";
        public string ImageFormat { get; init; } = "png";
        public int JpegQuality { get; init; } = 90;
        public bool ShowHelp { get; init; }

        public static CliOptions Parse(string[] args)
        {
            string command = args[0].Trim().ToLowerInvariant();
            if (command is "-h" or "--help" or "help")
            {
                return new CliOptions { ShowHelp = true };
            }

            var dict = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            for (int i = 1; i < args.Length; i++)
            {
                string arg = args[i];
                if (!arg.StartsWith("--", StringComparison.Ordinal))
                    continue;

                string key = arg[2..];
                string value = "true";
                int eqIndex = key.IndexOf('=');
                if (eqIndex >= 0)
                {
                    value = key[(eqIndex + 1)..];
                    key = key[..eqIndex];
                }
                else if (i + 1 < args.Length && !args[i + 1].StartsWith("--", StringComparison.Ordinal))
                {
                    value = args[++i];
                }

                dict[key] = value;
            }

            dict.TryGetValue("help", out string? helpValue);
            if (bool.TryParse(helpValue, out bool showHelp) && showHelp)
                return new CliOptions { ShowHelp = true };

            string sourceRoot = GetRequired(dict, "source");
            string workRoot = GetRequired(dict, "work");
            string managedRoot = GetOptional(dict, "managed", Path.Combine(sourceRoot, "Managed"));
            string replacementRoot = GetOptional(dict, "replacement-root", "");
            string resultRoot = GetOptional(dict, "result-root", "");
            string dumpFormat = GetOptional(dict, "dump-format", "json").ToLowerInvariant();
            string imageFormat = GetOptional(dict, "image-format", "png").ToLowerInvariant();
            int jpegQuality = int.TryParse(GetOptional(dict, "quality", "90"), out int parsedQuality) ? parsedQuality : 90;

            return new CliOptions
            {
                Command = command,
                SourceRoot = Path.GetFullPath(sourceRoot),
                WorkRoot = Path.GetFullPath(workRoot),
                ManagedRoot = Path.GetFullPath(managedRoot),
                ReplacementRoot = string.IsNullOrWhiteSpace(replacementRoot) ? "" : Path.GetFullPath(replacementRoot),
                ResultRoot = string.IsNullOrWhiteSpace(resultRoot) ? "" : Path.GetFullPath(resultRoot),
                DumpFormat = dumpFormat,
                ImageFormat = imageFormat,
                JpegQuality = jpegQuality
            };
        }

        public static void PrintHelp()
        {
            Console.WriteLine("UnityResourceCLI");
            Console.WriteLine("Usage:");
            Console.WriteLine("  UnityResourceCLI export --source <game_root> --work <work_root> [--managed <managed_dir>] [--dump-format json|txt] [--image-format png|jpg] [--quality 90]");
            Console.WriteLine("  UnityResourceCLI import --source <game_root> --work <work_root> [--replacement-root <overlay_root>] [--result-root <result_root>] [--managed <managed_dir>] [--dump-format json|txt] [--image-format png|jpg] [--quality 90]");
            Console.WriteLine();
            Console.WriteLine("Notes:");
            Console.WriteLine("  - Exports Texture2D as PNG/JPG and keeps the same support on import.");
            Console.WriteLine("  - Exports TextAsset, MonoBehaviour, and Material as structured dump files.");
            Console.WriteLine("  - MonoBehaviour templates are built from DLLs in the Managed folder.");
            Console.WriteLine("  - Import reads replacement files from --replacement-root first, then falls back to the export work tree.");
        }

        private static string GetRequired(Dictionary<string, string> dict, string key)
        {
            if (!dict.TryGetValue(key, out string? value) || string.IsNullOrWhiteSpace(value))
            {
                throw new ArgumentException($"Missing required option --{key}.");
            }
            return value;
        }

        private static string GetOptional(Dictionary<string, string> dict, string key, string defaultValue)
        {
            return dict.TryGetValue(key, out string? value) && !string.IsNullOrWhiteSpace(value)
                ? value
                : defaultValue;
        }
    }
}
