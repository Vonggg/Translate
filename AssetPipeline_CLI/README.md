# AssetPipeline CLI

这个目录里放的是一套本地化的开源资源管线工具，目标是尽量摆脱手动点 UABEA 的工作方式。
它由主项目的 `resource_menu.py` 调用，和汉化/字体流水线共享主项目的 `workspace` 工作区；本目录不再维护独立 `workspace`。

## 目录内容

- `UABEA/`
- `UnityResourceCLI/`
  - 一个专注于 `Texture2D`、`TextAsset`、`MonoBehaviour`、`Material` 和 `Font` 导入/导出的命令行工具。
  - 图片导入/导出保留 `png` 和 `jpg` 支持。
- `AssetsTools.NET/`
  - UABEA 所使用的核心资源读写库，也适合后续继续扩展自定义 CLI。
- `scripts/`
  - 资源 CLI 以及剩余 UABEA 辅助功能的轻量封装脚本。

## 脚本功能

- `Export-UnityResource.ps1`
  - 将 `Texture2D`、`TextAsset`、`MonoBehaviour`、`Material`、`Font` 导出到工作目录。
- `Import-UnityResource.ps1`
  - 把编辑后的导出结果导入到结果目录中，不会改动原始文件。
- `Export-Bundle.ps1`
  - 执行 `batchexportbundle`
- `Import-Bundle.ps1`
  - 执行 `batchimportbundle`
- `Apply-Emip.ps1`
  - 执行 `applyemip`
- `unity_resource_pipeline.py`
  - 用 Python 调用资源导入/导出的入口脚本。

## 快速开始

```powershell
cd D:\user\von\NewTools\Translate\Translate
python resource_menu.py
```

## 说明

- UABEA 仍然保留作为参考实现。
- 新的 CLI 就是你后续自动化资源流程的直接脚本目标。
- 如果后面还要继续扩展资源管线，这里就是在 `AssetsTools.NET` 基础上继续长大的地方。
