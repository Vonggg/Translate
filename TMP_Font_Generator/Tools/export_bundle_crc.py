from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export Unity AssetBundle CRC values through BuildPipeline.GetCRCForAssetBundle.")
    parser.add_argument("--bundle-root", required=True, help="Directory containing final .bundle files.")
    parser.add_argument("--output", required=True, help="Output JSON path.")
    parser.add_argument("--unity-exe", default=r"D:\user\von\Program\Develop\Unity\Editor\6000.5.1f1\Editor\Unity.exe")
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_root = Path(args.project_root).resolve()
    unity_exe = Path(args.unity_exe).resolve()
    bundle_root = Path(args.bundle_root).resolve()
    output_path = Path(args.output).resolve()

    if not unity_exe.is_file():
        print(f"Unity executable not found: {unity_exe}", file=sys.stderr)
        return 2
    if not project_root.is_dir():
        print(f"Unity project not found: {project_root}", file=sys.stderr)
        return 2
    if not bundle_root.is_dir():
        print(f"Bundle root not found: {bundle_root}", file=sys.stderr)
        return 2

    source_fonts_dir = project_root / "Assets" / "SourceFonts"
    source_fonts_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    job = {
        "bundleRoot": str(bundle_root),
        "outputPath": str(output_path),
    }
    job_path = source_fonts_dir / "bundle-crc-job.json"
    job_path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")

    log_dir = project_root.parent / "workspace" / "logs"
    if not log_dir.is_dir():
        log_dir = project_root.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "bundle_crc_unity.log"
    if log_file.exists():
        try:
            log_file.unlink()
        except OSError:
            pass

    print(f"[BundleCRC] Unity 项目: {project_root}", flush=True)
    print(f"[BundleCRC] Bundle 目录: {bundle_root}", flush=True)
    print(f"[BundleCRC] 输出 JSON: {output_path}", flush=True)
    print(f"[BundleCRC] Unity 日志: {log_file}", flush=True)

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
        "Translate.EditorTools.AssetBundleCrcExporter.Run",
        "--",
        "--job",
        str(job_path),
    ]

    print("[BundleCRC] 正在调用 Unity 计算 AssetBundle CRC...", flush=True)
    process = subprocess.run(cmd, cwd=str(project_root))
    print(f"[BundleCRC] Unity 结束，返回码: {process.returncode}", flush=True)
    if log_file.exists():
        try:
            tail = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()[-80:]
            print("[BundleCRC] Unity 日志尾部:", flush=True)
            for line in tail:
                print(line, flush=True)
        except Exception as exc:
            print(f"[BundleCRC] 读取 Unity 日志失败: {exc}", flush=True)
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
