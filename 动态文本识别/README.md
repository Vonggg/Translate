# 动态文本识别（独立工具）

本工具从主项目的“脚本 3”提取出动态文本识别部分：分析 `stringliteral.json`、`script.json`、`dump.cs` 和 ARM64 `libil2cpp.so` 的真实显示链路，并生成 C++ 字典。

它不读取 `config.json`、不调用翻译功能、不会修改主项目脚本，也不依赖主项目的 `pipeline` 目录。分析器和字典逻辑已内嵌在主脚本中；只需复制 `dynamic_text_recognition.py` 这一个文件。每个已识别文本的译文位置均导出为空字符串 `u""`，字典结构保持与原 Hook 一致。

首次使用请安装唯一的第三方依赖：`python -m pip install pyelftools`。

## 使用

直接在 [dynamic_text_recognition.py](dynamic_text_recognition.py) 顶部填写 `DEFAULT_INPUT_PATH` 后运行：

```powershell
python .\动态文本识别\dynamic_text_recognition.py
```

也可传入路径；命令行路径优先于脚本顶部变量：

```powershell
python .\动态文本识别\dynamic_text_recognition.py --input "D:\game\bak\64\stringliteral.json" --libil2cpp "D:\game\lib\arm64-v8a\libil2cpp.so"
python .\动态文本识别\dynamic_text_recognition.py "D:\game\bak\64" --libil2cpp "D:\game\lib\arm64-v8a\libil2cpp.so"
python .\动态文本识别\dynamic_text_recognition.py -i "D:\game\bak\64" --libil2cpp "D:\game\lib\arm64-v8a\libil2cpp.so" --records "D:\workspace\records\records.json"
python .\动态文本识别\dynamic_text_recognition.py -i "D:\game\bak\64" -o "D:\result\dictionary.cpp"
```

`--input` 可传 `stringliteral.json` 或其所在目录。未额外指定 `--script-json` / `--dump-cs` 时，工具会读取其同目录文件。`libil2cpp.so` 必须通过 `--libil2cpp` 或脚本顶部的 `DEFAULT_LIBIL2CPP_PATH` 指定。

`--records` 可选。传入原项目扫描得到的 `records.json` 后，会保留原脚本的“静态显示文本交集”确认规则；不传时只导出经 IL2CPP 显示链路证明的文本。

默认输出仍是：

```text
output/Hook_Translate/native_unity_translation_dictionary.generated.cpp
```

其中 `output` 为输入 `stringliteral.json` 同目录的 `output`。可用 `--output` 改成任意 `.cpp` 目标文件。
