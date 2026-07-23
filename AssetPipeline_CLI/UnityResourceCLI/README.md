# UnityResourceCLI

这是一个小型命令行工具，只负责我需要的那几类资源流程：

- 导入/导出 `Texture2D`
- 导入/导出 `TextAsset`
- 导入/导出 `MonoBehaviour`
- 导入/导出 `Material`
- 导入/导出 `Font`
- 图片导出保持 `png` 或 `jpg`

## 为什么要做它

`UABEA` 仍然作为参考实现保留着，但这个工具把脚本化路径收敛到我真正需要的资源类型上。

## 当前用法

```powershell
cd D:\CG\Font_Generator\AssetPipeline_CLI
dotnet run --project .\UnityResourceCLI\UnityResourceCLI.csproj -- export --source "D:\Game\Data" --work "D:\CG\Font_Generator\workspace\resource_cli"
dotnet run --project .\UnityResourceCLI\UnityResourceCLI.csproj -- import --source "D:\Game\Data" --work "D:\CG\Font_Generator\workspace\resource_cli"
```

## Python 入口

你也可以直接用 Python 驱动这个工具：

```powershell
python .\scripts\unity_resource_pipeline.py "D:\MyWorkbench\Works\APK\VS\0_Projects\菜鸟矿工\game-name\game\assets\bin\Data"
```

默认值：

- `Texture2D` 默认导出为 `png`
- `TextAsset` 默认导出为 `json`
- `MonoBehaviour` 默认导出为 `json`
- `Material` 默认导出为 `json`
- `Font` 默认导出为 `ttf` 或 `otf`
- `Managed` 默认取 `<source>\Managed`
- 输出默认写入 `D:\CG\Translate\workspace\resource_cli`
