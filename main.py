from __future__ import annotations

from pathlib import Path
import shutil

from support.config import load_config
from pipeline.font_ttf import build_ttf_replacements
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


def clean_translation_outputs(cfg) -> None:
    preserved_file_id_map = None
    file_id_map_path = cfg.stage_record_dir / cfg.output_file_id_map_json
    if file_id_map_path.is_file():
        preserved_file_id_map = file_id_map_path.read_bytes()

    if cfg.record_dir.exists():
        shutil.rmtree(cfg.record_dir)
    cfg.record_dir.mkdir(parents=True, exist_ok=True)

    if preserved_file_id_map is not None:
        file_id_map_path.parent.mkdir(parents=True, exist_ok=True)
        file_id_map_path.write_bytes(preserved_file_id_map)
        print(f"[清理] 已保留 FileID 映射: {file_id_map_path}")

    if cfg.stage_dir.exists():
        shutil.rmtree(cfg.stage_dir)
    cfg.stage_dir.mkdir(parents=True, exist_ok=True)


def existing_tmp_chars_path(cfg) -> Path | None:
    path = cfg.stage_record_dir / cfg.output_tmp_chars_txt
    if path.is_file():
        return path
    print(f"[TMP] 未找到已生成字符文件: {path}")
    print("[TMP] 请先执行菜单 7 生成 tmp_chars.txt，再执行菜单 8。")
    return None


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
    print(f"资源输入目录: {cfg.resource_input_root}")

    while True:
        print_menu()
        choice = input("请选择: ").strip().lower()

        if choice == "0":
            confirm = input(f"扫描前是否清空已有记录文件 ({cfg.record_dir} 和 {cfg.stage_dir}) ? 输入 y 确认，其它任意键取消: ").strip().lower()
            if confirm == "y":
                print("正在清空已有记录文件...")
                clean_translation_outputs(cfg)
            else:
                print("已取消清空，继续保留现有记录文件。")
                print()
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
            confirm = input(f"扫描前是否清空已有记录文件 ({cfg.record_dir} 和 {cfg.stage_dir}) ? 输入 y 确认，其它任意键取消: ").strip().lower()
            if confirm == "y":
                print("正在清空已有记录文件...")
                clean_translation_outputs(cfg)
            else:
                print("已取消清空，继续保留现有记录文件。")
                print()
            scan_and_record(cfg)
            if cfg.enable_ai_field_review:
                apply_ai_field_selection_to_records(cfg)
            translate_from_scan_records(cfg)
            export_translated_files(cfg)
            disable_translated_text_effect_components(cfg)
            build_ttf_replacements(cfg)
            tmp_chars_path = build_merged_tmp_chars(cfg)
            result = launch_unity_tmp_generator(cfg, tmp_chars_path)
            if result != 0:
                return result
            prepare_generated_tmp_import_replacements(cfg)
            return 0
        if choice in {"q", "quit", "exit"}:
            return 0

        print("无效选择，请输入 0、1、2、3、4、5、6、7、8、9、10 或 q。")
        print()


if __name__ == "__main__":
    raise SystemExit(main())
