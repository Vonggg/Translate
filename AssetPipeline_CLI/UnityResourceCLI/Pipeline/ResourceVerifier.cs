using AssetsTools.NET;
using AssetsTools.NET.Extra;
using System.Security.Cryptography;
using System.Text.Json;
using UABEAvalonia;

namespace UnityResourceCLI
{
    internal sealed class ResourceVerifier
    {
        private readonly CliOptions options;
        private readonly List<VerifyIssue> issues = new();
        private int checkedFiles;
        private int checkedSerializedFiles;
        private int changedObjects;
        private int parsedChangedObjects;

        public ResourceVerifier(CliOptions options)
        {
            this.options = options;
        }

        public int Run()
        {
            if (!Directory.Exists(options.SourceRoot))
                throw new DirectoryNotFoundException(options.SourceRoot);
            if (string.IsNullOrWhiteSpace(options.ResultRoot) || !Directory.Exists(options.ResultRoot))
                throw new DirectoryNotFoundException("verify requires an existing --result-root candidate directory.");

            List<string> candidates = Directory.EnumerateFiles(options.ResultRoot, "*", SearchOption.AllDirectories)
                .Where(path => FileTypeDetector.DetectFileType(path) is DetectedFileType.AssetsFile or DetectedFileType.BundleFile)
                .OrderBy(path => path, StringComparer.OrdinalIgnoreCase)
                .ToList();

            Console.WriteLine($"[资源验证] 原始资源: {options.SourceRoot}");
            Console.WriteLine($"[资源验证] 待验证资源: {options.ResultRoot}");
            Console.WriteLine($"[资源验证] 找到 {candidates.Count} 个修改后资源文件。");

            foreach (string candidatePath in candidates)
            {
                string relative = Path.GetRelativePath(options.ResultRoot, candidatePath);
                string sourcePath = Path.Combine(options.SourceRoot, relative);
                checkedFiles++;
                Console.WriteLine($"[资源验证] {checkedFiles}/{candidates.Count} {relative}");
                if (!File.Exists(sourcePath))
                {
                    Error(relative, "找不到对应的原始资源。", "file");
                    continue;
                }

                try
                {
                    DetectedFileType sourceType = FileTypeDetector.DetectFileType(sourcePath);
                    DetectedFileType candidateType = FileTypeDetector.DetectFileType(candidatePath);
                    if (sourceType != candidateType)
                    {
                        Error(relative, $"文件类型变化: original={sourceType}, candidate={candidateType}", "file");
                        continue;
                    }
                    if (candidateType == DetectedFileType.BundleFile)
                        VerifyBundle(sourcePath, candidatePath, relative);
                    else
                        VerifyStandaloneAssets(sourcePath, candidatePath, relative);
                }
                catch (Exception ex)
                {
                    Error(relative, $"验证过程异常: {ex.GetType().Name}: {ex.Message}", "file");
                }
            }

            int errorCount = issues.Count(issue => issue.Severity == "error");
            int warningCount = issues.Count(issue => issue.Severity == "warning");
            var report = new
            {
                generated_at = DateTimeOffset.Now,
                source_root = options.SourceRoot,
                candidate_root = options.ResultRoot,
                passed = errorCount == 0,
                checked_files = checkedFiles,
                checked_serialized_files = checkedSerializedFiles,
                changed_objects = changedObjects,
                parsed_changed_objects = parsedChangedObjects,
                errors = errorCount,
                warnings = warningCount,
                issues
            };
            string reportPath = string.IsNullOrWhiteSpace(options.ReportPath)
                ? Path.Combine(options.WorkRoot, "resource_repack_validation.json")
                : options.ReportPath;
            Directory.CreateDirectory(Path.GetDirectoryName(reportPath)!);
            File.WriteAllText(reportPath, JsonSerializer.Serialize(report, new JsonSerializerOptions { WriteIndented = true }));

            string status = errorCount == 0 ? "通过" : "失败";
            Console.WriteLine($"[资源验证][{status}] 文件={checkedFiles}, SerializedFile={checkedSerializedFiles}, " +
                $"变化对象={changedObjects}, 成功解析变化对象={parsedChangedObjects}, 错误={errorCount}, 警告={warningCount}");
            Console.WriteLine($"[资源验证] 报告: {reportPath}");
            return errorCount == 0 ? 0 : 1;
        }

        private void VerifyStandaloneAssets(string sourcePath, string candidatePath, string label)
        {
            AssetsManager sourceManager = CreateManager();
            AssetsManager candidateManager = CreateManager();
            try
            {
                PreloadPlayerDataDependencies(sourceManager);
                PreloadPlayerDataDependencies(candidateManager);
                AssetsFileInstance source = sourceManager.LoadAssetsFile(sourcePath, false);
                AssetsFileInstance candidate = candidateManager.LoadAssetsFile(candidatePath, false);
                PrepareManager(sourceManager, source);
                PrepareManager(candidateManager, candidate);
                VerifyAssetsFile(sourceManager, source, candidateManager, candidate, label);
            }
            finally
            {
                sourceManager.UnloadAll(true);
                candidateManager.UnloadAll(true);
            }
        }

        private void VerifyBundle(string sourcePath, string candidatePath, string label)
        {
            AssetsManager sourceManager = CreateManager();
            AssetsManager candidateManager = CreateManager();
            try
            {
                PreloadPlayerDataDependencies(sourceManager);
                PreloadPlayerDataDependencies(candidateManager);
                BundleFileInstance source = sourceManager.LoadBundleFile(sourcePath, true);
                BundleFileInstance candidate = candidateManager.LoadBundleFile(candidatePath, true);
                AssetBundleHeader sh = source.file.Header;
                AssetBundleHeader ch = candidate.file.Header;
                EqualOrError(sh.Signature, ch.Signature, label, "UnityFS signature", "bundle");
                EqualOrError(sh.Version, ch.Version, label, "UnityFS version", "bundle");
                EqualOrError(sh.GenerationVersion, ch.GenerationVersion, label, "generation version", "bundle");
                EqualOrError(sh.EngineVersion, ch.EngineVersion, label, "engine version", "bundle");
                EqualOrError(sh.FileStreamHeader.Flags, ch.FileStreamHeader.Flags, label, "UnityFS flags", "bundle");

                // Reading the complete decompressed stream forces every UnityFS block through the decoder.
                HashStream(candidate.file.DataReader.BaseStream);

                byte[] sourceHash = source.file.BlockAndDirInfo.Hash.data ?? new byte[16];
                byte[] candidateHash = candidate.file.BlockAndDirInfo.Hash.data ?? new byte[16];
                if (!sourceHash.SequenceEqual(candidateHash) && sourceHash.Any(value => value != 0))
                    Error(label, "原 UnityFS BlockInfo Hash 非零，但重打后发生变化。", "bundle");

                AssetBundleBlockInfo[] sourceBlocks = source.file.BlockAndDirInfo.BlockInfos;
                AssetBundleBlockInfo[] candidateBlocks = candidate.file.BlockAndDirInfo.BlockInfos;
                HashSet<byte> sourceCompression = sourceBlocks.Select(block => block.GetCompressionType()).ToHashSet();
                HashSet<byte> candidateCompression = candidateBlocks.Select(block => block.GetCompressionType()).ToHashSet();
                if (!sourceCompression.SetEquals(candidateCompression))
                    Error(label, $"UnityFS 数据块压缩类型变化: original={string.Join(',', sourceCompression)}, candidate={string.Join(',', candidateCompression)}", "bundle");
                if (sourceBlocks.Length != candidateBlocks.Length)
                    Warning(label, $"UnityFS 分块数量变化: original={sourceBlocks.Length}, candidate={candidateBlocks.Length}", "bundle");

                List<AssetBundleDirectoryInfo> sourceDirs = source.file.BlockAndDirInfo.DirectoryInfos;
                List<AssetBundleDirectoryInfo> candidateDirs = candidate.file.BlockAndDirInfo.DirectoryInfos;
                if (sourceDirs.Count != candidateDirs.Count)
                {
                    Error(label, $"UnityFS 目录条目数变化: original={sourceDirs.Count}, candidate={candidateDirs.Count}", "bundle");
                    return;
                }
                ValidateBundleDirectories(candidate, label);

                for (int i = 0; i < sourceDirs.Count; i++)
                {
                    AssetBundleDirectoryInfo sd = sourceDirs[i];
                    AssetBundleDirectoryInfo cd = candidateDirs[i];
                    EqualOrError(sd.Name, cd.Name, label, $"目录[{i}]名称", "bundle");
                    EqualOrError(sd.Flags, cd.Flags, label, $"目录[{i}]flags", "bundle");
                    if (sd.Name != cd.Name || sd.Flags != cd.Flags)
                        continue;
                    bool same = SegmentEquals(
                        source.file.DataReader.BaseStream, sd.Offset, sd.DecompressedSize,
                        candidate.file.DataReader.BaseStream, cd.Offset, cd.DecompressedSize);
                    if (same)
                        continue;
                    if ((sd.Flags & 4) == 0)
                    {
                        Warning(label, $"非 SerializedFile 条目内容发生变化: {sd.Name}", "bundle-entry");
                        continue;
                    }
                    try
                    {
                        AssetsFileInstance sourceAssets = sourceManager.LoadAssetsFileFromBundle(source, i, false);
                        AssetsFileInstance candidateAssets = candidateManager.LoadAssetsFileFromBundle(candidate, i, false);
                        PrepareManager(sourceManager, sourceAssets);
                        PrepareManager(candidateManager, candidateAssets);
                        VerifyAssetsFile(sourceManager, sourceAssets, candidateManager, candidateAssets, $"{label}::{sd.Name}");
                    }
                    catch (Exception ex)
                    {
                        Error(label, $"SerializedFile 条目无法重新打开: {sd.Name}: {ex.GetType().Name}: {ex.Message}", "bundle-entry");
                    }
                }
            }
            finally
            {
                sourceManager.UnloadAll(true);
                candidateManager.UnloadAll(true);
            }
        }

        private void ValidateBundleDirectories(BundleFileInstance bundle, string label)
        {
            long length = bundle.file.DataReader.BaseStream.Length;
            long previousEnd = 0;
            foreach (AssetBundleDirectoryInfo dir in bundle.file.BlockAndDirInfo.DirectoryInfos.OrderBy(dir => dir.Offset))
            {
                if (dir.Offset < 0 || dir.DecompressedSize < 0 || dir.Offset + dir.DecompressedSize > length)
                    Error(label, $"UnityFS 目录越界: {dir.Name}, offset={dir.Offset}, size={dir.DecompressedSize}, data={length}", "bundle-entry");
                if (dir.Offset < previousEnd)
                    Error(label, $"UnityFS 目录重叠: {dir.Name}, offset={dir.Offset}, previousEnd={previousEnd}", "bundle-entry");
                previousEnd = Math.Max(previousEnd, dir.Offset + dir.DecompressedSize);
            }
        }

        private void VerifyAssetsFile(
            AssetsManager sourceManager, AssetsFileInstance source,
            AssetsManager candidateManager, AssetsFileInstance candidate,
            string label)
        {
            checkedSerializedFiles++;
            AssetsFile sf = source.file;
            AssetsFile cf = candidate.file;
            EqualOrError(sf.Header.Version, cf.Header.Version, label, "SerializedFile format version", "serialized-file");
            EqualOrError(sf.Header.Endianness, cf.Header.Endianness, label, "endianness", "serialized-file");
            EqualOrError(sf.Metadata.UnityVersion, cf.Metadata.UnityVersion, label, "Unity version", "metadata");
            EqualOrError(sf.Metadata.TargetPlatform, cf.Metadata.TargetPlatform, label, "target platform", "metadata");
            EqualOrError(sf.Metadata.TypeTreeEnabled, cf.Metadata.TypeTreeEnabled, label, "TypeTreeEnabled", "metadata");
            EqualOrError(sf.Metadata.TypeTreeTypes.Count, cf.Metadata.TypeTreeTypes.Count, label, "type count", "metadata");
            EqualOrError(sf.Metadata.ScriptTypes.Count, cf.Metadata.ScriptTypes.Count, label, "script type count", "metadata");
            EqualOrError(sf.Metadata.Externals.Count, cf.Metadata.Externals.Count, label, "external reference count", "metadata");
            EqualOrError(sf.Metadata.RefTypes.Count, cf.Metadata.RefTypes.Count, label, "reference type count", "metadata");
            EqualOrError(sf.Metadata.UserInformation, cf.Metadata.UserInformation, label, "user information", "metadata");
            if (!SerializeTypeTrees(sf.Metadata.TypeTreeTypes, sf.Header.Version, sf.Metadata.TypeTreeEnabled)
                .SequenceEqual(SerializeTypeTrees(cf.Metadata.TypeTreeTypes, cf.Header.Version, cf.Metadata.TypeTreeEnabled)))
                Error(label, "TypeTree 类型定义内容发生变化。", "metadata");
            if (!SerializeTypeTrees(sf.Metadata.RefTypes, sf.Header.Version, sf.Metadata.TypeTreeEnabled)
                .SequenceEqual(SerializeTypeTrees(cf.Metadata.RefTypes, cf.Header.Version, cf.Metadata.TypeTreeEnabled)))
                Error(label, "RefTypes 类型定义内容发生变化。", "metadata");
            CompareScriptTypes(sf, cf, label);
            CompareExternalReferences(sf, cf, label);

            Dictionary<long, AssetFileInfo> sourceInfos = sf.Metadata.AssetInfos.ToDictionary(info => info.PathId);
            Dictionary<long, AssetFileInfo> candidateInfos = cf.Metadata.AssetInfos.ToDictionary(info => info.PathId);
            if (!sourceInfos.Keys.ToHashSet().SetEquals(candidateInfos.Keys))
            {
                Error(label, "对象 PathID 集合发生变化。", "object-table");
                return;
            }
            ValidateObjectTable(sf, source.AssetsStream.Length, label, DetectAlignment(sf));
            ValidateObjectTable(cf, candidate.AssetsStream.Length, label, DetectAlignment(sf));

            foreach ((long pathId, AssetFileInfo si) in sourceInfos)
            {
                AssetFileInfo ci = candidateInfos[pathId];
                if (si.TypeIdOrIndex != ci.TypeIdOrIndex || si.GetScriptIndex(sf) != ci.GetScriptIndex(cf))
                {
                    Error(label, $"对象类型信息变化: PathID={pathId}", "object-table");
                    continue;
                }
                if (SegmentEquals(
                    source.AssetsStream, si.GetAbsoluteByteOffset(sf), si.ByteSize,
                    candidate.AssetsStream, ci.GetAbsoluteByteOffset(cf), ci.ByteSize))
                    continue;

                changedObjects++;
                bool sourceParsed = TryParseObject(sourceManager, source, si, out string sourceError);
                bool candidateParsed = TryParseObject(candidateManager, candidate, ci, out string candidateError);
                if (candidateParsed)
                {
                    parsedChangedObjects++;
                }
                else if (sourceParsed)
                {
                    Error(label, $"修改对象无法重新解析: PathID={pathId}, TypeID={ci.TypeId}: {candidateError}", "object");
                }
                else
                {
                    Warning(label, $"原始和修改对象均无法展开自定义字段: PathID={pathId}, TypeID={ci.TypeId}; original={sourceError}; candidate={candidateError}", "object");
                }
            }
        }

        private void CompareExternalReferences(AssetsFile source, AssetsFile candidate, string label)
        {
            int count = Math.Min(source.Metadata.Externals.Count, candidate.Metadata.Externals.Count);
            for (int i = 0; i < count; i++)
            {
                AssetsFileExternal se = source.Metadata.Externals[i];
                AssetsFileExternal ce = candidate.Metadata.Externals[i];
                if (se.VirtualAssetPathName != ce.VirtualAssetPathName || se.Guid.ToString() != ce.Guid.ToString()
                    || se.Type != ce.Type || se.OriginalPathName != ce.OriginalPathName)
                    Error(label, $"外部引用[{i}]发生变化: original={se.OriginalPathName}, candidate={ce.OriginalPathName}", "metadata");
            }
        }

        private void CompareScriptTypes(AssetsFile source, AssetsFile candidate, string label)
        {
            int count = Math.Min(source.Metadata.ScriptTypes.Count, candidate.Metadata.ScriptTypes.Count);
            for (int i = 0; i < count; i++)
            {
                AssetPPtr sourcePtr = source.Metadata.ScriptTypes[i];
                AssetPPtr candidatePtr = candidate.Metadata.ScriptTypes[i];
                if (sourcePtr.FileId != candidatePtr.FileId || sourcePtr.PathId != candidatePtr.PathId)
                    Error(label, $"ScriptTypes[{i}]发生变化。", "metadata");
            }
        }

        private static byte[] SerializeTypeTrees(
            IReadOnlyList<TypeTreeType> types,
            uint version,
            bool typeTreeEnabled)
        {
            using MemoryStream stream = new MemoryStream();
            using AssetsFileWriter writer = new AssetsFileWriter(stream);
            writer.Write(types.Count);
            foreach (TypeTreeType type in types)
                type.Write(writer, version, typeTreeEnabled);
            return stream.ToArray();
        }

        private void ValidateObjectTable(AssetsFile file, long streamLength, string label, int expectedAlignment)
        {
            long previousEnd = file.Header.DataOffset;
            foreach (AssetFileInfo info in file.Metadata.AssetInfos.OrderBy(info => info.GetAbsoluteByteOffset(file)))
            {
                long offset = info.GetAbsoluteByteOffset(file);
                long end = offset + info.ByteSize;
                if (offset < file.Header.DataOffset || end > streamLength || end > file.Header.FileSize)
                    Error(label, $"对象越界: PathID={info.PathId}, offset={offset}, size={info.ByteSize}, file={streamLength}", "object-table");
                if (offset < previousEnd)
                    Error(label, $"对象重叠: PathID={info.PathId}, offset={offset}, previousEnd={previousEnd}", "object-table");
                if (offset % expectedAlignment != 0)
                    Error(label, $"对象未按原资源 {expectedAlignment} 字节对齐: PathID={info.PathId}, offset={offset}", "object-table");
                previousEnd = Math.Max(previousEnd, end);
            }
        }

        private static int DetectAlignment(AssetsFile file)
        {
            return file.Metadata.AssetInfos.Count > 0
                && file.Metadata.AssetInfos.All(info => info.GetAbsoluteByteOffset(file) % 16 == 0)
                ? 16 : 8;
        }

        private static bool TryParseObject(AssetsManager manager, AssetsFileInstance inst, AssetFileInfo info, out string error)
        {
            try
            {
                AssetTypeValueField field = manager.GetBaseField(inst, info);
                if (field == null)
                    throw new InvalidOperationException("GetBaseField returned null");
                long serializedSize = field.WriteToByteArray().LongLength;
                if (serializedSize != info.ByteSize)
                {
                    throw new InvalidDataException(
                        $"展开模板只覆盖 {serializedSize} / {info.ByteSize} 字节，可能缺少 MonoBehaviour 脚本字段"
                    );
                }
                error = "";
                return true;
            }
            catch (Exception ex)
            {
                error = $"{ex.GetType().Name}: {ex.Message}";
                return false;
            }
        }

        private AssetsManager CreateManager()
        {
            AssetsManager manager = new AssetsManager
            {
                UseQuickLookup = true,
                UseTemplateFieldCache = true,
                UseMonoTemplateFieldCache = true,
                UseRefTypeManagerCache = true
            };
            string classDataPath = Path.Combine(AppContext.BaseDirectory, "classdata.tpk");
            if (File.Exists(classDataPath))
                manager.LoadClassPackage(classDataPath);
            if (Directory.Exists(options.ManagedRoot))
                manager.MonoTempGenerator = new MonoCecilTempGenerator(options.ManagedRoot);
            return manager;
        }

        private void PreloadPlayerDataDependencies(AssetsManager manager)
        {
            string dataBundlePath = Path.Combine(options.SourceRoot, "bin", "Data", "data.unity3d");
            if (!File.Exists(dataBundlePath)
                || FileTypeDetector.DetectFileType(dataBundlePath) != DetectedFileType.BundleFile)
                return;

            BundleFileInstance bundle = manager.LoadBundleFile(dataBundlePath, true);
            for (int i = 0; i < bundle.file.BlockAndDirInfo.DirectoryInfos.Count; i++)
            {
                AssetBundleDirectoryInfo entry = bundle.file.BlockAndDirInfo.DirectoryInfos[i];
                if ((entry.Flags & 4) == 0)
                    continue;
                try
                {
                    manager.LoadAssetsFileFromBundle(bundle, i, true);
                }
                catch
                {
                    // The changed object check below will report any dependency that
                    // was actually required but could not be expanded.
                }
            }
        }

        private static void PrepareManager(AssetsManager manager, AssetsFileInstance inst)
        {
            if (!inst.file.Metadata.TypeTreeEnabled && manager.ClassPackage != null
                && !string.IsNullOrWhiteSpace(inst.file.Metadata.UnityVersion))
                manager.LoadClassDatabaseFromPackage(inst.file.Metadata.UnityVersion);
        }

        private static bool SegmentEquals(Stream first, long firstOffset, long firstSize, Stream second, long secondOffset, long secondSize)
        {
            if (firstSize != secondSize)
                return false;
            byte[] firstBuffer = new byte[64 * 1024];
            byte[] secondBuffer = new byte[64 * 1024];
            lock (first)
            lock (second)
            {
                first.Position = firstOffset;
                second.Position = secondOffset;
                long remaining = firstSize;
                while (remaining > 0)
                {
                    int wanted = (int)Math.Min(firstBuffer.Length, remaining);
                    int firstRead = ReadFull(first, firstBuffer, wanted);
                    int secondRead = ReadFull(second, secondBuffer, wanted);
                    if (firstRead != wanted || secondRead != wanted
                        || !firstBuffer.AsSpan(0, wanted).SequenceEqual(secondBuffer.AsSpan(0, wanted)))
                        return false;
                    remaining -= wanted;
                }
            }
            return true;
        }

        private static int ReadFull(Stream stream, byte[] buffer, int wanted)
        {
            int total = 0;
            while (total < wanted)
            {
                int read = stream.Read(buffer, total, wanted - total);
                if (read == 0)
                    break;
                total += read;
            }
            return total;
        }

        private static string HashStream(Stream stream)
        {
            lock (stream)
            {
                stream.Position = 0;
                return Convert.ToHexString(SHA256.HashData(stream));
            }
        }

        private void EqualOrError<T>(T original, T candidate, string file, string field, string scope)
        {
            if (!EqualityComparer<T>.Default.Equals(original, candidate))
                Error(file, $"{field} 变化: original={original}, candidate={candidate}", scope);
        }

        private void Error(string file, string message, string scope)
        {
            issues.Add(new VerifyIssue("error", file, scope, message));
            Console.WriteLine($"\u001b[91m[资源验证][错误] {file}: {message}\u001b[0m");
        }

        private void Warning(string file, string message, string scope)
        {
            issues.Add(new VerifyIssue("warning", file, scope, message));
            Console.WriteLine($"\u001b[93m[资源验证][警告] {file}: {message}\u001b[0m");
        }

        private sealed record VerifyIssue(string Severity, string File, string Scope, string Message);
    }
}
