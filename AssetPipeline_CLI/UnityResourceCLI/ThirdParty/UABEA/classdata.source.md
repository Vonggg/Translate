# Native Unity type templates

Updated 2026-09-11 from the AssetRipper **Type Tree TPK**, LZMA build:

- Project: https://github.com/AssetRipper/Tpk
- Data: https://github.com/AssetRipper/TypeTreeDumps
- Download: https://nightly.link/AssetRipper/Tpk/workflows/type_tree_tpk/master/lzma_file.zip
- Installed file: `classdata.tpk` (upstream `lzma.tpk`), 207720 bytes, TPK v2
- SHA-256: `b88678dc1b8c47af58873735d299f4ba968aefa6ca048d688274edfa195fe893`
- 1420 version records, from Unity 3.4.0f1 to 6000.7.0a3; 415 class records.
- Previous package: `classdata.legacy-v1.tpk`, 1008 version records ending at 6000.0.0b16.

This is a multi-version database, not a template that forces every game to use
the latest Unity layout. Version-range coverage does not guarantee compatibility
with every custom engine build or future version. Embedded type trees remain
preferred. DummyDll handles managed script fields separately.

## Local reader compatibility

The vendored AssetsTools.NET reads and writes both TPK v1 and v2. V2's explicit,
versioned common-string offsets are retained in the package. When constructing
a legacy CLDB, common-string substitution is disabled for v2; generated type
trees use local strings instead, avoiding incorrect common-table offsets.

Native template caches are isolated by assets file. Native field serialization
lengths are checked before exporting/importing through ResourcePipeline. Unknown
versions outside the package range are rejected when no embedded type tree exists.
Existing import type-tree fingerprints still reject stale, incompatible exports.

## Updating again

1. Download the Type Tree **LZMA or LZ4** archive, not Engine Assets or Brotli.
2. Keep the current package as a backup and test the downloaded file first:
   `dotnet run --project tools/TypeTreeCompatibilityCheck -- <downloaded.tpk> [game-bundle ...]`
3. Update `classdata.tpk`, its hash and version information in this document.
4. Rebuild UnityResourceCLI. Its project copies this file into the runtime folder.
5. Back up and regenerate affected exports and hierarchy caches. Do not reimport
   old malformed JSON merely because the binary template has been updated.

Validation on this update: TPK v1/v2 roundtrips; native object byte roundtrips for
Pet World (6000.5.10f1) and Modern Warfare (2022.3.22f1); Pet World full re-export
and hierarchy preview regeneration. Native PNG, Sprite and renderer references
were recovered; two unrelated managed objects were still safely preserved.
