using AssetsTools.NET;
using AssetsTools.NET.Extra;
using AssetsTools.NET.Texture;
using Newtonsoft.Json.Linq;
using StbImageSharp;
using System;
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
        private bool legacyManifestReferenceWarningShown;

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

        public ResourcePipeline(CliOptions options)
        {
            this.options = options;
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

            if (workerCount == 1)
            {
                int serialProcessed = 0;
                foreach (string sourcePath in sourceFiles)
                {
                    serialProcessed++;
                    Log($"[{serialProcessed}/{sourceFiles.Count}] Exporting {Path.GetFileName(sourcePath)}");
                    ExportFile(sourcePath);
                }
                Log($"Export finished. Processed {serialProcessed} file(s).");
                return 0;
            }

            int processed = 0;
            var parallelOptions = new ParallelOptions { MaxDegreeOfParallelism = workerCount };
            using var workers = new ThreadLocal<ResourcePipeline>(() => new ResourcePipeline(options), true);
            Parallel.ForEach(sourceFiles, parallelOptions, sourcePath =>
            {
                ResourcePipeline worker = workers.Value!;
                worker.ExportFile(sourcePath);
                int completed = Interlocked.Increment(ref processed);
                Log($"[{completed}/{sourceFiles.Count}] Exported {Path.GetFileName(sourcePath)}");
            });

            Log($"Export finished. Processed {processed} file(s).");
            return 0;
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

            ExportManifest manifest = new ExportManifest
            {
                SourceRelativePath = relativeSource,
                SourceKind = fileType == DetectedFileType.BundleFile ? "bundle" : "assets",
                SourceFileName = Path.GetFileName(sourcePath)
            };

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

            string manifestFilePath = Path.Combine(sourceDir, "manifest.json");
            WriteManifest(manifestFilePath, manifest);
            Log($"  Wrote manifest: {manifestFilePath}");
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

                foreach (AssetFileInfo info in infos)
                {
                    AssetTypeValueField? baseField = SafeGetBaseField(inst, info);
                    if (baseField == null)
                    {
                        if (type == AssetClassID.MonoBehaviour)
                        {
                            monoSummary.Total++;
                            monoSummary.Failed++;
                        }
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
                    string typeName = type.ToString();
                    string typeDir = Path.Combine(outputDir, typeName);
                    Directory.CreateDirectory(typeDir);

                    string exportKind;
                    string outputRelativePath;
                    string manifestRelativePath;
                    string exportedFilePath;

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
                        ExportDump(inst, info, baseField, exportedFilePath);
                    }

                    manifest.Items.Add(new ExportManifestItem
                    {
                        PathId = info.PathId,
                        TypeId = info.TypeId,
                        ScriptIndex = info.GetScriptIndex(inst.file),
                        TypeName = typeName,
                        AssetName = assetName,
                        ExportKind = exportKind,
                        RelativePath = manifestRelativePath.Replace('\\', '/'),
                        BundleEntryName = bundleEntryName ?? "",
                        ReferenceUnityVersion = referenceUnityVersion,
                        TypeTreeFingerprint = ComputeTypeTreeFingerprint(baseField.TemplateField),
                        JsonSchemaFingerprint = exportKind.Equals("json", StringComparison.OrdinalIgnoreCase)
                            && File.Exists(exportedFilePath)
                            ? ComputeJsonSchemaFingerprint(File.ReadAllText(exportedFilePath, Encoding.UTF8))
                            : ""
                    });
                }
            }

            if (monoSummary.Total > 0)
            {
                manifest.MonoBehaviourSummaries.Add(monoSummary);
                Log(
                    $"  MonoBehaviour summary ({(bundleEntryName ?? sourceStem)}): " +
                    $"total={monoSummary.Total}, custom={monoSummary.WithCustomFields}, " +
                    $"baseOnly={monoSummary.BaseOnly}, failed={monoSummary.Failed}"
                );
                if (monoSummary.WithCustomFields == 0)
                {
                    Log(
                        "  WARNING: MonoBehaviour custom fields were not expanded. " +
                        "Only m_GameObject/m_Enabled/m_Script/m_Name were exported; translation scan may find no text."
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

        private void ExportDump(AssetsFileInstance inst, AssetFileInfo info, AssetTypeValueField baseField, string outputPath)
        {
            EnsureDirectoryForFile(outputPath);
            using FileStream fs = File.Open(outputPath, FileMode.Create, FileAccess.Write);
            using StreamWriter sw = new StreamWriter(fs, Encoding.UTF8);

            AssetDumpHelper dumper = new AssetDumpHelper();
            if (options.DumpFormat == "json")
                dumper.DumpJsonAsset(sw, baseField);
            else
                dumper.DumpTextAsset(sw, baseField);
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
            string resultPath = fileType == DetectedFileType.BundleFile && IsBundleExtension(manifest.SourceRelativePath)
                ? Path.Combine(resultRoot, "Bundle", "Android", manifest.SourceRelativePath)
                : Path.Combine(resultRoot, manifest.SourceRelativePath);

            if (fileType == DetectedFileType.AssetsFile)
            {
                AssetsFileInstance inst = am.LoadAssetsFile(sourcePath, true);
                EnsureClassDatabase(inst);
                EnsureMonoTemplateGenerator(inst);
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

            Dictionary<int, byte[]> changedEntries = new Dictionary<int, byte[]>();
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
                bool changed = ApplyManifestToAssetsFile(inst, group.ToList(), manifestDir);
                if (changed)
                {
                    changedEntries[entryIndex] = WriteAssetsFileToByteArray(inst);
                }
            }

            if (changedEntries.Count > 0)
            {
                WriteBundleFilePreservingUnityFsShell(bunInst, changedEntries, resultPath);
            }
            return changedEntries.Count > 0;
        }

        private static bool IsBundleExtension(string path)
        {
            return string.Equals(Path.GetExtension(path), ".bundle", StringComparison.OrdinalIgnoreCase);
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
                    byte[]? bytes = ApplyTextureReplacement(inst, info, replacementPath);
                    if (bytes != null)
                    {
                        info.SetNewData(bytes);
                        changed = true;
                    }
                }
                else if (item.TypeName == nameof(AssetClassID.Font) || item.ExportKind.StartsWith("font-", StringComparison.OrdinalIgnoreCase))
                {
                    Log($"    {item.TypeName}: {item.AssetName} <- {item.RelativePath}");
                    byte[]? bytes = ApplyFontReplacement(inst, info, replacementPath);
                    if (bytes != null)
                    {
                        info.SetNewData(bytes);
                        changed = true;
                    }
                }
                else
                {
                    Log($"    {item.TypeName}: {item.AssetName} <- {item.RelativePath}");
                    byte[]? bytes = ApplyDumpReplacement(inst, info, replacementPath, item.ExportKind, manifestDir, item);
                    if (bytes != null)
                    {
                        info.SetNewData(bytes);
                        changed = true;
                    }
                }
            }
            return changed;
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

        private byte[]? ApplyTextureReplacement(AssetsFileInstance inst, AssetFileInfo info, string replacementPath)
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

            try
            {
                EncodeTextureImageFromReplacement(tex, replacementPath, options.JpegQuality);
            }
            catch (NotSupportedException)
            {
                Log($"      Texture format {(TextureFormat)tex.m_TextureFormat} is not supported for encoding; using RGBA32.");
                EncodeTextureImageFromReplacement(tex, replacementPath, TextureFormat.RGBA32, options.JpegQuality);
            }
            catch (NotImplementedException)
            {
                Log($"      Texture format {(TextureFormat)tex.m_TextureFormat} is not implemented for encoding; using RGBA32.");
                EncodeTextureImageFromReplacement(tex, replacementPath, TextureFormat.RGBA32, options.JpegQuality);
            }
            tex.WriteTo(baseField);
            return baseField.WriteToByteArray();
        }

        private static void EncodeTextureImageFromReplacement(TextureFile tex, string replacementPath, int jpegQuality)
        {
            using MemoryStream imageStream = LoadReplacementImageFlippedVertically(replacementPath, jpegQuality);
            tex.EncodeTextureImage(imageStream, 1, jpegQuality);
        }

        private static void EncodeTextureImageFromReplacement(TextureFile tex, string replacementPath, TextureFormat format, int jpegQuality)
        {
            using MemoryStream imageStream = LoadReplacementImageFlippedVertically(replacementPath, jpegQuality);
            tex.EncodeTextureImage(imageStream, format, 1, jpegQuality);
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
            AssetTypeTemplateField? tempField = SafeGetTemplateField(inst, info);
            if (tempField == null)
                return null;
            AssetTypeValueField? baseField = SafeGetBaseField(inst, info);
            if (baseField == null)
                return null;

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
                LogRed(
                    $"      导出/导入 Unity 版本不一致: " +
                    $"export={item.ReferenceUnityVersion}, import={currentUnityVersion}, asset={item.RelativePath}"
                );
            }

            string currentTypeTreeFingerprint = ComputeTypeTreeFingerprint(currentTemplate);
            if (!string.IsNullOrWhiteSpace(item.TypeTreeFingerprint)
                && !string.Equals(item.TypeTreeFingerprint, currentTypeTreeFingerprint, StringComparison.OrdinalIgnoreCase))
            {
                LogRed(
                    $"      导出/导入类型树不一致: " +
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
            JToken token = JToken.Parse(json);
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
                if (string.Equals(parent.Name, "workspace", StringComparison.OrdinalIgnoreCase) && parent.Parent != null)
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

        private static byte[] WriteAssetsFileToByteArray(AssetsFileInstance inst)
        {
            using MemoryStream ms = new MemoryStream();
            using AssetsFileWriter writer = new AssetsFileWriter(ms);
            inst.file.Write(writer);
            return ms.ToArray();
        }

        private void WriteBundleFile(BundleFileInstance bunInst, string destinationPath)
        {
            EnsureDirectoryForFile(destinationPath);
            using FileStream fs = File.Open(destinationPath, FileMode.Create, FileAccess.Write, FileShare.None);
            using AssetsFileWriter writer = new AssetsFileWriter(fs);
            bool blockDirAtEnd = (bunInst.originalFileStreamFlags & AssetBundleFSHeaderFlags.BlockAndDirAtEnd) != 0;
            if (bunInst.originalCompression != AssetBundleCompressionType.None)
            {
                byte[] unpackedBytes;
                using (MemoryStream unpackedStream = new MemoryStream())
                {
                    using (AssetsFileWriter unpackedWriter = new AssetsFileWriter(unpackedStream))
                    {
                        bunInst.file.Write(unpackedWriter);
                    }
                    unpackedBytes = unpackedStream.ToArray();
                }

                using MemoryStream replacedStream = new MemoryStream(unpackedBytes);
                AssetBundleFile replacedBundle = new AssetBundleFile();
                replacedBundle.Read(new AssetsFileReader(replacedStream));
                replacedBundle.Pack(writer, bunInst.originalCompression, blockDirAtEnd, null, bunInst.originalFileStreamFlags);
            }
            else
            {
                bunInst.file.Pack(writer, AssetBundleCompressionType.None, blockDirAtEnd, null, bunInst.originalFileStreamFlags);
            }
        }

        private void WriteBundleFilePreservingUnityFsShell(BundleFileInstance bunInst, Dictionary<int, byte[]> changedEntries, string destinationPath)
        {
            bool canPatchInPlace = bunInst.originalCompression == AssetBundleCompressionType.None;
            if (canPatchInPlace)
            {
                foreach (KeyValuePair<int, byte[]> pair in changedEntries)
                {
                    AssetBundleDirectoryInfo dirInfo = bunInst.file.BlockAndDirInfo.DirectoryInfos[pair.Key];
                    if (pair.Value.LongLength != dirInfo.DecompressedSize)
                    {
                        canPatchInPlace = false;
                        Log(
                            $"    Entry size changed, rebuilding UnityFS directory/data: {dirInfo.Name}, " +
                            $"old={dirInfo.DecompressedSize}, new={pair.Value.LongLength}"
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
                File.Copy(bunInst.path, destinationPath, true);

                long dataOffset = bunInst.file.Header.GetFileDataOffset();
                using FileStream fs = File.Open(destinationPath, FileMode.Open, FileAccess.ReadWrite, FileShare.None);
                foreach (KeyValuePair<int, byte[]> pair in changedEntries)
                {
                    AssetBundleDirectoryInfo dirInfo = bunInst.file.BlockAndDirInfo.DirectoryInfos[pair.Key];
                    fs.Position = dataOffset + dirInfo.Offset;
                    fs.Write(pair.Value, 0, pair.Value.Length);
                    Log($"    In-place UnityFS entry patch: {dirInfo.Name}, size={pair.Value.Length}");
                }
                return;
            }

            foreach (KeyValuePair<int, byte[]> pair in changedEntries)
            {
                bunInst.file.BlockAndDirInfo.DirectoryInfos[pair.Key].SetNewData(pair.Value);
            }
            WriteBundleFile(bunInst, destinationPath);
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

        private AssetTypeValueField? SafeGetBaseField(AssetsFileInstance inst, AssetFileInfo info)
        {
            try
            {
                return am.GetBaseField(inst, info);
            }
            catch
            {
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
    }
}
