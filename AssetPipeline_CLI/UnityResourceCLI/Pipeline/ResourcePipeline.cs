using AssetsTools.NET;
using AssetsTools.NET.Extra;
using AssetsTools.NET.Texture;
using Newtonsoft.Json.Linq;
using StbImageSharp;
using System;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using UABEAvalonia;

namespace UnityResourceCLI
{
    internal sealed class ResourcePipeline
    {
        private const int ExportManifestSchemaVersion = 5;
        private const long ExportProgressInterval = 10_000;
        private const int LargeReplacementSpoolThreshold = 8 * 1024 * 1024;
        private bool legacyManifestReferenceWarningShown;
        private readonly List<(FileStream Stream, string Path)> temporaryAssetReplacements = new();

        private static readonly AssetClassID[] BasicTypes =
        {
            AssetClassID.Texture2D,
            AssetClassID.TextAsset,
            AssetClassID.MonoBehaviour,
            AssetClassID.Material,
            AssetClassID.Font
        };

        private static readonly AssetClassID[] ObjectIndexTypes =
        {
            AssetClassID.GameObject,
            AssetClassID.Transform,
            AssetClassID.RectTransform,
            AssetClassID.Sprite,
            AssetClassID.SpriteAtlas,
            AssetClassID.SpriteRenderer
        };

        private static readonly AssetClassID[] MeshTypes =
        {
            AssetClassID.Mesh,
            AssetClassID.MeshFilter,
            AssetClassID.SkinnedMeshRenderer
        };

        private readonly CliOptions options;
        private readonly AssetsManager am;
        private readonly ExportProgress? exportProgress;
        private string? cachedExporterBuildFingerprint;
        private string? cachedManagedStateFingerprint;
        private bool playerDataDependenciesLoaded;

        public ResourcePipeline(CliOptions options) : this(options, null)
        {
        }

        private ResourcePipeline(CliOptions options, ExportProgress? exportProgress)
        {
            this.options = options;
            this.exportProgress = exportProgress;
            am = new AssetsManager
            {
                UseTemplateFieldCache = true,
                UseMonoTemplateFieldCache = true,
                UseRefTypeManagerCache = true,
                UseQuickLookup = true
            };

            string classDataPath = Path.Combine(AppContext.BaseDirectory, "classdata.tpk");
            if (File.Exists(classDataPath))
            {
                am.LoadClassPackage(classDataPath);
            }

            if (Directory.Exists(options.ManagedRoot))
            {
                am.MonoTempGenerator = new MonoCecilTempGenerator(options.ManagedRoot);
            }
        }

        public int Run()
        {
            Log($"Source: {options.SourceRoot}");
            Log($"Work:   {options.WorkRoot}");
            Log($"Managed:{options.ManagedRoot}");
            if (!string.IsNullOrWhiteSpace(options.ReplacementRoot))
                Log($"Overlay:{options.ReplacementRoot}");
            if (!string.IsNullOrWhiteSpace(options.ResultRoot))
                Log($"Result: {options.ResultRoot}");

            if (!Directory.Exists(options.SourceRoot))
                throw new DirectoryNotFoundException(options.SourceRoot);

            Directory.CreateDirectory(options.WorkRoot);

            Log($"Mode:   {options.Command}");
            if (options.Command == "export")
                Log($"Export profile: {options.ExportProfile}");

            return options.Command switch
            {
                "export" => Export(),
                "import" => Import(),
                _ => throw new ArgumentException($"Unknown command: {options.Command}")
            };
        }

        private int Export()
        {
            Log("Scanning resource files...");
            List<string> sourceFiles = EnumerateCandidateFiles(options.SourceRoot).ToList();
            Log($"Found {sourceFiles.Count} candidate file(s).");

            int workerCount = options.ExportWorkers > 0
                ? options.ExportWorkers
                : Math.Min(4, Math.Max(1, Environment.ProcessorCount / 2));
            workerCount = Math.Min(workerCount, Math.Max(1, sourceFiles.Count));
            Log($"Export workers: {workerCount}.");

            Log($"Single-pass export enabled. Progress heartbeat: every {ExportProgressInterval:N0} item(s).");
            var progress = new ExportProgress(ExportProgressInterval);

            if (workerCount == 1)
            {
                var worker = new ResourcePipeline(options, progress);
                int serialProcessed = 0;
                foreach (string sourcePath in sourceFiles)
                {
                    serialProcessed++;
                    Log($"[{serialProcessed}/{sourceFiles.Count}] Exporting {Path.GetFileName(sourcePath)}");
                    worker.ExportFile(sourcePath);
                }
                progress.LogFinal();
                if (progress.HasCriticalFailure)
                {
                    LogRed("Export stopped: one or more asset byte ranges are outside the source file.");
                    return 2;
                }
                Log($"Export finished. Processed {serialProcessed} file(s).");
                return 0;
            }

            int processed = 0;
            var parallelOptions = new ParallelOptions { MaxDegreeOfParallelism = workerCount };
            using var workers = new ThreadLocal<ResourcePipeline>(() => new ResourcePipeline(options, progress), true);
            Parallel.ForEach(sourceFiles, parallelOptions, sourcePath =>
            {
                ResourcePipeline worker = workers.Value!;
                worker.ExportFile(sourcePath);
                int completed = Interlocked.Increment(ref processed);
                Log($"[{completed}/{sourceFiles.Count}] Exported {Path.GetFileName(sourcePath)}");
            });

            progress.LogFinal();
            if (progress.HasCriticalFailure)
            {
                LogRed("Export stopped: one or more asset byte ranges are outside the source file.");
                return 2;
            }
            Log($"Export finished. Processed {processed} file(s).");
            return 0;
        }

        private long CountExportItems(IReadOnlyCollection<string> sourceFiles, int workerCount)
        {
            long total = 0;
            var parallelOptions = new ParallelOptions { MaxDegreeOfParallelism = workerCount };
            using var workers = new ThreadLocal<ResourcePipeline>(() => new ResourcePipeline(options), true);
            Parallel.ForEach(sourceFiles, parallelOptions, sourcePath =>
            {
                ResourcePipeline worker = workers.Value!;
                long fileTotal = worker.CountExportFileItems(sourcePath);
                Interlocked.Add(ref total, fileTotal);
            });
            return total;
        }

        private long CountExportFileItems(string sourcePath)
        {
            try
            {
                DetectedFileType fileType = FileTypeDetector.DetectFileType(sourcePath);
                if (fileType == DetectedFileType.AssetsFile)
                {
                    AssetsFileInstance inst = am.LoadAssetsFile(sourcePath, true);
                    return CountExportAssetsFileItems(inst);
                }
                if (fileType != DetectedFileType.BundleFile)
                    return 0;

                long total = 0;
                BundleFileInstance bunInst = am.LoadBundleFile(sourcePath, true);
                int entryCount = bunInst.file.BlockAndDirInfo.DirectoryInfos.Count;
                for (int i = 0; i < entryCount; i++)
                {
                    string entryName = bunInst.file.BlockAndDirInfo.DirectoryInfos[i].Name;
                    AssetsFileInstance? inst = TryLoadBundleEntry(bunInst, i, entryName);
                    if (inst != null)
                        total += CountExportAssetsFileItems(inst);
                }
                return total;
            }
            finally
            {
                am.UnloadAllAssetsFiles(true);
                am.UnloadAllBundleFiles();
            }
        }

        private long CountExportAssetsFileItems(AssetsFileInstance inst)
        {
            long total = 0;
            foreach (AssetClassID type in GetExportTypes())
                total += inst.file.GetAssetsOfType(type).Count;
            return total;
        }

        private int Import()
        {
            Log("Scanning manifests...");
            var manifestPaths = Directory.EnumerateFiles(options.WorkRoot, "manifest.json", SearchOption.AllDirectories)
                .OrderBy(p => p, StringComparer.OrdinalIgnoreCase)
                .ToList();
            Log($"Found {manifestPaths.Count} manifest file(s).");

            int workerCount = options.ImportWorkers > 0
                ? options.ImportWorkers
                : Math.Min(4, Math.Max(1, Environment.ProcessorCount / 2));
            workerCount = Math.Min(workerCount, Math.Max(1, manifestPaths.Count));
            Log($"Import workers: {workerCount}.");

            if (workerCount == 1)
            {
                int serialProcessed = 0;
                foreach (string manifestPath in manifestPaths)
                {
                    serialProcessed++;
                    Log($"[{serialProcessed}/{manifestPaths.Count}] Importing {Path.GetDirectoryName(manifestPath)}");
                    ImportManifest(manifestPath);
                }
                Log($"Import finished. Processed {serialProcessed} manifest(s).");
                return 0;
            }

            int processed = 0;
            var parallelOptions = new ParallelOptions { MaxDegreeOfParallelism = workerCount };
            using var workers = new ThreadLocal<ResourcePipeline>(() => new ResourcePipeline(options), true);
            Parallel.ForEach(manifestPaths, parallelOptions, manifestPath =>
            {
                ResourcePipeline worker = workers.Value!;
                worker.ImportManifest(manifestPath);
                int completed = Interlocked.Increment(ref processed);
                Log($"[{completed}/{manifestPaths.Count}] Imported {Path.GetDirectoryName(manifestPath)}");
            });

            Log($"Import finished. Processed {processed} manifest(s).");
            return 0;
        }

        private IEnumerable<string> EnumerateCandidateFiles(string root)
        {
            foreach (string path in Directory.EnumerateFiles(root, "*", SearchOption.AllDirectories))
            {
                DetectedFileType fileType = FileTypeDetector.DetectFileType(path);
                if (fileType == DetectedFileType.BundleFile || fileType == DetectedFileType.AssetsFile)
                    yield return path;
            }
        }

        private void ExportFile(string sourcePath)
        {
            DetectedFileType fileType = FileTypeDetector.DetectFileType(sourcePath);
            if (fileType == DetectedFileType.Unknown)
                return;

            string relativeSource = Path.GetRelativePath(options.SourceRoot, sourcePath);
            string sourceStem = Path.ChangeExtension(relativeSource, null) ?? Path.GetFileNameWithoutExtension(relativeSource);
            string sourceDir = Path.Combine(options.WorkRoot, sourceStem);
            Directory.CreateDirectory(sourceDir);

            string manifestFilePath = Path.Combine(sourceDir, "manifest.json");
            FileInfo sourceInfo = new FileInfo(sourcePath);
            HashSet<string> currentTypeNames = GetExportTypes()
                .Select(type => type.ToString())
                .ToHashSet(StringComparer.Ordinal);
            ExportManifest? previousManifest = ReadManifest(manifestFilePath);
            string exporterBuildFingerprint = GetExporterBuildFingerprint();
            string managedStateFingerprint = GetManagedStateFingerprint();
            bool sourceMatches = previousManifest != null
                && previousManifest.SourceRelativePath == relativeSource
                && previousManifest.SourceLength == sourceInfo.Length
                && previousManifest.SourceLastWriteTimeUtcTicks == sourceInfo.LastWriteTimeUtc.Ticks;
            bool exportContractMatches = previousManifest != null
                && previousManifest.ExporterSchemaVersion == ExportManifestSchemaVersion
                && previousManifest.ExporterBuildFingerprint == exporterBuildFingerprint
                && previousManifest.ManagedStateFingerprint == managedStateFingerprint
                && previousManifest.DumpFormat == options.DumpFormat
                && previousManifest.ImageFormat == options.ImageFormat
                && previousManifest.ImageQuality == options.JpegQuality;
            bool canMerge = sourceMatches && exportContractMatches;

            HashSet<string> requestedProfiles = GetRequestedProfiles();
            bool requestedProfilesAlreadyExported = canMerge
                && requestedProfiles.All(profile => previousManifest!.ExportedProfiles.Contains(profile, StringComparer.OrdinalIgnoreCase));
            if (requestedProfilesAlreadyExported)
            {
                if (AreProfileOutputsReusable(sourceDir, previousManifest!, currentTypeNames, out string reuseReason))
                {
                    if (currentTypeNames.Contains(nameof(AssetClassID.MonoBehaviour)))
                    {
                        foreach (ExportManifestMonoBehaviourFailure failure in previousManifest!.MonoBehaviourFailures)
                            exportProgress?.ReportMonoBehaviourFailure(failure, cached: true);
                    }
                    Log(
                        $"  Reusing unchanged export profile(s): {string.Join(", ", requestedProfiles.OrderBy(value => value))}; " +
                        $"validated {previousManifest!.Items.Count(item => currentTypeNames.Contains(item.TypeName)):N0} item(s)."
                    );
                    return;
                }
                Log($"  Cached profile cannot be reused: {reuseReason}");
            }

            if (previousManifest != null)
            {
                IEnumerable<ExportManifestItem> obsoleteItems = canMerge
                    ? previousManifest.Items.Where(item => currentTypeNames.Contains(item.TypeName))
                    : previousManifest.Items;
                DeleteExportedItems(sourceDir, obsoleteItems);
            }

            ExportManifest manifest = new ExportManifest
            {
                ExporterSchemaVersion = ExportManifestSchemaVersion,
                ExporterBuildFingerprint = exporterBuildFingerprint,
                ManagedStateFingerprint = managedStateFingerprint,
                DumpFormat = options.DumpFormat,
                ImageFormat = options.ImageFormat,
                ImageQuality = options.JpegQuality,
                SourceRelativePath = relativeSource,
                SourceKind = fileType == DetectedFileType.BundleFile ? "bundle" : "assets",
                SourceFileName = Path.GetFileName(sourcePath),
                SourceLength = sourceInfo.Length,
                SourceLastWriteTimeUtcTicks = sourceInfo.LastWriteTimeUtc.Ticks,
                ExportedProfiles = MergeExportedProfiles(canMerge ? previousManifest : null),
                Items = canMerge
                    ? previousManifest!.Items.Where(item => !currentTypeNames.Contains(item.TypeName)).ToList()
                    : new List<ExportManifestItem>(),
                MonoBehaviourSummaries = canMerge && !currentTypeNames.Contains(nameof(AssetClassID.MonoBehaviour))
                    ? previousManifest!.MonoBehaviourSummaries
                    : new List<ExportManifestMonoBehaviourSummary>(),
                MonoBehaviourFailures = canMerge && !currentTypeNames.Contains(nameof(AssetClassID.MonoBehaviour))
                    ? previousManifest!.MonoBehaviourFailures
                    : new List<ExportManifestMonoBehaviourFailure>()
            };
            if (canMerge)
                Log($"  Incremental manifest: retained {manifest.Items.Count:N0} item(s), replacing {string.Join(", ", currentTypeNames.OrderBy(value => value))}.");
            else if (previousManifest != null)
                Log("  Source changed or legacy manifest detected; starting a fresh manifest.");

            if (fileType == DetectedFileType.AssetsFile)
            {
                AssetsFileInstance inst = am.LoadAssetsFile(sourcePath, true);
                EnsureClassDatabase(inst);
                ExportAssetsFile(inst, sourceStem, sourceDir, "", null, manifest);
            }
            else
            {
                BundleFileInstance bunInst = am.LoadBundleFile(sourcePath, true);
                ExportBundle(bunInst, sourceStem, sourceDir, manifest);
            }

            WriteManifest(manifestFilePath, manifest);
            Log($"  Wrote manifest: {manifestFilePath}");
        }

        private ExportManifest? ReadManifest(string path)
        {
            if (!File.Exists(path))
                return null;
            try
            {
                return JsonSerializer.Deserialize<ExportManifest>(File.ReadAllText(path, Encoding.UTF8));
            }
            catch (Exception ex)
            {
                Log($"  WARNING: Existing manifest could not be read and will be replaced: {ex.Message}");
                return null;
            }
        }

        private List<string> MergeExportedProfiles(ExportManifest? previousManifest)
        {
            HashSet<string> profiles = previousManifest == null
                ? new HashSet<string>(StringComparer.OrdinalIgnoreCase)
                : previousManifest.ExportedProfiles.ToHashSet(StringComparer.OrdinalIgnoreCase);
            foreach (string profile in GetRequestedProfiles())
                profiles.Add(profile);
            return profiles.OrderBy(value => value, StringComparer.OrdinalIgnoreCase).ToList();
        }

        private HashSet<string> GetRequestedProfiles()
        {
            var profiles = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (string profile in options.ExportProfile.Split('+', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
            {
                if (profile.Equals("all", StringComparison.OrdinalIgnoreCase))
                {
                    profiles.Add("basic");
                    profiles.Add("objects");
                    profiles.Add("mesh");
                }
                else
                {
                    profiles.Add(profile);
                }
            }
            return profiles;
        }

        private static bool AreProfileOutputsReusable(
            string sourceDir,
            ExportManifest manifest,
            HashSet<string> currentTypeNames,
            out string reason)
        {
            string sourceRoot = Path.GetFullPath(sourceDir)
                .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)
                + Path.DirectorySeparatorChar;
            foreach (ExportManifestItem item in manifest.Items.Where(item => currentTypeNames.Contains(item.TypeName)))
            {
                if (!item.OutputFileExists)
                    continue;

                string outputPath = Path.GetFullPath(Path.Combine(sourceDir, item.RelativePath));
                if (!outputPath.StartsWith(sourceRoot, StringComparison.OrdinalIgnoreCase))
                {
                    reason = $"manifest path escaped export root: {item.RelativePath}";
                    return false;
                }
                if (!File.Exists(outputPath))
                {
                    reason = $"exported file is missing: {item.RelativePath}";
                    return false;
                }
                FileInfo outputInfo = new FileInfo(outputPath);
                if (outputInfo.Length != item.OutputFileLength)
                {
                    reason = $"exported file length changed: {item.RelativePath}";
                    return false;
                }
                if (outputInfo.LastWriteTimeUtc.Ticks != item.OutputFileLastWriteTimeUtcTicks)
                {
                    reason = $"exported file timestamp changed: {item.RelativePath}";
                    return false;
                }
            }
            reason = "all exported files are present and unchanged";
            return true;
        }

        private string GetExporterBuildFingerprint()
        {
            if (cachedExporterBuildFingerprint != null)
                return cachedExporterBuildFingerprint;

            string assemblyPath = typeof(ResourcePipeline).Assembly.Location;
            if (string.IsNullOrWhiteSpace(assemblyPath) || !File.Exists(assemblyPath))
                return cachedExporterBuildFingerprint = $"schema:{ExportManifestSchemaVersion}";

            string? buildRoot = Path.GetDirectoryName(assemblyPath);
            if (string.IsNullOrWhiteSpace(buildRoot) || !Directory.Exists(buildRoot))
            {
                FileInfo assemblyInfo = new FileInfo(assemblyPath);
                return cachedExporterBuildFingerprint = ComputeSha256(
                    $"{assemblyInfo.FullName}|{assemblyInfo.Length}|{assemblyInfo.LastWriteTimeUtc.Ticks}|{ExportManifestSchemaVersion}");
            }

            var signature = new StringBuilder()
                .Append("schema:").Append(ExportManifestSchemaVersion).Append(';');
            try
            {
                foreach (string path in Directory.EnumerateFiles(buildRoot, "*", SearchOption.AllDirectories)
                    .Where(path =>
                    {
                        string extension = Path.GetExtension(path);
                        return extension.Equals(".dll", StringComparison.OrdinalIgnoreCase)
                            || extension.Equals(".exe", StringComparison.OrdinalIgnoreCase)
                            || extension.Equals(".tpk", StringComparison.OrdinalIgnoreCase)
                            || extension.Equals(".json", StringComparison.OrdinalIgnoreCase);
                    })
                    .OrderBy(path => path, StringComparer.OrdinalIgnoreCase))
                {
                    FileInfo info = new FileInfo(path);
                    signature
                        .Append(Path.GetRelativePath(buildRoot, path).Replace('\\', '/'))
                        .Append('|').Append(info.Length)
                        .Append('|').Append(info.LastWriteTimeUtc.Ticks)
                        .Append(';');
                }
            }
            catch (Exception ex)
            {
                return cachedExporterBuildFingerprint = ComputeSha256(
                    $"exporter:error:{ex.GetType().Name}:{ex.Message}:{ExportManifestSchemaVersion}");
            }
            return cachedExporterBuildFingerprint = ComputeSha256(signature.ToString());
        }

        private string GetManagedStateFingerprint()
        {
            if (cachedManagedStateFingerprint != null)
                return cachedManagedStateFingerprint;
            if (!Directory.Exists(options.ManagedRoot))
                return cachedManagedStateFingerprint = "managed:missing";
            var signature = new StringBuilder();
            try
            {
                foreach (string path in Directory.EnumerateFiles(options.ManagedRoot, "*.dll", SearchOption.AllDirectories)
                    .OrderBy(path => path, StringComparer.OrdinalIgnoreCase))
                {
                    FileInfo info = new FileInfo(path);
                    signature
                        .Append(Path.GetRelativePath(options.ManagedRoot, path).Replace('\\', '/'))
                        .Append('|').Append(info.Length)
                        .Append('|').Append(info.LastWriteTimeUtc.Ticks)
                        .Append(';');
                }
            }
            catch (Exception ex)
            {
                return cachedManagedStateFingerprint = ComputeSha256($"managed:error:{ex.GetType().Name}:{ex.Message}");
            }
            return cachedManagedStateFingerprint = ComputeSha256(signature.ToString());
        }

        private static void DeleteExportedItems(string sourceDir, IEnumerable<ExportManifestItem> items)
        {
            string sourceRoot = Path.GetFullPath(sourceDir)
                .TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)
                + Path.DirectorySeparatorChar;
            foreach (ExportManifestItem item in items)
            {
                string targetPath = Path.GetFullPath(Path.Combine(sourceDir, item.RelativePath));
                if (!targetPath.StartsWith(sourceRoot, StringComparison.OrdinalIgnoreCase))
                    continue;
                try
                {
                    if (File.Exists(targetPath))
                        File.Delete(targetPath);
                }
                catch (IOException ex)
                {
                    Log($"  WARNING: Could not remove stale export {targetPath}: {ex.Message}");
                }
                catch (UnauthorizedAccessException ex)
                {
                    Log($"  WARNING: Could not remove stale export {targetPath}: {ex.Message}");
                }
            }
        }

        private void ExportBundle(BundleFileInstance bunInst, string sourceStem, string sourceDir, ExportManifest manifest)
        {
            int entryCount = bunInst.file.BlockAndDirInfo.DirectoryInfos.Count;
            for (int i = 0; i < entryCount; i++)
            {
                string bundleEntryName = bunInst.file.BlockAndDirInfo.DirectoryInfos[i].Name;
                AssetsFileInstance? inst = TryLoadBundleEntry(bunInst, i, bundleEntryName);
                if (inst == null)
                    continue;

                EnsureClassDatabase(inst);
                string entryRelativeDir = Path.Combine("bundle", Sanitize(bundleEntryName));
                string entryDir = Path.Combine(sourceDir, entryRelativeDir);
                Directory.CreateDirectory(entryDir);
                Log($"  Bundle entry: {bundleEntryName}");

                ExportAssetsFile(inst, sourceStem, entryDir, entryRelativeDir, bundleEntryName, manifest);
            }
        }

        private AssetsFileInstance? TryLoadBundleEntry(BundleFileInstance bunInst, int entryIndex, string entryName)
        {
            try
            {
                return am.LoadAssetsFileFromBundle(bunInst, entryIndex, true);
            }
            catch (Exception ex)
            {
                Log($"  Skip bundle entry: {entryName} ({ex.GetType().Name}: {ex.Message})");
                return null;
            }
        }

        private void ExportAssetsFile(AssetsFileInstance inst, string sourceStem, string outputDir, string manifestRelativeBase, string? bundleEntryName, ExportManifest manifest)
        {
            AddAssetsFileDependencyManifest(inst, manifestRelativeBase, bundleEntryName, manifest);
            var typeTreeFingerprintCache = new Dictionary<(int TypeIdOrIndex, ushort ScriptIndex), string>();
            string referenceUnityVersion = inst.file.Metadata.UnityVersion ?? "";
            if (string.IsNullOrWhiteSpace(manifest.UnityVersion) && !string.IsNullOrWhiteSpace(referenceUnityVersion))
                manifest.UnityVersion = referenceUnityVersion;
            ExportManifestMonoBehaviourSummary monoSummary = new ExportManifestMonoBehaviourSummary
            {
                RelativeBase = manifestRelativeBase.Replace('\\', '/'),
                BundleEntryName = bundleEntryName ?? ""
            };

            foreach (AssetClassID type in GetExportTypes())
            {
                List<AssetFileInfo> infos = inst.file.GetAssetsOfType(type);
                if (infos.Count == 0)
                    continue;

                string typeName = type.ToString();
                string typeDir = Path.Combine(outputDir, typeName);
                Directory.CreateDirectory(typeDir);

                foreach (AssetFileInfo info in infos)
                {
                    ExportManifestMonoBehaviourFailure? monoFailure = null;
                    AssetTypeValueField? baseField = type == AssetClassID.MonoBehaviour
                        ? TryGetMonoBehaviourBaseFieldForExport(inst, info, out monoFailure)
                        : SafeGetBaseField(inst, info);
                    if (baseField == null)
                    {
                        if (type == AssetClassID.MonoBehaviour)
                        {
                            monoSummary.Total++;
                            if (monoFailure != null)
                            {
                                if (IsRawObjectRangeInvalid(monoFailure))
                                    monoSummary.Failed++;
                                else
                                    monoSummary.Preserved++;
                                monoFailure.RelativeBase = manifestRelativeBase.Replace('\\', '/');
                                monoFailure.BundleEntryName = bundleEntryName ?? "";
                                manifest.MonoBehaviourFailures.Add(monoFailure);
                                exportProgress?.ReportMonoBehaviourFailure(monoFailure, cached: false);
                            }
                        }
                        exportProgress?.ReportItem();
                        continue;
                    }

                    if (type == AssetClassID.MonoBehaviour)
                    {
                        monoSummary.Total++;
                        if (IsBaseOnlyMonoBehaviour(baseField))
                            monoSummary.BaseOnly++;
                        else
                            monoSummary.WithCustomFields++;
                    }

                    string assetName = GetAssetName(baseField, info);

                    string exportKind;
                    string outputRelativePath;
                    string manifestRelativePath;
                    string exportedFilePath;
                    JToken? exportedJson = null;

                    if (type == AssetClassID.Texture2D)
                    {
                        exportKind = $"texture-{options.ImageFormat}";
                        string fileName = BuildSafeExportFileName(assetName, info.PathId, options.ImageFormat, typeDir);
                        outputRelativePath = Path.Combine(typeName, fileName);
                        manifestRelativePath = Path.Combine(manifestRelativeBase, outputRelativePath);
                        exportedFilePath = Path.Combine(outputDir, outputRelativePath);
                        if (options.VerboseExportAssets)
                            Log($"    {typeName}: {assetName} -> {manifestRelativePath}");
                        ExportTexture(inst, baseField, exportedFilePath);
                    }
                    else if (type == AssetClassID.Font)
                    {
                        string extension = DetectFontExtension(baseField);
                        exportKind = $"font-{extension}";
                        string fileName = BuildSafeExportFileName(assetName, info.PathId, extension, typeDir);
                        outputRelativePath = Path.Combine(typeName, fileName);
                        manifestRelativePath = Path.Combine(manifestRelativeBase, outputRelativePath);
                        exportedFilePath = Path.Combine(outputDir, outputRelativePath);
                        if (options.VerboseExportAssets)
                            Log($"    {typeName}: {assetName} -> {manifestRelativePath}");
                        if (!ExportFont(baseField, exportedFilePath))
                        {
                            Log($"      WARNING: Font has no embedded m_FontData; manifest target kept for replacement, but no source ttf/otf was exported.");
                        }
                    }
                    else
                    {
                        exportKind = options.DumpFormat;
                        string fileName = BuildSafeExportFileName(assetName, info.PathId, options.DumpFormat, typeDir);
                        outputRelativePath = Path.Combine(typeName, fileName);
                        manifestRelativePath = Path.Combine(manifestRelativeBase, outputRelativePath);
                        exportedFilePath = Path.Combine(outputDir, outputRelativePath);
                        if (options.VerboseExportAssets)
                            Log($"    {typeName}: {assetName} -> {manifestRelativePath}");
                        exportedJson = ExportDump(inst, info, baseField, exportedFilePath);
                    }

                    ushort scriptIndex = info.GetScriptIndex(inst.file);
                    var typeTreeKey = (info.TypeIdOrIndex, scriptIndex);
                    if (!typeTreeFingerprintCache.TryGetValue(typeTreeKey, out string? typeTreeFingerprint))
                    {
                        typeTreeFingerprint = ComputeTypeTreeFingerprint(baseField.TemplateField);
                        typeTreeFingerprintCache[typeTreeKey] = typeTreeFingerprint;
                    }

                    FileInfo? exportedFileInfo = File.Exists(exportedFilePath)
                        ? new FileInfo(exportedFilePath)
                        : null;
                    manifest.Items.Add(new ExportManifestItem
                    {
                        PathId = info.PathId,
                        TypeId = info.TypeId,
                        ScriptIndex = scriptIndex,
                        TypeName = typeName,
                        AssetName = assetName,
                        ExportKind = exportKind,
                        RelativePath = manifestRelativePath.Replace('\\', '/'),
                        BundleEntryName = bundleEntryName ?? "",
                        ReferenceUnityVersion = referenceUnityVersion,
                        TypeTreeFingerprint = typeTreeFingerprint,
                        JsonSchemaFingerprint = exportedJson != null
                            ? ComputeJsonSchemaFingerprint(exportedJson)
                            : "",
                        OutputFileExists = exportedFileInfo != null,
                        OutputFileLength = exportedFileInfo?.Length ?? 0,
                        OutputFileLastWriteTimeUtcTicks = exportedFileInfo?.LastWriteTimeUtc.Ticks ?? 0
                    });
                    exportProgress?.ReportItem();
                }
            }

            if (monoSummary.Total > 0)
            {
                manifest.MonoBehaviourSummaries.Add(monoSummary);
                Log(
                    $"  MonoBehaviour summary ({(bundleEntryName ?? sourceStem)}): " +
                    $"total={monoSummary.Total}, custom={monoSummary.WithCustomFields}, " +
                    $"baseOnly={monoSummary.BaseOnly}, preserved={monoSummary.Preserved}, " +
                    $"failed={monoSummary.Failed}"
                );
                if (monoSummary.BaseOnly > 0 && monoSummary.WithCustomFields == 0)
                {
                    Log(
                        "  WARNING: MonoBehaviour custom fields were not expanded. " +
                        "Only m_GameObject/m_Enabled/m_Script/m_Name were exported; translation scan may find no text."
                    );
                }
                if (monoSummary.Preserved > 0)
                {
                    LogYellow(
                        $"  MonoBehaviour 无法安全展开并保留原资源={monoSummary.Preserved}；" +
                        "这些对象未导出，导入时会保留原资源。"
                    );
                }
                if (monoSummary.Failed > 0)
                {
                    LogRed(
                        $"  MonoBehaviour 对象字节范围越界={monoSummary.Failed}；" +
                        "已确认源资源对象表指向文件范围之外，导出将返回失败。"
                    );
                }
            }
        }

        private IEnumerable<AssetClassID> GetExportTypes()
        {
            IEnumerable<AssetClassID> types = Array.Empty<AssetClassID>();
            string[] profiles = options.ExportProfile.Split(
                '+',
                StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries
            );
            foreach (string profile in profiles)
            {
                types = profile switch
                {
                    "basic" => types.Concat(BasicTypes),
                    "objects" => types.Concat(ObjectIndexTypes),
                    "mesh" => types.Concat(MeshTypes),
                    "all" => types.Concat(BasicTypes).Concat(ObjectIndexTypes).Concat(MeshTypes),
                    _ => types
                };
            }
            return types.Distinct();
        }

        private static bool IsBaseOnlyMonoBehaviour(AssetTypeValueField baseField)
        {
            HashSet<string> baseFieldNames = new HashSet<string>
            {
                "m_GameObject",
                "m_Enabled",
                "m_Script",
                "m_Name"
            };

            if (baseField.Children.Count == 0)
                return true;

            foreach (AssetTypeValueField child in baseField.Children)
            {
                if (!baseFieldNames.Contains(child.FieldName))
                    return false;
            }
            return true;
        }

        private static void AddAssetsFileDependencyManifest(AssetsFileInstance inst, string manifestRelativeBase, string? bundleEntryName, ExportManifest manifest)
        {
            manifest.AssetsFiles.Add(new ExportManifestAssetsFile
            {
                RelativeBase = manifestRelativeBase.Replace('\\', '/'),
                BundleEntryName = bundleEntryName ?? "",
                UnityVersion = inst.file.Metadata.UnityVersion ?? "",
                Externals = inst.file.Metadata.Externals.Select((external, index) => new ExportManifestExternal
                {
                    FileId = index + 1,
                    PathName = external.PathName ?? "",
                    OriginalPathName = external.OriginalPathName ?? "",
                    Type = external.Type.ToString()
                }).ToList()
            });
        }

        private JToken? ExportDump(AssetsFileInstance inst, AssetFileInfo info, AssetTypeValueField baseField, string outputPath)
        {
            EnsureDirectoryForFile(outputPath);
            using FileStream fs = File.Open(outputPath, FileMode.Create, FileAccess.Write);
            using StreamWriter sw = new StreamWriter(fs, Encoding.UTF8);

            AssetDumpHelper dumper = new AssetDumpHelper();
            if (options.DumpFormat == "json")
                return dumper.DumpJsonAsset(sw, baseField);
            else
                dumper.DumpTextAsset(sw, baseField);
            return null;
        }

        private void ExportTexture(AssetsFileInstance inst, AssetTypeValueField baseField, string outputPath)
        {
            EnsureDirectoryForFile(outputPath);
            TextureFile tex = TextureFile.ReadTextureFile(baseField);
            byte[]? textureData = tex.FillPictureData(inst);
            if (textureData == null || textureData.Length == 0)
                return;

            ImageExportType exportType = options.ImageFormat == "jpg" ? ImageExportType.Jpg : ImageExportType.Png;
            tex.DecodeTextureImage(textureData, outputPath, exportType, options.JpegQuality);
        }

        private bool ExportFont(AssetTypeValueField baseField, string outputPath)
        {
            byte[] byteData = GetFontData(baseField);
            if (byteData.Length == 0)
                return false;

            EnsureDirectoryForFile(outputPath);
            File.WriteAllBytes(outputPath, byteData);
            return true;
        }

        private void ImportManifest(string manifestPath)
        {
            string manifestDir = Path.GetDirectoryName(manifestPath)!;
            ExportManifest? manifest = JsonSerializer.Deserialize<ExportManifest>(File.ReadAllText(manifestPath));
            if (manifest == null)
                return;
            if (!legacyManifestReferenceWarningShown
                && manifest.Items.Any(item =>
                    item.ExportKind.Equals("json", StringComparison.OrdinalIgnoreCase)
                    && string.IsNullOrWhiteSpace(item.TypeTreeFingerprint)))
            {
                LogBlue(
                    "旧 manifest 未记录导出时 Unity 版本/类型树指纹，" +
                    "本次无法判断导出与导入参考模板是否一致；重新执行一键导出后会启用严格比较。"
                );
                legacyManifestReferenceWarningShown = true;
            }

            string sourcePath = Path.Combine(options.SourceRoot, manifest.SourceRelativePath);
            string resultRoot = string.IsNullOrWhiteSpace(options.ResultRoot)
                ? Path.Combine(options.WorkRoot, "result")
                : options.ResultRoot;
            DetectedFileType fileType = FileTypeDetector.DetectFileType(sourcePath);
            if (fileType == DetectedFileType.Unknown)
                return;

            if (manifest.Items.Any(item =>
                item.TypeName.Equals(nameof(AssetClassID.MonoBehaviour), StringComparison.Ordinal)
                && item.ExportKind.Equals("json", StringComparison.OrdinalIgnoreCase)))
            {
                EnsurePlayerDataDependenciesLoaded();
            }
            string resultPath = fileType == DetectedFileType.BundleFile && IsBundleExtension(manifest.SourceRelativePath)
                ? Path.Combine(resultRoot, "Bundle", "Android", manifest.SourceRelativePath)
                : Path.Combine(resultRoot, manifest.SourceRelativePath);

            if (fileType == DetectedFileType.AssetsFile)
            {
                AssetsFileInstance inst = am.LoadAssetsFile(sourcePath, true);
                EnsureClassDatabase(inst);
                EnsureMonoTemplateGenerator(inst);
                try
                {
                    bool changed = ApplyManifestToAssetsFile(inst, manifest, manifestDir);
                    if (changed)
                    {
                        WriteAssetsFile(inst, resultPath);
                    }
                    else
                    {
                        Log("  No matching replacements found. Skipping output.");
                    }
                }
                finally
                {
                    CleanupTemporaryAssetReplacements();
                }
            }
            else
            {
                BundleFileInstance bunInst = am.LoadBundleFile(sourcePath, true);
                bool changed = ImportBundleManifest(bunInst, manifest, manifestDir, resultPath);
                if (!changed)
                {
                    Log("  No matching replacements found. Skipping output.");
                }
            }
        }

        private bool ImportBundleManifest(BundleFileInstance bunInst, ExportManifest manifest, string manifestDir, string resultPath)
        {
            var grouped = manifest.Items
                .GroupBy(i => i.BundleEntryName, StringComparer.OrdinalIgnoreCase)
                .ToList();

            Dictionary<int, string> changedEntries = new Dictionary<int, string>();
            try
            {
                foreach (var group in grouped)
                {
                    string entryName = group.Key;
                    int entryIndex = bunInst.file.GetFileIndex(entryName);
                    if (entryIndex < 0)
                        continue;

                    AssetsFileInstance? inst = TryLoadBundleEntry(bunInst, entryIndex, entryName);
                    if (inst == null)
                        continue;

                    EnsureClassDatabase(inst);
                    EnsureMonoTemplateGenerator(inst);
                    Log($"  Bundle entry: {entryName}");
                    try
                    {
                        bool changed = ApplyManifestToAssetsFile(inst, group.ToList(), manifestDir);
                        if (changed)
                        {
                            EnsureDirectoryForFile(resultPath);
                            string temporaryEntry = resultPath + $".entry_{entryIndex}_{Guid.NewGuid():N}.tmp";
                            try
                            {
                                WriteAssetsFile(inst, temporaryEntry);
                                changedEntries[entryIndex] = temporaryEntry;
                            }
                            catch
                            {
                                File.Delete(temporaryEntry);
                                throw;
                            }
                        }
                    }
                    finally
                    {
                        CleanupTemporaryAssetReplacements();
                    }
                }

                if (changedEntries.Count > 0)
                    WriteBundleFilePreservingUnityFsShell(bunInst, changedEntries, resultPath);
                return changedEntries.Count > 0;
            }
            finally
            {
                foreach (string temporaryEntry in changedEntries.Values)
                    File.Delete(temporaryEntry);
            }
        }

        private static bool IsBundleExtension(string path)
        {
            return string.Equals(Path.GetExtension(path), ".bundle", StringComparison.OrdinalIgnoreCase);
        }

        private void EnsurePlayerDataDependenciesLoaded()
        {
            if (playerDataDependenciesLoaded)
                return;
            playerDataDependenciesLoaded = true;

            // Player-data bundles such as datapack.unity3d commonly keep their
            // MonoScript objects in data.unity3d/globalgamemanagers.assets. The
            // exporter naturally had data.unity3d loaded first, but a filtered or
            // parallel import can start directly at datapack and fail to resolve
            // m_Script, silently falling back to the four-field MonoBehaviour shell.
            // Preload the small player-data dependency bundle so script templates are
            // deterministic regardless of manifest selection and worker order.
            string dataBundlePath = Path.Combine(options.SourceRoot, "bin", "Data", "data.unity3d");
            if (File.Exists(dataBundlePath)
                && FileTypeDetector.DetectFileType(dataBundlePath) == DetectedFileType.BundleFile)
            {
                BundleFileInstance dependencyBundle = am.LoadBundleFile(dataBundlePath, true);
                int loadedEntries = 0;
                for (int i = 0; i < dependencyBundle.file.BlockAndDirInfo.DirectoryInfos.Count; i++)
                {
                    AssetBundleDirectoryInfo entry = dependencyBundle.file.BlockAndDirInfo.DirectoryInfos[i];
                    if ((entry.Flags & 4) == 0)
                        continue;
                    try
                    {
                        am.LoadAssetsFileFromBundle(dependencyBundle, i, true);
                        loadedEntries++;
                    }
                    catch (Exception ex)
                    {
                        Log($"  WARNING: Player-data dependency entry could not be loaded: {entry.Name}: {ex.Message}");
                    }
                }
                Log($"  Preloaded MonoBehaviour dependencies from data.unity3d: {loadedEntries} SerializedFile(s).");
            }

            string builtinPath = Path.Combine(
                options.SourceRoot,
                "bin", "Data", "Resources", "unity default resources"
            );
            if (File.Exists(builtinPath)
                && FileTypeDetector.DetectFileType(builtinPath) == DetectedFileType.AssetsFile)
            {
                am.LoadAssetsFile(builtinPath, true);
            }
        }

        private bool ApplyManifestToAssetsFile(AssetsFileInstance inst, ExportManifest manifest, string manifestDir)
        {
            return ApplyManifestToAssetsFile(inst, manifest.Items, manifestDir);
        }

        private bool ApplyManifestToAssetsFile(AssetsFileInstance inst, List<ExportManifestItem> items, string manifestDir)
        {
            bool changed = false;
            foreach (ExportManifestItem item in items)
            {
                AssetFileInfo info = inst.file.GetAssetInfo(item.PathId);
                if (info == null)
                    continue;

                string? replacementPath = ResolveReplacementPath(manifestDir, item);
                if (replacementPath == null)
                    continue;

                if (item.TypeName == nameof(AssetClassID.Texture2D) || item.ExportKind.StartsWith("texture-", StringComparison.OrdinalIgnoreCase))
                {
                    Log($"    {item.TypeName}: {item.AssetName} <- {item.RelativePath}");
                    byte[]? bytes = ApplyTextureReplacement(
                        inst, info, replacementPath, manifestDir, item);
                    if (bytes != null)
                    {
                        SetAssetReplacement(info, bytes);
                        changed = true;
                    }
                }
                else if (item.TypeName == nameof(AssetClassID.Font) || item.ExportKind.StartsWith("font-", StringComparison.OrdinalIgnoreCase))
                {
                    Log($"    {item.TypeName}: {item.AssetName} <- {item.RelativePath}");
                    byte[]? bytes = ApplyFontReplacement(inst, info, replacementPath);
                    if (bytes != null)
                    {
                        SetAssetReplacement(info, bytes);
                        changed = true;
                    }
                }
                else
                {
                    Log($"    {item.TypeName}: {item.AssetName} <- {item.RelativePath}");
                    byte[]? bytes = ApplyDumpReplacement(inst, info, replacementPath, item.ExportKind, manifestDir, item);
                    if (bytes != null)
                    {
                        SetAssetReplacement(info, bytes);
                        changed = true;
                    }
                }
            }
            return changed;
        }

        private void SetAssetReplacement(AssetFileInfo info, byte[] bytes)
        {
            if (bytes.Length < LargeReplacementSpoolThreshold)
            {
                info.SetNewData(bytes);
                return;
            }

            string temporaryPath = Path.Combine(
                options.WorkRoot,
                $".asset_replacement_{Guid.NewGuid():N}.tmp"
            );
            Directory.CreateDirectory(options.WorkRoot);
            File.WriteAllBytes(temporaryPath, bytes);
            FileStream stream = File.Open(temporaryPath, FileMode.Open, FileAccess.Read, FileShare.Read);
            temporaryAssetReplacements.Add((stream, temporaryPath));
            info.Replacer = new ContentReplacerFromStream(stream);
            Log($"      大型对象替换已转为磁盘流式缓存: {bytes.LongLength:N0} 字节");
        }

        private void CleanupTemporaryAssetReplacements()
        {
            foreach ((FileStream stream, string path) in temporaryAssetReplacements)
            {
                stream.Dispose();
                File.Delete(path);
            }
            temporaryAssetReplacements.Clear();
        }

        private string? ResolveReplacementPath(string manifestDir, ExportManifestItem item)
        {
            foreach (string candidate in EnumerateReplacementCandidates(manifestDir, item))
            {
                if (File.Exists(candidate))
                    return candidate;
            }
            return null;
        }

        private IEnumerable<string> EnumerateReplacementCandidates(string manifestDir, ExportManifestItem item)
        {
            string relativePath = item.RelativePath.Replace('/', Path.DirectorySeparatorChar);
            HashSet<string> yielded = new(StringComparer.OrdinalIgnoreCase);

            foreach (string baseDir in EnumerateReplacementBaseDirs(manifestDir))
            {
                string directPath = Path.Combine(baseDir, relativePath);
                if (yielded.Add(directPath))
                    yield return directPath;

                if (!string.IsNullOrWhiteSpace(item.BundleEntryName) && !StartsWithBundlePrefix(relativePath))
                {
                    string bundlePath = Path.Combine(baseDir, "bundle", Sanitize(item.BundleEntryName), relativePath);
                    if (yielded.Add(bundlePath))
                        yield return bundlePath;
                }
            }
        }

        private IEnumerable<string> EnumerateReplacementBaseDirs(string manifestDir)
        {
            if (!string.IsNullOrWhiteSpace(options.ReplacementRoot))
            {
                string manifestRelativeDir = Path.GetRelativePath(options.WorkRoot, manifestDir);
                yield return Path.Combine(options.ReplacementRoot, manifestRelativeDir);
                yield break;
            }

            yield return manifestDir;
        }

        private static bool StartsWithBundlePrefix(string relativePath)
        {
            return relativePath.StartsWith("bundle" + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)
                || relativePath.Equals("bundle", StringComparison.OrdinalIgnoreCase);
        }

        private byte[]? ApplyTextureReplacement(
            AssetsFileInstance inst,
            AssetFileInfo info,
            string replacementPath,
            string manifestDir,
            ExportManifestItem item)
        {
            AssetTypeValueField? baseField = SafeGetBaseField(inst, info);
            if (baseField == null)
                return null;

            TextureFile tex = TextureFile.ReadTextureFile(baseField);
            if (tex.m_Width <= 0 || tex.m_Height <= 0)
            {
                Log($"      Skipping Texture2D replacement because original texture size is {tex.m_Width}x{tex.m_Height}: {replacementPath}");
                return null;
            }

            TextureFormat originalFormat = (TextureFormat)tex.m_TextureFormat;
            TextureFormat importFormat = GetImportTextureFormat(originalFormat);
            int mipCount = Math.Max(1, tex.m_MipCount);
            bool requiresNativeEncoder = RequiresNativeTextureEncoder(importFormat);
            bool nativeEncodeSucceeded = false;

            if (importFormat != originalFormat)
            {
                Log(
                    $"      Texture format {originalFormat} cannot be re-encoded with its Crunch wrapper; " +
                    $"using GPU-compressed {importFormat} instead."
                );
            }

            try
            {
                EncodeTextureImageFromReplacement(
                    tex,
                    replacementPath,
                    importFormat,
                    mipCount,
                    options.JpegQuality
                );
                nativeEncodeSucceeded = requiresNativeEncoder;
            }
            catch (Exception ex) when (requiresNativeEncoder)
            {
                LogRed(
                    $"      原生纹理编码失败，将回退 RGBA32: " +
                    $"format={importFormat}, error={ex.GetType().Name}: {ex.Message}"
                );
                if (options.SaveSamples)
                {
                    SaveTextureEncodingFailureSample(
                        replacementPath,
                        manifestDir,
                        item,
                        originalFormat,
                        importFormat,
                        mipCount,
                        ex
                    );
                }
                else
                {
                    Log("      [样本] 自动保存已关闭，未记录纹理编码失败样本。");
                }

                EncodeRgba32Fallback(tex, replacementPath);
                Log(
                    $"      已使用旧版 RGBA32 链路完成回退: " +
                    $"mips={tex.m_MipCount}, data={tex.m_CompleteImageSize:N0} bytes."
                );
            }
            catch (NotSupportedException)
            {
                Log($"      Texture format {originalFormat} is not supported for encoding; using RGBA32.");
                EncodeTextureImageFromReplacement(
                    tex,
                    replacementPath,
                    TextureFormat.RGBA32,
                    mipCount,
                    options.JpegQuality
                );
            }
            catch (NotImplementedException)
            {
                Log($"      Texture format {originalFormat} is not implemented for encoding; using RGBA32.");
                EncodeTextureImageFromReplacement(
                    tex,
                    replacementPath,
                    TextureFormat.RGBA32,
                    mipCount,
                    options.JpegQuality
                );
            }
            if (nativeEncodeSucceeded)
            {
                Log(
                    $"      Native texture encode: {originalFormat} -> " +
                    $"{(TextureFormat)tex.m_TextureFormat}, mips={tex.m_MipCount}, " +
                    $"data={tex.m_CompleteImageSize:N0} bytes."
                );
            }
            tex.WriteTo(baseField);
            return baseField.WriteToByteArray();
        }

        private static TextureFormat GetImportTextureFormat(TextureFormat format)
        {
            return format switch
            {
                TextureFormat.DXT1Crunched => TextureFormat.DXT1,
                TextureFormat.DXT5Crunched => TextureFormat.DXT5,
                TextureFormat.ETC_RGB4Crunched => TextureFormat.ETC_RGB4,
                TextureFormat.ETC2_RGBA8Crunched => TextureFormat.ETC2_RGBA8,
                _ => format,
            };
        }

        private static bool RequiresNativeTextureEncoder(TextureFormat format)
        {
            return format is
                TextureFormat.ETC_RGB4 or
                TextureFormat.ETC2_RGB4 or
                TextureFormat.ETC2_RGBA1 or
                TextureFormat.ETC2_RGBA8 or
                TextureFormat.ASTC_RGB_4x4 or
                TextureFormat.ASTC_RGB_5x5 or
                TextureFormat.ASTC_RGB_6x6 or
                TextureFormat.ASTC_RGB_8x8 or
                TextureFormat.ASTC_RGB_10x10 or
                TextureFormat.ASTC_RGB_12x12 or
                TextureFormat.ASTC_RGBA_4x4 or
                TextureFormat.ASTC_RGBA_5x5 or
                TextureFormat.ASTC_RGBA_6x6 or
                TextureFormat.ASTC_RGBA_8x8 or
                TextureFormat.ASTC_RGBA_10x10 or
                TextureFormat.ASTC_RGBA_12x12;
        }

        private static void EncodeTextureImageFromReplacement(
            TextureFile tex,
            string replacementPath,
            TextureFormat format,
            int mipCount,
            int jpegQuality
        )
        {
            if (format == TextureFormat.Alpha8)
            {
                EncodeAlpha8Replacement(tex, replacementPath);
                return;
            }

            // RGB24's native image path applies a different row/channel
            // convention from the other encoders. Encode it explicitly so the
            // resulting Unity rows and RGB order are deterministic.
            if (format == TextureFormat.RGB24)
            {
                EncodeRgb24Replacement(tex, replacementPath);
                return;
            }

            if (RequiresNativeTextureEncoder(format))
            {
                Log(
                    $"      Native texture worker input: format={format}, " +
                    $"mips={mipCount}."
                );
                NativeTextureEncodingResult result = NativeTextureWorker.EncodeIsolated(
                    replacementPath,
                    format,
                    mipCount,
                    jpegQuality
                );
                tex.SetEncodedMips(result.Mips, result.Width, result.Height, format);
                return;
            }

            if (TextureEncoderWrapper.NativeLibrariesSupported())
            {
                using FileStream fs = File.OpenRead(replacementPath);
                ImageResult image = ImageResult.FromStream(fs, ColorComponents.RedGreenBlueAlpha);
                byte[] bgraData = (byte[])image.Data.Clone();
                TextureOperations.SwapRBComponentsInplace(bgraData);
                Log(
                    $"      Native texture input: format={format}, size={image.Width}x{image.Height}, " +
                    $"mips={mipCount}, buffer={bgraData.Length:N0} bytes."
                );
                tex.EncodeTextureRaw(
                    bgraData,
                    image.Width,
                    image.Height,
                    format,
                    mipCount,
                    jpegQuality,
                    useBgra: true
                );
                return;
            }

            using MemoryStream imageStream = LoadReplacementImageFlippedVertically(replacementPath, jpegQuality);
            tex.EncodeTextureImage(imageStream, format, mipCount, jpegQuality);
        }

        private static void EncodeRgba32Fallback(TextureFile tex, string replacementPath)
        {
            using FileStream fs = File.OpenRead(replacementPath);
            ImageResult image = ImageResult.FromStream(fs, ColorComponents.RedGreenBlueAlpha);
            byte[] unityRows = TextureOperations.FlipRGBA32Vertically(
                image.Data, image.Width, image.Height);
            tex.SetPictureData(
                unityRows,
                image.Width,
                image.Height,
                TextureFormat.RGBA32,
                mipCount: 1
            );
        }

        private static void EncodeRgb24Replacement(TextureFile tex, string replacementPath)
        {
            using FileStream fs = File.OpenRead(replacementPath);
            ImageResult image = ImageResult.FromStream(fs, ColorComponents.RedGreenBlueAlpha);
            byte[] unityRows = TextureOperations.FlipRGBA32Vertically(image.Data, image.Width, image.Height);
            byte[] rgb24 = new byte[checked(image.Width * image.Height * 3)];
            for (int source = 0, target = 0; target < rgb24.Length; source += 4, target += 3)
            {
                rgb24[target] = unityRows[source];
                rgb24[target + 1] = unityRows[source + 1];
                rgb24[target + 2] = unityRows[source + 2];
            }
            tex.SetPictureData(rgb24, image.Width, image.Height, TextureFormat.RGB24, 1);
        }

        private static void EncodeAlpha8Replacement(TextureFile tex, string replacementPath)
        {
            using FileStream fs = File.OpenRead(replacementPath);
            ImageResult image = ImageResult.FromStream(fs, ColorComponents.RedGreenBlueAlpha);
            byte[] unityRows = TextureOperations.FlipRGBA32Vertically(image.Data, image.Width, image.Height);
            byte[] alpha8 = new byte[checked(image.Width * image.Height)];
            for (int source = 3, target = 0; target < alpha8.Length; source += 4, target++)
                alpha8[target] = unityRows[source];

            // TextureOperations.GetPaddedTextureSize currently does not implement
            // Alpha8, so TextureFile.SetPictureData throws before assigning the raw
            // bytes. Alpha8 has no block padding requirement; update the same public
            // fields directly and preserve the original one-byte-per-pixel format.
            tex.m_TextureFormat = (int)TextureFormat.Alpha8;
            tex.m_Width = image.Width;
            tex.m_Height = image.Height;
            tex.m_StreamData.path = "";
            tex.m_StreamData.offset = 0;
            tex.m_StreamData.size = 0;
            tex.pictureData = alpha8;
            tex.m_CompleteImageSize = alpha8.Length;
            tex.m_MipCount = 1;
            tex.m_MipMap = false;
        }

        private static MemoryStream LoadReplacementImageFlippedVertically(string replacementPath, int jpegQuality)
        {
            using FileStream fs = File.OpenRead(replacementPath);
            ImageResult image = ImageResult.FromStream(fs, ColorComponents.RedGreenBlueAlpha);
            byte[] flippedData = TextureOperations.FlipRGBA32Vertically(image.Data, image.Width, image.Height);
            MemoryStream imageStream = new MemoryStream();
            TextureOperations.WriteRawImage(flippedData, image.Width, image.Height, imageStream, ImageExportType.Png, jpegQuality);
            imageStream.Position = 0;
            return imageStream;
        }

        private byte[]? ApplyFontReplacement(AssetsFileInstance inst, AssetFileInfo info, string replacementPath)
        {
            AssetTypeValueField? baseField = SafeGetBaseField(inst, info);
            if (baseField == null)
                return null;

            byte[] byteData = File.ReadAllBytes(replacementPath);
            baseField["m_FontData.Array"].AsByteArray = byteData;
            return baseField.WriteToByteArray();
        }

        private byte[]? ApplyDumpReplacement(AssetsFileInstance inst, AssetFileInfo info, string replacementPath, string exportKind, string manifestDir, ExportManifestItem item)
        {
            AssetTypeValueField? baseField = SafeGetBaseField(inst, info);
            if (baseField == null)
                return null;

            if (info.TypeId == (int)AssetClassID.MonoBehaviour)
            {
                long expandedSize;
                try
                {
                    expandedSize = baseField.WriteToByteArray().LongLength;
                }
                catch (Exception exception)
                {
                    LogRed(
                        $"      [MonoBehaviour][停止替换][已保留原资源] " +
                        $"当前模板无法完整回写 PathID={info.PathId}: " +
                        FormatDiagnosticException(exception)
                    );
                    return null;
                }
                if (expandedSize != info.ByteSize)
                {
                    LogRed(
                        $"      [MonoBehaviour][停止替换][已保留原资源] " +
                        $"当前模板只覆盖 {expandedSize}/{info.ByteSize} 字节，" +
                        $"PathID={info.PathId}，拒绝用不完整 JSON 截断对象。"
                    );
                    return null;
                }
            }

            // GetTemplateBaseField only returns the built-in MonoBehaviour shell for
            // script-backed objects. GetBaseField expands that shell with the managed
            // script fields and exposes the exact template that was used to deserialize
            // the object. Serializing JSON with the shell template truncates every
            // MonoBehaviour after m_EditorClassIdentifier; Unity then reads the missing
            // custom fields past the object boundary and aborts with
            // "Position out of bounds". Always import through the expanded template.
            AssetTypeTemplateField tempField = baseField.TemplateField;

            ReportImportReferenceCompatibility(
                inst,
                tempField,
                baseField,
                replacementPath,
                exportKind,
                item
            );

            AssetDumpHelper importer = new AssetDumpHelper();

            byte[]? bytes;
            string? exceptionMessage;
            if (replacementPath.EndsWith(".json", StringComparison.OrdinalIgnoreCase) || exportKind == "json")
            {
                string json = File.ReadAllText(replacementPath, Encoding.UTF8);
                bool managedReferenceContentChanged = false;
                if (IsLocalizationStringTableJson(json))
                {
                    bytes = importer.ImportLocalizationStringTableJsonAsset(baseField, json, out exceptionMessage);
                }
                else
                {
                    managedReferenceContentChanged = HasManagedReferenceContentChanges(baseField, json);
                    using MemoryStream ms = new MemoryStream(Encoding.UTF8.GetBytes(json));
                    using StreamReader sr = new StreamReader(ms, Encoding.UTF8, true);
                    bytes = importer.ImportJsonAssetPreserveManagedReferences(tempField, baseField, sr, out exceptionMessage);
                    if (bytes != null
                        && importer.PreservedManagedReferencesCount > 0
                        && managedReferenceContentChanged)
                    {
                        LogPurple(
                            $"      WARNING: SerializeReference 内部数据暂不按 JSON 导入，已保留原资源内部数据: " +
                            $"{importer.PreservedManagedReferencesCount} 处: {replacementPath}"
                        );
                        if (options.SaveSamples)
                            SaveSerializeReferenceSample(replacementPath, manifestDir, item, importer.PreservedManagedReferencesCount);
                    }
                    if (bytes != null && importer.PreservedMissingJsonFieldsCount > 0)
                    {
                        LogBlue(
                            $"      JSON 与目标类型树字段不完全一致，已从原资源保留缺失字段: " +
                            $"{importer.PreservedMissingJsonFieldsCount} 处 " +
                            $"({string.Join(", ", importer.PreservedMissingJsonFieldNames.Distinct())})。"
                        );
                    }
                }
            }
            else
            {
                using FileStream fs = File.OpenRead(replacementPath);
                using StreamReader sr = new StreamReader(fs, Encoding.UTF8, true);
                bytes = importer.ImportTextAsset(sr, out exceptionMessage);
            }

            if (bytes == null)
                throw new InvalidOperationException(exceptionMessage ?? $"Failed to import {replacementPath}");

            return bytes;
        }

        private static bool IsLocalizationStringTableJson(string json)
        {
            return json.Contains("\"m_LocaleId\"", StringComparison.Ordinal)
                && json.Contains("\"m_SharedData\"", StringComparison.Ordinal)
                && json.Contains("\"m_TableData\"", StringComparison.Ordinal)
                && json.Contains("\"m_Localized\"", StringComparison.Ordinal);
        }

        private static void LogPurple(string message)
        {
            Console.WriteLine($"\u001b[95m[UnityResourceCLI] {message}\u001b[0m");
        }

        private static void LogBlue(string message)
        {
            Console.WriteLine($"\u001b[94m[UnityResourceCLI] {message}\u001b[0m");
        }

        private static void LogYellow(string message)
        {
            Console.WriteLine($"\u001b[93m[UnityResourceCLI] {message}\u001b[0m");
        }

        private static void LogRed(string message)
        {
            Console.WriteLine($"\u001b[91m[UnityResourceCLI] {message}\u001b[0m");
        }

        private static void ReportImportReferenceCompatibility(
            AssetsFileInstance inst,
            AssetTypeTemplateField currentTemplate,
            AssetTypeValueField baseField,
            string replacementPath,
            string exportKind,
            ExportManifestItem item)
        {
            string currentUnityVersion = inst.file.Metadata.UnityVersion ?? "";
            if (!string.IsNullOrWhiteSpace(item.ReferenceUnityVersion)
                && !string.IsNullOrWhiteSpace(currentUnityVersion)
                && !string.Equals(item.ReferenceUnityVersion, currentUnityVersion, StringComparison.Ordinal))
            {
                throw new InvalidOperationException(
                    $"拒绝导入 Unity 版本不一致的资源: " +
                    $"export={item.ReferenceUnityVersion}, import={currentUnityVersion}, asset={item.RelativePath}"
                );
            }

            string currentTypeTreeFingerprint = ComputeTypeTreeFingerprint(currentTemplate);
            if (!string.IsNullOrWhiteSpace(item.TypeTreeFingerprint)
                && !string.Equals(item.TypeTreeFingerprint, currentTypeTreeFingerprint, StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidOperationException(
                    $"拒绝使用不一致的类型树重写资源: " +
                    $"export={item.TypeTreeFingerprint}, import={currentTypeTreeFingerprint}, asset={item.RelativePath}"
                );
            }

            if (exportKind.Equals("json", StringComparison.OrdinalIgnoreCase)
                && !string.IsNullOrWhiteSpace(item.JsonSchemaFingerprint))
            {
                string currentJsonSchemaFingerprint = ComputeJsonSchemaFingerprint(
                    File.ReadAllText(replacementPath, Encoding.UTF8)
                );
                if (!string.Equals(
                    item.JsonSchemaFingerprint,
                    currentJsonSchemaFingerprint,
                    StringComparison.OrdinalIgnoreCase))
                {
                    if (!JsonSchemasEquivalentIgnoringArrayWrappers(baseField, replacementPath))
                    {
                        LogBlue(
                            $"      待导入 JSON 结构已不同于导出时结构: " +
                            $"export={item.JsonSchemaFingerprint}, import={currentJsonSchemaFingerprint}, " +
                            $"asset={item.RelativePath}"
                        );
                    }
                }
            }
        }

        private static string ComputeTypeTreeFingerprint(AssetTypeTemplateField template)
        {
            StringBuilder signature = new StringBuilder();
            AppendTypeTreeSignature(template, signature);
            return ComputeSha256(signature.ToString());
        }

        private static void AppendTypeTreeSignature(AssetTypeTemplateField field, StringBuilder signature)
        {
            signature
                .Append('(')
                .Append(field.Type).Append('|')
                .Append(field.Name).Append('|')
                .Append(field.ValueType).Append('|')
                .Append(field.IsArray ? '1' : '0').Append('|')
                .Append(field.IsAligned ? '1' : '0').Append('|')
                .Append(field.HasValue ? '1' : '0');
            foreach (AssetTypeTemplateField child in field.Children)
                AppendTypeTreeSignature(child, signature);
            signature.Append(')');
        }

        private static string ComputeJsonSchemaFingerprint(
            string json,
            bool normalizeArrayWrappers = false)
        {
            return ComputeJsonSchemaFingerprint(JToken.Parse(json), normalizeArrayWrappers);
        }

        private static string ComputeJsonSchemaFingerprint(
            JToken token,
            bool normalizeArrayWrappers = false)
        {
            StringBuilder signature = new StringBuilder();
            AppendJsonSchemaSignature(token, signature, normalizeArrayWrappers);
            return ComputeSha256(signature.ToString());
        }

        private static void AppendJsonSchemaSignature(
            JToken token,
            StringBuilder signature,
            bool normalizeArrayWrappers)
        {
            if (token is JObject obj)
            {
                if (normalizeArrayWrappers
                    && obj.Count == 1
                    && obj.TryGetValue("Array", out JToken? arrayValue)
                    && arrayValue is JArray)
                {
                    signature.Append("[]");
                    return;
                }
                signature.Append('{');
                foreach (JProperty property in obj.Properties().OrderBy(property => property.Name, StringComparer.Ordinal))
                {
                    signature.Append(property.Name).Append(':');
                    AppendJsonSchemaSignature(property.Value, signature, normalizeArrayWrappers);
                    signature.Append(';');
                }
                signature.Append('}');
                return;
            }

            if (token is JArray)
            {
                // Array contents change legitimately for translated text and generated font tables.
                signature.Append("[]");
                return;
            }

            signature.Append(token.Type);
        }

        private static bool HasManagedReferenceContentChanges(
            AssetTypeValueField baseField,
            string replacementJson)
        {
            try
            {
                JToken original = JToken.Parse(DumpJsonAssetToString(baseField));
                JToken replacement = JToken.Parse(replacementJson);
                JToken? originalReferences = original["references"];
                JToken? replacementReferences = replacement["references"];
                if (originalReferences == null && replacementReferences == null)
                    return false;
                if (originalReferences == null || replacementReferences == null)
                    return true;
                return !JToken.DeepEquals(
                    NormalizeArrayWrappers(originalReferences),
                    NormalizeArrayWrappers(replacementReferences)
                );
            }
            catch
            {
                // If comparison is unavailable, retain the warning and sample.
                return true;
            }
        }

        private static bool JsonSchemasEquivalentIgnoringArrayWrappers(
            AssetTypeValueField baseField,
            string replacementPath)
        {
            try
            {
                string originalFingerprint = ComputeJsonSchemaFingerprint(
                    DumpJsonAssetToString(baseField),
                    normalizeArrayWrappers: true
                );
                string replacementFingerprint = ComputeJsonSchemaFingerprint(
                    File.ReadAllText(replacementPath, Encoding.UTF8),
                    normalizeArrayWrappers: true
                );
                return string.Equals(
                    originalFingerprint,
                    replacementFingerprint,
                    StringComparison.OrdinalIgnoreCase
                );
            }
            catch
            {
                return false;
            }
        }

        private static string DumpJsonAssetToString(AssetTypeValueField baseField)
        {
            using MemoryStream stream = new MemoryStream();
            using (StreamWriter writer = new StreamWriter(
                stream,
                new UTF8Encoding(false),
                1024,
                leaveOpen: true))
            {
                AssetDumpHelper dumper = new AssetDumpHelper();
                dumper.DumpJsonAsset(writer, baseField);
                writer.Flush();
            }
            return Encoding.UTF8.GetString(stream.ToArray());
        }

        private static JToken NormalizeArrayWrappers(JToken token)
        {
            if (token is JObject obj)
            {
                if (obj.Count == 1
                    && obj.TryGetValue("Array", out JToken? arrayValue)
                    && arrayValue is JArray)
                {
                    return NormalizeArrayWrappers(arrayValue);
                }
                JObject normalized = new JObject();
                foreach (JProperty property in obj.Properties())
                    normalized[property.Name] = NormalizeArrayWrappers(property.Value);
                return normalized;
            }
            if (token is JArray array)
                return new JArray(array.Select(NormalizeArrayWrappers));
            return token.DeepClone();
        }

        private static string ComputeSha256(string value)
        {
            byte[] digest = SHA256.HashData(Encoding.UTF8.GetBytes(value));
            return Convert.ToHexString(digest).ToLowerInvariant();
        }

        private void SaveTextureEncodingFailureSample(
            string replacementPath,
            string manifestDir,
            ExportManifestItem item,
            TextureFormat originalFormat,
            TextureFormat importFormat,
            int mipCount,
            Exception exception)
        {
            try
            {
                string sampleRoot = ResolveSampleRoot();
                Directory.CreateDirectory(sampleRoot);
                string safeName = Sanitize($"{item.AssetName}_{item.PathId}");
                string sampleDir = Path.Combine(
                    sampleRoot,
                    $"TextureEncode_{safeName}_{DateTime.Now:yyyyMMdd_HHmmss_fff}"
                );
                Directory.CreateDirectory(sampleDir);

                CopySampleFile(
                    replacementPath,
                    Path.Combine(sampleDir, "replacement", Path.GetFileName(replacementPath))
                );

                string originalPath = Path.Combine(manifestDir, item.RelativePath);
                if (File.Exists(originalPath))
                {
                    CopySampleFile(
                        originalPath,
                        Path.Combine(sampleDir, "original_export", Path.GetFileName(originalPath))
                    );
                }

                string manifestPath = Path.Combine(manifestDir, "manifest.json");
                if (File.Exists(manifestPath))
                    CopySampleFile(manifestPath, Path.Combine(sampleDir, "manifest.json"));

                File.WriteAllText(
                    Path.Combine(sampleDir, "info.txt"),
                    string.Join(
                        Environment.NewLine,
                        new[]
                        {
                            "Native texture encoding failure sample",
                            $"time={DateTime.Now:O}",
                            $"asset_name={item.AssetName}",
                            $"path_id={item.PathId}",
                            $"relative_path={item.RelativePath}",
                            $"bundle_entry={item.BundleEntryName}",
                            $"original_format={originalFormat}",
                            $"requested_format={importFormat}",
                            $"requested_mips={mipCount}",
                            $"replacement_path={replacementPath}",
                            $"original_export_path={originalPath}",
                            $"manifest_dir={manifestDir}",
                            $"source_root={options.SourceRoot}",
                            $"work_root={options.WorkRoot}",
                            $"replacement_root={options.ReplacementRoot}",
                            "exception:",
                            exception.ToString(),
                        }
                    ),
                    Encoding.UTF8
                );

                LogPurple($"      原生纹理编码失败样本已保存: {sampleDir}");
            }
            catch (Exception sampleException)
            {
                LogPurple(
                    $"      WARNING: 原生纹理编码失败样本保存失败: " +
                    $"{sampleException.GetType().Name}: {sampleException.Message}"
                );
            }
        }

        private void SaveSerializeReferenceSample(string replacementPath, string manifestDir, ExportManifestItem item, int preservedCount)
        {
            try
            {
                string sampleRoot = ResolveSampleRoot();
                Directory.CreateDirectory(sampleRoot);
                string safeName = Sanitize($"{item.AssetName}_{item.PathId}");
                string sampleDir = Path.Combine(sampleRoot, $"SerializeReference_{safeName}");
                if (Directory.Exists(sampleDir))
                    Directory.Delete(sampleDir, true);
                Directory.CreateDirectory(sampleDir);
                LogPurple($"      SerializeReference 样本目录: {sampleDir}");

                CopySampleFile(replacementPath, Path.Combine(sampleDir, "replacement", Path.GetFileName(replacementPath)));

                string originalPath = Path.Combine(manifestDir, item.RelativePath);
                if (File.Exists(originalPath))
                    CopySampleFile(originalPath, Path.Combine(sampleDir, "original_export", Path.GetFileName(originalPath)));

                string manifestPath = Path.Combine(manifestDir, "manifest.json");
                if (File.Exists(manifestPath))
                    CopySampleFile(manifestPath, Path.Combine(sampleDir, "manifest.json"));

                string infoPath = Path.Combine(sampleDir, "info.txt");
                File.WriteAllText(
                    infoPath,
                    string.Join(
                        Environment.NewLine,
                        new[]
                        {
                            "SerializeReference sample",
                            $"time={DateTime.Now:O}",
                            $"preserved_managed_references={preservedCount}",
                            $"asset_name={item.AssetName}",
                            $"type_name={item.TypeName}",
                            $"path_id={item.PathId}",
                            $"type_id={item.TypeId}",
                            $"script_index={item.ScriptIndex}",
                            $"export_kind={item.ExportKind}",
                            $"relative_path={item.RelativePath}",
                            $"bundle_entry={item.BundleEntryName}",
                            $"manifest_dir={manifestDir}",
                            $"replacement_path={replacementPath}",
                            $"original_export_path={originalPath}",
                            $"source_root={options.SourceRoot}",
                            $"work_root={options.WorkRoot}",
                            $"replacement_root={options.ReplacementRoot}",
                            $"managed_root={options.ManagedRoot}",
                        }
                    ),
                    Encoding.UTF8
                );

                LogPurple($"      SerializeReference 样本已保存: {sampleDir}");
            }
            catch (Exception ex)
            {
                LogPurple($"      WARNING: SerializeReference 样本保存失败: {ex.GetType().Name}: {ex.Message}");
            }
        }

        private string ResolveSampleRoot()
        {
            if (!string.IsNullOrWhiteSpace(options.SampleRoot))
                return Path.GetFullPath(options.SampleRoot);

            string? dir = AppContext.BaseDirectory;
            while (!string.IsNullOrWhiteSpace(dir))
            {
                if (File.Exists(Path.Combine(dir, "config.json")) && Directory.Exists(Path.Combine(dir, "AssetPipeline_CLI")))
                    return Path.Combine(dir, "样本");
                dir = Directory.GetParent(dir)?.FullName;
            }

            string workRoot = Path.GetFullPath(options.WorkRoot);
            DirectoryInfo? parent = Directory.GetParent(workRoot);
            while (parent != null)
            {
                if (parent.Name.StartsWith("workspace", StringComparison.OrdinalIgnoreCase) && parent.Parent != null)
                    return Path.Combine(parent.Parent.FullName, "样本");
                parent = parent.Parent;
            }
            return Path.Combine(Environment.CurrentDirectory, "样本");
        }

        private static void CopySampleFile(string sourcePath, string destinationPath)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(destinationPath)!);
            File.Copy(sourcePath, destinationPath, true);
        }

        private void WriteAssetsFile(AssetsFileInstance inst, string destinationPath)
        {
            EnsureDirectoryForFile(destinationPath);
            using FileStream fs = File.Open(destinationPath, FileMode.Create, FileAccess.Write, FileShare.None);
            using AssetsFileWriter writer = new AssetsFileWriter(fs);
            inst.file.Write(writer);
        }

        private void WriteBundleFile(BundleFileInstance bunInst, string destinationPath)
        {
            EnsureDirectoryForFile(destinationPath);
            long expectedDataSize = bunInst.file.BlockAndDirInfo.DirectoryInfos.Sum(
                info => info.Replacer?.GetSize() ?? info.DecompressedSize
            );
            EnsureRepackDiskSpace(destinationPath, expectedDataSize);
            string temporaryOutput = destinationPath + ".repack_tmp";
            File.Delete(temporaryOutput);
            bool blockDirAtEnd = (bunInst.originalFileStreamFlags & AssetBundleFSHeaderFlags.BlockAndDirAtEnd) != 0;
            string temporaryUnpacked = destinationPath + ".unpacked_tmp";
            File.Delete(temporaryUnpacked);
            try
            {
                using FileStream fs = File.Open(temporaryOutput, FileMode.Create, FileAccess.Write, FileShare.None);
                using AssetsFileWriter writer = new AssetsFileWriter(fs);
                using (FileStream unpackedStream = File.Open(temporaryUnpacked, FileMode.Create, FileAccess.Write, FileShare.None))
                using (AssetsFileWriter unpackedWriter = new AssetsFileWriter(unpackedStream))
                {
                    bunInst.file.Write(unpackedWriter);
                }

                using FileStream replacedStream = File.Open(temporaryUnpacked, FileMode.Open, FileAccess.Read, FileShare.Read);
                AssetBundleFile replacedBundle = new AssetBundleFile();
                replacedBundle.Read(new AssetsFileReader(replacedStream));
                replacedBundle.Pack(writer, bunInst.originalCompression, blockDirAtEnd, null, bunInst.originalFileStreamFlags);
                writer.Dispose();
                fs.Dispose();
                File.Move(temporaryOutput, destinationPath, true);
            }
            finally
            {
                File.Delete(temporaryUnpacked);
                File.Delete(temporaryOutput);
            }
        }

        private static void EnsureRepackDiskSpace(string destinationPath, long expectedDataSize)
        {
            string fullPath = Path.GetFullPath(destinationPath);
            string root = Path.GetPathRoot(fullPath) ?? throw new InvalidOperationException($"无法确定输出磁盘: {fullPath}");
            long safetyMargin = 64L * 1024 * 1024;
            long required = expectedDataSize > (long.MaxValue - safetyMargin) / 2
                ? long.MaxValue
                : expectedDataSize * 2 + safetyMargin;
            long available = new DriveInfo(root).AvailableFreeSpace;
            if (available < required)
            {
                throw new IOException(
                    $"UnityFS 流式重打临时空间不足: required={required:N0}, available={available:N0}, " +
                    $"drive={root}, output={destinationPath}"
                );
            }
        }

        private void WriteBundleFilePreservingUnityFsShell(BundleFileInstance bunInst, Dictionary<int, string> changedEntries, string destinationPath)
        {
            long expectedDecompressedSize = 0;
            for (int index = 0; index < bunInst.file.BlockAndDirInfo.DirectoryInfos.Count; index++)
            {
                AssetBundleDirectoryInfo dirInfo = bunInst.file.BlockAndDirInfo.DirectoryInfos[index];
                expectedDecompressedSize += changedEntries.TryGetValue(index, out string? replacementPath)
                    ? new FileInfo(replacementPath).Length
                    : dirInfo.DecompressedSize;
            }
            Log($"    UnityFS 流式重打预计解压数据: {expectedDecompressedSize:N0} 字节。");

            bool canPatchInPlace = bunInst.originalCompression == AssetBundleCompressionType.None;
            if (canPatchInPlace)
            {
                foreach (KeyValuePair<int, string> pair in changedEntries)
                {
                    AssetBundleDirectoryInfo dirInfo = bunInst.file.BlockAndDirInfo.DirectoryInfos[pair.Key];
                    long replacementSize = new FileInfo(pair.Value).Length;
                    if (replacementSize != dirInfo.DecompressedSize)
                    {
                        canPatchInPlace = false;
                        Log(
                            $"    Entry size changed, rebuilding UnityFS directory/data: {dirInfo.Name}, " +
                            $"old={dirInfo.DecompressedSize}, new={replacementSize}"
                        );
                        break;
                    }
                }
            }
            else
            {
                bool blockDirAtEnd = (bunInst.originalFileStreamFlags & AssetBundleFSHeaderFlags.BlockAndDirAtEnd) != 0;
                Log(
                    $"    Bundle is compressed ({bunInst.originalCompression}); rebuilding UnityFS directory/data " +
                    $"with original compression/layout, blockDirAtEnd={blockDirAtEnd}."
                );
            }

            if (canPatchInPlace)
            {
                EnsureDirectoryForFile(destinationPath);
                string temporaryOutput = destinationPath + ".repack_tmp";
                File.Delete(temporaryOutput);
                File.Copy(bunInst.path, temporaryOutput, true);

                long dataOffset = bunInst.file.Header.GetFileDataOffset();
                try
                {
                    using (FileStream fs = File.Open(temporaryOutput, FileMode.Open, FileAccess.ReadWrite, FileShare.None))
                    {
                        foreach (KeyValuePair<int, string> pair in changedEntries)
                        {
                            AssetBundleDirectoryInfo dirInfo = bunInst.file.BlockAndDirInfo.DirectoryInfos[pair.Key];
                            fs.Position = dataOffset + dirInfo.Offset;
                            using FileStream replacement = File.OpenRead(pair.Value);
                            replacement.CopyTo(fs);
                            Log($"    In-place UnityFS entry patch: {dirInfo.Name}, size={replacement.Length}");
                        }
                    }
                    File.Move(temporaryOutput, destinationPath, true);
                }
                finally
                {
                    File.Delete(temporaryOutput);
                }
                return;
            }

            List<FileStream> replacementStreams = new List<FileStream>();
            try
            {
                foreach (KeyValuePair<int, string> pair in changedEntries)
                {
                    FileStream replacement = File.Open(pair.Value, FileMode.Open, FileAccess.Read, FileShare.Read);
                    replacementStreams.Add(replacement);
                    bunInst.file.BlockAndDirInfo.DirectoryInfos[pair.Key].Replacer =
                        new ContentReplacerFromStream(replacement);
                }
                WriteBundleFile(bunInst, destinationPath);
            }
            finally
            {
                foreach (FileStream replacement in replacementStreams)
                    replacement.Dispose();
            }
        }


        private void EnsureClassDatabase(AssetsFileInstance inst)
        {
            if (inst.file.Metadata.TypeTreeEnabled)
                return;

            if (!string.IsNullOrWhiteSpace(inst.file.Metadata.UnityVersion))
            {
                am.LoadClassDatabaseFromPackage(inst.file.Metadata.UnityVersion);
            }
        }

        private void EnsureMonoTemplateGenerator(AssetsFileInstance inst)
        {
            if (am.MonoTempGenerator != null)
                return;

            if (Directory.Exists(options.ManagedRoot))
            {
                am.MonoTempGenerator = new MonoCecilTempGenerator(options.ManagedRoot);
            }
        }

        private AssetTypeValueField? TryGetMonoBehaviourBaseFieldForExport(
            AssetsFileInstance inst,
            AssetFileInfo info,
            out ExportManifestMonoBehaviourFailure? failure
        )
        {
            failure = null;
            AssetTypeValueField? baseField;
            try
            {
                baseField = am.GetBaseField(inst, info);
            }
            catch (Exception exception)
            {
                AssetTypeValueField? baseHeader = null;
                Exception? baseHeaderException = null;
                try
                {
                    baseHeader = am.GetBaseField(
                        inst,
                        info,
                        AssetReadFlags.SkipMonoBehaviourFields
                    );
                }
                catch (Exception fallbackException)
                {
                    baseHeaderException = fallbackException;
                }

                failure = CreateMonoBehaviourFailure(
                    inst,
                    info,
                    baseHeader,
                    exception,
                    baseHeaderException
                );
                return null;
            }

            try
            {
                long expandedSize = baseField.WriteToByteArray().LongLength;
                if (expandedSize != info.ByteSize)
                {
                    var exception = new InvalidDataException(
                        $"Expanded template covers {expandedSize} / {info.ByteSize} bytes."
                    );
                    failure = CreateMonoBehaviourFailure(
                        inst,
                        info,
                        baseField,
                        exception,
                        null
                    );
                    return null;
                }
            }
            catch (Exception exception)
            {
                failure = CreateMonoBehaviourFailure(
                    inst,
                    info,
                    baseField,
                    exception,
                    null
                );
                return null;
            }

            return baseField;
        }

        private ExportManifestMonoBehaviourFailure CreateMonoBehaviourFailure(
            AssetsFileInstance inst,
            AssetFileInfo info,
            AssetTypeValueField? baseHeader,
            Exception exception,
            Exception? baseHeaderException
        )
        {
            string reasonCode = exception switch
            {
                EndOfStreamException => "template_data_mismatch",
                TypeLoadException => "managed_type_unresolved",
                InvalidDataException => "template_size_mismatch",
                _ when exception.GetType().Name.Contains("ResolutionException", StringComparison.Ordinal)
                    => "managed_dependency_unresolved",
                _ => "template_parse_error"
            };

            var failure = new ExportManifestMonoBehaviourFailure
            {
                SourceFile = inst.path ?? "",
                PathId = info.PathId,
                TypeIdOrIndex = info.TypeIdOrIndex,
                ScriptIndex = GetScriptIndexOrUnknown(inst, info),
                ReasonCode = baseHeaderException == null
                    ? reasonCode
                    : "base_header_unreadable",
                ExceptionType = exception.GetType().Name,
                Reason = FormatDiagnosticException(exception),
                BaseHeaderReadable = baseHeaderException == null && baseHeader != null
            };
            if (baseHeaderException != null)
            {
                failure.Reason += $"; base header: {FormatDiagnosticException(baseHeaderException)}";
            }
            PopulateRawObjectRangeStatus(inst, info, failure);
            if (baseHeader != null)
            {
                PopulateMonoScriptIdentity(inst, baseHeader, failure);
            }
            return failure;
        }

        private void PopulateMonoScriptIdentity(
            AssetsFileInstance inst,
            AssetTypeValueField baseHeader,
            ExportManifestMonoBehaviourFailure failure
        )
        {
            try
            {
                AssetPPtr script = AssetPPtr.FromField(baseHeader["m_Script"]);
                failure.ScriptFileId = script.FileId;
                failure.ScriptPathId = script.PathId;
                if (script.IsNull())
                    return;

                AssetsFileInstance? scriptFile = script.FileId == 0
                    ? inst
                    : inst.GetDependency(am, script.FileId - 1);
                if (scriptFile == null)
                    return;

                AssetFileInfo scriptInfo = scriptFile.file.GetAssetInfo(script.PathId);
                if (scriptInfo == null)
                    return;

                AssetTypeValueField scriptField = am.GetBaseField(
                    scriptFile,
                    scriptInfo,
                    AssetReadFlags.SkipMonoBehaviourFields
                );
                failure.AssemblyName = scriptField["m_AssemblyName"].AsString;
                failure.Namespace = scriptField["m_Namespace"].AsString;
                failure.ClassName = scriptField["m_ClassName"].AsString;
            }
            catch
            {
                // FileID/PathID above remain sufficient for a stable diagnostic key.
            }
        }

        private static ushort GetScriptIndexOrUnknown(
            AssetsFileInstance inst,
            AssetFileInfo info
        )
        {
            try
            {
                return info.GetScriptIndex(inst.file);
            }
            catch
            {
                return ushort.MaxValue;
            }
        }

        private static void PopulateRawObjectRangeStatus(
            AssetsFileInstance inst,
            AssetFileInfo info,
            ExportManifestMonoBehaviourFailure failure
        )
        {
            try
            {
                long objectOffset = info.GetAbsoluteByteOffset(inst.file);
                long streamLength = inst.file.Reader.BaseStream.Length;
                long declaredLength = inst.file.Header.FileSize;
                long availableLength = Math.Min(streamLength, declaredLength);
                failure.RawObjectRangeChecked = true;
                failure.RawObjectRangeValid = objectOffset >= 0
                    && objectOffset >= inst.file.Header.DataOffset
                    && objectOffset <= availableLength
                    && info.ByteSize <= availableLength - objectOffset;
            }
            catch
            {
                failure.RawObjectRangeChecked = false;
                failure.RawObjectRangeValid = false;
            }
        }

        private static bool IsRawObjectRangeInvalid(
            ExportManifestMonoBehaviourFailure failure
        )
        {
            return failure.RawObjectRangeChecked && !failure.RawObjectRangeValid;
        }

        private static string FormatDiagnosticException(Exception exception)
        {
            string message = exception.Message
                .Replace('\r', ' ')
                .Replace('\n', ' ')
                .Trim();
            if (message.Length > 600)
                message = message.Substring(0, 600) + "...";
            return $"{exception.GetType().Name}: {message}";
        }

        private AssetTypeValueField? SafeGetBaseField(AssetsFileInstance inst, AssetFileInfo info)
        {
            try
            {
                return am.GetBaseField(inst, info);
            }
            catch (Exception ex)
            {
                ushort scriptIndex;
                try
                {
                    scriptIndex = info.GetScriptIndex(inst.file);
                }
                catch
                {
                    scriptIndex = ushort.MaxValue;
                }

                string monoDetails = "";
                if (info.TypeId == (int)AssetClassID.MonoBehaviour)
                {
                    try
                    {
                        AssetTypeValueField baseOnly = am.GetBaseField(
                            inst,
                            info,
                            AssetReadFlags.SkipMonoBehaviourFields
                        );
                        AssetPPtr script = AssetPPtr.FromField(baseOnly["m_Script"]);
                        monoDetails =
                            $", Name={baseOnly["m_Name"].AsString}, " +
                            $"ScriptFileId={script.FileId}, ScriptPathId={script.PathId}";
                    }
                    catch (Exception fallbackEx)
                    {
                        monoDetails =
                            $", BaseOnlyDiagnosticFailed={fallbackEx.GetType().Name}: {fallbackEx.Message}";
                    }
                }

                Log(
                    "  ERROR: Asset deserialization failed. " +
                    $"File={inst.path}, PathId={info.PathId}, TypeId={info.TypeId}, " +
                    $"TypeIdOrIndex={info.TypeIdOrIndex}, ScriptIndex={scriptIndex}{monoDetails}"
                );
                Log($"  ERROR DETAIL: {ex}");
                return null;
            }
        }

        private AssetTypeTemplateField? SafeGetTemplateField(AssetsFileInstance inst, AssetFileInfo info)
        {
            try
            {
                return am.GetTemplateBaseField(inst, info);
            }
            catch
            {
                return null;
            }
        }

        private static string GetAssetName(AssetTypeValueField baseField, AssetFileInfo info)
        {
            try
            {
                AssetTypeValueField nameField = baseField["m_Name"];
                if (!nameField.IsDummy)
                {
                    string name = nameField.AsString;
                    if (!string.IsNullOrWhiteSpace(name))
                        return name;
                }
            }
            catch
            {
            }

            return $"{info.TypeId}_{info.PathId}";
        }

        private static byte[] GetFontData(AssetTypeValueField baseField)
        {
            try
            {
                return baseField["m_FontData.Array"].AsByteArray;
            }
            catch
            {
                return Array.Empty<byte>();
            }
        }

        private static string DetectFontExtension(AssetTypeValueField baseField)
        {
            byte[] byteData = GetFontData(baseField);
            if (byteData.Length >= 4 &&
                byteData[0] == 0x4f &&
                byteData[1] == 0x54 &&
                byteData[2] == 0x54 &&
                byteData[3] == 0x4f)
            {
                return "otf";
            }

            return "ttf";
        }

        private static string Sanitize(string value)
        {
            if (string.IsNullOrWhiteSpace(value))
                return "unnamed";

            foreach (char c in Path.GetInvalidFileNameChars())
                value = value.Replace(c, '_');
            return value.Trim();
        }

        private static string BuildSafeExportFileName(string assetName, long pathId, string extension, string directoryPath)
        {
            string sanitized = Sanitize(assetName);
            if (string.IsNullOrWhiteSpace(sanitized))
                sanitized = "unnamed";

            string normalizedExtension = extension.TrimStart('.');
            string suffix = $"_{pathId}";
            int maxNameLength = GetMaxFileNameLength(directoryPath, normalizedExtension, suffix);
            string truncated = TruncateFileName(sanitized, maxNameLength);
            string candidate = $"{truncated}{suffix}.{normalizedExtension}";

            if (IsPathTooLong(Path.Combine(directoryPath, candidate)))
                candidate = $"asset{suffix}.{normalizedExtension}";

            return candidate;
        }

        private static int GetMaxFileNameLength(string directoryPath, string extension, string suffix)
        {
            const int defaultMaxNameLength = 80;
            int remaining = 240 - directoryPath.Length - 1 - suffix.Length - 1 - extension.Length;
            if (remaining < 16)
                return 16;
            return Math.Min(defaultMaxNameLength, remaining);
        }

        private static string TruncateFileName(string value, int maxLength)
        {
            if (string.IsNullOrEmpty(value) || value.Length <= maxLength)
                return value;

            int safeLength = Math.Max(1, maxLength);
            return value.Substring(0, safeLength).TrimEnd(' ', '.');
        }

        private static bool IsPathTooLong(string path)
        {
            return path.Length >= 240;
        }

        private static void WriteManifest(string path, ExportManifest manifest)
        {
            var json = JsonSerializer.Serialize(manifest, new JsonSerializerOptions
            {
                WriteIndented = true
            });
            File.WriteAllText(path, json, Encoding.UTF8);
        }

        private static void EnsureDirectoryForFile(string filePath)
        {
            string? directory = Path.GetDirectoryName(filePath);
            if (!string.IsNullOrEmpty(directory))
                Directory.CreateDirectory(directory);
        }

        private static void Log(string message)
        {
            Console.WriteLine($"[UnityResourceCLI] {message}");
        }

        private sealed class ExportProgress
        {
            private readonly long interval;
            private readonly ConcurrentDictionary<string, MonoBehaviourFailureGroup> monoBehaviourFailures = new();
            private long processed;
            private long nextHeartbeat;
            private long preservedMonoBehaviours;
            private long criticalMonoBehaviours;

            public bool HasCriticalFailure => Volatile.Read(ref criticalMonoBehaviours) > 0;

            public ExportProgress(long interval)
            {
                this.interval = interval;
                nextHeartbeat = interval;
            }

            public void ReportItem()
            {
                long current = Interlocked.Increment(ref processed);
                while (true)
                {
                    long next = Volatile.Read(ref nextHeartbeat);
                    if (current < next)
                        return;
                    if (Interlocked.CompareExchange(ref nextHeartbeat, next + interval, next) == next)
                    {
                        Log($"[导出进度] 已处理 {current:N0} 条资源");
                        return;
                    }
                }
            }

            public void ReportMonoBehaviourFailure(
                ExportManifestMonoBehaviourFailure failure,
                bool cached
            )
            {
                string scriptLabel = GetScriptLabel(failure);
                string key = string.Join(
                    "\n",
                    GetScriptKey(failure),
                    failure.ReasonCode,
                    failure.ExceptionType,
                    IsRawObjectRangeInvalid(failure).ToString()
                );
                MonoBehaviourFailureGroup group = monoBehaviourFailures.GetOrAdd(
                    key,
                    _ => new MonoBehaviourFailureGroup(scriptLabel, failure)
                );
                long count = Interlocked.Increment(ref group.Count);
                bool critical = IsRawObjectRangeInvalid(failure);
                if (critical)
                    Interlocked.Increment(ref criticalMonoBehaviours);
                else
                    Interlocked.Increment(ref preservedMonoBehaviours);

                if (count != 1)
                    return;

                string cacheLabel = cached ? "[缓存]" : "";
                string location = FormatFailureLocation(failure);
                if (critical)
                {
                    LogRed(
                        $"  [MonoBehaviour][对象字节越界][源资源损坏]{cacheLabel} " +
                        $"Script={scriptLabel}, 首个对象={location}, reason={failure.Reason}"
                    );
                }
                else if (failure.BaseHeaderReadable)
                {
                    LogYellow(
                        $"  [MonoBehaviour][模板信息不足或结构不匹配][已保留原资源]{cacheLabel} " +
                        $"Script={scriptLabel}, 首个对象={location}, reason={failure.Reason}"
                    );
                }
                else
                {
                    string rangeLabel = failure.RawObjectRangeChecked
                        ? "对象字节范围正常"
                        : "对象字节范围未能校验";
                    LogYellow(
                        $"  [MonoBehaviour][完整模板及基础头均无法读取][{rangeLabel}][已保留原资源]{cacheLabel} " +
                        $"Script={scriptLabel}, 首个对象={location}, reason={failure.Reason}"
                    );
                }
            }

            public void LogFinal()
            {
                long current = Volatile.Read(ref processed);
                Log($"[导出进度] 已处理 {current:N0} 条资源（完成）");

                long preserved = Volatile.Read(ref preservedMonoBehaviours);
                long critical = Volatile.Read(ref criticalMonoBehaviours);
                if (preserved == 0 && critical == 0)
                    return;

                if (preserved > 0)
                {
                    LogYellow(
                        $"[MonoBehaviour][最终汇总] 无法安全展开但对象范围未发现越界={preserved:N0}，" +
                        "均未导出且会保留原资源。"
                    );
                }
                if (critical > 0)
                {
                    LogRed(
                        $"[MonoBehaviour][最终汇总] 已确认对象字节范围越界={critical:N0}，" +
                        "源资源对象表无效，导出将返回失败。"
                    );
                }

                foreach (MonoBehaviourFailureGroup group in monoBehaviourFailures.Values
                    .OrderBy(value => value.ScriptLabel, StringComparer.OrdinalIgnoreCase)
                    .ThenBy(value => value.FirstFailure.ReasonCode, StringComparer.OrdinalIgnoreCase))
                {
                    ExportManifestMonoBehaviourFailure first = group.FirstFailure;
                    Action<string> logger = IsRawObjectRangeInvalid(first) ? LogRed : LogYellow;
                    logger(
                        $"[MonoBehaviour][汇总项] Script={group.ScriptLabel}, " +
                        $"count={Volatile.Read(ref group.Count):N0}, reason={first.ReasonCode}, " +
                        $"首个对象={FormatFailureLocation(first)}"
                    );
                }
            }

            private static string GetScriptKey(ExportManifestMonoBehaviourFailure failure)
            {
                if (!string.IsNullOrWhiteSpace(failure.ClassName))
                {
                    return string.Join(
                        "|",
                        failure.AssemblyName,
                        failure.Namespace,
                        failure.ClassName
                    );
                }
                return string.Join(
                    "|",
                    failure.SourceFile,
                    failure.BundleEntryName,
                    failure.RelativeBase,
                    failure.ScriptIndex,
                    failure.TypeIdOrIndex,
                    failure.ScriptFileId,
                    failure.ScriptPathId
                );
            }

            private static string GetScriptLabel(ExportManifestMonoBehaviourFailure failure)
            {
                if (!string.IsNullOrWhiteSpace(failure.ClassName))
                {
                    string qualifiedName = string.IsNullOrWhiteSpace(failure.Namespace)
                        ? failure.ClassName
                        : $"{failure.Namespace}.{failure.ClassName}";
                    string assemblyName = failure.AssemblyName.EndsWith(".dll", StringComparison.OrdinalIgnoreCase)
                        ? failure.AssemblyName.Substring(0, failure.AssemblyName.Length - 4)
                        : failure.AssemblyName;
                    return string.IsNullOrWhiteSpace(assemblyName)
                        ? qualifiedName
                        : $"{assemblyName}:{qualifiedName}";
                }
                if (failure.ScriptFileId != 0 || failure.ScriptPathId != 0)
                    return $"FileID={failure.ScriptFileId},PathID={failure.ScriptPathId}";
                string scriptIndex = failure.ScriptIndex == ushort.MaxValue
                    ? "unknown"
                    : failure.ScriptIndex.ToString();
                return $"ScriptIndex={scriptIndex},TypeIndex={failure.TypeIdOrIndex}";
            }

            private static string FormatFailureLocation(ExportManifestMonoBehaviourFailure failure)
            {
                string entry = string.IsNullOrWhiteSpace(failure.BundleEntryName)
                    ? failure.SourceFile
                    : failure.BundleEntryName;
                return $"{entry}/PathID={failure.PathId}";
            }

            private sealed class MonoBehaviourFailureGroup
            {
                public string ScriptLabel { get; }
                public ExportManifestMonoBehaviourFailure FirstFailure { get; }
                public long Count;

                public MonoBehaviourFailureGroup(
                    string scriptLabel,
                    ExportManifestMonoBehaviourFailure firstFailure
                )
                {
                    ScriptLabel = scriptLabel;
                    FirstFailure = firstFailure;
                }
            }
        }
    }
}
