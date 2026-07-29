using AssetsTools.NET;
using Newtonsoft.Json.Linq;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;

namespace UnityResourceCLI
{
    internal sealed class AssetDumpHelper
    {
        private StreamWriter? sw;
        private StreamReader? sr;
        private AssetsFileWriter? aw;
        public int PreservedManagedReferencesCount { get; private set; }
        public int PreservedMissingJsonFieldsCount { get; private set; }
        public List<string> PreservedMissingJsonFieldNames { get; } = new List<string>();

        public void DumpTextAsset(StreamWriter sw, AssetTypeValueField baseField)
        {
            this.sw = sw;
            RecurseTextDump(baseField, 0);
        }

        public void DumpJsonAsset(StreamWriter sw, AssetTypeValueField baseField)
        {
            this.sw = sw;
            JToken token = RecurseJsonDump(baseField);
            sw.Write(token.ToString());
        }

        public byte[]? ImportTextAsset(StreamReader sr, out string? exceptionMessage)
        {
            this.sr = sr;
            using MemoryStream ms = new MemoryStream();
            aw = new AssetsFileWriter(ms) { BigEndian = false };

            try
            {
                ImportTextAssetLoop();
                exceptionMessage = null;
                return ms.ToArray();
            }
            catch (Exception ex)
            {
                exceptionMessage = ex.Message;
                return null;
            }
        }

        public byte[]? ImportJsonAsset(AssetTypeTemplateField tempField, StreamReader sr, out string? exceptionMessage)
        {
            this.sr = sr;
            PreservedManagedReferencesCount = 0;
            PreservedMissingJsonFieldsCount = 0;
            PreservedMissingJsonFieldNames.Clear();
            using MemoryStream ms = new MemoryStream();
            aw = new AssetsFileWriter(ms) { BigEndian = false };

            try
            {
                JToken token = JToken.Parse(sr.ReadToEnd());
                RecurseJsonImport(tempField, token);
                exceptionMessage = null;
                return ms.ToArray();
            }
            catch (Exception ex)
            {
                exceptionMessage = ex.Message;
                return null;
            }
        }

        public byte[]? ImportJsonAssetPreserveManagedReferences(
            AssetTypeTemplateField tempField,
            AssetTypeValueField baseField,
            StreamReader sr,
            out string? exceptionMessage)
        {
            this.sr = sr;
            PreservedManagedReferencesCount = 0;
            PreservedMissingJsonFieldsCount = 0;
            PreservedMissingJsonFieldNames.Clear();
            using MemoryStream ms = new MemoryStream();
            aw = new AssetsFileWriter(ms) { BigEndian = false };

            try
            {
                JToken token = JToken.Parse(sr.ReadToEnd());
                RecurseJsonImport(tempField, token, baseField);
                exceptionMessage = null;
                return ms.ToArray();
            }
            catch (Exception ex)
            {
                exceptionMessage = ex.Message;
                return null;
            }
        }

        public byte[]? ImportLocalizationStringTableJsonAsset(AssetTypeValueField baseField, string json, out string? exceptionMessage)
        {
            using MemoryStream ms = new MemoryStream();
            aw = new AssetsFileWriter(ms) { BigEndian = false };

            try
            {
                JObject token = JObject.Parse(json);
                int matched = PatchLocalizationStringTable(baseField, token);
                if (matched == 0)
                    throw new Exception("No m_Localized string ids matched in Localization StringTable JSON.");

                baseField.Write(aw);
                exceptionMessage = null;
                return ms.ToArray();
            }
            catch (Exception ex)
            {
                exceptionMessage = ex.Message;
                return null;
            }
        }

        private void RecurseTextDump(AssetTypeValueField field, int depth)
        {
            AssetTypeTemplateField template = field.TemplateField;
            string align = template.IsAligned ? "1" : "0";
            string typeName = template.Type;
            string fieldName = template.Name;
            bool isArray = template.IsArray;

            if (template.ValueType == AssetValueType.String)
                align = "1";

            if (isArray)
            {
                AssetTypeTemplateField sizeTemplate = template.Children[0];
                string sizeAlign = sizeTemplate.IsAligned ? "1" : "0";
                string sizeTypeName = sizeTemplate.Type;
                string sizeFieldName = sizeTemplate.Name;

                if (template.ValueType != AssetValueType.ByteArray)
                {
                    int size = field.AsArray.size;
                    sw!.WriteLine($"{new string(' ', depth)}{align} {typeName} {fieldName} ({size} items)");
                    sw.WriteLine($"{new string(' ', depth + 1)}{sizeAlign} {sizeTypeName} {sizeFieldName} = {size}");
                    for (int i = 0; i < field.Children.Count; i++)
                    {
                        sw.WriteLine($"{new string(' ', depth + 1)}[{i}]");
                        RecurseTextDump(field.Children[i], depth + 2);
                    }
                }
                else
                {
                    byte[] data = field.AsByteArray;
                    int size = data.Length;

                    sw!.WriteLine($"{new string(' ', depth)}{align} {typeName} {fieldName} ({size} items)");
                    sw.WriteLine($"{new string(' ', depth + 1)}{sizeAlign} {sizeTypeName} {sizeFieldName} = {size}");
                    for (int i = 0; i < size; i++)
                    {
                        sw.WriteLine($"{new string(' ', depth + 1)}[{i}]");
                        sw.WriteLine($"{new string(' ', depth + 2)}0 UInt8 data = {data[i]}");
                    }
                }
            }
            else
            {
                string value = "";
                if (field.Value != null)
                {
                    AssetValueType evt = field.Value.ValueType;
                    if (evt == AssetValueType.String)
                    {
                        string fixedStr = TextDumpEscapeString(field.AsString);
                        value = $" = \"{fixedStr}\"";
                    }
                    else if (1 <= (int)evt && (int)evt <= 12)
                    {
                        value = $" = {field.AsString}";
                    }
                }

                sw!.WriteLine($"{new string(' ', depth)}{align} {typeName} {fieldName}{value}");

                if (field.Value != null && field.Value.ValueType == AssetValueType.ManagedReferencesRegistry)
                {
                    ManagedReferencesRegistry registry = field.Value.AsManagedReferencesRegistry;

                    if (registry.version == 1)
                    {
                        List<AssetTypeReferencedObject> referencesWithTerm = new List<AssetTypeReferencedObject>(registry.references)
                        {
                            new AssetTypeReferencedObject()
                            {
                                rid = 0,
                                type = AssetTypeReference.TERMINUS,
                                data = AssetTypeValueField.DUMMY_FIELD
                            }
                        };

                        sw.WriteLine($"{new string(' ', depth + 1)}0 int version = {registry.version}");
                        for (int i = 0; i < referencesWithTerm.Count; i++)
                        {
                            AssetTypeReferencedObject refObj = referencesWithTerm[i];
                            AssetTypeReference typeRef = refObj.type;
                            sw.WriteLine($"{new string(' ', depth + 1)}0 ReferencedObject {i:d8}");
                            sw.WriteLine($"{new string(' ', depth + 2)}0 ReferencedManagedType type");
                            sw.WriteLine($"{new string(' ', depth + 3)}1 string class = \"{TextDumpEscapeString(typeRef.ClassName)}\"");
                            sw.WriteLine($"{new string(' ', depth + 3)}1 string ns = \"{TextDumpEscapeString(typeRef.Namespace)}\"");
                            sw.WriteLine($"{new string(' ', depth + 3)}1 string asm = \"{TextDumpEscapeString(typeRef.AsmName)}\"");
                            sw.WriteLine($"{new string(' ', depth + 2)}0 ReferencedObjectData data");

                            foreach (AssetTypeValueField child in refObj.data.Children)
                            {
                                RecurseTextDump(child, depth + 3);
                            }
                        }
                    }
                    else if (registry.version == 2)
                    {
                        sw.WriteLine($"{new string(' ', depth + 1)}0 int version = {registry.version}");
                        sw.WriteLine($"{new string(' ', depth + 1)}0 vector RefIds");
                        sw.WriteLine($"{new string(' ', depth + 2)}1 Array Array");
                        sw.WriteLine($"{new string(' ', depth + 3)}0 int size = {registry.references.Count}");
                        for (int i = 0; i < registry.references.Count; i++)
                        {
                            AssetTypeReferencedObject refObj = registry.references[i];
                            AssetTypeReference typeRef = refObj.type;
                            sw.WriteLine($"{new string(' ', depth + 3)}0 ReferencedObject data");
                            sw.WriteLine($"{new string(' ', depth + 4)}0 SInt64 rid = {refObj.rid}");
                            sw.WriteLine($"{new string(' ', depth + 4)}0 ReferencedManagedType type");
                            sw.WriteLine($"{new string(' ', depth + 5)}1 string class = \"{TextDumpEscapeString(typeRef.ClassName)}\"");
                            sw.WriteLine($"{new string(' ', depth + 5)}1 string ns = \"{TextDumpEscapeString(typeRef.Namespace)}\"");
                            sw.WriteLine($"{new string(' ', depth + 5)}1 string asm = \"{TextDumpEscapeString(typeRef.AsmName)}\"");
                            sw.WriteLine($"{new string(' ', depth + 4)}0 ReferencedObjectData data");

                            foreach (AssetTypeValueField child in refObj.data.Children)
                            {
                                RecurseTextDump(child, depth + 5);
                            }
                        }
                    }
                    else
                    {
                        throw new NotSupportedException($"Registry version {registry.version} not supported!");
                    }
                }
                else
                {
                    foreach (AssetTypeValueField? child in field)
                    {
                        RecurseTextDump(child, depth + 1);
                    }
                }
            }
        }

        private JToken RecurseJsonDump(AssetTypeValueField field)
        {
            AssetTypeTemplateField template = field.TemplateField;

            if (template.IsArray)
            {
                JArray jArray = new JArray();
                if (template.ValueType != AssetValueType.ByteArray)
                {
                    for (int i = 0; i < field.Children.Count; i++)
                        jArray.Add(RecurseJsonDump(field.Children[i]));
                }
                else
                {
                    foreach (byte b in field.AsByteArray)
                        jArray.Add(b);
                }
                return jArray;
            }

            if (field.Value != null)
            {
                AssetValueType evt = field.Value.ValueType;
                if (field.Value.ValueType != AssetValueType.ManagedReferencesRegistry)
                {
                    object value = evt switch
                    {
                        AssetValueType.Bool => field.AsBool,
                        AssetValueType.Int8 or AssetValueType.Int16 or AssetValueType.Int32 => field.AsInt,
                        AssetValueType.Int64 => field.AsLong,
                        AssetValueType.UInt8 or AssetValueType.UInt16 or AssetValueType.UInt32 => field.AsUInt,
                        AssetValueType.UInt64 => field.AsULong,
                        AssetValueType.String => field.AsString,
                        AssetValueType.Float => field.AsFloat,
                        AssetValueType.Double => field.AsDouble,
                        _ => "invalid value"
                    };
                    return JToken.FromObject(value);
                }

                ManagedReferencesRegistry registry = field.Value.AsManagedReferencesRegistry;
                if (registry.version == 1 || registry.version == 2)
                {
                    JArray jArrayRefs = new JArray();
                    foreach (AssetTypeReferencedObject refObj in registry.references)
                    {
                        AssetTypeReference typeRef = refObj.type;
                        JObject jObjManagedType = new JObject
                        {
                            { "class", typeRef.ClassName },
                            { "ns", typeRef.Namespace },
                            { "asm", typeRef.AsmName }
                        };

                        JObject jObjData = new JObject();
                        foreach (AssetTypeValueField child in refObj.data)
                        {
                            jObjData.Add(child.FieldName, RecurseJsonDump(child));
                        }

                        JObject jObjRefObject = registry.version == 1
                            ? new JObject
                            {
                                { "type", jObjManagedType },
                                { "data", jObjData }
                            }
                            : new JObject
                            {
                                { "rid", refObj.rid },
                                { "type", jObjManagedType },
                                { "data", jObjData }
                            };

                        jArrayRefs.Add(jObjRefObject);
                    }

                    return new JObject
                    {
                        { "version", registry.version },
                        { "RefIds", jArrayRefs }
                    };
                }

                throw new NotSupportedException($"Registry version {registry.version} not supported!");
            }

            JObject jObject = new JObject();
            foreach (AssetTypeValueField child in field)
            {
                jObject.Add(child.FieldName, RecurseJsonDump(child));
            }
            return jObject;
        }

        private void ImportTextAssetLoop()
        {
            Stack<bool> alignStack = new Stack<bool>();
            while (true)
            {
                string? line = sr!.ReadLine();
                if (line == null)
                    return;

                int thisDepth = 0;
                while (thisDepth < line.Length && line[thisDepth] == ' ')
                    thisDepth++;

                if (thisDepth >= line.Length)
                    continue;

                if (line[thisDepth] == '[')
                    continue;

                if (thisDepth < alignStack.Count)
                {
                    while (thisDepth < alignStack.Count)
                    {
                        if (alignStack.Pop())
                            aw!.Align();
                    }
                }

                bool align = line.Substring(thisDepth, 1) == "1";
                int typeName = thisDepth + 2;
                int eqSign = line.IndexOf('=');
                string valueStr = eqSign >= 0 ? line.Substring(eqSign + 1).Trim() : string.Empty;

                if (eqSign != -1)
                {
                    string check = line.Substring(typeName);
                    if (StartsWithSpace(check, "int"))
                        aw!.Write(int.Parse(valueStr));
                    else if (StartsWithSpace(check, "float"))
                        aw!.Write(float.Parse(valueStr));
                    else if (StartsWithSpace(check, "bool"))
                        aw!.Write(bool.Parse(valueStr));
                    else if (StartsWithSpace(check, "SInt64"))
                        aw!.Write(long.Parse(valueStr));
                    else if (StartsWithSpace(check, "string"))
                    {
                        int firstQuote = valueStr.IndexOf('"');
                        int lastQuote = valueStr.LastIndexOf('"');
                        string valueStrFix = valueStr.Substring(firstQuote + 1, lastQuote - firstQuote - 1);
                        valueStrFix = UnescapeDumpString(valueStrFix);
                        aw!.WriteCountStringInt32(valueStrFix);
                    }
                    else if (StartsWithSpace(check, "UInt8"))
                        aw!.Write(byte.Parse(valueStr));
                    else if (StartsWithSpace(check, "unsigned int"))
                        aw!.Write(uint.Parse(valueStr));
                    else if (StartsWithSpace(check, "UInt16"))
                        aw!.Write(ushort.Parse(valueStr));
                    else if (StartsWithSpace(check, "SInt8"))
                        aw!.Write(sbyte.Parse(valueStr));
                    else if (StartsWithSpace(check, "SInt16"))
                        aw!.Write(short.Parse(valueStr));
                    else if (StartsWithSpace(check, "UInt64"))
                        aw!.Write(ulong.Parse(valueStr));
                    else if (StartsWithSpace(check, "double"))
                        aw!.Write(double.Parse(valueStr));
                    else if (StartsWithSpace(check, "char"))
                        aw!.Write(sbyte.Parse(valueStr));
                    else if (StartsWithSpace(check, "FileSize"))
                        aw!.Write(ulong.Parse(valueStr));
                    else if (StartsWithSpace(check, "short"))
                        aw!.Write(short.Parse(valueStr));
                    else if (StartsWithSpace(check, "long"))
                        aw!.Write(long.Parse(valueStr));
                    else if (StartsWithSpace(check, "SInt32"))
                        aw!.Write(int.Parse(valueStr));
                    else if (StartsWithSpace(check, "UInt32"))
                        aw!.Write(uint.Parse(valueStr));
                    else if (StartsWithSpace(check, "unsigned char"))
                        aw!.Write(byte.Parse(valueStr));
                    else if (StartsWithSpace(check, "unsigned short"))
                        aw!.Write(ushort.Parse(valueStr));
                    else if (StartsWithSpace(check, "unsigned long long"))
                        aw!.Write(ulong.Parse(valueStr));

                    if (align)
                        aw!.Align();
                }
                else
                {
                    alignStack.Push(align);
                }
            }
        }

        private void RecurseJsonImport(AssetTypeTemplateField tempField, JToken token)
        {
            RecurseJsonImport(tempField, token, null);
        }

        private void RecurseJsonImport(AssetTypeTemplateField tempField, JToken token, AssetTypeValueField? originalField)
        {
            bool align = tempField.IsAligned;

            if (!tempField.HasValue && !tempField.IsArray)
            {
                if (token is JArray)
                {
                    AssetTypeTemplateField? arrayChildTempField = tempField.Children
                        .FirstOrDefault(child => child.Name == "Array");
                    if (arrayChildTempField == null)
                        throw new Exception($"Field {tempField.Name} was an array in json but template has no Array child.");

                    RecurseJsonImport(arrayChildTempField, token, GetOriginalChild(originalField, "Array"));

                    if (align)
                        aw!.Align();
                    return;
                }

                foreach (AssetTypeTemplateField childTempField in tempField.Children)
                {
                    JToken? childToken = token[childTempField.Name];
                    if (childToken == null)
                    {
                        AssetTypeValueField? originalChild = GetOriginalChild(originalField, childTempField.Name);
                        if (originalChild == null)
                            throw new Exception($"Missing field {childTempField.Name} in JSON and original asset.");

                        originalChild.Write(aw!);
                        PreservedMissingJsonFieldsCount++;
                        PreservedMissingJsonFieldNames.Add(childTempField.Name);
                        continue;
                    }

                    RecurseJsonImport(childTempField, childToken, GetOriginalChild(originalField, childTempField.Name));
                }

                if (align)
                    aw!.Align();
            }
            else if (tempField.HasValue && tempField.ValueType == AssetValueType.ManagedReferencesRegistry)
            {
                if (originalField == null || originalField.Value == null || originalField.Value.ValueType != AssetValueType.ManagedReferencesRegistry)
                    throw new NotImplementedException("SerializeReference not supported in JSON import yet!");
                PreservedManagedReferencesCount++;
                originalField.Write(aw!);
            }
            else
            {
                switch (tempField.ValueType)
                {
                    case AssetValueType.Bool:
                        aw!.Write((bool)token);
                        break;
                    case AssetValueType.UInt8:
                        aw!.Write((byte)token);
                        break;
                    case AssetValueType.Int8:
                        aw!.Write((sbyte)token);
                        break;
                    case AssetValueType.UInt16:
                        aw!.Write((ushort)token);
                        break;
                    case AssetValueType.Int16:
                        aw!.Write((short)token);
                        break;
                    case AssetValueType.UInt32:
                        aw!.Write((uint)token);
                        break;
                    case AssetValueType.Int32:
                        aw!.Write((int)token);
                        break;
                    case AssetValueType.UInt64:
                        aw!.Write((ulong)token);
                        break;
                    case AssetValueType.Int64:
                        aw!.Write((long)token);
                        break;
                    case AssetValueType.Float:
                        aw!.Write((float)token);
                        break;
                    case AssetValueType.Double:
                        aw!.Write((double)token);
                        break;
                    case AssetValueType.String:
                        align = true;
                        aw!.WriteCountStringInt32((string?)token ?? "");
                        break;
                    case AssetValueType.ByteArray:
                        JArray byteArrayJArray = ((JArray?)token) ?? new JArray();
                        byte[] byteArrayData = new byte[byteArrayJArray.Count];
                        for (int i = 0; i < byteArrayJArray.Count; i++)
                            byteArrayData[i] = (byte)byteArrayJArray[i];
                        aw!.Write(byteArrayData.Length);
                        aw!.Write(byteArrayData);
                        break;
                }

                if (tempField.IsArray && tempField.ValueType != AssetValueType.ByteArray)
                {
                    AssetTypeTemplateField childTempField = tempField.Children[1];
                    JArray? tokenArray = (JArray?)token;
                    if (tokenArray == null)
                        throw new Exception($"Field {tempField.Name} was not an array in json.");

                    aw!.Write(tokenArray.Count);
                    foreach (JToken childToken in tokenArray.Children())
                    {
                        RecurseJsonImport(childTempField, childToken);
                    }
                }

                if (align)
                    aw!.Align();
            }
        }

        private static AssetTypeValueField? GetOriginalChild(AssetTypeValueField? originalField, string childName)
        {
            if (originalField == null)
                return null;
            try
            {
                AssetTypeValueField child = originalField[childName];
                return child.IsDummy ? null : child;
            }
            catch
            {
                return null;
            }
        }

        private int PatchLocalizationStringTable(AssetTypeValueField baseField, JObject token)
        {
            JArray? tableData = token["m_TableData"]?["Array"] as JArray;
            if (tableData == null)
                throw new Exception("Localization StringTable JSON missing m_TableData.Array.");

            Dictionary<long, string> localizedById = new Dictionary<long, string>();
            foreach (JToken item in tableData.Children())
            {
                long? id = (long?)item["m_Id"];
                string? localized = (string?)item["m_Localized"];
                if (id.HasValue && localized != null)
                    localizedById[id.Value] = localized;
            }

            AssetTypeValueField? baseTableData = FindChild(baseField, "m_TableData");
            AssetTypeValueField? baseArray = baseTableData == null ? null : FindChild(baseTableData, "Array");
            if (baseArray == null)
                throw new Exception("Original Localization StringTable field missing m_TableData.Array.");

            int matched = 0;
            foreach (AssetTypeValueField row in baseArray.Children)
            {
                AssetTypeValueField? idField = FindChild(row, "m_Id");
                AssetTypeValueField? localizedField = FindChild(row, "m_Localized");
                if (idField == null || localizedField == null)
                    continue;

                long id = idField.Value.ValueType == AssetValueType.UInt64
                    ? unchecked((long)idField.AsULong)
                    : idField.AsLong;
                if (!localizedById.TryGetValue(id, out string? localized))
                    continue;

                matched++;
                localizedField.AsString = localized;
            }

            return matched;
        }

        private static AssetTypeValueField? FindChild(AssetTypeValueField field, string name)
        {
            foreach (AssetTypeValueField child in field.Children)
            {
                if (child.FieldName == name)
                    return child;
            }
            return null;
        }

        private static bool StartsWithSpace(string str, string value) => str.StartsWith(value + " ");

        private string UnescapeDumpString(string str)
        {
            StringBuilder sb = new StringBuilder(str.Length);
            bool escaping = false;
            foreach (char c in str)
            {
                if (!escaping && c == '\\')
                {
                    escaping = true;
                    continue;
                }

                if (escaping)
                {
                    if (c == '\\')
                        sb.Append('\\');
                    else if (c == 'r')
                        sb.Append('\r');
                    else if (c == 'n')
                        sb.Append('\n');
                    else
                        sb.Append(c);
                    escaping = false;
                }
                else
                {
                    sb.Append(c);
                }
            }

            return sb.ToString();
        }

        private static string TextDumpEscapeString(string str)
        {
            return str
                .Replace("\\", "\\\\")
                .Replace("\r", "\\r")
                .Replace("\n", "\\n");
        }
    }
}
