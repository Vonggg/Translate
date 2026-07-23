from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from support.config import load_config
from pipeline.ai_translation_strategy import get_strategy
from pipeline.shared import atomic_write_json, read_json, write_json


def log(message: str) -> None:
    print(message, flush=True)


def log_green(message: str) -> None:
    print(f"\033[92m{message}\033[0m", flush=True)


def default_response_path(request_path: Path) -> Path:
    name = request_path.name
    if "request" in name:
        return request_path.with_name(name.replace("request", "response", 1))
    match = re.search(r"batch_(\d+)", name)
    if match:
        return request_path.with_name(f"ai_translation_response_batch_{match.group(1)}.json")
    return request_path.with_name("ai_translation_response.json")


def load_request_items(request_path: Path) -> dict[int, str]:
    payload = read_json(request_path)
    messages = payload.get("messages")
    if not isinstance(messages, list):
        raise ValueError(f"request JSON 缺少 messages: {request_path}")
    user_content = ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            user_content = str(message.get("content", ""))
            break
    if not user_content:
        raise ValueError(f"request JSON 没有 user content: {request_path}")
    data = json.loads(user_content)
    items = data.get("items")
    if not isinstance(items, list):
        raise ValueError(f"user content 缺少 items 数组: {request_path}")
    result: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        text = item.get("text")
        if isinstance(item_id, int) and isinstance(text, str):
            result[item_id] = text
    if not result:
        raise ValueError(f"request JSON 没有可用翻译条目: {request_path}")
    return result


def response_content(response_path: Path) -> str:
    data = read_json(response_path)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError(f"response JSON 缺少 choices: {response_path}")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise ValueError(f"response JSON 缺少 choices[0].message: {response_path}")
    return str(message.get("content", ""))


def post_ai_payload(
    payload: dict[str, Any],
    base_url: str,
    api_key: str,
    proxies: dict[str, str],
    timeout: int,
    label: str,
) -> dict[str, Any]:
    import requests

    session = requests.Session()
    session.trust_env = False
    start = time.monotonic()
    response = session.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        proxies=proxies or None,
        timeout=timeout,
    )
    elapsed = int(time.monotonic() - start)
    if response.status_code == 200:
        log_green(f"[AI补批] {label} AI 接口已响应: HTTP {response.status_code}，耗时={elapsed} 秒")
    else:
        log(f"[AI补批] {label} AI 接口已响应: HTTP {response.status_code}，耗时={elapsed} 秒")
    response.raise_for_status()
    return response.json()


def make_batch_payload(cfg: Any, strategy: Any, model: str, batch: list[tuple[int, str]], batch_index: int, batch_count: int) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": strategy.system_prompt()},
            {"role": "user", "content": strategy.user_content(batch, batch_index, batch_count)},
        ],
    }
    payload.update(strategy.extra_payload())
    return payload


def make_combined_response(model: str, translations: dict[int, str], usage_items: list[dict[str, Any]]) -> dict[str, Any]:
    items = [{"id": item_id, "translation": translations[item_id]} for item_id in sorted(translations)]
    content = json.dumps({"items": items}, ensure_ascii=False, separators=(",", ":"))
    usage_total: dict[str, int] = {}
    for usage in usage_items:
        for key, value in usage.items():
            if isinstance(value, int):
                usage_total[key] = usage_total.get(key, 0) + value
    return {
        "object": "chat.completion",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage_total,
    }


def resend_batch(request_path: Path, response_path: Path) -> None:
    cfg = load_config()
    strategy = get_strategy(cfg)
    base_url = cfg.ai_translation_base_url.strip().rstrip("/")
    api_key = cfg.ai_translation_api_key.strip()
    if not (base_url and api_key):
        raise RuntimeError("AI 翻译接口未配置 base_url/api_key，无法重发批次。")

    original_payload = read_json(request_path)
    model = str(original_payload.get("model") or cfg.ai_translation_model).strip()
    if not model:
        raise RuntimeError("request JSON 和 config.json 都没有配置 model。")

    proxies = {
        key: value
        for key, value in {
            "http": cfg.ai_translation_proxy_http.strip(),
            "https": cfg.ai_translation_proxy_https.strip(),
        }.items()
        if value
    }

    log(f"[AI补批] 请求文件: {request_path}")
    log(f"[AI补批] 输出文件: {response_path}")
    log(f"[AI补批] model={model}, base_url={base_url}, strategy={getattr(strategy, 'name', 'custom')}")

    id_to_source = load_request_items(request_path)
    pending_items = list(id_to_source.items())
    sub_batches = strategy.build_batches(pending_items)
    log(
        f"[AI补批] 原批条目={len(pending_items)}，按当前策略拆成 {len(sub_batches)} 个子批，"
        f"单批预计输出预算={getattr(strategy, 'batch_output_budget_chars', 'unknown')}"
    )

    all_translations: dict[int, str] = {}
    usage_items: list[dict[str, Any]] = []
    for sub_index, sub_batch in enumerate(sub_batches, start=1):
        estimated_output = sum(strategy.estimate_output_chars(text) for _item_id, text in sub_batch)
        user_content_size = len(strategy.user_content(sub_batch, sub_index, len(sub_batches)))
        log_green(
            f"[AI补批] 开始子批 {sub_index}/{len(sub_batches)}，"
            f"条目={len(sub_batch)}，输入字符={user_content_size}，预计输出={estimated_output}"
        )
        payload = make_batch_payload(cfg, strategy, model, sub_batch, sub_index, len(sub_batches))
        data = post_ai_payload(payload, base_url, api_key, proxies, cfg.ai_translation_timeout, f"子批 {sub_index}/{len(sub_batches)}")
        finish_reason = ""
        try:
            finish_reason = str(data["choices"][0].get("finish_reason", ""))
        except Exception:
            pass
        if finish_reason == "stop":
            log_green(f"[AI补批] 子批 {sub_index}/{len(sub_batches)} 正常结束: finish_reason=stop")
        elif finish_reason:
            log(f"[AI补批] 子批 {sub_index}/{len(sub_batches)} finish_reason={finish_reason}")
        if finish_reason == "length":
            raise RuntimeError(f"子批 {sub_index}/{len(sub_batches)} 仍被长度截断，请继续降低 ai_translation_max_output_chars 或拆得更小。")
        usage = data.get("usage")
        if isinstance(usage, dict):
            usage_items.append(usage)
        content = str(data["choices"][0]["message"]["content"])
        parsed = strategy.parse_response(content)
        all_translations.update(parsed)
        log(f"[AI补批] 子批 {sub_index}/{len(sub_batches)} 完成，返回={len(parsed)}，累计={len(all_translations)}")

    missing_ids = [item_id for item_id, _text in pending_items if item_id not in all_translations]
    if missing_ids:
        preview = ",".join(str(item_id) for item_id in missing_ids[:30])
        suffix = "..." if len(missing_ids) > 30 else ""
        raise RuntimeError(f"补批后仍缺少 {len(missing_ids)} 个 id: {preview}{suffix}")

    combined = make_combined_response(model, all_translations, usage_items)
    write_json(response_path, combined)
    log_green(f"[AI补批] 已合并写入 response: {response_path}，条目={len(all_translations)}")


def patch_trans_from_response(request_path: Path, response_path: Path, trans_path: Path) -> None:
    cfg = load_config()
    strategy = get_strategy(cfg)
    id_to_source = load_request_items(request_path)
    content = response_content(response_path)
    parsed = strategy.parse_response(content)
    if not parsed:
        raise RuntimeError(f"response 中没有解析到任何翻译: {response_path}")

    trans_data = read_json(trans_path)
    if not isinstance(trans_data, dict):
        raise ValueError(f"trans.json 不是对象: {trans_path}")

    changed = 0
    missing_ids: list[int] = []
    ids_not_in_trans: list[int] = []
    for item_id, source_text in id_to_source.items():
        translated = parsed.get(item_id)
        if not translated:
            missing_ids.append(item_id)
            continue
        if source_text not in trans_data:
            ids_not_in_trans.append(item_id)
            continue
        if trans_data.get(source_text) != translated:
            trans_data[source_text] = translated
            changed += 1

    atomic_write_json(trans_path, trans_data)
    log_green(f"[AI补批] 已修补 trans.json: 写入/更新={changed}，response解析={len(parsed)}，request条目={len(id_to_source)}")
    if missing_ids:
        preview = ",".join(str(item_id) for item_id in missing_ids[:30])
        suffix = "..." if len(missing_ids) > 30 else ""
        log(f"[AI补批][提示] response 未返回的 id: {len(missing_ids)} 个，前几个: {preview}{suffix}")
    if ids_not_in_trans:
        preview = ",".join(str(item_id) for item_id in ids_not_in_trans[:30])
        suffix = "..." if len(ids_not_in_trans) > 30 else ""
        log(f"[AI补批][提示] request 文本不在 trans.json 中: {len(ids_not_in_trans)} 个，前几个 id: {preview}{suffix}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="重发单个 AI 翻译批次，并可用返回结果修补 trans.json。")
    parser.add_argument("mode", choices=["resend", "patch-trans", "resend-and-patch"], help="执行模式")
    parser.add_argument("--request", required=True, type=Path, help="ai_translation_request_batch_XXX.json")
    parser.add_argument("--response", type=Path, help="ai_translation_response_batch_XXX.json；不填则按 request 文件名自动推导")
    parser.add_argument("--trans", type=Path, help="trans.json；不填则使用当前 config 的 workspace/records/trans.json")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    request_path = args.request.resolve()
    response_path = (args.response or default_response_path(request_path)).resolve()
    cfg = load_config()
    trans_path = (args.trans or (cfg.stage_record_dir / cfg.output_trans_json)).resolve()

    if args.mode in {"resend", "resend-and-patch"}:
        resend_batch(request_path, response_path)
    if args.mode in {"patch-trans", "resend-and-patch"}:
        patch_trans_from_response(request_path, response_path, trans_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
