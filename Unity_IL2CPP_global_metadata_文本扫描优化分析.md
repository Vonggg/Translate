# Unity IL2CPP：global-metadata.dat 硬编码文本显示路径与扫描器优化分析

## 对话背景

**用户：**

> 我说的是存在与 gloabal-gamedata 中的文本，不是资源中的文本，你需要区分一下。

**助手：**

对，这个边界很重要。下面只讨论：

```text
global-metadata.dat
  └─ Il2CppStringLiteral / ScriptString / stringliteral.json
       └─ 被 IL2CPP 代码引用
            └─ 经寄存器、返回值、字段、事件等路径
                 └─ 最终显示
```

**不讨论** Prefab、Scene、UXML、AssetBundle、Resources、网络下发文本。

我比较了两个脚本。新版语法检查通过，但从实际 diff 看，本次优化主要只改了 **carrier 类型家族匹配**：

- 旧版把类型压缩成命名空间第一段，容易把同一命名空间下完全不同的 Model 串起来。
- 新版保留了更完整的类型身份，并让 Builder/嵌套辅助类通过前缀匹配对应 Model。
- carrier 生产者匹配也从单一 family 改成了候选 family 集合。

这个修改是对的，主要提升的是**准确率、减少错误关联**。但造成“global-metadata 中有文字，却扫描不到”的几个核心问题仍然存在。

---

# 第一优先级：目前确定会造成静默遗漏的问题

## 1. 新算法可能根本没有运行：缓存没有失效

新版算法已经变化，但仍然是：

```python
SCHEMA_VERSION = 3
```

缓存指纹只包含：

```text
schema_version
max_wrapper_depth
输入文件路径、大小、mtime
```

不包含当前分析器代码版本。只要旧的：

```text
.il2cpp_display_usage.cache.json
```

仍在，脚本就可能直接返回旧结果。

建议马上改成：

```python
SCHEMA_VERSION = 4
ANALYZER_REVISION = "2026-09-04-carrier-v2"
```

并把它加入 fingerprint：

```python
{
    "schema_version": SCHEMA_VERSION,
    "analyzer_revision": ANALYZER_REVISION,
    "max_wrapper_depth": max_wrapper_depth,
    "inputs": fingerprint_rows,
}
```

更稳妥的设计是：

- `SCHEMA_VERSION`：输出 JSON 结构版本；
- `ANALYZER_REVISION`：算法版本；
- 任意一个变化都使缓存失效。

在修改之前测试新版，至少先删除旧缓存。

---

## 2. `LDR literal` 被预扫描识别，却在正式分析时丢掉

便宜预扫描已经识别 ARM64 的 PC-relative `LDR literal`：

```asm
ldr x1, #literal_slot
```

并把它加入引用索引。

但正式 `_analyse_method()` 中，只处理第二个操作数为 `MEM` 的 `ldr/ldur`。如果 Capstone 将 `LDR literal` 表示为立即数 `IMM`，代码会直接清空目标寄存器：

```python
if operands[1].type == ARM64_OP_MEM:
    ...
else:
    _set_register(state, destination, ())
```

这会形成一种很隐蔽的现象：

```text
候选方法找到了
literal reference 也统计到了
但正式数据流中字符串消失
最终没有 exact hit
```

应该单独处理：

```python
elif (
    mnemonic == "ldr"
    and len(operands) >= 2
    and operands[1].type == ARM64_OP_IMM
):
    destination = operand_register(operands[0])
    target = int(operands[1].imm)

    if target in slot_to_cell:
        cell = slot_to_cell[target]
        values = {AbstractValue("cell", cell, pc)}
        result.referenced_literals.add(cell)
    elif target in literal_cells:
        values = {AbstractValue("exact", target, pc)}
        result.referenced_literals.add(target)
    elif target in pointer_slots:
        values = {AbstractValue("pointer_slot", target, pc)}
    else:
        values = set()

    _set_register(state, destination, values)
    handled_register_writes = True
```

这是我建议最先补的代码级遗漏之一。

---

## 3. 字符串返回值摘要的候选方向有问题

当前返回值摘要从以下集合开始：

```python
relevant_return_callers =
    literal_reference_methods
    + 带 string 参数的 wrapper
```

随后寻找“被这些方法调用的字符串返回方法”。

但是最常见的代码是：

```csharp
string GetTitle()
{
    return "Victory";
}

void Refresh()
{
    title.text = GetTitle();
}
```

此时：

- `"Victory"` 的引用位于 `GetTitle`；
- `Refresh` 不直接引用 `"Victory"`；
- `Refresh` 也没有 string 参数；
- `GetTitle` 自己不会调用自己。

因此 `GetTitle` 很可能根本不进入 `return_summary_candidates`。

这意味着你虽然实现了 return summary，但最典型的：

```text
literal → string getter → UI setter
```

仍然可能漏。

最低限度应先加入：

```python
return_summary_candidates = {
    address
    for address in string_return_methods
    if address in literal_reference_methods
}
```

然后再从所有“能到达显示 sink 的方法”向下寻找字符串返回 callee。

更完整的方向应当是：

```text
显示 sink 的调用者
  ↓
该调用者调用的 string-return 方法
  ↓
这些 string-return 方法调用的下一级 string-return 方法
  ↓
直到固定点
```

而不是只从“引用了 literal 的方法”寻找它们的 callees。

另外，外部 tail-call：

```asm
b GetTitle
```

当前只检查目标是不是 sink，并不会应用返回摘要，所以：

```csharp
string A() => B();
string B() => "Title";
```

如果被优化成尾调用，也会断链。外部 `b` 分支也应该处理 `return_summaries_by_address`。

---

## 4. 32 个抽象值上限依旧会丢字符串，而且没有诊断

当前每个寄存器或栈位置最多保留：

```python
MAX_ABSTRACT_VALUES = 32
```

超过后按优先级截断。即便全都是 `exact`，最终也只保留前 32 个。

例如：

```csharp
switch (state)
{
    case 0: text = "State 0"; break;
    case 1: text = "State 1"; break;
    // ...
    case 99: text = "State 99"; break;
}

label.text = text;
```

控制流汇合后，最多只能留下 32 个来源。

还有一个更隐蔽的问题：`AbstractValue` 的相等性包含：

```text
kind
source
origin
transforms
```

因此同一个 literal 从多个指令路径到达，也可能占据多个槽位。

建议不要简单把 32 改成 128，而是把：

```text
抽象语义值
```

和：

```text
证据来源 origin
```

分开。

例如：

```python
semantic_key = (kind, source, transforms)
provenance[semantic_key].add(origin)
```

然后：

- literal 地址集合可以使用紧凑的 `frozenset[int]`；
- 每个 literal 只保存有限数量的 origin 示例；
- 对象别名、虚表路径等复杂值继续限制数量；
- 一旦截断，必须输出：

```json
{
  "reason": "abstract_value_truncated",
  "method_address": "...",
  "register": "x1",
  "kept": 32,
  "discarded": 68
}
```

否则你无法区分“真实没有显示”和“分析器自己扔掉了”。

---

## 5. 预扫描只把 relocation slot 当作 literal reference

正式候选集合中的：

```python
literal_reference_methods
```

只检查：

```python
referenced_slots & set(slot_to_cell)
```

这会遗漏以下直接物化形式：

```asm
adrp x8, literal_cell@PAGE
add  x8, x8, literal_cell@PAGEOFF
ldr  x1, [x8]
```

因为这里引用的是 literal cell 本身，而不一定是 `.rela.dyn` 中的中转 slot。

建议 `_scan_code_indexes()` 不再返回原始 slot，而是直接标准化成 literal 地址：

```python
literal_references_by_method[owner].add(
    slot_to_cell.get(referenced_address, referenced_address)
)
```

调用时应跟踪：

```python
tracked_locations = (
    set(slot_to_cell)
    | set(literals)
    | set(pointer_slots)
)
```

这样：

```text
GOT/relocation slot → literal cell
直接 literal cell
LDR literal → slot
```

最终都统一成同一个 literal ID。

---

# 第二优先级：大量 metadata literal 的真实代码路径仍未覆盖

## 6. Carrier 只覆盖“字符串参数写字段”，不覆盖“literal 直接写字段”

当前字段写入只记录：

```python
stored.kind in {"param", "derived_param"}
```

不会记录 `exact` 和 `derived`。

所以这个可以覆盖：

```csharp
PopupData SetTitle(string value)
{
    data.title = value;
}
```

但覆盖不了更直接、更常见的：

```csharp
void BuildPopup()
{
    PopupData data = new PopupData();
    data.title = "Connection failed";
    queue.Enqueue(data);
}
```

第二段代码中的 `"Connection failed"` 明确存在于 `global-metadata.dat`，但：

- 方法没有 string 参数；
- literal 被写入新对象字段；
- 当前 carrier 第二阶段只检查有 string 参数的方法；
- `call_result` 对象的字段内容也没有建模。

建议增加：

```text
literal_field_writes
derived_literal_field_writes
static_field_writes
allocation_field_writes
```

至少记录：

```text
Literal(addr)
    → ObjectField(object_identity, declaring_type, field_path)
```

然后与 consumer 中的：

```text
ObjectField(...)
    → display sink
```

进行连接。

---

## 7. 新 carrier family 匹配仍然存在名称碰撞

新版把所有非字母数字字符删除：

```text
Game.UI.PopupModel
→ GameUIPopupModel
```

然后通过 `startswith()` 判断 Builder 是否属于 Model。

这仍然会产生碰撞：

```text
A.BC        → ABC
AB.C        → ABC

Popup       → Popup
PopupExtra  → PopupExtra   # startswith(Popup) 为真
```

更稳妥的是保留结构化名称：

```python
("Game", "UI", "PopupModel")
("Game", "UI", "PopupModel", "Builder")
```

判断：

```python
owner_parts[:len(family_parts)] == family_parts
```

更理想的是直接使用：

```text
TypeDefinitionIndex
DeclaringTypeIndex
FieldDefinitionIndex
```

而不是字符串猜测。既然分析源本来就来自 `global-metadata.dat`，使用 metadata 类型索引会比 C++ 名称和 C# 名称之间做字符串归一化可靠得多。

---

## 8. Carrier 仍然只比较最后一个字段偏移

consumer 得到完整 `field_path` 后，当前保存的是：

```python
displayed_offsets[family][field_path[-1]]
```

也就是只留下最后一级偏移。

例如：

```text
PopupData.header.title  → 0x20 → 0x18
PopupData.body.message → 0x30 → 0x18
```

两条路径最后都是 `0x18`，会被当成同一字段。

这既可能误报，也可能让后续为避免误报而收紧时漏报。

建议 carrier key 至少是：

```python
@dataclass(frozen=True)
class CarrierFieldKey:
    concrete_type: int | str
    full_path: tuple[int, ...]
```

对于 Builder，应当匹配“有类型信息的路径后缀”：

```text
Builder._data : PopupData
PopupData.title
```

而不是简单匹配最后一个 offset。

---

## 9. 非 void 包装函数仍被排除

包装函数当前必须返回 `void`。

以下都会漏：

```csharp
PopupHandle ShowPopup(string text);
bool TryShowError(string text);
UIBuilder SetTitle(string text);
Tweener AnimateText(string text);
```

只要你已经通过数据流证明：

```text
参数 i → display sink
```

那么包装函数的返回类型并不会降低这个证明的可信度。

建议只保留：

```text
排除 Unity/System/TMPro 框架内部方法
参数必须被证明到达 sink
```

删除“必须 void”的限制。

---

## 10. 经过变换的 wrapper 参数被直接终止

当前：

```csharp
void Show(string value)
{
    label.text = value.ToUpperInvariant();
}
```

在内部会形成 `derived_param`。

但 `record_sink()` 对 `derived_param` 只写入：

```text
wrapper_parameter_is_transformed
```

不会生成 wrapper sink。

因此调用：

```csharp
Show("offline");
```

中的 `"offline"` 不会进入 `derived_influence`。

应该把参数命中改成：

```python
parameter_hits: list[
    tuple[
        parameter_index,
        downstream_sink,
        callsite,
        transforms
    ]
]
```

然后 wrapper 生成：

```python
SinkSpec(
    ...,
    argument_transforms=("ToUpperInvariant",)
)
```

caller 中的 metadata literal 就能正确进入 `derived_influence`。

同样的问题还存在于 carrier：`derived_param` 写入字段后，当前记录结构没有保留 transforms，后续可能错误地将其升级为 exact。

---

## 11. 字符串返回摘要深度和 wrapper 深度都是固定 4

默认：

```python
max_wrapper_depth = 4
```

返回摘要和包装传播都受它限制。

真实项目中经常是：

```text
业务函数
→ Presenter
→ ViewModel
→ UIService
→ PopupService
→ Widget
→ TMP_Text.set_text
```

建议使用 worklist 固定点：

```text
有新 summary 产生
→ 重新分析它的调用者
→ 直到没有变化
```

通过：

```text
每个 method + parameter + sink-path 只加入一次
```

控制终止，而不是通过任意深度 4 截断。

路径输出可以只保留前 N 层，但数据流传播本身不必受 4 层限制。

---

# 第三优先级：显示终点仍然过窄

当前 exact display sink 仍然只有：

```text
TMPro.TMP_Text.set_text
TMPro.TMP_Text.SetText
UnityEngine.UI.Text.set_text
UILabel.set_text
```

这里讨论的依旧全是 `global-metadata.dat` 中的代码 literal，不涉及资源。至少应补：

```text
UnityEngine.TextMesh.set_text

UnityEngine.UI.InputField.set_text
UnityEngine.UI.InputField.SetTextWithoutNotify

TMPro.TMP_InputField.set_text
TMPro.TMP_InputField.SetTextWithoutNotify

UnityEngine.UIElements.TextElement.set_text

UnityEngine.GUI.Label
UnityEngine.GUI.Button
UnityEngine.GUI.Box
UnityEngine.GUI.Toggle
UnityEngine.GUI.TextField
UnityEngine.GUI.TextArea

UnityEngine.GUILayout.Label
UnityEngine.GUILayout.Button
UnityEngine.GUILayout.Box
UnityEngine.GUILayout.Toggle
UnityEngine.GUILayout.TextField
UnityEngine.GUILayout.TextArea
```

然后增加“显示载体”，但不要直接放入 exact：

```text
UnityEngine.GUIContent..ctor
UnityEngine.GUIContent.set_text

UnityEngine.UI.Dropdown.OptionData..ctor
UnityEngine.UI.Dropdown.OptionData.set_text

TMPro.TMP_Dropdown.OptionData..ctor
TMPro.TMP_Dropdown.OptionData.set_text
```

建议分成：

```text
display_now       可以算 exact/derived
display_container 需要证明容器后来送入真实 UI
```

否则把 `GUIContent`、`OptionData` 构造器直接当成最终显示，容易误报。

对于跨多个游戏的通用扫描器，sink 最好改成 JSON/YAML 注册表，而不是继续增长 `_match_display_sink()` 的 `if` 链。

---

# 第四优先级：事件和虚调用逻辑仍有明显缺口

## 标准 C# event 很可能匹配不到

当前要求同一个方法同时：

```text
调用 delegate 构造器
调用 System.Delegate.Combine
```

即：

```python
subscription_candidates =
    combine_callers & delegate_constructor_callers
```

但普通 C#：

```csharp
publisher.OnMessage += OnMessage;
```

通常编译成：

```text
订阅者方法：
  new Action(OnMessage)
  publisher.add_OnMessage(delegate)

add_OnMessage：
  Delegate.Combine
  写 event 字段
```

构造器和 Combine 通常不在同一方法。

因此当前交集很可能为空。

需要拆成两个摘要：

```text
订阅者：
DelegateTarget(callback) → add_Event 参数

add_Event：
参数 → event field

invoker：
event field + literal 参数 → delegate Invoke
```

另外，callback 目前还要求自己拥有 Text/TMP 字段，才会继续分析。

这会漏掉：

```csharp
void OnMessage(string text)
{
    PopupService.Show(text);
}
```

callback 不拥有文本字段，但明显调用了已经证明过的 wrapper。这里不应该要求 `fields or nested_fields`。

---

## 虚调用证明并没有真正证明它是 `set_text`

当前虚调用逻辑中：

```text
接收者是 TMP/Text
函数槽后面紧邻 MethodInfo 槽
```

就认为是虚拟文本 setter。

但“函数指针后面紧邻 MethodInfo”是普通 IL2CPP vtable entry 的常规布局，并不能证明这个槽就是 `set_text`。

代码还保留了：

```python
if function.transforms[-1].upper() == "5E8":
    return True
```

这会有两个风险：

- 不同 Unity/TMP 版本槽位变化，产生漏报；
- TMP_Text 上其他接受 string 的虚方法被误认为 setter，产生误报。

更可靠的方式是从 metadata 中解析目标方法的：

```text
virtual slot index
declaring type
method definition
```

然后验证实际 vtable slot。

在没有 slot 语义证明时，当前虚调用结果更适合归入：

```text
probable_display_literals
```

而不是和直接调用的 exact 混在一起。

---

# 还需要特别区分 global-metadata 中的两类字符串

当前代码实际加载的是：

```text
ScriptString
stringliteral.json
```

也就是 `Il2CppStringLiteral`。

但 `global-metadata.dat` 中还存在另一类文本：

```text
类型名
方法名
字段名
属性名
枚举成员名
参数名
```

这些属于 metadata name string table，不是普通 StringLiteral。

因此下面的显示内容即使物理上也存在于 `global-metadata.dat`，当前仍不会进入 literals：

```csharp
label.text = enumValue.ToString();
label.text = typeof(PlayerState).Name;
label.text = propertyInfo.Name;
```

所以建议在输出中明确分源：

```text
literal_strings       Il2CppStringLiteral
metadata_names        类型、方法、字段、枚举名称
```

如果你的目标只包括“代码中的引号字符串”，继续只扫描 `ScriptString` 即可；如果目标是“所有存在于 global-metadata.dat 且可能显示的文本”，metadata name table 也需要纳入。

---

# 另外两个很值得补的 IL2CPP 专项问题

## 同一 RVA 的方法会被覆盖

`_prepare_methods()` 使用：

```python
method_data[address] = (name, signature)
```

同一地址只保留最后一个方法。

IL2CPP 泛型共享、方法共享时，多个 managed method 可能指向同一个 native RVA。这样可能丢失：

- 正确的 string 返回签名；
- 正确的类 family；
- 正确的 sink 名称；
- 正确的参数布局。

建议改为：

```python
methods_by_address: dict[int, list[MethodAlias]]
```

把 native body 和 managed alias 分开：

```text
MethodBody
  address
  end
  aliases[]
```

分析指令只做一次，但对相关签名分别解释参数。

---

## 数组、List、Dictionary 是 metadata literal 的大户

以下字符串仍然全都来自 `global-metadata.dat`：

```csharp
string[] difficulties = {
    "Easy",
    "Normal",
    "Hard"
};

label.text = difficulties[index];
```

当前：

- 没有数组元素域；
- 没有集合元素域；
- 遇到寄存器索引形式的 `ldr [base, index]` 会直接丢弃值。

可以先采用索引不敏感模型：

```text
ArrayElements(allocation_site)
    = {"Easy", "Normal", "Hard"}
```

读取任意下标时返回整个集合，并归为：

```text
probable_display_literals
```

如果索引是可解析常量，再升级为 exact。

这对菜单项、错误文本表、状态文本表、教程步骤和 Dropdown 文本的召回提升通常很明显。

---

# 建议的实际修改顺序

| 顺序 | 修改 | 主要收益 |
|---|---|---|
| 1 | 修复缓存版本 | 确保新版真的执行 |
| 2 | 补 `LDR literal` 正式传播 | 修复确定性漏报 |
| 3 | 修复 return-summary 种子和方向 | 覆盖 getter/property/helper 返回文本 |
| 4 | 处理 32 值截断并输出诊断 | 消除 switch/多分支静默漏报 |
| 5 | carrier 支持 literal 直接写字段、静态字段、新对象 | 覆盖 Popup/ViewModel/Builder |
| 6 | wrapper 支持非 void 和 `derived_param` | 覆盖 UI Service、动画、格式加工 |
| 7 | 增加主要代码显示 sink | 覆盖 InputField、TextMesh、UI Toolkit、IMGUI |
| 8 | 修复标准 event 跨方法订阅 | 覆盖回调和消息总线 |
| 9 | 数组/List/Dictionary 元素域 | 覆盖文本表 |
| 10 | 用 metadata slot 验证虚调用 | 同时提高召回和准确率 |

## 总体评价

这版的 carrier family 修改值得保留，它解决的是旧版一个真实的错误关联问题。但它更像是**局部精度修复**，不是召回率架构升级。

在只考虑 `global-metadata.dat` 硬编码文本的前提下，当前最可能造成大量遗漏的不是资源问题，而是：

```text
返回字符串 helper 未纳入摘要
LDR literal 正式分析断链
literal 直接写对象/静态字段
数组和集合
derived wrapper 被终止
事件订阅跨方法
抽象值超过 32 被静默截断
sink 类型数量过少
```

其中缓存、`LDR literal`、return summary 和 32 值截断，建议直接列为下一版的 P0 修复项。
