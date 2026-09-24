# 从返回的数据对象追踪显示字段

动态词库不根据英文任务句式判断是否翻译。原先的动作、数量及词数兜底已删除。

## 实际遗漏链路

挑战天台跑酷的日常任务包含以下链路：

1. `DatasManager.LoadSignTask` 将字符串传给 `DatasManager.SignTaskData..ctor`。
2. 构造函数在 `0x1685700` 把字符串参数 `x2` 保存到对象字段 `+0x18`（`tittleKey`）。
3. `SignTaskItem.initUI` 调用 `DatasManager.GetSignTask`，其返回类型是 `DatasManager_SignTaskData_o*`。
4. 界面在 `0x162D780`、`0x162D79C` 读取返回对象的 `+0x18`。
5. 读取结果分别在 `0x162D78C`、`0x162D7A8` 传给由 Text 组件类型及虚表槽位确认的 `virtual_set_text`。

字段名虽然包含 `Key`，实际原生指令却直接把字段值交给显示接口。因此既不能凭字段名排除，也不能凭字符串句式放行。

## 分析器处理

分析器为具体托管对象返回值保留调用目标与字段访问路径。只有字段读取到达已识别的显示参数时，才将该具体类型及字段偏移连接到已有的字段写入分析。构造函数的字符串参数随后成为可追踪入口，调用者中的字面量沿原有分析流程被识别。

证据中保留返回对象的方法、消费方法、读取位置、持久化字段路径和显示入口。原先用于容器内容与委托别名分析的临时对象身份同时保留。

该机制不依赖游戏名称、任务动词、数字或句子长度。回归测试包含没有数字的任意文本，以及另一个类型中相同偏移的内部字段，验证只有存在显示证据的类型被连接。

另一组任务由 `DatasManager.TaskData..ctor` 初始化 `tittle`（`+0x20`）。`QuestView.initView` 通过 `this +0x60` 的 `mData` 再读取 `+0x20`，随后调用 Text。分析器根据 `dump.cs` 中的具体引用字段类型，将 `QuestView +0x60.+0x20` 解析为 `DatasManager.TaskData +0x20`，再连接该模型的字段写入。静态字段及类型名有歧义的字段不会被这条规则连接。

## 枚举名称经过返回函数显示

`Ec_Skil_Ctl.GetSkatingDataTitle` 取出 `SkatingEffectType`，在栈上构造枚举对象，于 `0x1629664` 调用 `System.Enum.ToString` 返回名称。`Ec_Skil_Ctl.OnValueChanged` 接收返回值，并在 `0x1629838`、`0x162998C` 调用 Text 虚拟 setter。

修复包含两处：字符串返回摘要分析传入 ELF 指针映射，以保留枚举 TypeInfo 来源；拥有已证明字符串/枚举返回摘要的函数，其调用者也可进入显示候选分析。最终仍要求值实际到达显示参数。仅调用 Enum.ToString 后用于日志等其他用途不会纳入。

## 拼接段位与跨模型名单

字段进入 `String.Concat` 等字符串转换后，需要保留“派生文本”角色并传回字段写入入口。否则 `Bronze` 只进入整句字典，不能覆盖运行时拼接的 `Bronze 4`。转换后的字段路径与转换标记分开保存，段位名据实际转换证据进入片段字典。

名单还可能经过返回列表的 getter、`List.get_Item`、另一个显示模型的构造函数。分析器追踪 getter 实际返回的实例字段，并从新证明的模型写入参数继续向上追踪，直到没有新增入口。后续轮次只检查新入口的调用者；不根据名字或字面量形态放行名单。

挑战天台跑酷的实际路径是：构造函数把 51 个姓名写入 `RankNames`（`+0xB8`）字符串数组；`ResetRanks` 通过 `Enumerable.ToList<object>`、`List<object>.get_Item` 随机抽取，再用 `AddWithResize` 构建名单，交给 `set_RandomNames` 保存到 `+0xD0`；`LoadRankDatas` 通过 getter 取出名单元素，传入 `TopRunRank`，最终由排行榜 Text 显示。setter 同时保存本地存储，但内存字段已经提供可验证的完整路径，不需要猜测存档键。

通用集合处理保留 `ToList/ToArray` 的源、泛型共享后 `List<object>.AddWithResize` 的元素来源，以及容器参数内容。固定偏移写入新分配对象时只记录该分配对象的局部槽位，不能当成宿主类字段；仅当该对象确实写入 `dump.cs` 证明的字符串集合字段时，才按集合元素汇总。任意嵌套类型也不再因名称前缀相同而与外层类型字段混用。

`Copper` 是另一条路径：`RankHeader.ResetUI` 的 switch 分支通过 `0x1662DE4` 的 Text 虚拟尾调用显示 Copper、Silver、Gold、Master、Champion。编译器先比较 `w22`，再将其复制到 `w8` 用作跳转表索引。跳转表分析现在反向追踪寄存器复制以找到实际边界；若索引经过未证明的算术或覆盖，则不沿用旧边界。该规则不包含上述文本或游戏方法名。

## 原生字符串指针表

角色名和皮肤名还使用一种不同结构。`JoyGame.PlayerData.ShowName` 在确认索引不超过 4 后，以 `ldr x8, [table, x8, lsl #3]` 从原生重定位指针表返回 Bunny、Selina、Max、Fox、Caesar，调用者随后直接把返回值交给 Text。`DatasManager.SkinData.SetDatas` 也通过受边界约束的分支表选择名称，并写入 `SkinData.name(+0x10)`；领取与皮肤界面读取该字段后交给 Text。

分析器现在只在同一基本代码窗口内找到索引上界，并确认表基址及每个槽位都有 ELF RELATIVE 重定位且目标确实是 stringliteral 单元时，才枚举字符串指针表。索引寄存器允许经过纯复制；算术修改、无边界索引、普通数据表或没有字符串重定位的表都不会放行。这使规则可覆盖同类 IL2CPP switch/只读名称表，同时不按名称词形猜测。

## 原子订阅的消息事件

我的跑酷世界的熔岩提示由 `MapLavaView.CountDownRoutine/StartRoutine.MoveNext`
格式化后传给 `MessageHandler.InvokeMessage`，再经 `OnUpdateMessage` 到
`MessageView.DataOnUpdateMessage`，最终调用 `TMP_Text.SetText`。

事件的 `add_OnUpdateMessage` 并不直接存储 `Delegate.Combine` 的结果，而是
将字段地址交给 IL2CPP 原生引用 CompareExchange。分析器保留前索引加载的
字段地址及组合委托参数，验证原生包装函数的参数置换和 ARM64 CAS/独占读写
循环后，才建立参数到具体实例字段的订阅关系。没有原子指令证据的普通
原生调用不会被当成事件写入。缓存版本同步升级，旧的漏识别结果不再复用。

订阅调用既可使用 `BL`，也可在恢复栈后使用尾分支 `B`。对于从界面对象字段
取出的事件对象，使用已知 `add_Event` 方法的接收者类型定位事件字段，而不是
错误地归到界面类型之下；订阅和触发因此能连接到同一个具体类型及字段偏移。

此规则不依赖游戏名称、提示文本或函数地址；其他编译器生成不同原子实现时，
仍需新增相应指令证据，不能仅凭 `add_` 或 `Invoke` 名称放行。

## 当前限制

当前新增能力要求方法签名保留具体返回类型。被擦除为 `Il2CppObject*`、无法解析的间接调用及未建模的数据转换，仍需各自的类型或数据流证据，不会因此自动放行。
