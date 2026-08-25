# Translate

Translate 是一套 Unity 游戏资源汉化工具链。本文只说明本仓库自身的安装、配置和操作；目标游戏仅作为 Translate 处理的输入与输出。主要流程是：导出目标游戏资源，扫描并翻译文本，生成待导入的文本、字体和图片替换文件，最后重新导入并输出到当前工作区的 `FinalResult`。

工作区目录名直接由 `workspace` 和 `project_name` 拼接：`project_name` 为空时是 `workspace`，填写 `<游戏名>` 时是 `workspace<游戏名>`。切换 `project_name` 会同时切换目标游戏目录和整套工作区；本文其余位置为简洁而写的 `workspace/...`，均表示当前配置实际选中的工作区。

## 首次安装环境

本项目包含 Python 流程脚本、C# Unity 资源处理程序，以及用于 ETC2/ASTC 的原生纹理编码器。新电脑需要准备 Python、.NET SDK 和 Windows C++ 构建工具。

### 1. Python 3.9 或更高版本

Python 负责快速配置、菜单、翻译、图片处理和整个流程的调度。先确认版本：

```powershell
python --version
```

推荐在仓库目录创建虚拟环境并安装 [requirements.txt](requirements.txt) 中的依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
```

也可以使用已有的虚拟环境。之后在快速配置中把该环境目录或其中的 `python.exe` 填入 `python_executable`；留空则统一启动器继续使用启动它的当前 Python。

### 2. .NET 8 SDK

一键导出、一键导入和资源验证实际会通过 `dotnet run` 编译并启动 `AssetPipeline_CLI/UnityResourceCLI`。该工程目标框架是 `net8.0`，因此需要安装 .NET 8 SDK，或者能够构建 `net8.0` 的更高版本 SDK；只有 .NET Runtime 不够。安装后检查：

```powershell
dotnet --version
```

第一次运行时，`dotnet` 还可能需要还原 NuGet 依赖。请通过可用的软件源或 Microsoft 提供的 .NET SDK 安装包完成安装。

### 3. Visual Studio C++ 与 CMake

图片导入会调用仓库内的原生 `TextureEncoder`，把替换图重新编码为游戏原有的 ETC2/ASTC GPU 压缩格式。Windows 上请通过 Visual Studio Installer 安装：

- 使用 C++ 的桌面开发
- MSVC x64/x86 生成工具
- Windows SDK
- 适用于 Windows 的 C++ CMake 工具

无需手动配置 PATH。第一次执行 `dotnet build`、一键导入或资源验证时，项目会通过 `vswhere` 自动找到 Visual Studio 自带的 CMake，构建 `textureencoder.dll` 和不启用 PVRTC 的 `PVRTexLib.dll` 兼容占位库，并把它们与 `cuttlefish.dll` 一起复制到 CLI 输出目录。ETC2/ASTC 编码在隔离子进程中执行；原生 DLL 缺失、编码异常或子进程崩溃时，主导入流程会回退为兼容的单 mip RGBA32。仅当 `enable_sample_collection=true` 时才会把替换图片、manifest 和错误信息写入 `样本/TextureEncode_*`，关闭时不保存失败样本。RGBA32 回退结果通常比 GPU 压缩格式大，应结合日志检查后再发布。

### 4. Unity Editor（按资源类型需要）

只有脚本 0 检测到 TMP/SDF FontAsset、并在步骤 8 生成 TMP/SDF 字体时才需要完整 Unity Editor。纯文本、图片、TTF 或仅 NGUI 位图字体流程不依赖 Unity 字体生成；`unity_exe=auto` 时工具会按辅助工程版本自动查找已安装的 Unity。

## 首次使用快速配置

仓库提供的公开配置模板是：

```text
config（删除括弧后缀后运行快速配置）.json
```

首次使用时先手动重命名为 `config.json`，再运行快速配置：

```powershell
Rename-Item '.\config（删除括弧后缀后运行快速配置）.json' 'config.json'
python .\快速配置.py
```

快速配置会设置目标游戏路径基准目录和可选的虚拟 Python 环境，并询问 Unity 与可选翻译方式。资源、Managed、Addressables catalog 和 IL2CPP stringliteral 使用固定的游戏相对路径，不会扫描或选择资源目录。仓库自带的 `templates/fzkt.ttf` 会直接使用，无需配置。写入前会显示检查结果；已有 `config.json` 会按时间生成备份。

快速配置不会检查这些固定资源相对路径当前是否存在；具体资源流程运行时再按需检查。

也可运行统一启动器并选择 `0. 首次使用快速配置`：

```powershell
python .\run_with_config_python.py
```

快速配置入口固定使用启动器当前的 Python，因此旧 `config.json` 中失效的 `python_executable` 不会阻止配置。`python_executable` 是可选的虚拟 Python 环境配置：快速配置接受环境目录或具体 Python 可执行文件，留空时 `run_with_config_python.py` 使用启动器自身的当前 Python；填写时会检查 Python 版本及 `requirements.txt` 对应依赖。

启动器也支持为一次会话指定独立配置。传入的文件就是一份普通的完整 Translate JSON 配置，内容结构与 `config.json` 相同，可以保存在 Translate 目录之外。推荐从完整配置模板复制一份，仅按处理目标修改 `project_root_dir` 和 `project_name`，其余翻译、字体和工作目录设置继续保留模板值。例如完整副本中的两个项目定位字段可以是：

```json
{
    "project_root_dir": "<目标游戏共同父目录>",
    "project_name": "<目标游戏目录名>"
}
```

上面只展示需要按目标修改的字段，不表示配置文件只能包含这两个字段。配置文件自身所在的目录不参与路径计算；配置中的普通相对路径仍按 Translate 根目录解析。因此从其他程序调用时建议传配置文件的绝对路径。配置参数必须放在脚本名之前；启动器会把它传给资源菜单、主菜单、工具脚本以及它们创建的 Python 子进程，因此多个 Translate 终端可以同时使用不同配置：

```powershell
python .\run_with_config_python.py --config '<配置文件路径>'
python .\run_with_config_python.py --config '<配置文件路径>' main.py
```

也可以直接调用入口脚本：

```powershell
python .\resource_menu.py --config '<配置文件路径>'
python .\main.py --config '<配置文件路径>'
python .\工具脚本.py --config '<配置文件路径>'
```

交互启动器会在子菜单结束后重新显示上一级菜单；只有在 `Translate Project Launcher` 中输入 `q` 才会结束启动器。资源菜单中的导出/导入二级选择输入 `q` 只取消当前选择并返回资源菜单。

只要各配置最终解析出的 workspace 不同，诊断样本和 UnityResourceCLI 的 .NET 构建目录也会随之隔离，可以同时执行；两份配置若解析到同一个 workspace，则不应并发运行。多个会话共享同一个 `unity_font_project` 时，TMP 字体生成与 catalog CRC 计算会自动跨进程排队，并定时打印等待时间；取得执行权后会继续运行，不会覆盖另一个会话的 job 或输出。

可用以下命令检查当前配置：

```powershell
python .\快速配置.py --check
python .\快速配置.py --config '<配置文件路径>' --check
```

## 先配置什么

默认配置文件是 `config.json`。切换处理目标、运行电脑或 Unity 环境时，优先检查下面这些字段。

### 游戏路径

- `project_root_dir`
  - 目标游戏路径的基准目录；实际游戏目录由它和可选的 `project_name` 组成。`project_name` 为空时，该目录本身应是 `game-name` 的上一级目录。快速配置只检查基准目录是否存在。
- `project_name`
  - 可留空；快速配置默认留空。填写后既会与 `project_root_dir` 拼出游戏目录，也会把工作区切换为同级命名规则 `workspace<project_name>`（直接拼接，没有中间斜杠）。
  - 例如 `project_name=<游戏目录名>` 时，游戏目录是 `project_root_dir/<游戏目录名>`，工作区是 Translate 目录下的 `workspace<游戏目录名>`；改回空值时直接使用 `project_root_dir` 和 `workspace`。
- `resource_source_subpath`
  - 游戏资源目录相对路径，一般是 `game-name/game/assets/bin/Data`。
- `resource_managed_subpath`
  - Managed DLL 目录相对路径，一般是 `game-name/game/assets/bin/Data/Managed`。
- `catalog_source_subpath`
  - Addressables `catalog.json` 或 `catalog.bin` 相对路径，一般在 `game-name/game/assets/aa/`。
  - 即使模板填写 `catalog.json`，运行时也会读取同目录 `settings.json` 并自动选择实际存在的 `catalog.bin`/`catalog.json`。
- `resource_staging_root`
  - 一键导出使用的统一原始资源暂存目录，默认 `workspace/input_sources`。
  - 配置文件继续保留这个通用写法；加载配置时会自动映射到当前配置的实际工作区，例如 `workspace<游戏目录名>/input_sources`。
  - 源文件指纹没有变化时会直接复用；源文件变化或用户明确清空 workspace 时才会重建。不要在其中保存手工文件。
- `addressables_download_workers` / `addressables_download_timeout`
  - 远程 Addressables 下载线程数和单次请求超时，默认 `5` 和 `60` 秒。
- `stringliteral_json_subpath`  （现在暂未使用）
  - 如果有 IL2CPP 字符串导出，可配置到 `game-name/bak/64/stringliteral.json`。

`bin/Data` 资源路径由：

```text
project_root_dir + （可选的 project_name） + resource_source_subpath
```

拼出来。导出前如果 Managed 目录缺 DLL，脚本会尝试从：

```text
project_root_dir/[project_name/]game-name/bak/64/DummyDll
```

补到 Managed 目录。

### Unity 和字体

- `unity_exe`
  - 本机 Unity Editor 的 `Unity.exe` 路径。
  - 也可以写成 `auto`，脚本会优先按 `unity_font_project/ProjectSettings/ProjectVersion.txt` 记录的版本从 Unity Hub 安装目录查找 Unity。
  - 找不到同版本时会退到已安装的其他 Unity，并打印提示。
  - 生成 TMP/SDF 字体时需要完整 Unity Editor，单独复制 `Unity.exe` 不够。
- `unity_font_project`
  - 内置 Unity 辅助工程目录，默认 `TMP_Font_Generator`。
- `unity_font_launcher`
  - Unity 辅助工程内的生成脚本，默认 `Tools/generate_tmp_font.py`。
- `tmp_max_atlas_size`
  - TMP/SDF 字体图集允许上限，默认 `8192`；实际尺寸仍按字符数量选择 `2048/4096/8192` 档位。
  - Unity 会检查当前图形环境；若不支持 `8192x8192`、生成失败或得到 `0x0` 图集，将自动回退到 `4096x4096`。
- `ngui_max_atlas_size`
  - NGUI 静态字体扩展图集的尺寸上限，默认 `4096`。原图扩大一倍后仍放不下全部新增字形时会停止，不生成不完整字体。
- `ngui_glyph_padding`
  - NGUI 新字形之间的透明间距，默认 `2` 像素，用于避免双线性采样串色。
- `include_old_sdf_template_chars`
  - 脚本 7 是否把 `templates/老工具的SDF模板.json` 里的常用汉字一并合入 `tmp_chars.txt`，默认 `false`。
  - 关闭后只合并模板 TTF 非中文字符、原游戏 TMP 字符和译文字符，可明显减少新 TMP 字符数。
- `protect_i2_tmp_fonts_from_replacement`
  - 脚本 9 是否跳过 I2 运行时文本正在使用的 TMP FontAsset/Atlas，默认 `true`。
  - 用于排查或规避 I2 文本因字体替换引起的运行时刷新、卡顿或材质状态问题。
- `enable_text_effect_material_cleanup`
  - 为兼容旧配置保留；当前主流程不再读取这个开关。
  - 脚本 5 只处理同 GameObject 上额外挂载的 Shadow/Outline 组件；TMP/SDF Material 的阴影、描边和发光参数由脚本 9 在字体导入覆盖层中统一处理。
- `ttf_template_path`
  - 替换字体模板 TTF。模板不支持的译文字符会在脚本 7 中提示并停止。

### 翻译配置

- `enable_ai_translation`
  - `true` 时优先使用 AI 整表翻译。
  - `codex_cli` 模式下，Codex CLI 进程一旦报错、超时或额度不足，会立即触发本次操作的熔断：当前批立刻切换到已配置的 OpenAI-compatible HTTP AI，后续批次全部跳过 Codex。Codex 成功响应但只漏少量条目时仍可重试缺失条目；HTTP AI 最多重试 3 次，最后才回落到 `translate_provider` 指定的普通翻译（通常为百度）。
- `ai_translation_transport`
  - `http`：使用下方的 OpenAI-compatible HTTP 接口配置。
  - `codex_cli`：通过本机已登录的 `codex exec` 调用 Codex，使用 `ai_translation_codex_model`；无需 API key。若同时填写 HTTP 接口配置，该接口会作为 Codex 的第二级 AI 回退。
- `ai_translation_codex_model`
  - Codex CLI 模型，例如 `gpt-5.3-codex-spark`。
- `ai_translation_codex_reasoning_effort`
  - 支持 `low`、`medium`、`high`、`xhigh`。它控制模型的思考深度，不是单纯的运行速度开关；越高通常越慢、额度消耗也越多。纯文本翻译默认推荐 `low`。
- `ai_translation_base_url` / `ai_translation_api_key` / `ai_translation_model`
  - AI 翻译接口配置。
- `ai_translation_strategy`
  - AI 翻译分批策略脚本名，例如 `deepseek_translation_strategy.py`。
  - 没有对应策略时使用默认策略。
- `ai_translation_output_safety_divisor`
  - AI 分批时将模型最大输出字符预算除以该值作为单批目标预算，默认 `12`。批次仍按每条文本的预计输出长度动态计算，不固定条目数；增大该值会产生更多、更短的批次。
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
- `ai_field_review_transport` / `ai_field_review_codex_model`
  - 可设为 `codex_cli` 并指定 Codex 模型，让字段判断复用本机 Codex 登录。若同时填写字段判断 HTTP 接口配置，Codex CLI 一旦报错便立即熔断，当前批和本次操作的后续批次直接使用 HTTP AI；HTTP AI 最多重试 3 次。
- `ai_field_review_codex_reasoning_effort`
  - 字段判断默认使用 `medium`，在判断质量和额度消耗之间取平衡。
- `ai_field_review_base_url` / `ai_field_review_api_key` / `ai_field_review_model`
  - AI 字段判断接口配置；在 `codex_cli` 模式下也会保留并用作第二级 AI 回退。
  - Codex 和 HTTP AI 均未配置或重试后仍失败时，脚本会提示把 `workspace/records/string_field_review.txt` 手动交给 AI 判断。
- `string_field_blacklist`
  - 字段黑名单。典型例子：`m_Script`、`m_Name`、`m_Entries.Array*.m_Key`。
  - Unity Localization 的 Shared Data key 不应该翻译，所以 `m_Entries.Array*.m_Key` 必须保留在黑名单里。
  - 黑名单只适合所有上下文都不能翻译的字段；可能同时承载界面文本和运行时标识的字段不要直接加入黑名单。
- `text_keys`
  - 关闭 AI 字段判断时使用的白名单字段。

### 工作目录

通常不用改。配置文件中的相对路径继续统一写成 `workspace/...`；程序加载时会根据 `project_name` 将首段为 `workspace` 的相对路径自动改为实际目录名。绝对路径以及不以 `workspace` 开头的自定义路径不会自动改写，需要自行保证不同配置之间不共用目录。使用默认写法时，切换处理目标只需修改 `project_name`，不需要逐项修改下面这些路径。

目录规则：

```text
project_name = ""         -> workspace
project_name = "<游戏名>" -> workspace<游戏名>
```

配置值：

- `resource_input_root`: `workspace/input`
- `record_dir`: `workspace/records`
- `result_dir`: `workspace/output`
- `log_dir`: `workspace/logs`
- `image_import_dir`: `workspace/output/Image/ToImport`

重要输出（以下 `workspace` 表示当前实际工作区）：

- 文本待导入：`workspace/output/Text`
- TTF 待导入：`workspace/output/Font/TTF/ToImport`
- SDF 模板：`workspace/output/Font/SDF/generated_templates`
- SDF 待导入：`workspace/output/Font/SDF/ToImport`
- NGUI 生成产物：`workspace/output/Font/NGUI/generated`
- NGUI 待导入：`workspace/output/Font/NGUI/ToImport`
- 图片待导入：`workspace/output/Image/ToImport`
- 最终结果：`workspace/FinalResult`

## 常规使用顺序

以下命令均在 Translate 仓库根目录执行。下面优先使用统一启动器调用各脚本，这样 `config.json` 中配置的 `python_executable` 才会生效。若直接执行 `python .\main.py` 等命令，则使用当前终端的 Python。

### 1. 一键导出资源

```powershell
python .\run_with_config_python.py resource_menu.py
```

选择：

```text
1. 一键导出
```

它会：

- 检测 `.splitN` 分卷并在暂存区自动合并，不修改游戏原目录。
- 导出资源到 `workspace/input`。
- 生成 `workspace/records/file_id_map.json`。
- `workspace` 非空时默认保留并增量导出；只有输入 `c` 才会清空并从零开始。

### 2. 扫描、判断字段、翻译、生成待替换文件

```powershell
python .\run_with_config_python.py main.py
```

推荐顺序：

```text
0 -> 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8 -> 9
```

如果 `enable_ai_field_review=false`，跳过脚本 1：

```text
0 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8 -> 9
```

如果想一口气执行：

```text
a. 全部执行
```

注意：`a` 在 AI 字段判断开启时会自动执行脚本 1；也支持输入 `1-2`、`4-7` 或 `0,2,4-9` 连续执行指定步骤。

### 3. 手动处理图片或工具修补

```powershell
python .\run_with_config_python.py 工具脚本.py
```

常用：

- 验证修改后重打资源与原始资源：主菜单 0
- 图片导出整理：主菜单 1
- 按 Sprite / NGUI UIAtlas 数据拆分 Texture2D 图集：主菜单 2
- AI 翻译批次补跑：主菜单 3
- 清理模板 TTF 不支持字符：主菜单 4
- 按图片定位对象并选择层级屏蔽：主菜单 5
- 按 Mesh 定位对象并选择层级屏蔽：主菜单 6
- 按 Object 名称获取链路并选择层级屏蔽：主菜单 7
- 动态列表索引与选择性屏蔽（测试阶段）：主菜单 8
- 统一撤销 Object/商品屏蔽：主菜单 9
- 查找字符串、PathID 或资源名：`S. 搜索`
- 兼容性检查、maybe title 清理及 records 字段恢复：`T. 测试`
- 按产物清单清理主脚本文件：`C. 清理主脚本产物`

### 4. 一键导入

```powershell
python .\run_with_config_python.py resource_menu.py
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
- `2`: TMP/SDF/NGUI 字体替换，同时读取 `workspace/output/Font/SDF/ToImport` 和 `workspace/output/Font/NGUI/ToImport`
- `3`: TTF 字体替换，读取 `workspace/output/Font/TTF/ToImport`
- `4`: 图片替换；导入前自动将 `workspace/AllPNG/修改后的图片目录` 恢复到 `workspace/output/Image/ToImport`，并回拼修改过的 Sprite 子图
- `5`: Object 屏蔽，读取 `workspace/output/Object/ToImport`
- `a`: 全部

UnityResourceCLI 不会用重打后的 Bundle/Data 直接覆盖原始资源，结果输出到：

```text
workspace/FinalResult
```

Addressables Bundle 和 `bin/Data` 资源会分别放到：

```text
workspace/FinalResult/Bundle/Android
workspace/FinalResult/Data
```

例外是 Addressables catalog：如果导出阶段已把远程 InternalId 本地化，导入收尾可能同步更新游戏源目录中的 catalog/hash，并用橙色 `[源文件已修改]` 明确提示。

如果检测到 Addressables catalog，导入收尾阶段会复用导出前生成的
`workspace/output/catalog/Output.json`，按最终 Bundle 修正后回打生成最终 catalog 到：
若该文件缺失，才会兜底重新解析原始 catalog。

```text
workspace/FinalResult/Bundle/catalog.json
```

当前默认策略是匹配最终 Bundle 后把对应 catalog 条目的 CRC 置为 `0`。
回打成功后会输出到 `workspace/FinalResult/Bundle`。只要 catalog 中的远程
InternalId 被改为本地 RuntimePath，就会同步覆盖源 `game/assets/aa` 中的 catalog；
无论资源是本次下载，还是此前已经存在于本地，规则都相同。没有远程路径被本地化时
保留源文件，并用蓝色日志提示手动替换最终产物。所有直接写入游戏源目录的操作均以
橙色 `[源文件已修改]` 日志提示。

## main.py 菜单说明

### 0. 扫描导出的 JSON，生成文本、字体、材质、引用索引

读取 `workspace/input` 下的导出 JSON。

扫描前只检查脚本 0 会重新生成的扫描产物；这些文件不存在时直接开始，存在时才提示是否清理。
确认清理时只删除扫描索引、扫描状态/缓存、TMP manifest 索引，以及 AI 字段判断启用时的字段统计；
不会触碰资源导出阶段生成的 `file_id_map.json`、翻译记录或 `workspace/output`。

输出到 `workspace/records`：

- `records.json`: 待翻译文本记录。
- `ids.json`: 实际导出替换文件时用到的目标文件索引。
- `font_map.json`: 字体使用关系。
- `material_map.json`: 材质引用关系。
- `ref_map.json`: MonoBehaviour 引用关系。
- `tmp_manifest_index.json`: TMP/TTF 替换定位索引。
- `string_field_stats.json` / `string_field_stats.tsv`: AI 字段判断启用时生成的完整字段统计，包含资源类型、MonoBehaviour 脚本引用、同级字段类型结构、来源文件和上下文选择标识。
- `bitmap_font_detection.json`: 扫描阶段生成的字体类型检测报告，同时记录 TMP/SDF FontAsset、NGUI 位图 UIFont/BMFont，以及 `UILabel.mTrueTypeFont` 使用的 NGUI 动态 TTF。动态 TTF 会提示执行步骤 6 并确认替换字体包含全部译文字符；它不触发 NGUI 位图生成。已确认的 NGUI 位图 `UIFont` 允许继续并交给 NGUI 专用流程；标准 `.fnt` 或嵌入 TextAsset 的非 NGUI BMFont 仍会使脚本 0 返回失败。脚本 8/9 根据报告只运行实际存在的 TMP/SDF、NGUI 位图生成与替换流程。
- `string_field_review.txt`: 发给 AI 判断字段用的精简文本。同一字段只有在脚本类型或同级字段结构不同时才自动拆成多个 `@@context_...` 候选；`m_Text`、`m_text`、`mText`、`_text` 等明确文本字段保持全局聚合。

如果 `enable_ai_field_review=false`，脚本 0 直接按 `text_keys` 白名单生成 `records.json`。

如果 `enable_ai_field_review=true`，脚本 0 会先按 `string_field_blacklist` 排除明显不该翻译的字段，再记录字符串字段，等待脚本 1 过滤。

脚本 0 会结合导出 manifest、JSON 大小/修改时间、FileID 映射和扫描规则生成输入指纹。输入完全未变化时直接恢复未过滤记录及已有索引，不再解析数千个 JSON；只有部分文件变化时会校验文件级缓存并增量重扫。BMFont/NGUI 检测仅读取可能承载字体描述的 MonoBehaviour、TextAsset、Font，并在输入未变化时复用检测报告。

脚本 0 还会在 AI 判断前执行高置信度运行时字段排除：识别 Unity Input System 动作表/控制方案、动作 GUID 与控制路径、Unity SerializeReference 类型元数据、Addressables InternalId/ProviderId 等技术字符串。命中项不会进入 `records.json`、字段统计或 AI 候选；扫描日志会按原因汇总排除数量。翻译文件导出时会再次执行同一策略，防止旧记录或旧译文改写运行时字段。

脚本 4 每次实际翻译并导出 Text 资源前会清空 `workspace/output/Text`，避免上一次运行留下的旧覆盖文件再次进入一键导入。

导出的 TextAsset 如果在 `m_Script` 中内嵌带 `Key` 和英文源文本列（如 `EN`）的 CSV/TSV 本地化表，
脚本 0 会安全解析单元格，并以 `m_Script.csv[].EN` 这类统一字段加入扫描记录；不会把整段 `m_Script`
当作一条文本，也不会从该静态表猜测字体或材质引用。

### 1. AI 判断字段后过滤 records.json

只在 `enable_ai_field_review=true` 时执行。

它会读取脚本 0 生成的 `string_field_review.txt`，让 AI 只返回可能需要翻译的 field 候选标识，然后按字段及上下文过滤 `records.json`。普通字段仍只判断一次；同名字段跨越不同脚本类型或同级字段结构时，脚本 0 会自动按完整结构签名分组，不限制上下文组数。AI 必须原样返回带 `@@context_...` 后缀的候选，脚本 1 才会只保留该上下文中的具体记录。

脚本 4 回写时会再次依据过滤后的 `records.json` 建立 `(文件、完整 field 路径、原文)` 精确白名单。即使同一原文同时出现在可翻译字段和运行时字段中，也只修改 AI 已选择的具体位置；运行时字段安全规则仍会进行第二次拦截。

一键全部执行时，步骤 1 采用严格非交互模式：Codex CLI 报错后立即熔断并切换 HTTP AI，HTTP AI 完成重试后仍失败才会返回失败；Windows 下同时终止对应超时进程树，外层不会继续执行步骤 2–9，也不会停在人工粘贴提示。单独执行步骤 1 时仍允许全部自动通道失败后人工粘贴字段列表。每次请求的等待上限由 `ai_field_review_timeout` 控制。

`a` 全部执行以及 `1-3`、`0,2,4-9` 等连续/组合执行统一采用 fail-fast：任一步骤抛出异常、返回非零状态或收到人工中断，当前执行链立即结束，尚未启动的后续步骤不会执行。只有明确返回成功的步骤才会启动下一步。

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

读取过滤后的 `records.json`，去重后生成 `trans.json`，先在本地分流疑似资源键，
再把其余文本交给翻译接口。

输出：

- `trans.json`: 原文到译文的映射。
- `trans_maybe_title.json`: 本地识别出的资源键式文本，例如
  `PPS_PP_desc_10.1`。这类键会从 `trans.json` 中移除，不发送给 AI
  或百度翻译。
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

### 4. 导出并实际翻译 Text 资源

读取 `records.json` 和 `trans.json`。

只处理 `trans.json` 命中的待汉化源 JSON，然后把翻译写入 `workspace/input` 的副本结构，输出到：

```text
workspace/output/Text
```

Unity Localization 要特别注意：

- StringTable 的 `m_TableData.Array[].m_Localized` 应该翻译。
- Shared Data 的 `m_Entries.Array[].m_Key` 不应该翻译。

TextAsset 内嵌 CSV/TSV 会根据 `records.json` 中保留的结构化定位信息，只回写已选中的源语言单元格；
Key、注释和其它语言列保持不变。字体和材质无法从这类静态表定位时会跳过，后续 TMP 字体仍通过译文字符集合统一生成。

### 5. 清理译文/I2 文本额外挂载的阴影/描边组件

脚本 5 根据 `records.json`、`material_map.json` 和译文使用关系定位文本所在的 GameObject，只处理额外挂载的 Shadow/Outline MonoBehaviour：

- 普通译文会处理同 GameObject 上的 Shadow/Outline 组件。
- 有 I2 语言表时，会精确定位相关 I2 GameObject 并处理同物体组件。
- 不重写 Text/TMP/SDF 字体组件，也不在步骤 5 写出普通 Material 覆盖层。

TMP/SDF Material 的阴影、描边和发光参数已合并到步骤 9 的 SDF 导入覆盖层中统一处理；I2 TMP 字体和实际使用材质也由步骤 9 处理。

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
- 可选的老工具 SDF 模板字符

生成：

```text
workspace/records/tmp_chars.txt
```

如果 `trans.json` 里有模板 TTF 不支持的字符，会输出：

- `translation_chars_missing_from_ttf.txt`
- `translation_chars_missing_from_ttf.tsv`

并停止，避免生成缺字字体。

`include_old_sdf_template_chars=false` 时，不会再把老工具 SDF 模板里的约 3000 个常用汉字强制合进新字体。

### 8. 生成字体（支持 TMP、NGUI）

脚本 8 会读取脚本 0 写入的字体类型检测结论并按需执行：仅检测到 TMP/SDF 时只调用 Unity；仅检测到 NGUI 位图字体时跳过 Unity、只运行 NGUI 静态字形生成；两者同时存在时才依次运行两套生成流程。NGUI 动态 TTF 由步骤 6 直接替换 TTF，不进入步骤 8/9 的位图生成流程。未检测到的字体类型不会生成空产物。

读取脚本 7 生成的 `tmp_chars.txt`，调用 Unity 辅助工程生成 TMP/SDF 字体资源；同时读取
`trans.json`、脚本 0 的 NGUI 检测报告和 `ttf_template_path`，为已确认的 NGUI 位图字体生成静态字形。

NGUI 会把原图集区域设为禁止写入区，将“各 UIFont 原字符全集 + 译文字符”全部使用模板 TTF 重新生成并逐字打包到扩大图集后的 L 形空间。普通 UI Sprite 的原像素和坐标不改变，但 UIFont 不再引用原字体 Sprite 的旧字形像素与旧排版指标。
共用图集且字号相同的多个 UIFont 会复用同一套重生成字形像素和坐标，并写入相同的完整字符集，以统一中英文、数字和标点的风格与基线。所有保留下来的字符均由模板 TTF 重生成，不会混用旧字形。
缺字规则与 SDF 一致：模板 TTF 缺少译文字符时停止；只缺少原 NGUI 字体附带字符时，将其从重生成字符集删除并继续，同时输出 `ngui_source_chars_removed_unsupported_by_ttf.txt/tsv`，TSV 会记录 Unicode、字符名称和来源 UIFont。

输出：

```text
workspace/output/Font/SDF/generated_templates/generated_tmp_font.asset
workspace/output/Font/SDF/generated_templates/generated_tmp_font.asset.meta
workspace/output/Font/SDF/generated_templates/generated_tmp_font.json
workspace/output/Font/SDF/generated_templates/generated_tmp_font.png
workspace/output/Font/NGUI/generated/ngui_font_generation.json
```

### 9. 根据已生成字体准备导入替换（支持 TMP、NGUI）

脚本 9 使用与脚本 8 相同的脚本 0 检测结论，只准备实际存在的字体类型；TMP/SDF 与 NGUI 同时存在时分别生成各自的待导入替换文件。

读取脚本 8 生成的 TMP/SDF 与 NGUI 字体产物，校验源资源和生成文件哈希后准备待导入文件：

```text
workspace/output/Font/SDF/ToImport
workspace/output/Font/NGUI/ToImport
```

如果某个 TMP FontAsset 没有有效图集，脚本会跳过该字体，避免出现“新字体表 + 旧/缺失图集”的错配。

`protect_i2_tmp_fonts_from_replacement=true` 时，脚本 9 会额外跳过 I2 运行时文本正在使用的 TMP FontAsset/Atlas。

### a. 全部执行

按顺序执行：

```text
0 -> 1(仅 AI 字段判断开启时) -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8 -> 9
```

`a` 直接按顺序调用脚本 0 至 9 的同一执行入口，每一步使用独立子进程；步骤之间不会保留扫描、翻译产生的大量内存和线程状态。若检测到 Unity 在进入字体生成代码前发生原生启动崩溃，会安全清理失效的 `UnityLockfile` 并自动重试一次；失败时停止，不执行后续步骤。

## resource_menu.py 菜单说明

### 1. 一键导出

先把两处游戏资源汇总到统一暂存区，再导出到 `workspace/input`：

```text
game/assets/aa/Android -> workspace/input_sources/aa/Android
game/assets/bin/Data   -> workspace/input_sources/bin/Data
```

导出前会：

- `workspace` 非空时默认保留；只有输入 `c` 才会重建整个工作区。保留时，本次 profile 会替换同类型旧文件并与已有 manifest 合并。
- 把当前 `game/assets/aa` 完整备份到 `game-name/bak/aa_before_resource_export`；下次导出会用新的操作前快照覆盖该备份。
- 如果 catalog 存在，先解析并检查远程 InternalId。
- 可确定完整 URL 时，自动把本地缺失资源下载到游戏的 `assets/aa/Android`。
- 全部下载成功后用绿色日志汇总；失败时只用红色日志列出失败项。每项结果记录在 `workspace/resource_state/addressables_remote_resources.json`。
- 下载文件完整落地后，把 catalog 中对应的远程 URL 改为 `{UnityEngine.AddressableAssets.Addressables.RuntimePath}/Android/...` 本地加载路径。
- 远程资源下载、catalog 本地化、Managed DLL 自动补齐等直接修改游戏源目录的操作，都会输出橙色 `[源文件已修改]` 提示。
- 远程条目不是完整 HTTP/HTTPS 下载链接时停止，并写出 `workspace/resource_state/addressables_remote_resources.json`。
- 根据源文件路径、大小和修改时间计算指纹；源资源未变化时复用 `workspace/input_sources`，变化时才清空重建。
- 在暂存区自动合并 `.splitN`，不询问、不修改游戏原目录。
- 检查 Managed DLL，必要时从 DummyDll 补齐。
- 使用单次扫描直接导出，不再为了计算总数提前完整解析一遍资源包；每处理 10,000 条输出一次当前数量，结束时输出最终计数。
- 相同导出档位重复执行时，会先校验源资源、导出器及依赖库、Managed DLL、格式参数和已导出文件；全部未变化便直接复用该资源的已有结果。导出文件缺失、大小或修改时间变化时会自动重新导出，不会静默沿用不完整结果。

导出后会：

- 写入 `workspace/records/file_id_map.json`。
- 写入 `workspace/resource_state/resource_source_map.json`，记录暂存资源对应的原始路径。
- 写入 `workspace/resource_state/split_bundle_merges.json`，记录自动合并的 split。
- 在 manifest 的 `ExportedProfiles` 中记录已累计档位，并输出当前已完成和缺失的档位。

支持按阶段执行 `1 -> 2 -> 3`：完成基础资源处理后，可以再次进入一键导出、保留 workspace 并只选对象索引，之后再用相同方式导出 Mesh 索引。后一次导出只替换本档资源类型，其他档位的文件和 manifest 条目都会保留。若游戏源文件在阶段之间发生变化，对应源资源的旧 manifest 不会继续合并，需要重新导出缺失档位。
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

如果修改资源原本来自 `.splitN`，导入收尾会在 `FinalResult/Data` 或
`FinalResult/Bundle/Android` 的对应目录中直接恢复原 `.splitN` 文件，并删除仅供
导入处理的合并版资源。`FinalResult` 因此只保留可以按目录覆盖回游戏的最终文件，
不再生成额外的 `SplitBundles` 目录。

导入完成后会再次用绿色日志提醒替换顺序：

1. 先把已下载资源和本地化 catalog 所在的 `game/assets/aa` 同步到目标游戏的 `assets/aa`。
2. 再用 `workspace/FinalResult` 中的修改资源覆盖目标游戏对应文件。

## 工具脚本.py 菜单说明

- `0. 验证修改后重打资源与原始资源`
  - 对照 `workspace/FinalResult`、统一资源暂存区和导出路径映射，验证重打资源能够重新导出，并检查资源类型、对象数量及 split 分卷边界等一致性。
- `1. 一键复制导出图片到 workspace/AllPNG`
  - 把导出的 PNG 平铺整理，方便批量编辑；同时创建并保留 `修改后的图片目录` 和 `屏蔽object` 两个用户工作目录。
- `2. 按 Sprite / NGUI UIAtlas 数据拆分 Texture2D 图集`
  - Unity 图片按 Sprite 或 SpriteAtlas 的运行时 textureRect 拆分，并记录 Sprite PathID；NGUI 图片按 UIAtlas.mSprites 拆分，并记录 Atlas PathID + SpriteName。旧导出若缺少图集对象索引，请重新一键导出并选择对象索引后再拆分。
  - 按图片定位对象时，普通 Texture2D 会同时检查 Unity RawImage/NGUI UITexture 的直接引用；图集子图则按上述 Sprite PathID 或 NGUI Atlas PathID + SpriteName 精确反查。
- 图片目录结构恢复已并入一键导入的“图片替换”，工具菜单中不再需要单独执行。
  - 修改后的图片统一放入 `workspace/AllPNG/修改后的图片目录`。选择导入图片时，脚本会把改好的普通图片恢复成导入需要的目录结构；同时读取 `workspace/AllPNG/Sprite/_allsprite_map.json`，将其中已修改的 Unity Sprite 或 NGUI 子图按各自坐标规则贴回原 Texture2D，再输出完整图集到 `Image/ToImport`。
  - 再次执行菜单 1 刷新 `AllPNG` 时会保留该修改目录及其中的文件，不会误删已经修改的图片。
  - 只需放入实际修改过的子图，未放入的子图和图集空白区域会保持原样。子图像素尺寸必须与拆分结果一致；尺寸变化时会跳过并提示，避免拉伸或错位破坏图集。
- `3. AI 翻译批次补跑 / 修补 trans.json`
  - 可选择一个或多个 `ai_translation_request_batch_XXX.json` 补跑，并用各自 response 修补 `trans.json`；编号支持 `10-12`、`10,12,15`、`10-12,15` 等格式。
  - 多选时按编号顺序逐批处理；单批失败不会中断后续批次，结束后统一汇总成功和失败列表。
- `4. 清理 trans.json 中模板 TTF 不支持的字符`
  - 根据 `translation_chars_missing_from_ttf.txt` 查找并批量替换/删除不支持字符。
- `5. 按图片定位对象并选择层级屏蔽`
  - 根据普通图片或拆分后的 Sprite 子图定位 GameObject，并提供交互式层级预览与屏蔽。进入后可选择手动输入图片名称/路径，或从 `workspace/AllPNG/屏蔽object` 自动批量读取。该目录由主菜单 1 导出 AllPNG 时创建，之后刷新 AllPNG 也不会清空其中的图片。
- `6. 按 Mesh 定位对象并选择层级屏蔽`
  - 根据 Mesh 引用定位 GameObject，并选择需要屏蔽的对象层级。
- `7. 按 Object 名称获取链路并选择层级屏蔽`
  - 按 GameObject 名称搜索第 2 类对象导出数据，精确匹配优先，无精确结果时自动包含匹配；也可使用 `*关键词*` 强制模糊搜索。
  - 显示每个命中对象的完整父链，可直接选择层级屏蔽，或通过 `p`/`p3` 进入交互预览后右键屏蔽。
- `8. 动态列表索引与选择性屏蔽（测试阶段）`
  - 静态分析商店、任务、成就、活动等 MonoBehaviour 数据数组，综合数组名称、控制器语义、条目字段、类型一致性、图片和 Prefab 进行评分，不再把 Unity Sprite 作为硬条件。
  - 图片统一支持 Unity Sprite、NGUI Atlas+SpriteName、Texture2D，以及条目 Prefab 内的图片组件；没有图片但存在商品 ID/价格或任务 ID/进度/目标等结构证据时仍可识别。
  - 优先反向查找引用该列表配置的场景组件，并解析列表根对象、Content、公共条目模板及 GridLayout 参数；找到完整链路时，按真实单元格、间距、对齐、起始轴及顺序还原列表布局。
  - 真实布局解析失败时才使用原来的可滚动顺序画布，并明确标注“回退布局”；两种模式都支持点击多选及右键屏蔽。
  - 可操作商品显示绿色边框及“编号 + 商品名”，当前选中项叠加橙色边框；已屏蔽商品仍保留原图片，并使用红框和“已屏蔽”标签标记。
  - 屏蔽只从识别到的数据数组移除引用，不删除可能被其他界面共享的图片、Prefab 或条目源数据；修改结果写入 `workspace/output/Object/ToImport`，旧的商品屏蔽记录保持兼容。
- `9. 统一撤销已记录的 Object/商品屏蔽`
  - 在同一个窗口管理脚本 5/6/7 产生的 GameObject 屏蔽和脚本 8 产生的商品屏蔽记录；支持多选撤销。
  - 新的 GameObject 屏蔽记录会保存父链、屏蔽层级、最多 3 层下级节点和图片引用。撤销预览以目标为中心，最多显示向上 3 层及已记录的向下 3 层，并排除祖先的其他旁支；没有下层记录时只显示目标及上 3 层，避免大型层级树导致卡顿。
  - 商品或缺少层级信息的旧记录只显示对应图片。商品恢复会从原始数组重建顺序，再排除仍处于屏蔽状态的商品，避免多次恢复造成错位。

菜单 5、6、7、8 共用 `workspace/records/object_graph_cache.pkl` 对象图缓存。当前工具进程内会直接复用内存数据，重启工具后也可复用磁盘缓存；manifest、导出根目录或 `file_id_map.json` 变化时缓存自动失效。图片、Mesh、Object 名称、动态列表及布局链路还会分别复用各自的查询结果。

菜单 8 会同时识别两类布局：一类是由 Content/Grid 等字段显式引用的配置型布局；另一类是 NGUI 中已经实例化、依靠 Transform 层级和 Widget 尺寸排列的手工布局。预览会累计父级缩放，并使用真实条目对象、UITexture/UISprite、UILabel 的字号缩放、对齐、pivot、overflow 和 depth，而不是因为没有 GridLayout 字段就回退为空白卡片。只有容器或条目模板、具体数据由运行时代码注入的任务/成就/活动界面也会以 `R1`、`R2` 等编号列出；可输入对应 `R编号` 只读预览其真实 prefab 布局，但由于资源中不存在可安全删除的静态条目引用，暂不提供逐项屏蔽。
- `S. 搜索`
  - 包含字符串、PathID 和资源名搜索工具。
- `T. 测试`
  - 包含 Unity 资源兼容性检查、maybe title 清理和 records 字段恢复。
- `C. 清理主脚本产物`
  - 按产物清单选择并清理主流程脚本 0 至 9 生成的文件。

一键导出会自动读取 `settings.json`，识别 Addressables 使用的是 `catalog.json`
还是 `catalog.bin`。遇到二进制 catalog 时会同时输出：

- `workspace/output/catalog/catalog_bin_raw.json`：完整原生结构和二进制偏移。
- `workspace/output/catalog/Output.json`：与旧 JSON catalog 相同顶层字段的兼容视图。
- 展开 key、唯一 location、依赖、InternalId、Provider，以及 Bundle 的
  Hash、CRC、BundleSize 和对应二进制偏移。
- 自动检测配套 `catalog.hash` 使用的是 MD5，还是 Unity Scriptable Build
  Pipeline 的 SpookyHash128；修改并回写 catalog 时沿用检测到的原算法。
- 只要远程 InternalId 被本地化，就会自动回写源 `catalog.bin` 并更新
  `catalog.hash`；复用已经存在的本地资源时同样会修改源 catalog。
- 一键导入收尾修改 Bundle CRC/size 后，会输出
  `FinalResult/Bundle/catalog.bin` 与 `FinalResult/Bundle/catalog.hash`。存在远程路径
  本地化时同步覆盖游戏源 catalog；否则由用户手动替换。JSON catalog 使用相同规则。
## 图片对象层级预览（工具脚本主菜单 5）

`工具脚本.py` 主菜单 5“按图片定位对象并选择层级屏蔽”在层级选择处支持静态效果预览：

- 主菜单 5 可直接使用主菜单 2 输出到 `workspace/AllPNG/Sprite/PNG` 的拆分图集子图；输入子图文件名或完整路径，或将多张普通图片/子图复制到 `workspace/AllPNG/屏蔽object` 后批量处理。匹配时使用 `_allsprite_map.json` 中的 Sprite PathID，不再把整张 Texture2D 图集当作查询目标。
- 普通 `_allpng_map.json` 与拆分图集映射彼此独立：即使普通图片映射不存在，只要主菜单 2 已生成 `_allsprite_map.json`，主菜单 5 仍可查询拆分子图。若只有 `Sprite/PNG` 而映射缺失，工具会提示重新执行主菜单 2，避免仅凭同名图片误匹配对象。
- 在预览图片上右键命中使用了 Sprite/Texture2D 的层级时，菜单会提供“查询 Sprite / Texture2D 资源名字”，显示对应资源名、PathID、来源资源和 Bundle entry；没有图片资源的纯层级不会显示该项。
- 在左侧层级树或预览图片上右键选择“仅显示此层级”时，会保留所选层级分支，同时隐藏其它同级分支以及这些分支全部下级层级的图片；恢复或重新选择显示范围后会重新合成预览。
- Sprite 位于 `sharedassets*.assets`、UI 组件位于 `level*` 等其他资源文件时，会使用 `workspace/records/file_id_map.json` 解析非零 FileID，跨文件反查静态组件；层级预览也会从目标资源 scope 加载对应 Sprite/SpriteAtlas。对象查询缓存格式升级时会自动清除旧的空结果。
- 输入 `p`，直接从当前父链的最高层开始预览。
- 要选择起始层级，就在 `p` 后加层级数字；例如输入 `p3` 从层级 3 开始预览。
- 预览会递归加载该对象下的 UI Image/SpriteRenderer，按 RectTransform 合成图片，并用彩色边框标出可屏蔽的层级 0、1、2……。
- 静态合成会还原 `AspectRatioFitter`、`HorizontalLayoutGroup`，并为脚本控制的纵向状态容器恢复横向对齐；动画、业务脚本、完整 Layout/Mask/Shader 仍不会执行，因此多个运行时互斥状态可能同时叠加显示。
- 结果写入 `workspace/preview/ObjectHierarchy` 并在交互窗口中打开。左侧分为“进入时所在的层级链路”和“当前显示的层级链路（树）”；选择逻辑只响应名称列表，点击图片本身不会选择对象。
- 点击叶子节点时，当前树改为显示预览根到该叶子的所在链路；点击有子节点的对象时，以它为新的 0 级根节点，重算并显示其完整子树和所有分支。对应图片会用独立图片层重新合成，不属于当前链/树的图片不显示。
- 叶子模式会区分“框选链路”和“图片上下文”：绿色框只标出该叶子的所在链，但图片会从链路预览根递归加载完整视觉子树，避免只剩叶子自身图片而丢失父级背景、按钮等运行时上下文。非叶子模式的图片仍严格使用所选节点子树。
- 每次切换当前链或子树时，会按其中全部 RectTransform 的联合范围重新裁剪画布、重设坐标原点并自动适应窗口；不会继续沿用入口根对象的大画布和旧缩放倍率。
- 当前显示树的每个 RectTransform 范围默认显示绿色边框，所选节点名称和范围显示橙色；层级编号每次按当前树重新从 0 计算，不复用进入时父链的旧编号。
- 终端输出匹配对象父链时，已存在屏蔽记录的层级会以红色显示并带有 `[已屏蔽]`；预览中选择红色层级后可点击“取消屏蔽此层级”，恢复原始 `m_IsActive` 并即时刷新侧边栏、树和标注颜色。
- 预览默认自动适应窗口，确保完整画布先出现在屏幕内；鼠标滚轮或左侧 `−`/`+` 可以在 20%–400% 之间缩放，`适应窗口` 可随时恢复全图。放大后使用横向、纵向滚动条查看画布外区域。
- 层级名称、左侧列表和橙色选择名称使用宋体，作为独立 Canvas 图层按记录的原始相对位置实时绘制，不写入或重采样进背景图片。图片缩放使用离散倍率和最近四档缓存，文本与边框只重新计算坐标。终端对象链保持屏蔽编号顺序，从当前层级向上输出。
- 放大后除滚动条外，也可按住图片区域鼠标左键直接拖动画布。选择左侧层级名称后，“屏蔽此层级”会把它加入侧边栏的当前待屏蔽项；只有再次点击“确认屏蔽并进入下一个 Object 链路”才会写出屏蔽 JSON。成功后预览自动关闭并继续处理下一个匹配 Object 链路。
- 选中红色的已屏蔽节点时，“取消屏蔽此层级”按钮会启用；撤销按完整 GameObject JSON 路径和 PathID 精确匹配，完成后窗口保持打开，并立即刷新树中的红色状态和侧边栏已屏蔽列表。
- 层级与屏蔽侧边栏位于窗口左侧，图片预览位于右侧；两者之间的分隔条可左右拖动。拉宽侧栏会缩小图片可视区，缩窄侧栏会扩大图片可视区，侧栏和预览分别保留最小宽度。
- 侧边栏会列出已记录屏蔽对象的资源路径、Bundle entry 和 PathID；撤销列表还会分别输出外层资源、包内资源（如 `level2`）以及相对于 `workspace/input` 的 GameObject JSON 路径。当前树中已屏蔽的对象名称显示为红色。屏蔽身份优先按完整 GameObject JSON 路径和 PathID 精确判断，旧记录则按“来源资源路径 + Bundle entry + PathID”判断，因此不同 `level*` 中相同的 PathID 不会互相误判。
- 关闭交互窗口后会回到屏蔽层级选择；如果系统无法创建交互窗口，则回退到普通图片查看器。

这是导出数据的静态近似效果。脚本会处理 Sprite 图集裁切、锚点、位置、尺寸、旋转、颜色和原始启用状态，但不会执行 Unity 动画、业务脚本、Layout、Mask 或 Shader；预览图底部会列出本次无法解码的图片数量。

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

当前导入工具暂不支持直接改 SerializeReference 内部字段。遇到修改时会提示，并保留原资源内部数据，避免破坏引用结构；只有 `enable_sample_collection=true` 时才会额外收集诊断样本。一般 Unity Localization Settings、Locale 以及运行时 ID 之类的配置内容也不应该汉化。

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
- `workspace*/`
  - 不同配置会话相互隔离的工作区；空 `project_name` 使用 `workspace`，非空时使用 `workspace<project_name>`。
