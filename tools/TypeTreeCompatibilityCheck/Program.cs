using AssetsTools.NET;
using AssetsTools.NET.Extra;

// Read-only resource checks. No writes to game resources or export directories.
if (args.Length < 1) throw new ArgumentException("Usage: <classdata.tpk> [bundle]");
var manager = new AssetsManager { UseTemplateFieldCache = true };
manager.LoadClassPackage(args[0]);
var package = manager.ClassPackage;
var versions = package.TpkTypeTree.Versions;
Console.WriteLine($"TPK v{package.Header.FileVersion}: {versions.Count} versions, {versions.First()} through {versions.Last()}, {package.TpkTypeTree.ClassInformation.Count} classes");
foreach (string version in new[] { "5.6.7f1", "2019.4.40f1", "2021.3.45f1", "2022.3.62f1", "6000.0.58f2", "6000.5.10f1" })
{
    var database = package.GetClassDatabase(version);
    foreach (int id in new[] { 1, 4, 28, 212, 213 })
        if (!database.Classes.Any(c => c.ClassId == id)) throw new Exception($"Missing {id} for {version}");
    Console.WriteLine($"VERSION_OK {version} classes={database.Classes.Count}");
}
// Both package formats must survive all supported compression modes.
foreach (var compression in new[] { ClassFileCompressionType.Uncompressed, ClassFileCompressionType.Lz4, ClassFileCompressionType.Lzma })
using (var stream = new MemoryStream())
{
    package.Write(new AssetsFileWriter(stream), compression);
    stream.Position = 0;
    var roundtrip = new ClassPackageFile();
    roundtrip.Read(new AssetsFileReader(stream));
    if (roundtrip.TpkTypeTree.Nodes.Count != package.TpkTypeTree.Nodes.Count ||
        roundtrip.TpkTypeTree.Versions.Count != versions.Count)
        throw new Exception("TPK roundtrip failed");
    using var second = new MemoryStream();
    roundtrip.Write(new AssetsFileWriter(second), compression);
    if (!stream.ToArray().SequenceEqual(second.ToArray())) throw new Exception("TPK byte roundtrip failed");
    Console.WriteLine($"TPK_ROUNDTRIP_OK {compression}");
}
if (args.Length < 2) return;
var totals = new Dictionary<string, (int ok, int fail)>();
foreach (string bundlePath in args.Skip(1))
{
Console.WriteLine($"BUNDLE {bundlePath}");
var bundle = manager.LoadBundleFile(bundlePath, true);
for (int i = 0; i < bundle.file.BlockAndDirInfo.DirectoryInfos.Count; i++)
{
    AssetsFileInstance file = manager.LoadAssetsFileFromBundle(bundle, i, false);
    if (file == null) continue;
    Console.WriteLine($"ASSET {file.path}: {file.file.Metadata.UnityVersion}");
    manager.LoadClassDatabaseFromPackage(file.file.Metadata.UnityVersion);
    foreach (AssetClassID type in new[] { AssetClassID.GameObject, AssetClassID.Transform, AssetClassID.Texture2D, AssetClassID.Sprite, AssetClassID.SpriteRenderer })
    {
        foreach (var info in file.file.GetAssetsOfType(type))
        {
            var count = totals.GetValueOrDefault(type.ToString());
            try
            {
                var field = manager.GetBaseField(file, info);
                byte[] serialized = field.WriteToByteArray();
                if (serialized.Length != info.ByteSize) throw new Exception($"size={serialized.Length}/{info.ByteSize}");
                file.file.Reader.Position = info.GetAbsoluteByteOffset(file.file);
                byte[] original = file.file.Reader.ReadBytes((int)info.ByteSize);
                if (!serialized.SequenceEqual(original)) throw new Exception("Roundtrip bytes differ");
                count.ok++;
                if (type == AssetClassID.SpriteRenderer && count.ok <= 2)
                    Console.WriteLine($"SPRITE_REF {file.name}/{info.PathId}: {field["m_Sprite"]["m_FileID"].AsInt}:{field["m_Sprite"]["m_PathID"].AsLong}");
            }
            catch (Exception ex)
            {
                if (count.fail < 3) Console.WriteLine($"FAIL {file.name}/{info.PathId} {type}: {ex.Message}");
                count.fail++;
            }
            totals[type.ToString()] = count;
        }
    }
}
// AssetsTools resolves dependencies by basename. Separate games may have
// identical level/sharedassets names, so release them between bundles while
// retaining the manager and its package to exercise version switching.
manager.UnloadAllBundleFiles();
}
foreach (var row in totals) Console.WriteLine($"SUMMARY {row.Key}: ok={row.Value.ok} fail={row.Value.fail}");
if (totals.Values.Any(c => c.fail > 0)) Environment.ExitCode = 1;
