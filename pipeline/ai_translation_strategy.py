from __future__ import annotations

import importlib
import json
from typing import Any


DEFAULT_BATCH_MAX_CHARS = 1200000
DEFAULT_MAX_OUTPUT_CHARS = 384000


def get_strategy(cfg: Any) -> Any:
    strategy_name = str(getattr(cfg, "ai_translation_strategy", "default") or "default").strip().lstrip("@")
    if strategy_name in {"", "default", "none"}:
        return DefaultAITranslationStrategy(cfg)

    strategy_path = strategy_name.replace("\\", "/").rsplit("/", 1)[-1]
    module_stem = strategy_path[:-3] if strategy_path.endswith(".py") else strategy_path
    module_name = f"pipeline.{module_stem}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        return DefaultAITranslationStrategy(cfg)
    if hasattr(module, "create_strategy"):
        return module.create_strategy(cfg)
    if hasattr(module, "DeepSeekTranslationStrategy"):
        return module.DeepSeekTranslationStrategy(cfg)
    return DefaultAITranslationStrategy(cfg)


class DefaultAITranslationStrategy:
    name = "default"

    def __init__(self, cfg: Any) -> None:
        self.cfg = cfg

    @property
    def batch_max_chars(self) -> int:
        return max(10000, int(getattr(self.cfg, "ai_translation_batch_max_chars", DEFAULT_BATCH_MAX_CHARS) or DEFAULT_BATCH_MAX_CHARS))

    @property
    def batch_output_budget_chars(self) -> int:
        max_output = int(getattr(self.cfg, "ai_translation_max_output_chars", DEFAULT_MAX_OUTPUT_CHARS) or DEFAULT_MAX_OUTPUT_CHARS)
        return max(10000, max_output // 6)

    def estimate_output_chars(self, source_text: str) -> int:
        return len(source_text) + 64

    def build_batches(self, pending_items: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
        batches: list[list[tuple[int, str]]] = []
        current: list[tuple[int, str]] = []
        current_input_size = 0
        current_output_size = 0

        for index, source_text in pending_items:
            item_size = len(json.dumps({"id": index, "text": source_text}, ensure_ascii=False)) + 2
            output_size = self.estimate_output_chars(source_text)
            if current and (
                current_input_size + item_size > self.batch_max_chars
                or current_output_size + output_size > self.batch_output_budget_chars
            ):
                batches.append(current)
                current = []
                current_input_size = 0
                current_output_size = 0
            current.append((index, source_text))
            current_input_size += item_size
            current_output_size += output_size

        if current:
            batches.append(current)
        return batches

    def system_prompt(self) -> str:
        return (
            "你是游戏逆向汉化翻译助手。输入 JSON 中的 items[].text 都是从游戏资源中导出的文本，"
            "目标是制作简体中文汉化，不是只翻译英文；英文、日文、韩文、俄文、繁体中文和其它语言都要翻译成简体中文。"
            "繁体中文必须转换为简体中文，不能因为已经是中文就原样保留。"
            "语言名称也要汉化，例如 Español 译为西班牙语、Français 译为法语、日本語译为日语、한국어译为韩语。"
            "如果不同语言文本表达的是同一句话或同一个 UI 含义，要翻译成一致的简体中文说法。"
            "保留 id，不要新增、删除、合并、重排项目。"
            "保留换行、占位符、数字、货币符号、格式控制符和富文本标签。"
            "只返回严格 JSON，格式为 {\"items\":[{\"id\":数字,\"translation\":\"译文\"}]}。"
            "译文需要引号时优先使用中文引号“”或‘’，例如 <color=blue>“服务”</color>；"
            "如果必须使用英文双引号，必须按 JSON 规则转义为 \\\"，绝不能在 translation 字符串中输出未转义的英文双引号。"
            "返回前必须检查整个响应可以被标准 JSON 解析器直接解析。"
            "禁止输出 JSON 以外的任何内容；禁止解释、注释、Markdown、代码块、推理过程、注意事项、总结或示例。"
            "如果不确定某条译文，也必须直接给出最合适的简体中文译文，不要写括号说明。"
        )

    def user_content(self, batch: list[tuple[int, str]], batch_index: int, batch_count: int) -> str:
        items = [{"id": index, "text": source_text} for index, source_text in batch]
        return json.dumps({"items": items}, ensure_ascii=False, separators=(",", ":"))

    def extra_payload(self) -> dict[str, Any]:
        return {"temperature": 0, "response_format": {"type": "json_object"}}

    def parse_response(self, content: str) -> dict[int, str]:
        result = _extract_json_object(content)
        translated: dict[int, str] = {}
        for item in result.get("items", []):
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            translation = item.get("translation")
            if isinstance(item_id, int) and isinstance(translation, str) and translation:
                translated[item_id] = translation
        return translated


def _extract_json_object(content: str) -> dict[str, Any]:
    content = content.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            raise
        data = json.loads(content[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("AI translation response is not a JSON object.")
    return data
