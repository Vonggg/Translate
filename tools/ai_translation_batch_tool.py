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
from pipeline.codex_cli_provider import (
    codex_display_name,
    codex_transport_models,
    is_codex_transport,
    request_structured_output,
    translation_schema,
)
from pipeline.shared import atomic_write_json, read_json, write_json
from pipeline.translation import _ai_translation_transport_chain

_REQUEST_JSON_DELIMITER_TYPO = re.compile(
    r'("(?:items|id|translation|text)")\s*[=>]\s*(?=[\[\{"\-0-9tfn])'
)
MAX_MISSING_RETRY_ROUNDS = 3


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
    try:
        payload = read_json(request_path)
    except (OSError, json.JSONDecodeError):
        raw = request_path.read_text(encoding="utf-8-sig")
        payload = _extract_request_file_payload(raw, request_path)
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
    data = _extract_request_payload_json(user_content, request_path)
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


def _extract_request_file_payload(raw_content: str, request_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(raw_content)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError as original_error:
        start = raw_content.find("{")
        end = raw_content.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"request JSON 无法解析: {request_path}") from original_error

        fragment = raw_content[start : end + 1]
        try:
            data = json.loads(fragment)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            repaired = _REQUEST_JSON_DELIMITER_TYPO.sub(r"\1:", fragment)
            data = json.loads(repaired)
            if isinstance(data, dict):
                return data

    raise ValueError(f"request JSON 无法解析: {request_path}")


def _extract_request_payload_json(content: str, request_path: Path) -> dict[str, Any]:
    """Extract the inner request payload object from message content, tolerating minor corruption."""
    raw = content.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError as original_error:
        repaired = _REQUEST_JSON_DELIMITER_TYPO.sub(r"\1:", raw)
        if repaired != raw:
            try:
                data = json.loads(repaired)
            except json.JSONDecodeError:
                data = None
            else:
                if isinstance(data, dict):
                    return data

        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"request JSON 无法提取对象: {request_path}") from original_error

        fragment = raw[start:end + 1]
        try:
            data = json.loads(fragment)
        except json.JSONDecodeError:
            repaired_fragment = _REQUEST_JSON_DELIMITER_TYPO.sub(r"\1:", fragment)
            data = json.loads(repaired_fragment)

        if isinstance(data, dict):
            return data

    raise ValueError(f"request JSON 解析失败: {request_path}")


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


def _archive_raw_response(response_path: Path, data: dict[str, Any]) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    for sequence in range(1, 1000):
        raw_path = response_path.with_name(
            f"{response_path.stem}_raw_{stamp}_{sequence:03d}.json"
        )
        if not raw_path.exists():
            write_json(raw_path, data)
            log(f"[AI补批] 原始响应已保存: {raw_path}")
            return raw_path
    raise RuntimeError(f"无法为原始响应分配存档文件名: {response_path.parent}")


def _response_message_content(data: dict[str, Any], label: str) -> tuple[str, str]:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            raise RuntimeError(f"{label} AI 服务返回错误: {error['message']}")
        raise RuntimeError(f"{label} response 缺少 choices")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict):
        raise RuntimeError(f"{label} response 缺少 choices[0].message")
    return str(message.get("content", "")), str(choice.get("finish_reason", ""))


def resend_batch(
    request_path: Path,
    response_path: Path,
    codex_circuit_file: Path | None = None,
) -> None:
    cfg = load_config(quiet=True)
    strategy = get_strategy(cfg)
    configured_transport = str(
        getattr(cfg, "ai_translation_transport", "http") or "http"
    ).strip().lower()
    transports = _ai_translation_transport_chain(cfg)
    models = codex_transport_models(
        str(getattr(cfg, "ai_translation_codex_model", "gpt-5.3-codex-spark"))
    )
    models["http"] = str(cfg.ai_translation_model).strip()
    disabled_codex_models: set[str] = set()
    if codex_circuit_file is not None and codex_circuit_file.is_file():
        try:
            circuit_data = read_json(codex_circuit_file)
            disabled_codex_models.update(
                str(model)
                for model in circuit_data.get("disabled_models", [])
                if str(model).strip()
            )
            if circuit_data.get("open") and not disabled_codex_models:
                disabled_codex_models.update(
                    models[item] for item in transports if is_codex_transport(item)
                )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            disabled_codex_models.update(
                models[item] for item in transports if is_codex_transport(item)
            )
    original_transports = list(transports)
    transports = [
        item for item in transports
        if not (is_codex_transport(item) and models[item] in disabled_codex_models)
    ]
    codex_skipped_by_circuit = len(transports) != len(original_transports)
    if codex_skipped_by_circuit:
        log(
            "[AI补批][模型熔断] 跳过本次批量操作中已失败的 Codex 模型: "
            + ", ".join(sorted(disabled_codex_models))
        )
    base_url = cfg.ai_translation_base_url.strip().rstrip("/")
    api_key = cfg.ai_translation_api_key.strip()
    if not transports:
        raise RuntimeError("Codex CLI 和 DeepSeek 均不可用或配置不完整，无法重发批次。")

    original_payload = read_json(request_path)
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
    log(
        f"[AI补批] chain={' -> '.join(transports)}, "
        f"strategy={getattr(strategy, 'name', 'custom')}"
    )
    if (
        configured_transport == "codex_cli"
        and transports[0] == "http"
        and not codex_skipped_by_circuit
    ):
        log("[AI补批][AI回退] Codex CLI 不可用，直接切换到 DeepSeek。")

    id_to_source = load_request_items(request_path)
    pending_items = list(id_to_source.items())
    sub_batches = strategy.build_batches(pending_items)
    log(
        f"[AI补批] 原批条目={len(pending_items)}，按当前策略拆成 {len(sub_batches)} 个子批，"
        f"单批预计输出预算={getattr(strategy, 'batch_output_budget_chars', 'unknown')}"
    )

    all_translations: dict[int, str] = {}
    usage_items: list[dict[str, Any]] = []
    last_model = models[transports[0]]
    last_error: Exception | None = None

    def request_sub_batch(
        sub_batch: list[tuple[int, str]],
        label: str,
        sub_index: int,
        sub_count: int,
        request_transport: str,
    ) -> dict[int, str]:
        nonlocal last_model
        model = models[request_transport]
        last_model = model
        estimated_output = sum(strategy.estimate_output_chars(text) for _item_id, text in sub_batch)
        user_content_size = len(strategy.user_content(sub_batch, sub_index, sub_count))
        log_green(
            f"[AI补批] 开始{label}，"
            f"条目={len(sub_batch)}，输入字符={user_content_size}，预计输出={estimated_output}"
        )
        payload = make_batch_payload(cfg, strategy, model, sub_batch, sub_index, sub_count)
        if is_codex_transport(request_transport):
            started_at = time.monotonic()
            structured, usage = request_structured_output(
                model=model,
                reasoning_effort=str(getattr(cfg, "ai_translation_codex_reasoning_effort", "low")),
                system_prompt=strategy.system_prompt(),
                user_content=payload["messages"][1]["content"],
                output_schema=translation_schema(),
                timeout=cfg.ai_translation_timeout,
                working_directory=cfg.root_dir,
            )
            data = {
                "object": "codex.cli.response",
                "model": model,
                "provider": "codex_cli",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(structured, ensure_ascii=False),
                    },
                    "finish_reason": "stop",
                }],
                "usage": usage,
            }
            elapsed = int(time.monotonic() - started_at)
            log_green(
                f"[AI补批] {label} {codex_display_name(request_transport, model)} "
                f"已响应，耗时={elapsed} 秒"
            )
        else:
            data = post_ai_payload(
                payload,
                base_url,
                api_key,
                proxies,
                cfg.ai_translation_timeout,
                label,
            )
        _archive_raw_response(response_path, data)
        content, finish_reason = _response_message_content(data, label)
        if finish_reason == "stop":
            log_green(f"[AI补批] {label} 正常结束: finish_reason=stop")
        elif finish_reason:
            log(f"[AI补批] {label} finish_reason={finish_reason}")
        if finish_reason == "length":
            raise RuntimeError(f"{label} 仍被长度截断，请继续降低 ai_translation_max_output_chars 或拆得更小。")
        usage = data.get("usage")
        if isinstance(usage, dict):
            usage_items.append(usage)
        parsed = strategy.parse_response(content)
        expected_ids = {item_id for item_id, _text in sub_batch}
        accepted = {
            item_id: translation
            for item_id, translation in parsed.items()
            if item_id in expected_ids and translation
        }
        unexpected_count = len(parsed) - len(accepted)
        if unexpected_count:
            log(f"[AI补批][提示] {label} 忽略了 {unexpected_count} 个非本批或无效 id")
        return accepted

    for transport_index, request_transport in enumerate(transports):
        missing_items = [
            (item_id, source_text)
            for item_id, source_text in pending_items
            if item_id not in all_translations
        ]
        if not missing_items:
            break
        display_name = (
            codex_display_name(request_transport, models[request_transport])
            if is_codex_transport(request_transport)
            else "DeepSeek"
        )
        if transport_index > 0:
            previous = transports[transport_index - 1]
            previous_name = (
                codex_display_name(previous, models[previous])
                if is_codex_transport(previous)
                else "DeepSeek"
            )
            log(
                f"[AI补批][AI回退] {previous_name} 仍缺少 {len(missing_items)} 条，"
                f"切换到 {display_name}；同样最多重试 {MAX_MISSING_RETRY_ROUNDS} 次。"
            )
        codex_transport_failed = False
        for retry_round in range(0, MAX_MISSING_RETRY_ROUNDS + 1):
            missing_items = [
                (item_id, source_text)
                for item_id, source_text in pending_items
                if item_id not in all_translations
            ]
            if not missing_items:
                break
            retry_batches = strategy.build_batches(missing_items)
            attempt_label = (
                "首轮"
                if retry_round == 0
                else f"重试 {retry_round}/{MAX_MISSING_RETRY_ROUNDS}"
            )
            before_count = len(all_translations)
            for retry_index, retry_batch in enumerate(retry_batches, start=1):
                label = (
                    f"{display_name} {attempt_label} "
                    f"子批 {retry_index}/{len(retry_batches)}"
                )
                try:
                    parsed = request_sub_batch(
                        retry_batch,
                        label,
                        retry_index,
                        len(retry_batches),
                        request_transport,
                    )
                except Exception as exc:
                    last_error = exc
                    log(f"[AI补批][AI重试] {label}失败: {exc}")
                    if is_codex_transport(request_transport):
                        codex_transport_failed = True
                        disabled_codex_models.add(models[request_transport])
                        if codex_circuit_file is not None:
                            atomic_write_json(
                                codex_circuit_file,
                                {
                                    "open": not any(
                                        is_codex_transport(item)
                                        and models[item] not in disabled_codex_models
                                        for item in transports[transport_index + 1 :]
                                    ),
                                    "disabled_models": sorted(disabled_codex_models),
                                    "reason": str(exc),
                                },
                            )
                        remaining = transports[transport_index + 1 :]
                        next_name = (
                            codex_display_name(remaining[0], models[remaining[0]])
                            if remaining and is_codex_transport(remaining[0])
                            else "DeepSeek" if "http" in remaining else "失败"
                        )
                        log(
                            f"[AI补批][模型熔断] {display_name} 请求失败；"
                            f"本批切换到 {next_name}，后续批次仅跳过这个已失败模型。"
                        )
                        break
                    continue
                all_translations.update(parsed)
                log(
                    f"[AI补批] {label} 完成，返回={len(parsed)}，累计={len(all_translations)}"
                )
            if len(all_translations) == before_count:
                log(f"[AI补批][AI重试] {display_name} {attempt_label}没有补回任何条目")
            if codex_transport_failed:
                break

    missing_ids = [item_id for item_id, _text in pending_items if item_id not in all_translations]
    if missing_ids:
        preview = ",".join(str(item_id) for item_id in missing_ids[:30])
        suffix = "..." if len(missing_ids) > 30 else ""
        error_suffix = f"；最后错误: {last_error}" if last_error is not None else ""
        raise RuntimeError(
            f"经过 {MAX_MISSING_RETRY_ROUNDS} 轮缺失重试后仍缺少 "
            f"{len(missing_ids)} 个 id: {preview}{suffix}；原始响应已单独留档"
            f"{error_suffix}。"
        )

    combined = make_combined_response(last_model, all_translations, usage_items)
    write_json(response_path, combined)
    log_green(f"[AI补批] 已合并写入 response: {response_path}，条目={len(all_translations)}")


def patch_trans_from_response(request_path: Path, response_path: Path, trans_path: Path) -> None:
    cfg = load_config(quiet=True)
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
    parser.add_argument(
        "--codex-circuit-file",
        type=Path,
        help="同一次多批操作共享的 Codex 熔断标记文件。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    request_path = args.request.resolve()
    response_path = (args.response or default_response_path(request_path)).resolve()
    cfg = load_config(quiet=True)
    trans_path = (args.trans or (cfg.stage_record_dir / cfg.output_trans_json)).resolve()

    if args.mode in {"resend", "resend-and-patch"}:
        resend_batch(request_path, response_path, args.codex_circuit_file)
    if args.mode in {"patch-trans", "resend-and-patch"}:
        patch_trans_from_response(request_path, response_path, trans_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
