using System;
using System.IO;
using System.Linq;
using System.Text;
using TMPro;
using TMPro.EditorUtilities;
using UnityEditor;
using UnityEngine;

namespace Translate.EditorTools
{
    [Serializable]
    public class TmpFontJob
    {
        public string sourceFontAssetPath;
        public string outputAssetPath;
        public string charactersFilePath;
        public string characters;
        public int pointSizeSamplingMode = 0;
        public int pointSize = 90;
        public int padding = 9;
        public int paddingMode = 2;
        public int packingMode = 4;
        public int atlasWidth = 2048;
        public int atlasHeight = 2048;
        public int characterSetSelectionMode = 8;
        public string renderMode = "SDFAA";
        public string atlasPopulationMode = "Dynamic";
        public bool multiAtlasSupport = true;
        public bool includeFontFeatures = false;
    }

    public static class TmpFontGenerator
    {
        private const string DefaultJobPath = "Assets/SourceFonts/font-job.json";

        public static void Run()
        {
            var jobPath = GetArgumentValue("--job") ?? DefaultJobPath;
            var job = LoadJob(jobPath);
            Generate(job);
        }

        private static TmpFontJob LoadJob(string jobPath)
        {
            var absoluteJobPath = ToAbsolutePath(jobPath);
            if (!File.Exists(absoluteJobPath))
            {
                throw new FileNotFoundException($"Job file not found: {absoluteJobPath}");
            }

            var json = File.ReadAllText(absoluteJobPath, Encoding.UTF8);
            var job = JsonUtility.FromJson<TmpFontJob>(json);
            if (job == null)
            {
                throw new InvalidOperationException($"Failed to parse job file: {absoluteJobPath}");
            }

            return job;
        }

        private static void Generate(TmpFontJob job)
        {
            EnsureTmpEssentialResources();

            if (string.IsNullOrWhiteSpace(job.sourceFontAssetPath))
            {
                throw new ArgumentException("sourceFontAssetPath is required");
            }

            if (string.IsNullOrWhiteSpace(job.outputAssetPath))
            {
                throw new ArgumentException("outputAssetPath is required");
            }

            var font = AssetDatabase.LoadAssetAtPath<Font>(NormalizeAssetPath(job.sourceFontAssetPath));
            if (font == null)
            {
                throw new FileNotFoundException($"Unity font asset not found: {job.sourceFontAssetPath}");
            }

            var renderMode = ParseRenderMode(job.renderMode);
            var requestedPopulationMode = ParseAtlasPopulationMode(job.atlasPopulationMode);
            var generationPopulationMode = requestedPopulationMode == AtlasPopulationMode.Static
                ? AtlasPopulationMode.Dynamic
                : requestedPopulationMode;
            var requestedCharacters = ReadRequestedCharacters(job);
            var fontAsset = CreatePopulatedFontAsset(
                font,
                job,
                renderMode,
                generationPopulationMode,
                requestedCharacters);

            ApplyCreationSettings(fontAsset, font, job, renderMode, requestedCharacters);
            PreserveGeneratedData(fontAsset, requestedPopulationMode);
            SaveFontAsset(fontAsset, NormalizeAssetPath(job.outputAssetPath));
        }

        private static void EnsureTmpEssentialResources()
        {
            if (Resources.Load<TMP_Settings>("TMP Settings") != null)
            {
                return;
            }

            Debug.Log("TMP Settings not found. Importing TMP Essential Resources...");
            string packageFullPath = TMP_EditorUtility.packageFullPath;
            string essentialPackagePath = packageFullPath + "/Package Resources/TMP Essential Resources.unitypackage";
            Debug.Log("Importing TMP resources from: " + essentialPackagePath);
            AssetDatabase.ImportPackage(essentialPackagePath, false);
            AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);

            if (Resources.Load<TMP_Settings>("TMP Settings") == null)
            {
                throw new InvalidOperationException("TMP Essential Resources import did not create TMP Settings.");
            }
        }

        private static string ReadRequestedCharacters(TmpFontJob job)
        {
            if (!string.IsNullOrWhiteSpace(job.characters))
            {
                return job.characters;
            }

            if (string.IsNullOrWhiteSpace(job.charactersFilePath))
            {
                return string.Empty;
            }

            var charFile = ToAbsolutePath(job.charactersFilePath);
            if (!File.Exists(charFile))
            {
                throw new FileNotFoundException($"Character file not found: {charFile}");
            }

            return File.ReadAllText(charFile, Encoding.UTF8);
        }

        private static TMP_FontAsset CreatePopulatedFontAsset(
            Font font,
            TmpFontJob job,
            UnityEngine.TextCore.LowLevel.GlyphRenderMode renderMode,
            AtlasPopulationMode generationPopulationMode,
            string requestedCharacters)
        {
            if (job.pointSizeSamplingMode == 0 && !string.IsNullOrEmpty(requestedCharacters))
            {
                var maxPointSize = Mathf.Max(1, job.pointSize);
                var low = 1;
                var high = maxPointSize;
                TMP_FontAsset bestCandidate = null;
                var bestPointSize = 0;

                while (low <= high)
                {
                    var pointSize = (low + high) / 2;
                    var candidate = CreateFontAsset(font, job, renderMode, generationPopulationMode, pointSize);
                    var missingCharacters = AddCharacters(candidate, requestedCharacters, job.includeFontFeatures, false);
                    if (string.IsNullOrEmpty(missingCharacters))
                    {
                        if (bestCandidate != null)
                        {
                            UnityEngine.Object.DestroyImmediate(bestCandidate);
                        }
                        bestCandidate = candidate;
                        bestPointSize = pointSize;
                        low = pointSize + 1;
                        continue;
                    }

                    UnityEngine.Object.DestroyImmediate(candidate);
                    high = pointSize - 1;
                }

                if (bestCandidate != null)
                {
                    Debug.Log($"Auto point size selected: {bestPointSize}");
                    return bestCandidate;
                }

                throw new InvalidOperationException(
                    $"TMP font asset creation failed: no point size from {maxPointSize} down to 1 could contain all requested characters.");
            }

            var samplingPointSize = job.pointSizeSamplingMode == 0 ? 0 : job.pointSize;
            var fontAsset = CreateFontAsset(font, job, renderMode, generationPopulationMode, samplingPointSize);
            AddCharacters(fontAsset, requestedCharacters, job.includeFontFeatures, true);
            return fontAsset;
        }

        private static TMP_FontAsset CreateFontAsset(
            Font font,
            TmpFontJob job,
            UnityEngine.TextCore.LowLevel.GlyphRenderMode renderMode,
            AtlasPopulationMode generationPopulationMode,
            int samplingPointSize)
        {
            var fontAsset = TMP_FontAsset.CreateFontAsset(
                font,
                samplingPointSize,
                job.padding,
                renderMode,
                job.atlasWidth,
                job.atlasHeight,
                generationPopulationMode,
                job.multiAtlasSupport);

            if (fontAsset == null)
            {
                throw new InvalidOperationException("TMP font asset creation failed.");
            }

            return fontAsset;
        }

        private static string AddCharacters(TMP_FontAsset fontAsset, string characters, bool includeFontFeatures, bool logMissing)
        {
            if (string.IsNullOrEmpty(characters))
            {
                return string.Empty;
            }

            if (!fontAsset.TryAddCharacters(characters, out var missingCharacters, includeFontFeatures) && !string.IsNullOrEmpty(missingCharacters))
            {
                if (logMissing)
                {
                    Debug.LogWarning($"Some characters were not added: {missingCharacters}");
                }
                return missingCharacters;
            }

            return string.Empty;
        }

        private static void ApplyCreationSettings(
            TMP_FontAsset fontAsset,
            Font sourceFont,
            TmpFontJob job,
            UnityEngine.TextCore.LowLevel.GlyphRenderMode renderMode,
            string requestedCharacters)
        {
            var sourceFontPath = AssetDatabase.GetAssetPath(sourceFont);
            fontAsset.creationSettings = new FontAssetCreationSettings
            {
                sourceFontFileName = sourceFont.name,
                sourceFontFileGUID = AssetDatabase.AssetPathToGUID(sourceFontPath),
                faceIndex = 0,
                pointSizeSamplingMode = job.pointSizeSamplingMode,
                pointSize = Mathf.RoundToInt(fontAsset.faceInfo.pointSize),
                padding = fontAsset.atlasPadding,
                paddingMode = job.paddingMode,
                packingMode = job.packingMode,
                atlasWidth = fontAsset.atlasWidth,
                atlasHeight = fontAsset.atlasHeight,
                characterSetSelectionMode = job.characterSetSelectionMode,
                characterSequence = string.IsNullOrEmpty(requestedCharacters) ? string.Empty : requestedCharacters,
                referencedFontAssetGUID = string.Empty,
                referencedTextAssetGUID = string.Empty,
                fontStyle = 0,
                fontStyleModifier = 0,
                renderMode = (int)renderMode,
                includeFontFeatures = job.includeFontFeatures,
            };
        }

        private static void PreserveGeneratedData(TMP_FontAsset fontAsset, AtlasPopulationMode requestedPopulationMode)
        {
            fontAsset.atlasPopulationMode = requestedPopulationMode;

            var serialized = new SerializedObject(fontAsset);
            var clearDynamicData = serialized.FindProperty("m_ClearDynamicDataOnBuild");
            if (clearDynamicData != null)
            {
                clearDynamicData.boolValue = false;
            }
            serialized.ApplyModifiedPropertiesWithoutUndo();
        }

        private static void SaveFontAsset(TMP_FontAsset fontAsset, string assetPath)
        {
            EnsureProjectDirectory(assetPath);

            if (AssetDatabase.LoadAssetAtPath<UnityEngine.Object>(assetPath) != null)
            {
                AssetDatabase.DeleteAsset(assetPath);
            }

            AssetDatabase.CreateAsset(fontAsset, assetPath);

            if (fontAsset.material != null)
            {
                AssetDatabase.AddObjectToAsset(fontAsset.material, fontAsset);
            }

            if (fontAsset.atlasTextures != null)
            {
                foreach (var atlasTexture in fontAsset.atlasTextures.Where(texture => texture != null))
                {
                    AssetDatabase.AddObjectToAsset(atlasTexture, fontAsset);
                }
            }

            EditorUtility.SetDirty(fontAsset);
            AssetDatabase.SaveAssets();
            AssetDatabase.Refresh();

            Debug.Log($"TMP font asset generated: {assetPath}");
        }

        private static UnityEngine.TextCore.LowLevel.GlyphRenderMode ParseRenderMode(string value)
        {
            if (!string.IsNullOrWhiteSpace(value) && int.TryParse(value, out var numeric))
            {
                return (UnityEngine.TextCore.LowLevel.GlyphRenderMode)numeric;
            }

            if (!string.IsNullOrWhiteSpace(value) &&
                Enum.TryParse(value, true, out UnityEngine.TextCore.LowLevel.GlyphRenderMode parsed))
            {
                return parsed;
            }

            return UnityEngine.TextCore.LowLevel.GlyphRenderMode.SDFAA;
        }

        private static TMPro.AtlasPopulationMode ParseAtlasPopulationMode(string value)
        {
            if (!string.IsNullOrWhiteSpace(value) &&
                Enum.TryParse(value, true, out TMPro.AtlasPopulationMode parsed))
            {
                return parsed;
            }

            return TMPro.AtlasPopulationMode.Dynamic;
        }

        private static void EnsureProjectDirectory(string assetPath)
        {
            var absolute = ToAbsolutePath(assetPath);
            var directory = Path.GetDirectoryName(absolute);
            if (!string.IsNullOrEmpty(directory))
            {
                Directory.CreateDirectory(directory);
            }
        }

        private static string GetArgumentValue(string key)
        {
            var args = Environment.GetCommandLineArgs();
            for (var i = 0; i < args.Length - 1; i++)
            {
                if (string.Equals(args[i], key, StringComparison.OrdinalIgnoreCase))
                {
                    return args[i + 1];
                }
            }

            return null;
        }

        private static string NormalizeAssetPath(string path)
        {
            var normalized = path.Replace('\\', '/');
            if (!normalized.StartsWith("Assets/", StringComparison.OrdinalIgnoreCase) &&
                !string.Equals(normalized, "Assets", StringComparison.OrdinalIgnoreCase))
            {
                throw new ArgumentException($"Path must be inside the project Assets folder: {path}");
            }

            return normalized;
        }

        private static string ToAbsolutePath(string path)
        {
            if (Path.IsPathRooted(path))
            {
                return Path.GetFullPath(path);
            }

            var projectRoot = Path.GetFullPath(Path.Combine(Application.dataPath, ".."));
            return Path.GetFullPath(Path.Combine(projectRoot, path));
        }

        [MenuItem("Tools/TMP Font Asset Generator/Run Job")]
        public static void RunFromMenu()
        {
            Run();
        }
    }
}
