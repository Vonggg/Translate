#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def resolve_repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def resolve_cli_project(repo_root: Path) -> Path:
    return repo_root / "UnityResourceCLI" / "UnityResourceCLI.csproj"


def resolve_managed_root(source_root: Path, managed_root: str | None) -> Path:
    if managed_root:
        return Path(managed_root).expanduser().resolve()
    return (source_root / "Managed").resolve()


def build_command(args: argparse.Namespace, repo_root: Path) -> list[str]:
    cli_project = resolve_cli_project(repo_root)
    command = [
        "dotnet",
        "run",
        "--project",
        str(cli_project),
        "--",
        args.mode,
        "--source",
        str(args.source),
        "--work",
        str(args.work),
        "--managed",
        str(args.managed),
        "--dump-format",
        args.dump_format,
        "--image-format",
        args.image_format,
        "--quality",
        str(args.quality),
    ]
    if args.mode == "export":
        command.extend(["--export-profile", args.export_profile])
        command.extend(["--export-workers", str(args.export_workers)])
        command.extend(["--verbose-export-assets", str(args.verbose_export_assets).lower()])
    if args.replacement_root:
        command.extend(["--replacement-root", str(args.replacement_root)])
    if args.result_root:
        command.extend(["--result-root", str(args.result_root)])
    if args.mode == "import":
        command.extend(["--import-workers", str(args.import_workers)])
        command.extend(["--save-samples", str(args.save_samples).lower()])
    return command


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export or import Unity resources with UnityResourceCLI."
    )
    parser.add_argument(
        "source",
        help=r"Game Data folder, for example D:\MyWorkbench\Works\APK\VS\0_Projects\菜鸟矿工\game-name\game\assets\bin\Data",
    )
    parser.add_argument(
        "--work",
        default=r"D:\CG\Translate\workspace\input",
        help="Working directory used to store exports and import results.",
    )
    parser.add_argument(
        "--managed",
        default=None,
        help="Managed folder containing the game's .dll files. Defaults to <source>\\Managed.",
    )
    parser.add_argument(
        "--replacement-root",
        default=None,
        help="Overlay folder containing replacement files. Import reads this folder before the export work tree.",
    )
    parser.add_argument(
        "--result-root",
        default=None,
        help="Output folder for imported result files. Defaults to <work>\\result.",
    )
    parser.add_argument(
        "--mode",
        choices=("export", "import"),
        default="export",
        help="Operation mode. Export is the default.",
    )
    parser.add_argument(
        "--dump-format",
        choices=("json", "txt"),
        default="json",
        help="Export format for TextAsset and MonoBehaviour.",
    )
    parser.add_argument(
        "--image-format",
        choices=("png", "jpg"),
        default="png",
        help="Export format for Texture2D.",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=90,
        help="JPEG quality when image-format is jpg.",
    )
    parser.add_argument(
        "--verbose-export-assets",
        action="store_true",
        help="Print one log line for every exported asset. Disabled by default for speed.",
    )
    parser.add_argument(
        "--export-workers",
        type=int,
        default=0,
        help="Parallel source-file export workers. 0 selects a conservative automatic value.",
    )
    parser.add_argument(
        "--import-workers",
        type=int,
        default=0,
        help="Parallel manifest import workers. 0 selects a conservative automatic value.",
    )
    parser.add_argument(
        "--save-samples",
        action="store_true",
        help="Save diagnostic samples. Disabled by default because samples can be very large.",
    )
    parser.add_argument(
        "--export-profile",
        choices=(
            "basic", "objects", "mesh",
            "basic+objects", "basic+mesh", "objects+mesh",
            "basic+objects+mesh", "all",
        ),
        default="all",
        help="Export basic translation assets, object hierarchy indexes, meshes, or a combination.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_root = Path(args.source).expanduser().resolve()
    work_root = Path(args.work).expanduser().resolve()
    managed_root = resolve_managed_root(source_root, args.managed)
    replacement_root = Path(args.replacement_root).expanduser().resolve() if args.replacement_root else None
    result_root = Path(args.result_root).expanduser().resolve() if args.result_root else None

    args.source = source_root
    args.work = work_root
    args.managed = managed_root
    args.replacement_root = replacement_root
    args.result_root = result_root

    repo_root = resolve_repo_root()
    command = build_command(args, repo_root)

    work_root.mkdir(parents=True, exist_ok=True)
    return subprocess.call(command, cwd=str(repo_root))


if __name__ == "__main__":
    raise SystemExit(main())
