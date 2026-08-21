# AssetPipeline CLI

这里是当前项目使用的 Unity 资源命令行管线，由主项目的 `resource_menu.py` 调用，并与汉化、字体流程共用主项目的 `workspace`。

## 目录内容

- `UnityResourceCLI/`
  - 负责 `Texture2D`、`TextAsset`、`MonoBehaviour`、`Material` 和 `Font` 的导入、导出。
  - `ThirdParty/UABEA/` 仅保留运行必需的 `FileTypeDetector.cs`、`classdata.tpk` 和 UABEA 许可证。
- `AssetsTools.NET/`
  - 当前 CLI 实际使用的 Unity assets、bundle 读写及纹理编码库。
- `scripts/`
  - 当前资源 CLI 的 PowerShell/Python 封装，以及本机纹理编码库的构建脚本。

## 脚本功能

- `Export-UnityResource.ps1`
  - 将支持的 Unity 资源导出到工作目录。
- `Import-UnityResource.ps1`
  - 把编辑后的资源导入结果目录，不改动原始文件。
- `unity_resource_pipeline.py`
  - Python 侧的资源导入、导出入口。
- `build_texture_encoder.ps1`
  - 构建 ETC2/ASTC 等格式所需的本机纹理编码库。

## 快速开始

```powershell
cd D:\user\von\NewTools\Translate
python resource_menu.py
```

## UABEA 参考源码

完整 UABEA 源码不参与当前构建，也不随本仓库同步，已移至：

```text
D:\user\von\NewTools\样本\UABEA
```

项目需要的两个 UABEA 运行依赖已经单独提取到 `UnityResourceCLI/ThirdParty/UABEA/`，无需从上述参考源码目录加载。
