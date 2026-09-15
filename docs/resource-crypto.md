# 可选资源加密适配

## AI 辅助分析（2026-09-15）

未知资源包可复用现有翻译 AI 的 transport、模型、地址、密钥、代理与超时；使用 `pipeline/resource_crypto_ai.py` 中独立的分析提示词，不执行翻译策略。未启用 AI、没有可用配置或请求失败时自动跳过，继续导出正常资源，保留末尾疑似加密/不支持格式提示。`resource_crypto.json` 可用 `ai_analysis: false` 单独关闭 AI。

当前发送的是限定证据：最多 12 个异常资源的长度、前 64 字节、前 4 KiB 的熵统计，catalog 加载器声明、包名及有限的相关 dump.cs 声明。熵函数复用步骤 4 的扫描模块；未将该扫描器的全部字段扫描结果直接当作加密结论。完整 DLL、SO、metadata 不自动上传，也不将路径当作文件内容。

AI 职责为区分版本、压缩/编码、损坏和加密，给出证据、算法、实际密钥候选和 `needed_files`。证据不足只记录尚需哪些文件/函数内容；当前不会自动读取 AI 指定的任意路径，也不会递归上传它索取的文件。建议记录在工作区 `resource_state/resource_crypto_ai_report.json`，相同证据复用结果。

只有 AI 给出加密证据和已支持算法的密钥候选时，才交给本地适配器验证；本地完整验证通过后才修改暂存。AI 的高置信度不是解密成功证明，不执行 AI 返回的代码。已知方案本地验证成功的资源无需调用 AI。

本次 AI 接入进行了模拟接口测试（无配置、失败、缓存、候选白名单），未进行真实付费接口调用或声称 AI 已自动取得新密钥。

## 结构与边界

`pipeline/resource_crypto.py` 的 `ALGORITHMS` 注册算法；`load_candidates` 独立提供密钥参数。目前唯一算法为整个文件从偏移 0 开始的循环 XOR；局部 XOR、带偏移的 XOR、AES 等是不同变换，不能仅更换密钥来假装兼容。

自动候选：`UTF8(UpperHex(MD5(ASCII(Android package))))`。也可通过工作区配置指定任意 XOR 密钥字节。不依赖游戏名、固定包名、固定 DLL 地址或固定密钥。其他密钥派生方式尚未自动提取，已知密钥可直接配置。

若渠道打包另行更改了游戏运行时的 Application.identifier，派生密钥也可能改变。本次验证只覆盖原包名；不能将“原密钥重新加密成功”视为改包名后的运行保证。

导出只修改 input_sources 暂存副本。必须通过 UnityFS 文件头、声明大小、解压及目录/对象读取、加密往返一致性检查才写入暂存；可识别但损坏的包使流程失败。未知方案不修改并保留扫描警告。解析对象数量不等于所有 MonoBehaviour 业务模板都完整可用。

导出参数及 SHA256 保存在路径映射的 `resource_crypto` 中。包名/设置改变使暂存缓存失效。导入先用明文进行 catalog 处理，然后使用导出记录的密钥重新加密，再分卷、回打 OBB 和同步。catalog 回写失败的加密 Addressables 结果禁止继续同步。重复加密不会静默执行：输入必须是有效明文 UnityFS。

JSON catalog 兼容补充：按 entry 表的 extra-data 偏移关联资源真实 InternalId，支持空 Hash、CRC=0、内部 BundleName 与文件名不一致的目录。保留唯一性和原始大小检查，不按大小猜测映射，也不修改 Provider。

## 战地闯关离线验证（2026-09-14）

- 加载代码 `EncryptedBundleUtils.DecryptXOR` 确认 MD5(Application.identifier) → 大写十六进制字符串 → UTF8 → 全文件循环 XOR。
- 262 个 Bundle 全部成功解析，共 871,689 个对象；原样重新加密与各自原文件逐字节相同，原文件哈希不变。
- 用现有 .NET CLI 导出 `scenes_scenes_all_4ceb0f1c0245a5fb468063d706e90943.bundle`，选取 PathID=4715 的 TMP 文本 `SPIN` → `转动测试`，通过现有导入器重打。
- catalog 自校验通过，准确命中 1 条，更新大小、保持 CRC=0；之后重新加密并解密复查。
- 6,731 个对象的身份集合不变；逐对象原始字节比较只有 PathID=4715 改变；目标文本读回正确。
- 测试目录：`temp/crypto-battle-validation-20260914`，全量结果 `verification.json`，修改闭环结果 `modified_roundtrip.json`。`smoke_FinalResult` 仅为测试包与配套 catalog，不是完整汉化结果；不要当作正式全量输出。
- 尚未手机/模拟器运行验收。未覆盖游戏文件、未修改正式翻译字典、未调用翻译 API；中文字形是否显示仍取决于正常字体替换流程。

全量资源离线验证脚本：`tools/verify_resource_crypto.py --game <解包游戏目录> --output <不存在的新目录>`。仅写新测试目录，不覆盖源文件。暂存副本占用额外磁盘空间，测试目录结束后可删除。
