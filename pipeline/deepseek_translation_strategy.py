from __future__ import annotations

from typing import Any

from .ai_translation_strategy import DefaultAITranslationStrategy


class DeepSeekTranslationStrategy(DefaultAITranslationStrategy):
    name = "deepseek"

    @property
    def batch_max_chars(self) -> int:
        # Input-side context budget. Output budget is controlled separately by
        # ai_translation_max_output_chars / 6 in the base strategy.
        configured = int(getattr(self.cfg, "ai_translation_batch_max_chars", 1200000) or 1200000)
        return max(10000, configured)

    def system_prompt(self) -> str:
        return (
            "你是游戏逆向汉化翻译助手。输入 JSON 中的 items[].text 都是从游戏资源中导出的文本，"
            "目标是制作简体中文汉化，不是只翻译英文；英文、日文、韩文、俄文、繁体中文和其它语言都要翻译成简体中文。"
            "繁体中文必须转换为简体中文，不能因为已经是中文就原样保留。"
            "如果 items[].text（原始键）本身含有中文，translation 中的所有中文字符也必须是简体中文，不得夹杂繁体字。"
            "语言名称也要汉化，例如 Español 译为西班牙语、Français 译为法语、日本語译为日语、한국어译为韩语。"
            "如果不同语言文本表达的是同一句话或同一个 UI 含义，要翻译成一致的简体中文说法。"
            "保留 id，不要新增、删除、合并、重排项目。"
            "保留换行、占位符、数字、货币符号、格式控制符和富文本标签。"
            "短 UI 文本要自然、紧凑，适合按钮、菜单和弹窗。"
            "如果输入项包含 context，它只用于说明文本出现的函数、显示组件和拼接方式，"
            "不得翻译或输出 context。"
            "只返回严格 JSON，格式为 {\"items\":[{\"id\":数字,\"translation\":\"译文\"}]}。"
            "必须严格使用 JSON 属性分隔符：每个 id、translation、items 键后都必须是英文冒号 :，"
            "绝不能误写成 >、=，也不能漏掉冒号；相邻对象之间必须使用英文逗号分隔。"
            "译文需要引号时优先使用中文引号“”或‘’，例如 <color=blue>“服务”</color>；"
            "如果必须使用英文双引号，必须按 JSON 规则转义为 \\\"，绝不能在 translation 字符串中输出未转义的英文双引号。"
            "返回前必须检查整个响应可以被标准 JSON 解析器直接解析。"
            "禁止输出 JSON 以外的任何内容；禁止解释、注释、Markdown、代码块、推理过程、注意事项、总结或示例。"
            "如果不确定某条译文，也必须直接给出最合适的简体中文译文，不要写括号说明。"
        )

    def extra_payload(self) -> dict[str, Any]:
        return {"temperature": 0, "response_format": {"type": "json_object"}}


def create_strategy(cfg: Any) -> DeepSeekTranslationStrategy:
    return DeepSeekTranslationStrategy(cfg)
