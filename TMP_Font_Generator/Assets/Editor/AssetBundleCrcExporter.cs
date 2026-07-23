using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using UnityEditor;
using UnityEngine;

namespace Translate.EditorTools
{
    [Serializable]
    public class AssetBundleCrcJob
    {
        public string bundleRoot;
        public string outputPath;
    }

    [Serializable]
    public class AssetBundleCrcEntry
    {
        public string name;
        public string relativePath;
        public string fullPath;
        public uint crc;
        public long size;
        public bool success;
        public string error;
    }

    [Serializable]
    public class AssetBundleCrcReport
    {
        public List<AssetBundleCrcEntry> bundles = new List<AssetBundleCrcEntry>();
    }

    public static class AssetBundleCrcExporter
    {
        private const string DefaultJobPath = "Assets/SourceFonts/bundle-crc-job.json";

        public static void Run()
        {
            var jobPath = GetArgumentValue("--job") ?? DefaultJobPath;
            var job = LoadJob(jobPath);
            Export(job);
        }

        private static AssetBundleCrcJob LoadJob(string jobPath)
        {
            var absoluteJobPath = ToAbsolutePath(jobPath);
            if (!File.Exists(absoluteJobPath))
            {
                throw new FileNotFoundException($"CRC job file not found: {absoluteJobPath}");
            }

            var json = File.ReadAllText(absoluteJobPath, Encoding.UTF8);
            var job = JsonUtility.FromJson<AssetBundleCrcJob>(json);
            if (job == null)
            {
                throw new InvalidOperationException($"Failed to parse CRC job file: {absoluteJobPath}");
            }
            return job;
        }

        private static void Export(AssetBundleCrcJob job)
        {
            if (string.IsNullOrWhiteSpace(job.bundleRoot))
            {
                throw new ArgumentException("bundleRoot is required");
            }
            if (string.IsNullOrWhiteSpace(job.outputPath))
            {
                throw new ArgumentException("outputPath is required");
            }

            var bundleRoot = ToAbsolutePath(job.bundleRoot);
            var outputPath = ToAbsolutePath(job.outputPath);
            if (!Directory.Exists(bundleRoot))
            {
                throw new DirectoryNotFoundException($"Bundle root not found: {bundleRoot}");
            }

            var report = new AssetBundleCrcReport();
            foreach (var bundlePath in Directory.GetFiles(bundleRoot, "*.bundle", SearchOption.AllDirectories))
            {
                var entry = new AssetBundleCrcEntry
                {
                    name = Path.GetFileName(bundlePath),
                    relativePath = ToRelativePath(bundleRoot, bundlePath).Replace('\\', '/'),
                    fullPath = bundlePath,
                    size = new FileInfo(bundlePath).Length,
                    success = false,
                    error = "",
                };

                try
                {
                    if (BuildPipeline.GetCRCForAssetBundle(bundlePath, out var crc))
                    {
                        entry.crc = crc;
                        entry.success = true;
                    }
                    else
                    {
                        entry.error = "BuildPipeline.GetCRCForAssetBundle returned false";
                    }
                }
                catch (Exception ex)
                {
                    entry.error = ex.GetType().Name + ": " + ex.Message;
                }

                report.bundles.Add(entry);
                Debug.Log($"[BundleCRC] {entry.relativePath} success={entry.success} crc={entry.crc} size={entry.size} {entry.error}");
            }

            Directory.CreateDirectory(Path.GetDirectoryName(outputPath));
            File.WriteAllText(outputPath, JsonUtility.ToJson(report, true), Encoding.UTF8);
            Debug.Log($"[BundleCRC] Wrote report: {outputPath}, count={report.bundles.Count}");
        }

        private static string GetArgumentValue(string name)
        {
            var args = Environment.GetCommandLineArgs();
            for (int i = 0; i < args.Length - 1; i++)
            {
                if (args[i] == name)
                {
                    return args[i + 1];
                }
            }
            return null;
        }

        private static string ToAbsolutePath(string path)
        {
            if (Path.IsPathRooted(path))
            {
                return Path.GetFullPath(path);
            }
            return Path.GetFullPath(Path.Combine(Directory.GetCurrentDirectory(), path));
        }

        private static string ToRelativePath(string root, string path)
        {
            var rootUri = new Uri(AppendDirectorySeparatorChar(Path.GetFullPath(root)));
            var pathUri = new Uri(Path.GetFullPath(path));
            return Uri.UnescapeDataString(rootUri.MakeRelativeUri(pathUri).ToString()).Replace('/', Path.DirectorySeparatorChar);
        }

        private static string AppendDirectorySeparatorChar(string path)
        {
            if (!path.EndsWith(Path.DirectorySeparatorChar.ToString(), StringComparison.Ordinal))
            {
                return path + Path.DirectorySeparatorChar;
            }
            return path;
        }
    }
}
