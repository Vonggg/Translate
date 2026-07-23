using System.Collections.Generic;

namespace UnityResourceCLI
{
    internal sealed class ExportManifest
    {
        public string SourceRelativePath { get; set; } = "";
        public string SourceKind { get; set; } = "";
        public string SourceFileName { get; set; } = "";
        public string UnityVersion { get; set; } = "";
        public List<ExportManifestAssetsFile> AssetsFiles { get; set; } = new();
        public List<ExportManifestMonoBehaviourSummary> MonoBehaviourSummaries { get; set; } = new();
        public List<ExportManifestItem> Items { get; set; } = new();
    }

    internal sealed class ExportManifestAssetsFile
    {
        public string RelativeBase { get; set; } = "";
        public string BundleEntryName { get; set; } = "";
        public List<ExportManifestExternal> Externals { get; set; } = new();
    }

    internal sealed class ExportManifestExternal
    {
        public int FileId { get; set; }
        public string PathName { get; set; } = "";
        public string OriginalPathName { get; set; } = "";
        public string Type { get; set; } = "";
    }

    internal sealed class ExportManifestMonoBehaviourSummary
    {
        public string RelativeBase { get; set; } = "";
        public string BundleEntryName { get; set; } = "";
        public int Total { get; set; }
        public int BaseOnly { get; set; }
        public int WithCustomFields { get; set; }
        public int Failed { get; set; }
    }

    internal sealed class ExportManifestItem
    {
        public long PathId { get; set; }
        public int TypeId { get; set; }
        public ushort ScriptIndex { get; set; }
        public string TypeName { get; set; } = "";
        public string AssetName { get; set; } = "";
        public string ExportKind { get; set; } = "";
        public string RelativePath { get; set; } = "";
        public string BundleEntryName { get; set; } = "";
    }
}
