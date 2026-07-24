from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

from support.config import load_config
from pipeline.font_ttf import build_ttf_replacements
from pipeline.manifest_index import tmp_manifest_index_path
from pipeline.tmp_pipeline import (
    build_merged_tmp_chars,
    launch_unity_tmp_generator,
    prepare_generated_tmp_import_replacements,
)
from pipeline.translation import (
    apply_ai_field_selection_to_records,
    disable_translated_text_effect_components,
    export_translated_files,
    rebuild_game_text_outputs,
    scan_and_record,
    translate_from_scan_records,
)


CONFIG_PATH = Path("config.json")
SDF_FINALIZE_ARGUMENT = "--finish-sdf"
RUN_STEP_ARGUMENT = "--run-step"
UNITY_NATIVE_CRASH_CODES = {0xC0000005, 0xFFFFFFFF}
UNITY_GENERATOR_ENTRY_MARKER = "[TMP] Generator entry reached"


def prompt_input(message: str) -> str:
    return input(f"\033[96m{message}\033[0m")


def scan_generated_artifact_paths(cfg) -> list[Path]:
    paths = [
        cfg.stage_record_dir / cfg.output_scan_records_json,
        cfg.stage_record_dir / cfg.output_ids_json,
        cfg.stage_record_dir / cfg.output_font_map_json,
        cfg.stage_record_dir / cfg.output_material_map_json,
        cfg.stage_record_dir / cfg.output_ref_map_json,
        cfg.stage_record_dir / cfg.output_path_id_map_json,
        cfg.scan_state_path,
        cfg.scan_cache_path,
        tmp_manifest_index_path(cfg),
    ]
    if cfg.enable_ai_field_review:
        paths.extend(
            [
                cfg.stage_record_dir / cfg.output_string_field_stats_json,
                cfg.stage_record_dir / cfg.output_string_field_stats_tsv,
                cfg.stage_record_dir / cfg.output_string_field_review_txt,
            ]
        )
    return paths


def clean_scan_artifacts(cfg) -> int:
    removed = 0
    for path in scan_generated_artifact_paths(cfg):
        if path.is_dir():
            shutil.rmtree(path)
            removed += 1
        elif path.is_file():
            path.unlink()
            removed += 1
    cfg.record_dir.mkdir(parents=True, exist_ok=True)
    return removed


def maybe_clean_scan_records(cfg) -> None:
    existing_paths = [path for path in scan_generated_artifact_paths(cfg) if path.exists()]
    if not existing_paths:
        cfg.record_dir.mkdir(parents=True, exist_ok=True)
        return

    confirm = prompt_input(
        f"扫描产物已存在（{len(existing_paths)} 项），是否清理后重新扫描？"
        " 输入 y 确认，其它任意键取消: "
    ).strip().lower()
    if confirm == "y":
        removed = clean_scan_artifacts(cfg)
        print(f"[清理] 已清理扫描产物: {removed} 项；未触碰其它 records 文件。")
    else:
        print("已取消清空，继续保留现有扫描记录。")
        print()


def existing_tmp_chars_path(cfg) -> Path | None:
    path = cfg.stage_record_dir / cfg.output_tmp_chars_txt
    if path.is_file():
        return path
    print(f"[TMP] 未找到已生成字符文件: {path}")
    print("[TMP] 请先执行菜单 7 生成 tmp_chars.txt，再执行菜单 8。")
    return None


def _unity_log_path(cfg) -> Path:
    return cfg.root_dir / "workspace" / "logs" / "tmp_font_unity.log"


def _is_unity_startup_crash(cfg, return_code: int) -> bool:
    normalized_code = return_code & 0xFFFFFFFF
    if normalized_code not in UNITY_NATIVE_CRASH_CODES:
        return False
    log_path = _unity_log_path(cfg)
    if not log_path.is_file():
        return True
    try:
        log_text = log_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return True
    return UNITY_GENERATOR_ENTRY_MARKER not in log_text


def _clear_stale_unity_lock(cfg) -> bool:
    lock_path = cfg.unity_font_project / "Temp" / "UnityLockfile"
    if not lock_path.exists():
        return True
    try:
        lock_path.unlink()
    except OSError as exc:
        print(f"[TMP][重试失败] UnityLockfile 仍被占用，未删除: {lock_path} ({exc})")
        print("[TMP][重试失败] 请关闭正在使用该辅助工程的 Unity 后，再单独执行菜单 8。")
        return False
    print(f"[TMP][重试] 已清理失效 UnityLockfile: {lock_path}")
    return True


def _run_sdf_finalize_in_fresh_process(cfg) -> int:
    tmp_chars_path = existing_tmp_chars_path(cfg)
    if tmp_chars_path is None:
        return 1

    print(f"[TMP] 独立进程使用字符文件: {tmp_chars_path}")
    result = launch_unity_tmp_generator(cfg, tmp_chars_path)
    if result != 0 and _is_unity_startup_crash(cfg, result):
        print(
            f"[TMP][重试] 检测到 Unity 在进入字体生成前原生崩溃，"
            f"返回码={result}，将清理失效锁后重试一次。"
        )
        if not _clear_stale_unity_lock(cfg):
            return 1
        result = launch_unity_tmp_generator(cfg, tmp_chars_path)

    if result != 0:
        print(f"[TMP][停止] Unity TMP 字体生成失败，返回码={result}；不会继续执行步骤 9。")
        return 1

    prepare_generated_tmp_import_replacements(cfg)
    print("[完成] 独立进程已完成步骤 8 和步骤 9。")
    return 0


def _run_noninteractive_step(cfg, step: str) -> int:
    if step == "0":
        return scan_and_record(cfg) or 0
    if step == "1":
        return apply_ai_field_selection_to_records(cfg) or 0
    if step == "2":
        return translate_from_scan_records(cfg) or 0
    if step == "4":
        return export_translated_files(cfg) or 0
    if step == "5":
        return disable_translated_text_effect_components(cfg) or 0
    if step == "6":
        build_ttf_replacements(cfg)
        return 0
    if step == "7":
        build_merged_tmp_chars(cfg)
        return 0
    print(f"[全部执行][停止] 不支持的内部步骤: {step}")
    return 1


def _run_full_pipeline_in_isolated_processes(cfg) -> int:
    script_path = Path(__file__).resolve()
    steps = ["0"]
    if cfg.enable_ai_field_review:
        steps.append("1")
    steps.extend(["2", "4", "5", "6", "7"])

    for index, step in enumerate(steps, start=1):
        print(f"[全部执行] 启动独立步骤 {step}（{index}/{len(steps)}）")
        sys.stdout.flush()
        result = subprocess.run(
            [sys.executable, str(script_path), RUN_STEP_ARGUMENT, step],
            cwd=str(cfg.root_dir),
        )
        if result.returncode != 0:
            print(f"[全部执行][停止] 步骤 {step} 失败，返回码={result.returncode}。")
            return 1

    print("[全部执行] 步骤 0-7 已完成，启动独立进程执行步骤 8 和步骤 9。")
    sys.stdout.flush()
    result = subprocess.run(
        [sys.executable, str(script_path), SDF_FINALIZE_ARGUMENT],
        cwd=str(cfg.root_dir),
    )
    if result.returncode != 0:
        print(f"[全部执行][停止] 步骤 8-9 失败，返回码={result.returncode}。")
        return 1
    print("[完成] 脚本 10 全部步骤执行结束。")
    return 0


def print_menu() -> None:
    print("Font Generator Menu")
    print("详细说明:")
    print("  0: 读取 workspace/input 下导出的 JSON；生成文本、字体、材质、引用索引；")
    print("     若 enable_ai_field_review=false，按 text_keys 白名单生成 records.json。")
    print("     若 enable_ai_field_review=true，按 string_field_blacklist 黑名单排除后记录所有字符串，")
    print("     同时生成完整 string_field_stats.json/tsv，以及发送给 AI 的 string_field_review.txt/json。")
    print("     输出 workspace/records/records.json、ids.json、font_map.json、material_map.json、ref_map.json，")
    print("     如果资源导出阶段已生成 file_id_map.json，会用它解析外部 file_id 材质引用。")
    print("     另生成 tmp_manifest_index.json，供后续文本、TMP、TTF 替换定位资源。")
    print("  1: 仅在 enable_ai_field_review=true 时执行；读取 AI 返回字段，")
    print("     若配置了 AI 接口则自动判断；未配置或访问失败则提示人工使用 string_field_review.txt 询问 AI。")
    print("     最后按字段名过滤 records.json，删除无关字段记录。")
    print("  2: 读取过滤后的 records.json；对其中原文去重并翻译；")
    print("     输出 trans.json、game.txt、game_chars.txt、mapping.tsv 等文本记录文件。")
    print("  3: 只读取已有 trans.json；重新生成 game.txt 和 game_chars.txt；")
    print("     用于手动修改 trans.json 后刷新文本/字符清单，不重新扫描也不重新翻译。")
    print("  4: 读取 records.json 和 trans.json；只处理 trans.json 命中的待汉化源 JSON；")
    print("     把翻译写入 workspace/input 的资源副本结构，输出到 workspace/output/Text。")
    print("  5: 根据 records.json 的译文 GameObject、ref_map.json 被引用表和 path_id_map.json；")
    print("     找到同物体上的 Shadow/Outline 组件，输出屏蔽后的 JSON 到 workspace/output/Text。")
    print("  6: 读取导出的 Legacy TTF/OTF 信息和固定模板字体；")
    print("     生成 workspace/output/Font/TTF/ToImport 下的 TTF 待导入替换文件。")
    print("  7: 检查 trans.json 译文字符是否被模板 TTF 和老工具 SDF 模板支持；")
    print("     模板 TTF 缺译文字符会输出 translation_chars_missing_from_ttf.tsv 并停止；")
    print("     老工具 SDF 模板缺译文字符只输出提示文件，不中断后续流程。")
    print("     再合并原游戏字体字符、译文字符、模板 TTF 非中文字符和老工具 SDF 模板全部字符，")
    print("     删除模板 TTF 不支持字符后生成 tmp_chars.txt。")
    print("  8: 读取 tmp_chars.txt；调用 Unity 辅助工程生成 TMP/SDF 字体资源；")
    print("     输出 workspace/output/Font/SDF/generated_templates/generated_tmp_font.*。")
    print("  9: 读取脚本 8 已生成的 generated_tmp_font.json/png 和脚本 0 的字体索引；")
    print("     生成 workspace/output/Font/SDF/ToImport 下真正准备导入替换的 TMP/SDF 文件。")
    print("  10: 依次执行 0 -> 1(仅 AI 模式) -> 2 -> 4 -> 5 -> 6 -> 7 -> 8 -> 9。")
    print()
    print("菜单:")
    print("0. 扫描导出的 JSON，生成文本、字体、材质、引用索引")
    print("1. AI 判断字段后过滤 records.json")
    print("2. 根据扫描记录翻译")
    print("3. 从 trans.json 重建 game.txt 和 game_chars.txt")
    print("4. 导出翻译后的待替换 JSON")
    print("5. 屏蔽译文文本同物体上的阴影/描边组件")
    print("6. 生成 TTF 替换字体")
    print("7. 合并 TMP 字符并提示新增字符")
    print("8. 生成 Unity TMP 字体")
    print("9. 根据已生成 TMP 字体准备导入替换文件")
    print("10. 全部执行")
    print("q. 退出")
    print()


def main() -> int:
    cfg = load_config(CONFIG_PATH)
    if len(sys.argv) > 1 and sys.argv[1] == SDF_FINALIZE_ARGUMENT:
        return _run_sdf_finalize_in_fresh_process(cfg)
    if len(sys.argv) > 2 and sys.argv[1] == RUN_STEP_ARGUMENT:
        return _run_noninteractive_step(cfg, sys.argv[2])

    print(f"资源输入目录: {cfg.resource_input_root}")

    while True:
        print_menu()
        choice = prompt_input("请选择: ").strip().lower()

        if choice == "0":
            maybe_clean_scan_records(cfg)
            return scan_and_record(cfg) or 0
        if choice == "1":
            return apply_ai_field_selection_to_records(cfg) or 0
        if choice == "2":
            return translate_from_scan_records(cfg) or 0
        if choice == "3":
            return rebuild_game_text_outputs(cfg) or 0
        if choice == "4":
            return export_translated_files(cfg) or 0
        if choice == "5":
            return disable_translated_text_effect_components(cfg) or 0
        if choice == "6":
            build_ttf_replacements(cfg)
            return 0
        if choice == "7":
            build_merged_tmp_chars(cfg)
            return 0
        if choice == "8":
            tmp_chars_path = existing_tmp_chars_path(cfg)
            if tmp_chars_path is None:
                return 1
            print(f"[TMP] 使用已生成字符文件: {tmp_chars_path}")
            return launch_unity_tmp_generator(cfg, tmp_chars_path)
        if choice == "9":
            prepare_generated_tmp_import_replacements(cfg)
            return 0
        if choice == "10":
            maybe_clean_scan_records(cfg)
            return _run_full_pipeline_in_isolated_processes(cfg)
        if choice in {"q", "quit", "exit"}:
            return 0

        print("无效选择，请输入 0、1、2、3、4、5、6、7、8、9、10 或 q。")
        print()


if __name__ == "__main__":
    raise SystemExit(main())
