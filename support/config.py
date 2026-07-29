from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
from typing import Any


_UNITY_RESOLUTION_CACHE: dict[tuple[str, str, str], tuple[Path, str | None]] = {}
_UNITY_RESOLUTION_NOTICES_SHOWN: set[str] = set()
_SUPPRESS_UNITY_NOTICE_ENV = "TRANSLATE_SUPPRESS_UNITY_CONFIG_NOTICE"


def _print_unity_resolution_notice(message: str | None, quiet: bool) -> None:
    if (
        not message
        or quiet
        or os.environ.get(_SUPPRESS_UNITY_NOTICE_ENV) == "1"
        or message in _UNITY_RESOLUTION_NOTICES_SHOWN
    ):
        return
    print(message, flush=True)
    _UNITY_RESOLUTION_NOTICES_SHOWN.add(message)


def _persist_resolved_unity_exe(config_file: Path, resolved_editor: Path) -> bool:
    try:
        raw_bytes = config_file.read_bytes()
        has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
        text = raw_bytes.decode("utf-8-sig")
        pattern = re.compile(r'("unity_exe"\s*:\s*)"(?:\\.|[^"\\])*"')
        replacement = json.dumps(resolved_editor.as_posix(), ensure_ascii=False)
        updated, count = pattern.subn(lambda match: match.group(1) + replacement, text, count=1)
        if count != 1 or updated == text:
            return False
        payload = updated.encode("utf-8")
        if has_bom:
            payload = b"\xef\xbb\xbf" + payload
        temporary = config_file.with_name(config_file.name + ".unity.tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, config_file)
        print(
            f"\033[38;5;208m[config][源配置已修改] unity_exe: {resolved_editor}\033[0m",
            flush=True,
        )
        return True
    except Exception as exc:
        print(
            f"\033[91m[config][修改失败] 无法写回 unity_exe: {exc}\033[0m",
            flush=True,
        )
        return False


DEFAULT_TEXT_KEYS = [
    "description",
    "m_text",
    "m_Text",
    "mText",
    "mtext",
    "Text",
    "text",
    "itemName",
    "firstString",
    "strEng",
    "m_Wording",
    "m_WordingFormat",
    "m_Localized",
    "prefsKeyName",
    "Format",
    "message",
    "Description",
    "RewardText",
    "translationName",
    "jsonKey",
    "_text",
    "TextBeforePreset",
    "TextAfterPreset",
    "LevelDescription",
    "testo",
    "_objectDescription",
    "_objectName",
    "_actionName",
    "objective",
    "Name",
    "speech"
]


DEFAULT_CONFIG_FILE = "config.json"
DEFAULT_STRING_FIELD_BLACKLIST = [
    "m_Script",
    "m_EditorClassIdentifier",
    "m_TagString",
    "m_AssetBundleName",
    "m_AssetBundleVariant",
    "m_Name",
    "m_AnimationTriggers.*",
    "m_OnClick.*",
    "m_PersistentCalls.*",
    "m_SavedProperties.*",
    "disabledShaderPasses.*",
    "m_ValidKeywords.*",
    "m_InvalidKeywords.*",
    "stringTagMap.*",
    "scaleCurve.*",
    "m_MethodName",
    "m_TargetAssemblyTypeName",
    "m_ObjectArgumentAssemblyTypeName",
    "triggerName",
    "m_Entries.Array*.m_Key",
    "mTerm",
    "mTermSecondary",
    "TermPrefix",
    "TermSuffix",
    "mLocalizeTargetName",
    "mSource.mTerm_AppName",
    "mSource.mTerms.Array*.Term",
]


def _default_home() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve(base: Path, value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()


UNITY_VERSION_PART_RE = re.compile(r"^\d+(?:\.\d+){2,3}[a-z]\d+(?:c\d+)?$", re.IGNORECASE)


def _read_json_value(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def _unity_version_from_path(path: Path) -> str | None:
    for part in reversed(path.parts):
        if UNITY_VERSION_PART_RE.match(part):
            return part
    return None


def _unity_project_version(unity_font_project: Path) -> str | None:
    project_version_path = unity_font_project / "ProjectSettings" / "ProjectVersion.txt"
    if not project_version_path.is_file():
        return None
    try:
        text = project_version_path.read_text(encoding="utf-8-sig")
    except OSError:
        return None
    match = re.search(r"^m_EditorVersion:\s*(\S+)", text, re.MULTILINE)
    return match.group(1) if match else None


def _unity_search_roots(configured_path: Path) -> list[Path]:
    roots: list[Path] = []

    hub_install_path = Path.home() / "AppData" / "Roaming" / "UnityHub" / "secondaryInstallPath.json"
    hub_root = _read_json_value(hub_install_path)
    if isinstance(hub_root, str) and hub_root.strip():
        roots.append(Path(hub_root))

    roots.extend(
        [
            Path(r"C:\Program Files\Unity\Hub\Editor"),
            Path(r"C:\Program Files (x86)\Unity\Hub\Editor"),
            Path(r"D:\Program Files\Unity\Hub\Editor"),
            Path(r"D:\Program Files (x86)\Unity\Hub\Editor"),
            Path(r"D:\ProgramFile\SoftwareTool\Develop\Unity\Unity"),
        ]
    )

    if configured_path.parts:
        for parent in configured_path.parents:
            if parent.name.lower() in {"editor", "unity"}:
                roots.append(parent.parent if parent.name.lower() == "editor" else parent)

    unique_roots: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).lower()
        if key not in seen:
            unique_roots.append(root)
            seen.add(key)
    return unique_roots


def _find_unity_editors(configured_path: Path) -> list[Path]:
    editors: list[Path] = []
    seen: set[str] = set()
    for root in _unity_search_roots(configured_path):
        if not root.is_dir():
            continue
        try:
            candidates = root.rglob("Unity.exe")
        except OSError:
            continue
        for candidate in candidates:
            key = str(candidate).lower()
            if key not in seen and candidate.is_file():
                editors.append(candidate)
                seen.add(key)
    return editors


def _resolve_unity_exe(
    root_dir: Path,
    raw_value: Any,
    unity_font_project: Path,
    quiet: bool = False,
) -> Path:
    raw = str(raw_value or "").strip()
    configured_path = _resolve(root_dir, raw or str(PipelineConfig.unity_exe))
    cache_key = (
        str(root_dir.resolve()).casefold(),
        raw.casefold(),
        str(unity_font_project.resolve()).casefold(),
    )
    cached = _UNITY_RESOLUTION_CACHE.get(cache_key)
    if cached is not None:
        editor, notice = cached
        _print_unity_resolution_notice(notice, quiet)
        return editor

    if raw.lower() != "auto" and configured_path.is_file():
        _UNITY_RESOLUTION_CACHE[cache_key] = (configured_path, None)
        return configured_path

    expected_version = _unity_project_version(unity_font_project)
    editors = _find_unity_editors(configured_path)
    if expected_version:
        for editor in editors:
            if _unity_version_from_path(editor) == expected_version:
                notice = None
                if raw and raw.lower() != "auto":
                    notice = f"[config] Unity 路径不存在，已自动匹配工程版本 {expected_version}: {editor}"
                _UNITY_RESOLUTION_CACHE[cache_key] = (editor, notice)
                _print_unity_resolution_notice(notice, quiet)
                return editor

    if editors:
        chosen = sorted(editors, key=lambda p: str(p).lower(), reverse=True)[0]
        notice = None
        if raw.lower() == "auto":
            notice = f"[config] 未找到工程版本 {expected_version or '未知'} 的 Unity，改用已安装版本: {chosen}"
        elif raw:
            notice = f"[config] Unity 路径不存在，改用已安装版本: {chosen}"
        _UNITY_RESOLUTION_CACHE[cache_key] = (chosen, notice)
        _print_unity_resolution_notice(notice, quiet)
        return chosen

    _UNITY_RESOLUTION_CACHE[cache_key] = (configured_path, None)
    return configured_path


@dataclass
class PipelineConfig:
    root_dir: Path
    project_root_dir: Path
    project_name: str
    resource_source_subpath: Path
    resource_managed_subpath: Path
    catalog_source_subpath: Path
    stringliteral_json_subpath: Path
    resource_input_root: Path
    log_dir: Path
    result_dir: Path
    record_dir: Path
    import_overlay_dir: Path
    image_import_dir: Path
    object_import_dir: Path
    resource_staging_root: Path = Path("workspace/input_sources")
    addressables_download_workers: int = 5
    addressables_download_timeout: int = 60
    translation_mode: str = "translate"
    translate_provider: str = "baidu"
    baidu_appid: str = ""
    baidu_appkey: str = ""
    google_proxy_http: str = "http://127.0.0.1:10808"
    google_proxy_https: str = "http://127.0.0.1:10809"
    enable_ai_translation: bool = False
    ai_translation_base_url: str = ""
    ai_translation_api_key: str = ""
    ai_translation_model: str = ""
    ai_translation_timeout: int = 60
    ai_translation_proxy_http: str = ""
    ai_translation_proxy_https: str = ""
    ai_translation_batch_max_chars: int = 1200000
    ai_translation_max_output_chars: int = 384000
    ai_translation_strategy: str = "default"
    text_keys: list[str] = field(default_factory=lambda: list(DEFAULT_TEXT_KEYS))
    enable_ai_field_review: bool = False
    ai_field_review_base_url: str = ""
    ai_field_review_api_key: str = ""
    ai_field_review_model: str = ""
    ai_field_review_timeout: int = 60
    ai_field_review_proxy_http: str = ""
    ai_field_review_proxy_https: str = ""
    string_field_blacklist: list[str] = field(default_factory=lambda: list(DEFAULT_STRING_FIELD_BLACKLIST))
    font_keys: list[str] = field(default_factory=list)
    ignore_text: list[str] = field(default_factory=list)
    ttf_template_path: Path = Path("templates/fzkt.ttf")
    ttf_old_dir: Path = Path("Need/font/TTF_old")
    ttf_new_dir: Path = Path("Result/font/TTF_new")
    tmp_template_json_path: Path = Path("templates/templates.json")
    tmp_template_atlas_path: Path = Path("templates/Atlasa-templates.png")
    tmp_max_atlas_size: int = 8192
    include_old_sdf_template_chars: bool = False
    protect_i2_tmp_fonts_from_replacement: bool = True
    enable_text_effect_material_cleanup: bool = True
    unity_exe: Path = Path(r"D:\user\von\Program\Develop\Unity\Editor\6000.5.1f1\Editor\Unity.exe")
    unity_font_project: Path = Path("TMP_Font_Generator")
    unity_font_launcher: Path = Path("Tools/generate_tmp_font.py")
    output_trans_json: str = "trans.json"
    output_ids_json: str = "ids.json"
    output_font_map_json: str = "font_map.json"
    output_material_map_json: str = "material_map.json"
    output_file_id_map_json: str = "file_id_map.json"
    output_ref_map_json: str = "ref_map.json"
    output_path_id_map_json: str = "path_id_map.json"
    output_disabled_effect_components_json: str = "disabled_text_effect_components.json"
    output_runtime_text_binding_report_json: str = "runtime_text_binding_report.json"
    output_scan_records_json: str = "records.json"
    output_string_field_stats_json: str = "string_field_stats.json"
    output_string_field_stats_tsv: str = "string_field_stats.tsv"
    output_string_field_review_txt: str = "string_field_review.txt"
    output_game_txt: str = "game.txt"
    output_game_chars_txt: str = "game_chars.txt"
    output_tmp_chars_txt: str = "tmp_chars.txt"
    output_mapping_tsv: str = "mapping.tsv"
    scan_state_json: str = "scan_state.json"
    scan_cache_dir: str = "scan_cache"
    max_scan_workers: int = 0
    max_translate_workers: int = 0
    max_export_workers: int = 0
    max_import_workers: int = 0
    verbose_export_assets: bool = False
    enable_sample_collection: bool = False
    include_ascii: bool = True
    allow_missing_chars: bool = False

    @property
    def stage_dir(self) -> Path:
        return self.result_dir

    @property
    def translated_dump_dir(self) -> Path:
        return self.stage_dir / "Text"

    @property
    def stage_record_dir(self) -> Path:
        return self.record_dir

    @property
    def project_dir(self) -> Path:
        return (self.project_root_dir / self.project_name).resolve()

    @property
    def resource_source_root(self) -> Path:
        return (self.project_dir / self.resource_source_subpath).resolve()

    @property
    def resource_managed_root(self) -> Path:
        return (self.project_dir / self.resource_managed_subpath).resolve()

    @property
    def catalog_source_path(self) -> Path:
        configured = (self.project_dir / self.catalog_source_subpath).resolve()
        catalog_root = configured.parent
        settings_path = catalog_root / "settings.json"
        if settings_path.is_file():
            settings = _read_json_value(settings_path)
            locations = settings.get("m_CatalogLocations") if isinstance(settings, dict) else None
            if isinstance(locations, list):
                for location in locations:
                    internal_id = location.get("m_InternalId") if isinstance(location, dict) else None
                    if not isinstance(internal_id, str):
                        continue
                    name = Path(internal_id.replace("\\", "/")).name
                    if name.lower() in {"catalog.bin", "catalog.json"}:
                        candidate = catalog_root / name
                        if candidate.is_file():
                            return candidate.resolve()
        if configured.is_file():
            return configured
        for name in ("catalog.bin", "catalog.json"):
            candidate = catalog_root / name
            if candidate.is_file():
                return candidate.resolve()
        return configured

    @property
    def il2cpp_dummydll_root(self) -> Path:
        return (self.project_dir / "game-name" / "bak" / "64" / "DummyDll").resolve()

    @property
    def stringliteral_json_path(self) -> Path:
        return (self.project_dir / self.stringliteral_json_subpath).resolve()

    @property
    def scan_state_path(self) -> Path:
        return self.stage_record_dir / self.scan_state_json

    @property
    def scan_cache_path(self) -> Path:
        return self.stage_record_dir / self.scan_cache_dir


def load_config(config_path: str | Path | None = None, quiet: bool = False) -> PipelineConfig:
    root_dir = _default_home()
    config_file = _resolve(root_dir, config_path or DEFAULT_CONFIG_FILE)
    raw: dict[str, Any] = {}
    if config_file.is_file():
        raw = json.loads(config_file.read_text(encoding="utf-8-sig"))

    def get_value(key: str, default: Any) -> Any:
        return raw.get(key, default)

    unity_font_project = _resolve(root_dir, get_value("unity_font_project", "TMP_Font_Generator"))
    raw_unity_exe = str(get_value("unity_exe", str(PipelineConfig.unity_exe)) or "").strip()
    unity_exe = _resolve_unity_exe(
        root_dir,
        raw_unity_exe,
        unity_font_project,
        quiet=quiet,
    )
    configured_unity_exe = _resolve(root_dir, raw_unity_exe or str(PipelineConfig.unity_exe))
    should_persist_unity = (
        not quiet
        and unity_exe.is_file()
        and (
            raw_unity_exe.casefold() == "auto"
            or not configured_unity_exe.is_file()
        )
    )
    if should_persist_unity:
        _persist_resolved_unity_exe(config_file, unity_exe)

    cfg = PipelineConfig(
        root_dir=root_dir,
        project_root_dir=_resolve(root_dir, get_value("project_root_dir", r"D:\user\von\MyWorkbench\Works\APK\VS\0_Projects")),
        project_name=get_value("project_name", "融合大作战"),
        resource_source_subpath=Path(get_value("resource_source_subpath", "game-name/game/app/src/main/assets/bin/Data")),
        resource_managed_subpath=Path(get_value("resource_managed_subpath", "game-name/game/app/src/main/assets/bin/Data/Managed")),
        catalog_source_subpath=Path(get_value("catalog_source_subpath", "game-name/game/assets/aa/catalog.json")),
        stringliteral_json_subpath=Path(get_value("stringliteral_json_subpath", "game-name/bak/64/stringliteral.json")),
        resource_input_root=_resolve(
            root_dir,
            get_value(
                "resource_input_root",
                get_value("resource_work_root", get_value("uabea_dump_json_dir", "workspace/input")),
            ),
        ),
        log_dir=_resolve(root_dir, get_value("log_dir", "workspace/logs")),
        result_dir=_resolve(root_dir, get_value("result_dir", "workspace/output")),
        record_dir=_resolve(root_dir, get_value("record_dir", "workspace/records")),
        import_overlay_dir=_resolve(root_dir, get_value("import_overlay_dir", "workspace/output/Font/SDF/ToImport")),
        image_import_dir=_resolve(root_dir, get_value("image_import_dir", "workspace/output/Image/ToImport")),
        object_import_dir=_resolve(root_dir, get_value("object_import_dir", "workspace/output/Object/ToImport")),
        resource_staging_root=_resolve(root_dir, get_value("resource_staging_root", "workspace/input_sources")),
        addressables_download_workers=max(1, int(get_value("addressables_download_workers", 5) or 5)),
        addressables_download_timeout=max(1, int(get_value("addressables_download_timeout", 60) or 60)),
        translation_mode=get_value("translation_mode", "translate"),
        translate_provider=get_value("translate_provider", "baidu"),
        baidu_appid=get_value("baidu_appid", ""),
        baidu_appkey=get_value("baidu_appkey", ""),
        google_proxy_http=get_value("google_proxy_http", "http://127.0.0.1:10808"),
        google_proxy_https=get_value("google_proxy_https", "http://127.0.0.1:10809"),
        enable_ai_translation=bool(get_value("enable_ai_translation", False)),
        ai_translation_base_url=get_value("ai_translation_base_url", ""),
        ai_translation_api_key=get_value("ai_translation_api_key", ""),
        ai_translation_model=get_value("ai_translation_model", ""),
        ai_translation_timeout=int(get_value("ai_translation_timeout", 60) or 60),
        ai_translation_proxy_http=get_value("ai_translation_proxy_http", ""),
        ai_translation_proxy_https=get_value("ai_translation_proxy_https", ""),
        ai_translation_batch_max_chars=int(get_value("ai_translation_batch_max_chars", 1200000) or 1200000),
        ai_translation_max_output_chars=int(get_value("ai_translation_max_output_chars", 384000) or 384000),
        ai_translation_strategy=get_value("ai_translation_strategy", "default"),
        text_keys=list(get_value("text_keys", DEFAULT_TEXT_KEYS)),
        enable_ai_field_review=bool(get_value("enable_ai_field_review", False)),
        ai_field_review_base_url=get_value("ai_field_review_base_url", ""),
        ai_field_review_api_key=get_value("ai_field_review_api_key", ""),
        ai_field_review_model=get_value("ai_field_review_model", ""),
        ai_field_review_timeout=int(get_value("ai_field_review_timeout", 60) or 60),
        ai_field_review_proxy_http=get_value("ai_field_review_proxy_http", ""),
        ai_field_review_proxy_https=get_value("ai_field_review_proxy_https", ""),
        string_field_blacklist=list(get_value("string_field_blacklist", DEFAULT_STRING_FIELD_BLACKLIST)),
        font_keys=list(get_value("font_keys", [])),
        ignore_text=list(get_value("ignore_text", [])),
        ttf_template_path=_resolve(root_dir, get_value("ttf_template_path", "templates/fzkt.ttf")),
        ttf_old_dir=_resolve(root_dir, get_value("ttf_old_dir", "workspace/output/Font/TTF/source")),
        ttf_new_dir=_resolve(root_dir, get_value("ttf_new_dir", "workspace/output/Font/TTF/ToImport")),
        tmp_template_json_path=_resolve(root_dir, get_value("tmp_template_json_path", "templates/templates.json")),
        tmp_template_atlas_path=_resolve(root_dir, get_value("tmp_template_atlas_path", "templates/Atlasa-templates.png")),
        tmp_max_atlas_size=int(get_value("tmp_max_atlas_size", 8192) or 8192),
        include_old_sdf_template_chars=bool(get_value("include_old_sdf_template_chars", False)),
        protect_i2_tmp_fonts_from_replacement=bool(get_value("protect_i2_tmp_fonts_from_replacement", True)),
        enable_text_effect_material_cleanup=bool(get_value("enable_text_effect_material_cleanup", True)),
        unity_exe=unity_exe,
        unity_font_project=unity_font_project,
        unity_font_launcher=Path(get_value("unity_font_launcher", "Tools/generate_tmp_font.py")),
        output_trans_json=get_value("output_trans_json", "trans.json"),
        output_ids_json=get_value("output_ids_json", "ids.json"),
        output_font_map_json=get_value("output_font_map_json", "font_map.json"),
        output_material_map_json=get_value("output_material_map_json", "material_map.json"),
        output_file_id_map_json=get_value("output_file_id_map_json", "file_id_map.json"),
        output_ref_map_json=get_value("output_ref_map_json", "ref_map.json"),
        output_path_id_map_json=get_value("output_path_id_map_json", "path_id_map.json"),
        output_disabled_effect_components_json=get_value(
            "output_disabled_effect_components_json",
            "disabled_text_effect_components.json",
        ),
        output_runtime_text_binding_report_json=get_value(
            "output_runtime_text_binding_report_json",
            "runtime_text_binding_report.json",
        ),
        output_scan_records_json=get_value("output_scan_records_json", "records.json"),
        output_string_field_stats_json=get_value("output_string_field_stats_json", "string_field_stats.json"),
        output_string_field_stats_tsv=get_value("output_string_field_stats_tsv", "string_field_stats.tsv"),
        output_string_field_review_txt=get_value("output_string_field_review_txt", "string_field_review.txt"),
        output_game_txt=get_value("output_game_txt", "game.txt"),
        output_game_chars_txt=get_value("output_game_chars_txt", "game_chars.txt"),
        output_tmp_chars_txt=get_value("output_tmp_chars_txt", "tmp_chars.txt"),
        output_mapping_tsv=get_value("output_mapping_tsv", "mapping.tsv"),
        scan_state_json=get_value("scan_state_json", "scan_state.json"),
        scan_cache_dir=get_value("scan_cache_dir", "scan_cache"),
        max_scan_workers=int(get_value("max_scan_workers", 0) or 0),
        max_translate_workers=int(get_value("max_translate_workers", 0) or 0),
        max_export_workers=max(0, int(get_value("max_export_workers", 0) or 0)),
        max_import_workers=max(0, int(get_value("max_import_workers", 0) or 0)),
        verbose_export_assets=bool(get_value("verbose_export_assets", False)),
        enable_sample_collection=bool(get_value("enable_sample_collection", False)),
        include_ascii=bool(get_value("include_ascii", True)),
        allow_missing_chars=bool(get_value("allow_missing_chars", False)),
    )
    return cfg
