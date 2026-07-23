from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a TMP font asset through Unity batch mode for the TMP Font Asset Generator project.")
    parser.add_argument("--font", required=True, help="Path to a .ttf or .otf file.")
    parser.add_argument("--output-name", required=True, help="Output asset name inside Assets/GeneratedFonts.")
    parser.add_argument("--characters", default="", help="Inline character set to populate into the font asset.")
    parser.add_argument("--characters-file", default="", help="Path to a UTF-8 text file with characters.")
    parser.add_argument("--point-size-mode", choices=("auto", "custom"), default="auto", help="TMP Font Asset Creator sampling mode.")
    parser.add_argument("--point-size", type=int, default=90)
    parser.add_argument("--padding", type=int, default=9)
    parser.add_argument("--padding-mode", choices=("percent", "pixel"), default="pixel", help="TMP Font Asset Creator padding unit.")
    parser.add_argument("--packing-mode", type=int, default=4, help="TMP Font Asset Creator packing mode: Fast=0, Optimum=4.")
    parser.add_argument("--atlas-width", type=int, default=2048)
    parser.add_argument("--atlas-height", type=int, default=2048)
    parser.add_argument("--render-mode", default="SDFAA", help="TMP GlyphRenderMode name, for example SDFAA or SmoothHinted.")
    parser.add_argument("--include-font-features", action="store_true", help="Match the Font Asset Creator Get Font Features toggle.")
    parser.add_argument("--dynamic", action="store_true", help="Use dynamic atlas population. This is the default behavior.")
    parser.add_argument("--static", action="store_true", help="Use static atlas population.")
    parser.add_argument("--multi-atlas", action="store_true", help="Enable multi-atlas support.")
    parser.add_argument("--unity-exe", default=r"D:\user\von\Program\Develop\Unity\Editor\6000.5.1f1\Editor\Unity.exe")
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    return parser.parse_args()


def find_tmp_essential_package(project_root: Path) -> Path | None:
    package_cache = project_root / "Library" / "PackageCache"
    for package_dir in sorted(package_cache.glob("com.unity.textmeshpro@*")):
        candidate = package_dir / "Package Resources" / "TMP Essential Resources.unitypackage"
        if candidate.is_file():
            return candidate
    return None


def ensure_tmp_essential_resources(project_root: Path) -> None:
    settings_path = project_root / "Assets" / "TextMesh Pro" / "Resources" / "TMP Settings.asset"
    if settings_path.is_file():
        print(f"[TMP] TMP Essential Resources 已存在: {settings_path}", flush=True)
        return

    package_path = find_tmp_essential_package(project_root)
    if package_path is None:
        print("[TMP] 未找到 TMP Essential Resources.unitypackage，将交给 Unity 内部初始化尝试处理。", flush=True)
        return

    print(f"[TMP] 解包 TMP Essential Resources: {package_path}", flush=True)
    extracted = 0
    with tarfile.open(package_path, "r:*") as archive:
        entries: dict[str, dict[str, tarfile.TarInfo]] = {}
        for member in archive.getmembers():
            parts = Path(member.name).parts
            if len(parts) != 2:
                continue
            entries.setdefault(parts[0], {})[parts[1]] = member

        for group in entries.values():
            pathname_member = group.get("pathname")
            if pathname_member is None:
                continue
            pathname_file = archive.extractfile(pathname_member)
            if pathname_file is None:
                continue
            relative_path = pathname_file.read().decode("utf-8-sig").strip().replace("\\", "/")
            if not relative_path.startswith("Assets/"):
                continue

            target_path = (project_root / relative_path).resolve()
            try:
                target_path.relative_to(project_root)
            except ValueError:
                continue

            asset_member = group.get("asset")
            meta_member = group.get("asset.meta")
            if asset_member is not None:
                asset_file = archive.extractfile(asset_member)
                if asset_file is not None:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.write_bytes(asset_file.read())
                    extracted += 1
            else:
                target_path.mkdir(parents=True, exist_ok=True)

            if meta_member is not None:
                meta_file = archive.extractfile(meta_member)
                if meta_file is not None:
                    meta_path = target_path.with_name(target_path.name + ".meta")
                    meta_path.parent.mkdir(parents=True, exist_ok=True)
                    meta_path.write_bytes(meta_file.read())

    print(f"[TMP] TMP Essential Resources 解包完成，资源数: {extracted}", flush=True)


def main() -> int:
    args = parse_args()
    project_root = Path(args.project_root).resolve()
    unity_exe = Path(args.unity_exe).resolve()
    font_path = Path(args.font).resolve()
    characters_file = Path(args.characters_file).resolve() if args.characters_file else ""

    if not unity_exe.exists():
        print(f"Unity executable not found: {unity_exe}", file=sys.stderr)
        return 2

    if not font_path.exists():
        print(f"Font file not found: {font_path}", file=sys.stderr)
        return 2

    source_fonts_dir = project_root / "Assets" / "SourceFonts"
    generated_fonts_dir = project_root / "Assets" / "GeneratedFonts"
    source_fonts_dir.mkdir(parents=True, exist_ok=True)
    generated_fonts_dir.mkdir(parents=True, exist_ok=True)
    ensure_tmp_essential_resources(project_root)

    print(f"[TMP] Unity 项目: {project_root}", flush=True)
    print(f"[TMP] 字体模板: {font_path}", flush=True)
    if characters_file:
        print(f"[TMP] 字符文件: {characters_file}", flush=True)
    print(f"[TMP] 输入暂存目录: {source_fonts_dir}", flush=True)
    print(f"[TMP] 输出目录: {generated_fonts_dir}", flush=True)

    staged_font = source_fonts_dir / font_path.name
    if staged_font.resolve() != font_path:
        shutil.copy2(font_path, staged_font)
    print(f"[TMP] 已暂存字体: {staged_font}", flush=True)

    output_name = args.output_name if args.output_name.lower().endswith(".asset") else f"{args.output_name}.asset"
    output_asset_path = generated_fonts_dir / output_name
    print(f"[TMP] 目标输出: {output_asset_path}", flush=True)
    job = {
        "sourceFontAssetPath": f"Assets/SourceFonts/{font_path.name}",
        "outputAssetPath": f"Assets/GeneratedFonts/{output_name}",
        "characters": args.characters,
        "charactersFilePath": str(characters_file) if characters_file else "",
        "pointSizeSamplingMode": 0 if args.point_size_mode == "auto" else 1,
        "pointSize": args.point_size,
        "padding": args.padding,
        "paddingMode": 1 if args.padding_mode == "percent" else 2,
        "packingMode": args.packing_mode,
        "atlasWidth": args.atlas_width,
        "atlasHeight": args.atlas_height,
        "characterSetSelectionMode": 8 if characters_file else 7,
        "renderMode": args.render_mode,
        "atlasPopulationMode": "Dynamic" if args.dynamic or not args.static else "Static",
        "multiAtlasSupport": bool(args.multi_atlas),
        "includeFontFeatures": bool(args.include_font_features),
    }

    job_path = project_root / "Assets" / "SourceFonts" / "font-job.json"
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[TMP] 任务文件: {job_path}", flush=True)

    log_dir = project_root.parent / "workspace" / "logs"
    if not log_dir.is_dir():
        log_dir = project_root.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "tmp_font_unity.log"
    if log_file.exists():
        try:
            log_file.unlink()
        except OSError:
            pass
    print(f"[TMP] Unity 日志: {log_file}", flush=True)

    cmd = [
        str(unity_exe),
        "-batchmode",
        "-nographics",
        "-quit",
        "-logFile",
        str(log_file),
        "-projectPath",
        str(project_root),
        "-executeMethod",
        "Translate.EditorTools.TmpFontGenerator.Run",
        "--",
        "--job",
        str(job_path),
    ]

    print("[TMP] 正在调用 Unity 批处理生成字体...", flush=True)
    process = subprocess.run(cmd, cwd=str(project_root))
    print(f"[TMP] Unity 结束，返回码: {process.returncode}", flush=True)
    if log_file.exists():
        try:
            tail = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()[-60:]
            print("[TMP] Unity 日志尾部:", flush=True)
            for line in tail:
                print(line, flush=True)
        except Exception as exc:
            print(f"[TMP] 读取 Unity 日志失败: {exc}", flush=True)
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
