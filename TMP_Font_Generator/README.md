# TMP 字体资源生成项目

这个目录是一个小型 Unity 编辑器工程，用来从 Python 侧触发生成 TextMeshPro 字体资源。

## 包含内容

- `Assets/Editor/TmpFontGenerator.cs`
  - 可通过 `-executeMethod` 调用的 Unity 编辑器入口。
- `Tools/generate_tmp_font.py`
  - Python 启动器，会把字体复制到工程里并以批处理模式启动 Unity。
- `Packages/manifest.json`
  - 最小化的包清单，包含 `com.unity.textmeshpro`。

## 依赖条件

- Unity 编辑器安装在：
  - `D:\ProgramFile\SoftwareTool\Develop\Unity\Unity\2022.3.62f1c1`
- 一份源字体文件：
  - `.ttf` 或 `.otf`

## 标准流程

1. 把源字体文件放到任意磁盘位置。
2. 运行 Python 启动器。
3. 启动器会把字体复制到 `Assets/SourceFonts/`。
4. Unity 执行批处理任务，并在 `Assets/GeneratedFonts/` 下生成 `.asset` 文件。

## 示例

```powershell
python .\Tools\generate_tmp_font.py --font "D:\Fonts\NotoSansCJKsc-Regular.otf" --output-name "NotoSansCJKsc-Regular.asset"
```

如果你想要更大的图集或不同的渲染模式，可以再加参数：

```powershell
python .\Tools\generate_tmp_font.py --font "D:\Fonts\NotoSansCJKsc-Regular.otf" --output-name "NotoSansCJKsc-Regular.asset" --atlas-width 4096 --atlas-height 4096 --point-size 90 --padding 9 --render-mode SDFAA --multi-atlas
```

## 说明

- 生成出来的资源会保存在 Unity 工程里，后续可以复用，也可以提交。
- 对于大字符集的中文字体，通常需要更大的图集，或者拆成多个字体资源。
- Python 脚本只负责调度，真正的 TMP 资源生成还是由 Unity 完成。
