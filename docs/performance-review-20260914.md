# 性能清单评审与本轮优化（2026-09-14）

## 结论

采用 OPT-00 的最小计时、OPT-01/03 的安全分析复用及元数据共享读取。
没有按清单大规模重构字体、纹理、导入或并发。前述功能修复的未提交改动均保留。

## 实测

环境：Windows / PowerShell，本地虚拟环境 Python；样本为王牌战术精英 ARM64。
使用现有项目配置，只读取必要路径，不输出配置内容或凭据。
未调用翻译接口、未启动 Unity、未写入游戏资源。

测试顺序：先空分析缓存 A，再 B、B 重复；独立输出目录不等于操作系统冷缓存。
冷分析只测一次；运行期间还有短时离线测试和元数据微基准，不能把结果当作隔离环境基准。

| 项目 | 时间 | 实际调用 / 结果 |
| --- | ---: | --- |
| A：空分析缓存 | 149.330 秒 | 原生分析 1 次 |
| B：输入不变 | 0.229 秒 | 原生分析 0 次，内容校验后命中 |
| B：重复 | 0.162 秒 | 原生分析 0 次，内容校验后命中 |
| script.json 原方式（两次） | 1.520 / 1.565 秒 | 每轮 4 次读取及 JSON 解析 |
| script.json 共享读取（两次） | 0.498 / 0.528 秒 | 每轮 1 次读取及 JSON 解析；四份索引与原方式一致 |

本次实测范围内最慢的三个子阶段：原生调用链分析 142.511 秒、dump.cs 索引 3.639 秒、ELF/重定位解析 1.735 秒。
元数据索引 0.518 秒；初始内容指纹 0.566 秒。
总墙钟还包含最终二次内容校验、缓存原子写入、对象释放等，不能把子阶段再与总时间累加。
没有完整导出、翻译、字体、导入的本轮耗时，不能声称以上是整条工具链的前三名。

正确性：精确显示 115、派生显示 151、枚举类型 3（成员 64），与优化前项目报告一致。
完整逐项比较最初报告不等：只有三个派生条目的 evidence 列表顺序不同。
仅对独立证据列表排序后，三次输出都与原报告相等；未忽略地址、调用链、转换步骤或步骤顺序。
原始 measurements.json 保留当时的严格顺序比较结果，另有 semantic_verification.json 说明复核结果。

## 优化项逐项评审

| 项目 | 状态 | 位置（文件:行 / 函数） | 实测或根因证据 | 本轮修改 / 影响 | 风险与验收 |
| --- | --- | --- | --- | --- | --- |
| OPT-00 阶段计时 | 已确认，部分实施 | one_click_pipeline.py:291 run_python_script；pipeline/il2cpp_display_usage.py:4753 analyze_il2cpp_display_usage | 原一键阶段没有单调时钟耗时；分析耗时见上表 | 增加 elapsed_seconds 事件字段及日志；分析 stats.performance 分阶段计时 | 不改变执行顺序；162 项相关测试基础上新增计时测试，最终 163 项通过 |
| OPT-01 阶段重复 | 已确认（脚本3）；全流程待测 | pipeline/dynamic_translation_dictionary.py:900；one_click_pipeline.py:395 _run_checkpointed_stage | 原脚本3主动删除缓存并传 use_cache=False；一键已有阶段断点 | 移除强制删除，委托分析器验证后复用；不跳过翻译/字典生成 | 缓存按真实依赖失效；损坏/规则变化回到完整分析 |
| OPT-02 容器重复处理 | 部分已排除，次数待测 | resource_menu.py:143 run_pipeline；ResourcePipeline.cs:337、455 AreProfileOutputsReusable | 导出已有容器级 CLI 和 profile 复用，并非每个对象启动 CLI | 不改资源读写 | 未测逐容器解压/保存次数；不能取消资源验证 |
| OPT-03 IL2CPP重复 | 已确认并实施 | pipeline/il2cpp_display_usage.py:778、4697、4718、4738、4753 | 缓存强制关闭；script.json 约56.6MB，四个解析入口重复全文读取 | 内容寻址缓存；同次任务共享 JSON；A/B 与元数据基准见上 | 候选和证据语义不减少；未减少调用深度/候选集 |
| OPT-04 TMP重复生成 | 待测 | pipeline/tmp_pipeline.py:1816、1832 launch_unity_tmp_generator | 当前路径会准备工程并启动 Unity；是否可复用需要完整字体依赖键 | 未改 | 未运行 Unity，未验证新字符/图集复用；不降低质量 |
| OPT-05 纹理转换 | 待测 | AssetPipeline_CLI/UnityResourceCLI/Pipeline/ResourcePipeline.cs 纹理导入分支 | 有格式转换与回退；缺少本轮编码计时 | 未改 | 需按格式、mipmap、编码器版本验证，不能只比较字节 |
| OPT-06 翻译重复 | 已排除“全无缓存/批处理”；其余待测 | pipeline/translation.py:2794 build_translation_map_for_texts | 已有分批、非空缓存恢复；上轮刚修复枚举译文传给 builder 的恢复问题 | 本轮不改翻译调度，不请求付费接口 | 保留语境与写回权限规则；未测真实网络速度 |
| OPT-07 大JSON | 待测 | pipeline/shared.py:21 atomic_write_json；translation.py 批次缓存保存 | 存在全量 JSON 保存，但已有批次而非统一逐条刷盘 | 仅分析缓存用紧凑 JSON、唯一临时文件、原子替换；不迁移数据库 | 写入失败保留旧缓存；未测通用检查点累计开销 |
| OPT-08 日志/子进程 | 部分已排除，其余待测 | one_click_pipeline.py:291 run_python_script | 此入口继承 stdout/stderr，没有 capture_output 全量累积 | 仅补计时；未修改其他进程封装 | 等待算墙钟，不归因于 Python 算法；取消链路未重测 |
| OPT-09 小文件/目录遍历 | 待测 | translation.py:5581 scan_and_record；resource_menu.py:961 run_export_profile | 已有扫描缓存/清单；历史样本含大量文件，但本轮未计磁盘耗时 | 未删减导出、未合并小文件、未更改 Defender | 需独立副本衡量后再改 |
| OPT-10 并发 | 待测 | one_click_pipeline.py；TMP 工程锁；原生导出 workers | 已有独立 workspace 和共享 Unity 工程锁 | 不提高线程数、不改变任务并发 | 同 workspace 仍不能并行运行；唯一缓存临时文件不是整个流程并发支持 |
| OPT-11 检查/哈希 | 部分已确认并实施 | il2cpp_display_usage.py:4697 _content_fingerprint | 原缓存仅 size/mtime，不足以安全启用 | 分块 SHA-256 校验输入、分析器源码、依赖版本与分析参数；发布前复核输入 | 同大小同mtime修改也失效；增加约0.1～0.6秒本地校验，不冒充物理磁盘读量 |

## 实际调用链

`run_with_config_python.py` 启动入口 → `one_click_pipeline.py.run_one_click_pipeline`
→ `resource_menu.py.run_export_profile` 准备 input_sources / Managed
→ Python wrapper → .NET UnityResourceCLI 导出
→ `main.py --run-step 0/1/2/3` 扫描、字段筛选、静态翻译、IL2CPP动态词库
→ 步骤4～8 字符清单、文本写回、特效清理及字体准备
→ 步骤9～10 Unity TMP / NGUI 生成与导入替换准备
→ 图片复制与拆图工具。

一键准备链本身不等于游戏导入验收；资源菜单的导入流程另行执行 C# CLI，生成 FinalResult 并处理资源路径/目录。
本轮只执行分析器与离线测试，没有运行准备、解包、字体、纹理、导入或游戏验收。

## A～F覆盖与安全设计

- A/B：真实样本、独立分析缓存已测；不是整套 workspace 全流程 A/B。
- C/D：离线单元测试验证译文内容/字符变化不会改变原生分析缓存键；字体增量行为未测。
- E：缓存发布失败、坏 JSON、结果被改动测试通过；没有中断 Unity 或实际游戏导入。
- F：同大小同mtime输入变化、追踪参数变化、静态预分类地址变化已测。源码字节与 capstone/pyelftools 版本也属于缓存键。
- 缓存结果自身带 SHA-256；旧格式或损坏缓存自动失效。只在完整分析结束后发布，失败不覆盖旧缓存。
- 本次保持独立任务级 JSON 共享，不引入跨游戏全局可变 JSON 缓存。
- 保留底层 `use_cache=False` 强制诊断接口；删除专用分析缓存也可强制下次重算，但正常重跑无需删除。
- 未实现同 workspace 重任务去重：README 已要求不要同时运行同工作区，不能因缓存原子写入就宣称全流程并发安全。

## 复现与产物

独立测试入口（不调用翻译）:

```powershell
& 'D:/user/von/NewTools/.venv/Scripts/python.exe' -u tools/benchmark_il2cpp_analysis.py --config '<现有项目配置路径>' --output '<不存在的独立输出目录>'
```

本次原始结果：`temp/perf-il2cpp-20260914/measurements.json`、`metadata.json`、`semantic_verification.json`，以及三份原生分析报告。
性能基准没有内存/物理磁盘/完整流水线统计，不给出未测指标。

回归命令：

```powershell
& 'D:/user/von/NewTools/.venv/Scripts/python.exe' -m pytest tests/test_one_click_pipeline.py tests/test_il2cpp_analysis_cache.py tests/test_il2cpp_display_usage.py tests/test_dynamic_translation_dictionary.py tests/test_resource_menu_export_profile.py tests/test_object_hierarchy_preview.py -q
```

结果：163 passed。工具源码与测试未打包原包、配置、密钥、缓存或 Unity Library。
当前工作树还包含先前的布局及漏译修复，不能用整文件 git restore 来回滚本轮。
回滚本轮时仅撤销安全缓存/共享JSON/阶段计时及新增测试、基准、报告的对应差异；不要撤销此前尾调用、组件查找、枚举缓存和布局修复。

后续优先测字体 C/D 与资源重复导入，不在没有完整依赖和验收证据时直接启用图集复用。
