# Translate

Unity 游戏资源汉化工具链。主要流程是：导出资源到 `workspace/input`，扫描并翻译文本，生成待导入的文本/字体/图片替换文件，最后重新导入并输出到 `workspace/FinalResult`。

## 先配置什么

配置文件是 `config.json`。换游戏、换电脑或换 Unity 环境时，优先检查下面这些字段。

### 游戏路径

- `project_root_dir`
  - 游戏项目所在的总目录。
- `project_name`
  - 当前要处理的游戏项目目录名。
- `resource_source_subpath`
  - 游戏资源目录相对路径，一般是 `game-name/game/assets/bin/Data`。
- `resource_managed_subpath`
  - Managed DLL 目录相对路径，一般是 `game-name/game/assets/bin/Data/Managed`。
- `catalog_source_subpath`
  - Addressables `catalog.json` 相对路径，一般在 `game-name/game/assets/aa/catalog.json`。
- `resource_staging_root`
  - 一键导出使用的统一原始资源暂存目录，默认 `workspace/input_sources`。
  - 每次一键导出都会清空重建，不要在其中保存手工文件。
- `addressables_remote_base_url`
  - 可选。catalog 只记录 `http/...` 或 RemoteLoadPath 相对地址、无法自动推导 CDN 地址时填写。
  - 如果 catalog 已含完整 URL，或能从 `m_InternalIdPrefixes`、同目录设置 JSON、`workspace/logs` 中的资源 URL 推导，可以留空。
- `addressables_download_workers` / `addressables_download_timeout`
  - 远程 Addressables 下载线程数和单次请求超时，默认 `5` 和 `60` 秒。
- `stringliteral_json_subpath`  （现在暂未使用）
  - 如果有 IL2CPP 字符串导出，可配置到 `game-name/bak/64/stringliteral.json`。

`bin/Data` 资源路径由：

```text
project_root_dir + project_name + resource_source_subpath
```

拼出来。导出前如果 Managed 目录缺 DLL，脚本会尝试从：

```text
project_root_dir/project_name/game-name/bak/64/DummyDll
```

补到 Managed 目录。

### Unity 和字体

- `unity_exe`
  - 本机 Unity Editor 的 `Unity.exe` 路径。
  - 生成 TMP/SDF 字体时需要完整 Unity Editor，单独复制 `Unity.exe` 不够。
- `unity_font_project`
  - 内置 Unity 辅助工程目录，默认 `TMP_Font_Generator`。
- `unity_font_launcher`
  - Unity 辅助工程内的生成脚本，默认 `Tools/generate_tmp_font.py`。
- `ttf_template_path`
  - 替换字体模板 TTF。模板不支持的译文字符会在脚本 7 中提示并停止。

### 翻译配置

- `enable_ai_translation`
  - `true` 时优先使用 AI 整表翻译。
  - AI 失败时回落到 `translate_provider` 指定的普通翻译。
- `ai_translation_base_url` / `ai_translation_api_key` / `ai_translation_model`
  - AI 翻译接口配置。
- `ai_translation_strategy`
  - AI 翻译分批策略脚本名，例如 `deepseek_translation_strategy.py`。
  - 没有对应策略时使用默认策略。
- `translate_provider`
  - AI 失败后的兜底翻译，目前常用 `baidu`。
- `baidu_appid` / `baidu_appkey`
  - 百度翻译配置。
- `max_translate_workers`
  - 百度等逐条翻译的最大线程数。`0` 表示脚本自动决定。

### AI 字段判断

- `enable_ai_field_review`
  - `false`：扫描时按 `text_keys` 白名单提取文本。
  - `true`：扫描时先用 `string_field_blacklist` 排除明显不该翻译的字段，再记录所有字符串字段，之后让 AI 判断哪些字段需要汉化。
- `ai_field_review_base_url` / `ai_field_review_api_key` / `ai_field_review_model`
  - AI 字段判断接口配置。
  - 如果未配置或访问失败，脚本会提示把 `workspace/records/string_field_review.txt` 手动交给 AI 判断。
- `string_field_blacklist`
  - 字段黑名单。典型例子：`m_Script`、`m_Name`、`m_Entries.Array*.m_Key`。
  - Unity Localization 的 Shared Data key 不应该翻译，所以 `m_Entries.Array*.m_Key` 必须保留在黑名单里。
- `text_keys`
  - 关闭 AI 字段判断时使用的白名单字段。

### 工作目录

通常不用改：

- `resource_input_root`: `workspace/input`
- `record_dir`: `workspace/records`
- `result_dir`: `workspace/output`
- `log_dir`: `workspace/logs`
- `image_import_dir`: `workspace/output/Image/ToImport`

重要输出：

- 文本待导入：`workspace/output/Text`
- TTF 待导入：`workspace/output/Font/TTF/ToImport`
- SDF 模板：`workspace/output/Font/SDF/generated_templates`
- SDF 待导入：`workspace/output/Font/SDF/ToImport`
- 图片待导入：`workspace/output/Image/ToImport`
- 最终结果：`workspace/FinalResult`

## 常规使用顺序

### 1. 一键导出资源

```powershell
cd D:\user\von\NewTools\Translate\Translate
python .\resource_menu.py
```

选择：

```text
1. 一键导出
```

它会：

- 检测 `.splitN` 分卷，确认后可自动合并。
- 导出资源到 `workspace/input`。
- 生成 `workspace/records/file_id_map.json`。
- 清理 `workspace/temp`。

### 2. 扫描、判断字段、翻译、生成待替换文件

```powershell
python .\main.py
```

推荐顺序：

```text
0 -> 1 -> 2 -> 4 -> 5 -> 6 -> 7 -> 8 -> 9
```

如果 `enable_ai_field_review=false`，跳过脚本 1：

```text
0 -> 2 -> 4 -> 5 -> 6 -> 7 -> 8 -> 9
```

如果想一口气执行：

```text
10. 全部执行
```

注意：脚本 10 在 AI 字段判断开启时会自动执行脚本 1。

### 3. 手动处理图片或工具修补

```powershell
python .\工具脚本.py
```

常用：

- 图片导出整理：菜单 4
- 图片恢复到 `Image/ToImport`：菜单 5
- AI 翻译单批补跑：菜单 9
- 清理模板 TTF 不支持字符：菜单 10
- 清理 records/trans 中当前黑名单字段：菜单 15  （新增黑名单但不想重扫描翻译等就用这个）
- 可选同步生成字体的 SDF 材质参数：菜单 16
- catalog 解析/回打/CRC 修正：菜单 11、12、13、14

### 4. 一键导入

```powershell
python .\resource_menu.py
```

选择：

```text
2. 一键导入
```

导入内容可输入多个编号，例如：

```text
2,3,4
```

含义：

- `1`: 文本替换，读取 `workspace/output/Text`
- `2`: TMP/SDF 字体替换，读取 `workspace/output/Font/SDF/ToImport`
- `3`: TTF 字体替换，读取 `workspace/output/Font/TTF/ToImport`
- `4`: 图片替换，读取 `workspace/output/Image/ToImport`
- `5`: 全部

导入不会覆盖原始资源，结果输出到：

```text
workspace/FinalResult
```

Bundle 文件会放到：

```text
workspace/FinalResult/Bundle/Android
```

如果检测到 Addressables catalog，导入收尾阶段会尝试解析并生成最终 catalog 到：

```text
workspace/FinalResult/Bundle/catalog.json
```

当前默认策略是匹配最终 Bundle 后把对应 catalog 条目的 CRC 置为 `0`。

## main.py 菜单说明

### 0. 扫描导出的 JSON，生成文本、字体、材质、引用索引

读取 `workspace/input` 下的导出 JSON。

输出到 `workspace/records`：

- `records.json`: 待翻译文本记录。
- `ids.json`: 实际导出替换文件时用到的目标文件索引。
- `font_map.json`: 字体使用关系。
- `material_map.json`: 材质引用关系。
- `ref_map.json`: MonoBehaviour 引用关系。
- `tmp_manifest_index.json`: TMP/TTF 替换定位索引。
- `string_field_stats.json` / `string_field_stats.tsv`: AI 字段判断启用时生成的完整字段统计。
- `string_field_review.txt`: 发给 AI 判断字段用的精简文本。

如果 `enable_ai_field_review=false`，脚本 0 直接按 `text_keys` 白名单生成 `records.json`。

如果 `enable_ai_field_review=true`，脚本 0 会先按 `string_field_blacklist` 排除明显不该翻译的字段，再记录字符串字段，等待脚本 1 过滤。

### 1. AI 判断字段后过滤 records.json

只在 `enable_ai_field_review=true` 时执行。

它会读取脚本 0 生成的 `string_field_review.txt`，让 AI 返回可能需要翻译的字段，然后按字段过滤 `records.json`。

字段路径里的数组下标会归一化，例如：

```text
list_Title.Array[0]
```

会作为：

```text
list_Title.Array[]
```

交给 AI 判断，避免同类字段爆炸。

### 2. 根据扫描记录翻译

读取过滤后的 `records.json`，去重后生成空 `trans.json`，然后执行翻译。

输出：

- `trans.json`: 原文到译文的映射。
- `game.txt`: 去重后的译文文本。
- `game_chars.txt`: 译文字符集合。
- `mapping.tsv`: 翻译对照。

AI 翻译开启时会把整批 trans 内容发给 AI，并把请求/响应写入：

- `ai_translation_request*.json`
- `ai_translation_response*.json`

如果 AI 漏翻或失败，剩余内容会回落到普通翻译。

### 3. 从 trans.json 重建 game.txt 和 game_chars.txt

适合手动修改 `trans.json` 后使用。

它不会重新扫描，也不会重新翻译，只根据现有 `trans.json` 刷新：

- `game.txt`
- `game_chars.txt`

### 4. 导出翻译后的待替换 JSON

读取 `records.json` 和 `trans.json`。

只处理 `trans.json` 命中的待汉化源 JSON，然后把翻译写入 `workspace/input` 的副本结构，输出到：

```text
workspace/output/Text
```

Unity Localization 要特别注意：

- StringTable 的 `m_TableData.Array[].m_Localized` 应该翻译。
- Shared Data 的 `m_Entries.Array[].m_Key` 不应该翻译。

### 5. 屏蔽译文文本同物体上的阴影/描边组件

根据 `records.json`、`ref_map.json` 和 PathID 映射，找到译文文本同 GameObject 上的 Shadow/Outline 组件，输出屏蔽后的 JSON 到 `workspace/output/Text`。

这是为了解决部分游戏中文字体叠加描边/阴影后显示异常的问题。

### 6. 生成 TTF 替换字体

读取脚本 0 的字体索引，使用 `ttf_template_path` 指向的模板字体，生成 TTF 待导入文件：

```text
workspace/output/Font/TTF/ToImport
```

### 7. 合并 TMP 字符并提示新增字符

读取：

- `trans.json`
- 原游戏 SDF 字体字符
- 模板 TTF 支持字符
- 模板 TTF 非中文字符

生成：

```text
workspace/records/tmp_chars.txt
```

如果 `trans.json` 里有模板 TTF 不支持的字符，会输出：

- `translation_chars_missing_from_ttf.txt`
- `translation_chars_missing_from_ttf.tsv`

并停止，避免生成缺字字体。

### 8. 生成 Unity TMP 字体

读取脚本 7 生成的 `tmp_chars.txt`，调用 Unity 辅助工程生成 TMP/SDF 字体资源。

输出：

```text
workspace/output/Font/SDF/generated_templates/generated_tmp_font.asset
workspace/output/Font/SDF/generated_templates/generated_tmp_font.asset.meta
workspace/output/Font/SDF/generated_templates/generated_tmp_font.json
workspace/output/Font/SDF/generated_templates/generated_tmp_font.png
```

### 9. 根据已生成 TMP 字体准备导入替换文件

读取脚本 8 生成的 TMP/SDF 模板和脚本 0 的字体索引，生成真正准备导入的 SDF 替换文件：

```text
workspace/output/Font/SDF/ToImport
```

如果某个 TMP FontAsset 没有有效图集，脚本会跳过该字体，避免出现“新字体表 + 旧/缺失图集”的错配。

### 10. 全部执行

按顺序执行：

```text
0 -> 1(仅 AI 字段判断开启时) -> 2 -> 4 -> 5 -> 6 -> 7 -> 8 -> 9
```

脚本 10 使用一个轻量主进程，按顺序等待各个独立子进程完成；步骤之间不会保留扫描、翻译产生的大量内存和线程状态。步骤 8-9 同样在全新进程中执行，结束后自动返回终端，不需要按回车。若检测到 Unity 在进入字体生成代码前发生原生启动崩溃，会安全清理失效的 `UnityLockfile` 并自动重试一次；重试仍失败时停止，不执行步骤 9。

## resource_menu.py 菜单说明

### 1. 一键导出

先把两处游戏资源汇总到统一暂存区，再导出到 `workspace/input`：

```text
game/assets/aa/Android -> workspace/input_sources/aa/Android
game/assets/bin/Data   -> workspace/input_sources/bin/Data
```

导出前会：

- 清理 `workspace/temp`。
- 如果 catalog 存在，先解析并检查远程 InternalId。
- 可确定完整 URL 时，自动把本地缺失资源下载到游戏的 `assets/aa/Android`。
- 无法确定远程 BASE_URL 时停止，并写出 `workspace/resource_state/addressables_remote_resources.json`。
- 强制清空并重建 `workspace/input_sources`。
- 在暂存区自动合并 `.splitN`，不询问、不修改游戏原目录。
- 检查 Managed DLL，必要时从 DummyDll 补齐。

导出后会：

- 写入 `workspace/records/file_id_map.json`。
- 写入 `workspace/resource_state/resource_source_map.json`，记录暂存资源对应的原始路径。
- 写入 `workspace/resource_state/split_bundle_merges.json`，记录自动合并的 split。
- 打印 MonoBehaviour 展开统计。

### 2. 一键导入

按选择收集 `workspace/output` 下的替换文件，复制到临时导入覆盖层，再调用导入流程。

导入临时目录会在导入前清空，避免上一次残留文件混入。

最终输出：

```text
workspace/FinalResult
```

导入结果会按映射整理到：

```text
workspace/FinalResult/Bundle/Android
workspace/FinalResult/Data
```

如果修改资源原本来自 `.splitN`，还会额外输出：

```text
workspace/FinalResult/SplitBundles/Merged
workspace/FinalResult/SplitBundles/Parts
```

其中 `Parts` 可按原路径同步替换原 split。若不替换，程序可能继续读取 split 缓存而忽略修改后的合并资源。

## 工具脚本.py 菜单说明

- `1. 查找字符串并复制 JSON`
  - 搜索导出 JSON 中的字符串，命中后可复制样本。
- `2. 查找 PathID 对应文件路径`
  - 用 PathID 定位导出文件。
- `3. 查找资源名对应文件路径`
  - 用资源名定位导出文件。
- `4. 一键复制导出图片到 workspace/AllPNG`
  - 把导出的 PNG 平铺整理，方便批量编辑。
- `5. 从修改后的图片目录恢复结构到 Image/ToImport`
  - 把改好的图片恢复成导入需要的目录结构。
- `6. Unity 资源兼容性/导出状态检查`
  - 检查导出状态、Texture、MonoBehaviour 等常见问题。
- `7. 清空成空 Mesh`
  - 保留 Mesh JSON 结构，清空顶点/索引等数据。
- `8. 快捷替换空 Mesh（旧方式）`
  - 用固定空 Mesh 覆盖指定目录。
- `9. AI 翻译单批补跑 / 修补 trans.json`
  - 针对某个 `ai_translation_request_batch_XXX.json` 单独补跑，并可用 response 修补 `trans.json`。
- `10. 清理 trans.json 中模板 TTF 不支持的字符`
  - 根据 `translation_chars_missing_from_ttf.txt` 查找并批量替换/删除不支持字符。
- `11. 解析 Addressables catalog 到 workspace/output/catalog`
  - 把原始 `catalog.json` 展开成可读的 `Output.json`。
- `12. 将 Output.json 回打成原始 catalog 格式`
  - 把展开后的 `Output.json` 重新编码回 Addressables catalog。
- `13. 按最终 Bundle 自动修正并回打 catalog（CRC 置 0）`
  - 匹配 `FinalResult/Bundle/Android` 下的 bundle，修正 size，并把命中条目的 CRC 置 0。
- `14. 按最终 Bundle 真实 CRC 修正 catalog（长度溢出时询问）`
  - 写真实 CRC；如果原位写入会导致片段变长，则询问置 0 或跳过。
- `15. 清理 records.json 中当前黑名单字段，并同步清理 trans.json`
  - 适合新增黑名单后使用，不必重新跑完整扫描。
- `16. 同步生成字体的 SDF 材质参数到待导入目录（可选实验）`
  - 主流程默认保留原游戏材质；执行本项后，才会在 `workspace/output/Font/SDF/ToImport` 中生成 Material 替换 JSON。
  - 保留原材质的 PathID、Shader、纹理、颜色和遮罩，只同步影响 SDF 边缘和笔画粗细的数值参数。
  - 重新执行主流程的 SDF 待导入准备步骤，即可清除实验材质并恢复默认行为。

## Unity Localization 规则

Unity Localization 里有两类资源最容易混：

- Shared Data
  - 保存 key/id 映射。
  - 典型字段：`m_Entries.Array[].m_Key`。
  - 不应该翻译。
- StringTable
  - 保存 key/id 对应的实际显示文本。
  - 典型字段：`m_TableData.Array[].m_Localized`。
  - 应该翻译。

如果把 Shared Data 的 key 翻译掉，运行时仍然用原 key 查表，就会出现类似 “No translation found” 或 fallback 长文本。

## 常见问题

### Texture format Alpha8 is not implemented for encoding; using RGBA32.

意思是工具无法直接把图片编码成 Unity 的 `Alpha8`，所以导出/中转 PNG 时用 `RGBA32`。单纯查看或中转通常不要紧；真正要注意的是导入后 Texture2D 的宽高、格式和图集引用是否合理。

### Texture size is 0x0. Texture cannot be exported.

表示这个 Texture2D 只是占位或没有实际图像数据。导入时不应强行替换这类无效图集，否则可能造成字体表和图集错配。

### SerializeReference not supported in JSON import yet.

当前导入工具暂不支持直接改 SerializeReference 内部字段。遇到时会提示并收集样本。一般 Unity Localization Settings、Locale 这类 Unity 自己的配置资产也不应该汉化。

## 目录结构

- `main.py`
  - 文本、TTF、TMP/SDF 字体主流程。
- `resource_menu.py`
  - 一键导出和一键导入。
- `工具脚本.py`
  - 排查、修补、catalog、AI 补批等辅助工具。
- `support/config.py`
  - 加载并规范化根目录的 `config.json`。
- `support/`
  - 放不需要直接从根目录运行的辅助模块和查找脚本。
- `pipeline/translation.py`
  - 扫描、字段判断、翻译、导出文本替换。
- `pipeline/font_ttf.py`
  - 生成 TTF 替换字体。
- `pipeline/tmp_pipeline.py`
  - TMP 字符合并、Unity TMP 字体生成、SDF 待导入文件准备。
- `pipeline/catalog_tools.py`
  - Addressables catalog 解析、回打和 CRC/size 修正。
- `AssetPipeline_CLI/`
  - Unity 资源导出/导入 CLI。
- `TMP_Font_Generator/`
  - Unity TMP/SDF 字体生成辅助工程。
- `templates/`
  - 字体模板和 SDF 模板资源。
- `workspace/`
  - 当前项目工作区。
