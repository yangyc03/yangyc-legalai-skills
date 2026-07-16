#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import importlib.util
import io
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "1.0"
CONFIG_SCHEMA_VERSION = "1.0"
TOOL_VERSION = "0.1.0-beta"
PROFILES = {"litigation", "special_nonlitigation", "regular_counsel_close"}
LAYERS = {"core", "docx", "image", "visual", "pdf-compat", "encrypted-pdf", "ocr"}
PYTHON_LAYER_PACKAGES = {
    "core": ["pypdf>=5,<7", "reportlab>=4,<5"],
    "docx": ["lxml>=5,<7"],
    "image": ["Pillow>=10,<13"],
}
BINARY_BY_LAYER = {
    "docx": "soffice",
    "visual": "pdftoppm",
    "pdf-compat": "qpdf",
    "encrypted-pdf": "gs",
    "ocr": "tesseract",
}
STAGES = {
    "labor_arbitration", "commercial_arbitration", "first_instance",
    "second_instance", "retrial", "enforcement", "other",
}
OFFICE_SUFFIXES = {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
PDF_SUFFIXES = {".pdf"}
SUPPORTED_SUFFIXES = OFFICE_SUFFIXES | IMAGE_SUFFIXES | PDF_SUFFIXES
PACKAGE_SUFFIXES = {".docx", ".xlsx", ".pptx", ".odt", ".ods"}
SECRET_MARKERS = {"app_secret", "api_key", "apikey", "password", "credential", "authorization", "bearer"}
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
SAFE_RUN_ID = re.compile(r"^run-[A-Za-z0-9._-]+$")
INVOICE_MARKERS = ("发票",)
PAYMENT_RECEIPT_MARKERS = ("付款回单", "银行回单", "支付回单", "转账回单", "付款凭证")
FORMAL_INTERNAL_MARKERS = (
    "律师确认", "gate", "preview", "tool", "ai", "哈希", "sha256", "路径", "token", "确认令牌",
)
DIRECTORY_CATEGORY_RANK = {
    "委托代理协议": 10,
    "补充协议": 10,
    "收费材料": 20,
    "客户及委托手续": 30,
    "法院材料": 40,
    "诉讼文书": 50,
    "证据材料": 60,
    "诉讼费材料": 70,
    "最终裁判文书": 80,
    "办案小结": 90,
    "项目工作小结": 90,
}
SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


def require_core_dependencies() -> None:
    global PdfReader, PdfWriter, UserAccessPermissions, canvas
    try:
        pypdf = importlib.import_module("pypdf")
        constants = importlib.import_module("pypdf.constants")
        reportlab_canvas = importlib.import_module("reportlab.pdfgen.canvas")
    except ImportError as exc:
        raise RuntimeError("缺少基础 PDF 依赖；请先运行 setup --plan，再确认执行 setup --apply") from exc
    PdfReader = pypdf.PdfReader
    PdfWriter = pypdf.PdfWriter
    UserAccessPermissions = constants.UserAccessPermissions
    canvas = reportlab_canvas


def require_docx_dependency() -> None:
    global etree
    try:
        etree = importlib.import_module("lxml.etree")
    except ImportError as exc:
        raise RuntimeError("缺少 DOCX 模板依赖 lxml；请启用 docx 功能层") from exc


def require_image_dependency() -> None:
    global Image, ImageOps
    try:
        Image = importlib.import_module("PIL.Image")
        ImageOps = importlib.import_module("PIL.ImageOps")
    except ImportError as exc:
        raise RuntimeError("缺少图像依赖 Pillow；请启用 image 功能层") from exc


def default_config_path() -> Path:
    override = os.environ.get("LEGAL_MATTER_ARCHIVE_CONFIG")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "legal-matter-archive" / "config.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return base / "legal-matter-archive" / "config.json"


def default_data_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return base / "legal-matter-archive"
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / "legal-matter-archive"


def selected_config_path(explicit: str | None) -> Path:
    return Path(explicit).expanduser() if explicit else default_config_path()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_json_schema_value(
    value: Any,
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    path: str = "$",
) -> list[str]:
    errors: list[str] = []
    reference = schema.get("$ref")
    if reference:
        if not isinstance(reference, str) or not reference.startswith("#/"):
            return [f"{path} 使用了不受支持的 schema 引用：{reference}"]
        target: Any = root_schema
        for part in reference[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                return [f"{path} 无法解析 schema 引用：{reference}"]
            target = target[part]
        if not isinstance(target, dict):
            return [f"{path} schema 引用不是对象：{reference}"]
        errors.extend(validate_json_schema_value(value, target, root_schema, path))

    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list):
        branch_errors = [
            validate_json_schema_value(value, child, root_schema, path)
            for child in alternatives if isinstance(child, dict)
        ]
        if not branch_errors or all(branch for branch in branch_errors):
            errors.append(f"{path} 不符合 anyOf 中任一允许结构")

    for child in schema.get("allOf", []):
        if isinstance(child, dict):
            errors.extend(validate_json_schema_value(value, child, root_schema, path))

    condition = schema.get("if")
    if isinstance(condition, dict):
        matched = not validate_json_schema_value(value, condition, root_schema, path)
        selected = schema.get("then") if matched else schema.get("else")
        if isinstance(selected, dict):
            errors.extend(validate_json_schema_value(value, selected, root_schema, path))

    if "const" in schema and canonical(value) != canonical(schema["const"]):
        errors.append(f"{path} 必须等于 {schema['const']!r}")
    if "enum" in schema and not any(canonical(value) == canonical(item) for item in schema["enum"]):
        errors.append(f"{path} 必须是允许值之一：{schema['enum']}")

    type_checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "boolean": lambda item: isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "null": lambda item: item is None,
    }
    expected_type = schema.get("type")
    allowed_types = expected_type if isinstance(expected_type, list) else [expected_type]
    allowed_types = [item for item in allowed_types if item in type_checks]
    if allowed_types and not any(type_checks[item](value) for item in allowed_types):
        errors.append(f"{path} 必须是 {'/'.join(allowed_types)}")
        return errors

    if isinstance(value, dict):
        required = schema.get("required", [])
        for field in required:
            if field not in value:
                errors.append(f"{path} 缺少必填字段 {field}")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        unknown = sorted(set(value) - set(properties)) if isinstance(properties, dict) else []
        if additional is False:
            for field in unknown:
                errors.append(f"{path} 包含 schema 未允许字段 {field}")
        elif isinstance(additional, dict):
            for field in unknown:
                errors.extend(validate_json_schema_value(value[field], additional, root_schema, f"{path}.{field}"))
        if isinstance(properties, dict):
            for field, child_schema in properties.items():
                if field in value and isinstance(child_schema, dict):
                    errors.extend(validate_json_schema_value(value[field], child_schema, root_schema, f"{path}.{field}"))

    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append(f"{path} 至少需要 {schema['minItems']} 项")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append(f"{path} 最多允许 {schema['maxItems']} 项")
        if schema.get("uniqueItems"):
            serialized = [canonical(item) for item in value]
            if len(serialized) != len(set(serialized)):
                errors.append(f"{path} 不得包含重复项")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(validate_json_schema_value(item, item_schema, root_schema, f"{path}[{index}]"))

    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append(f"{path} 长度不得小于 {schema['minLength']}")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.search(pattern, value) is None:
            errors.append(f"{path} 不符合格式 {pattern}")
        if schema.get("format") == "date":
            try:
                date.fromisoformat(value)
            except ValueError:
                errors.append(f"{path} 必须是 YYYY-MM-DD")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path} 不得小于 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path} 不得大于 {schema['maximum']}")
    return errors


def validate_schema_document(value: Any, schema_name: str, label: str) -> None:
    schema = read_json(SCHEMA_DIR / schema_name)
    errors = validate_json_schema_value(value, schema, schema)
    if errors:
        raise ValueError(f"{label} 未通过 Schema 校验：" + "；".join(sorted(set(errors))))


def digest(value: Any, length: int | None = None) -> str:
    result = hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
    return result[:length] if length else result


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"JSON 根节点必须是对象：{path}")
    reject_secrets(data)
    return data


def reject_secrets(value: Any, trail: str = "") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if any(marker in normalized for marker in SECRET_MARKERS):
                raise ValueError(f"禁止在归档输入中保存凭证字段：{trail}{key}")
            reject_secrets(child, f"{trail}{key}.")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_secrets(child, f"{trail}[{index}].")


def write_new_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)


def write_new_json(path: Path, value: Any) -> None:
    write_new_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def detect_incomplete_transactions(run_dir: Path, phase: str) -> None:
    leftovers = sorted(path.name for path in run_dir.glob(f".{phase}-*") if path.is_dir())
    if leftovers:
        raise RuntimeError(
            f"发现未完成 {phase} 事务：{', '.join(leftovers)}；"
            "不自动继续或覆盖，请先核对事务标记和已生成目标"
        )


def write_transaction_marker(temp_root: Path, phase: str, targets: list[Path]) -> Path:
    marker = temp_root / "transaction.json"
    write_new_json(marker, {
        "transaction_version": "1.0",
        "tool": "legal-matter-archive",
        "phase": phase,
        "status": "in_progress",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "targets": [str(path) for path in targets],
    })
    return marker


def promote_directory(source: Path, target: Path, promoted: list[Path]) -> None:
    if target.exists():
        raise RuntimeError(f"输出目标已存在，禁止覆盖：{target}")
    os.replace(source, target)
    promoted.append(target)


def rollback_promotions(promoted_files: list[Path], promoted_directories: list[Path]) -> None:
    for path in reversed(promoted_files):
        path.unlink(missing_ok=True)
    for path in reversed(promoted_directories):
        shutil.rmtree(path, ignore_errors=True)


def ensure_no_symlink(path: Path, label: str) -> None:
    current = path
    while True:
        if current.exists() and current.is_symlink():
            raise ValueError(f"{label} 不得包含符号链接：{path}")
        if current.parent == current:
            break
        current = current.parent


def resolve_inside(root: Path, value: str | Path, label: str, *, must_exist: bool = False) -> Path:
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError(f"{label} 必须是项目内相对路径：{value}")
    root = root.resolve()
    resolved = (root / candidate).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} 越出项目根目录：{value}") from exc
    ensure_no_symlink(root / candidate, label)
    if must_exist and not resolved.exists():
        raise ValueError(f"{label} 不存在：{value}")
    return resolved


def resolve_current_source(root: Path, descriptor: dict[str, Any] | None, label: str) -> tuple[Path | None, dict[str, Any] | None, str | None]:
    if not descriptor or not descriptor.get("path"):
        return None, None, f"未配置 {label} current"
    raw = Path(str(descriptor["path"])).expanduser()
    try:
        path = raw.resolve() if raw.is_absolute() else resolve_inside(root, raw, label, must_exist=True)
        if raw.is_absolute() and raw.is_symlink():
            raise ValueError(f"{label} 文件本身不得是符号链接：{raw}")
    except (OSError, ValueError) as exc:
        return None, None, str(exc)
    if not path.is_file():
        return None, None, f"{label} current 不是文件：{path.name}"
    actual = sha256_file(path)
    expected = str(descriptor.get("sha256", "")).strip().lower()
    if expected and expected != actual:
        return path, None, f"{label} current 哈希不匹配：{path.name}"
    snapshot = {"name": path.name, "sha256": actual, "size": path.stat().st_size, "suffix": path.suffix.lower()}
    if descriptor.get("expected_pages") is not None:
        snapshot["expected_pages"] = int(descriptor["expected_pages"])
    return path, snapshot, None


def relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def repository_root(path: Path) -> Path | None:
    current = path.resolve(strict=False)
    if not current.is_dir():
        current = current.parent
    while True:
        marker = current / ".git"
        if marker.is_dir() or marker.is_file():
            return current
        if current.parent == current:
            return None
        current = current.parent


def file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in OFFICE_SUFFIXES:
        return "office"
    return "unsupported"


def pdf_security_profile(path: Path) -> dict[str, Any]:
    require_core_dependencies()
    reader = PdfReader(str(path), strict=False)
    if not reader.is_encrypted:
        return {
            "encrypted": False,
            "opens_without_prompt": True,
            "print_allowed": True,
            "print_conversion_required": False,
            "page_count": len(reader.pages),
        }
    try:
        password_type = reader.decrypt("")
    except Exception:  # noqa: BLE001 - malformed encryption must be reported as blocked input
        password_type = 0
    if not password_type:
        return {
            "encrypted": True,
            "opens_without_prompt": False,
            "print_allowed": False,
            "print_conversion_required": False,
            "page_count": None,
        }
    permissions = reader.user_access_permissions
    print_allowed = permissions is None or bool(permissions & UserAccessPermissions.PRINT)
    return {
        "encrypted": True,
        "opens_without_prompt": True,
        "print_allowed": print_allowed,
        "print_conversion_required": print_allowed,
        "page_count": len(reader.pages),
    }


def structural_check(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    try:
        if suffix in PACKAGE_SUFFIXES:
            if not zipfile.is_zipfile(path):
                return "invalid", "扩展名与 OOXML/ODF 容器不符"
            with zipfile.ZipFile(path, "r") as archive:
                bad = archive.testzip()
                if bad:
                    return "invalid", f"压缩包成员损坏：{bad}"
            return "valid", "容器结构可读"
        if suffix == ".pdf":
            security = pdf_security_profile(path)
            if not security["encrypted"]:
                return "valid", f"PDF {security['page_count']} 页"
            if not security["opens_without_prompt"]:
                return "blocked", "PDF 需要打开密码；不尝试绕过或保存密码"
            if not security["print_allowed"]:
                return "blocked", "PDF 权限型加密且禁止打印"
            return "conversion_required", (
                f"PDF 权限型加密；无需打开密码且允许打印，prepare 将生成打印派生副本"
                f"（{security['page_count']} 页）"
            )
        if suffix in IMAGE_SUFFIXES:
            require_image_dependency()
            with Image.open(path) as image:
                image.verify()
            return "valid", "图片可读"
        if suffix in {".doc", ".xls", ".ppt"}:
            return "conversion_required", "旧版 Office 文件需由 LibreOffice 转换并人工复核"
        return "unsupported", "不支持的文件类型"
    except Exception as exc:  # noqa: BLE001 - input validation must report malformed files
        return "invalid", f"结构检查失败：{type(exc).__name__}: {exc}"


def load_docx_document_xml(path: Path) -> etree._Element:
    require_docx_dependency()
    if path.suffix.lower() != ".docx" or not zipfile.is_zipfile(path):
        raise RuntimeError(f"模板必须是结构可读的 DOCX：{path.name}")
    with zipfile.ZipFile(path, "r") as archive:
        try:
            data = archive.read("word/document.xml")
        except KeyError as exc:
            raise RuntimeError(f"DOCX 缺少 word/document.xml：{path.name}") from exc
    return etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False))


def cover_layout_fingerprint_root(root: etree._Element) -> str:
    """Hash cover geometry/structure while deliberately ignoring editable text runs."""
    clone = deepcopy(root)
    for paragraph in clone.xpath(".//w:p", namespaces={"w": W_NS}):
        for child in list(paragraph):
            if child.tag != f"{W}pPr":
                paragraph.remove(child)
    canonical_xml = etree.tostring(clone, method="c14n", exclusive=False, with_comments=False)
    return hashlib.sha256(canonical_xml).hexdigest()


def cover_layout_fingerprint(path: Path) -> str:
    return cover_layout_fingerprint_root(load_docx_document_xml(path))


def docx_package_hashes(path: Path, *, exclude: set[str] | None = None) -> dict[str, str]:
    excluded = exclude or set()
    with zipfile.ZipFile(path, "r") as archive:
        return {
            name: hashlib.sha256(archive.read(name)).hexdigest()
            for name in sorted(archive.namelist())
            if name not in excluded
        }


def material_inclusion_policy(*values: Any) -> tuple[str, bool] | None:
    text = " ".join(str(value or "") for value in values)
    if any(marker in text for marker in PAYMENT_RECEIPT_MARKERS):
        return "payment_receipt_default_exclude", False
    if any(marker in text for marker in INVOICE_MARKERS):
        return "invoice_default_include", True
    return None


def locate_cover_field_target(root: etree._Element, field: dict[str, Any]) -> tuple[str, etree._Element]:
    label = normalized_label(str(field.get("label", "")))
    mode = str(field.get("target_mode", "adjacent_blank"))
    try:
        occurrence = int(field.get("occurrence", 1))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"封皮字段 occurrence 必须是正整数：{field.get('label', '')}") from exc
    if occurrence < 1:
        raise RuntimeError(f"封皮字段 occurrence 必须是正整数：{field.get('label', '')}")

    if mode == "inline_brackets":
        matched = 0
        for paragraph in root.xpath(".//w:p", namespaces={"w": W_NS}):
            if label and label in normalized_label("".join(paragraph.itertext())):
                matched += 1
                if matched == occurrence:
                    bracket_nodes = [
                        node for node in paragraph.xpath(".//w:t", namespaces={"w": W_NS})
                        if re.search(r"\[[^\[\]]*\]", node.text or "")
                    ]
                    if len(bracket_nodes) != 1:
                        raise RuntimeError(f"封皮行内字段必须且只能有一个方括号占位区：{field.get('label', '')}")
                    inner = re.search(r"\[([^\[\]]*)\]", bracket_nodes[0].text or "").group(1)
                    expected = field.get("expected_text")
                    if expected is None and inner.strip():
                        raise RuntimeError(f"封皮行内字段方括号内已有内容，禁止覆盖：{field.get('label', '')}")
                    if expected is not None and inner != str(expected):
                        raise RuntimeError(f"封皮行内字段占位内容与 expected_text 不一致：{field.get('label', '')}")
                    return mode, paragraph
        raise RuntimeError(f"封皮模板未找到字段标签：{field.get('label', '')}")

    if mode not in {"adjacent_blank", "adjacent_placeholder"}:
        raise RuntimeError(f"不支持的封皮字段 target_mode：{mode}")
    try:
        offset = int(field.get("target_offset", 1))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"封皮字段 target_offset 必须是正整数：{field.get('label', '')}") from exc
    if offset < 1:
        raise RuntimeError(f"封皮字段 target_offset 必须是正整数：{field.get('label', '')}")
    matched = 0
    for row in root.xpath(".//w:tr", namespaces={"w": W_NS}):
        cells = row.findall(f"{W}tc")
        for index, cell in enumerate(cells):
            if label and label in normalized_label(cell_text(cell)):
                matched += 1
                if matched != occurrence:
                    continue
                target_index = index + offset
                if target_index >= len(cells):
                    raise RuntimeError(f"封皮字段目标单元格越界：{field.get('label', '')}")
                target = cells[target_index]
                actual = normalized_label(cell_text(target))
                if mode == "adjacent_blank" and actual:
                    raise RuntimeError(f"封皮字段目标单元格不是空白值格，禁止修改模板既有内容：{field.get('label', '')}")
                if mode == "adjacent_placeholder":
                    if "expected_text" not in field:
                        raise RuntimeError(f"adjacent_placeholder 必须提供 expected_text：{field.get('label', '')}")
                    if actual != normalized_label(str(field["expected_text"])):
                        raise RuntimeError(f"封皮占位内容与 expected_text 不一致：{field.get('label', '')}")
                return mode, target
    raise RuntimeError(f"封皮模板未找到字段标签：{field.get('label', '')}")


def preflight_cover_template(path: Path, fields: list[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    try:
        root = load_docx_document_xml(path)
    except RuntimeError as exc:
        return [str(exc)]
    for field in fields:
        if normalized_label(str(field.get("label", ""))) == "案件编号":
            if str(field.get("value", "")).strip():
                failures.append("封皮“案件编号”由律所归档部门填写，归档工作流必须留空")
            continue
        try:
            locate_cover_field_target(root, field)
        except RuntimeError as exc:
            failures.append(str(exc))
    return failures


def cover_template_business_labels(path: Path) -> list[str]:
    """Extract the complete business-field inventory from the current cover structure."""
    root = load_docx_document_xml(path)
    labels: list[str] = []

    # Inline fields such as `案件编号：[ ]` live in body paragraphs outside tables.
    for paragraph in root.xpath("./w:body/w:p", namespaces={"w": W_NS}):
        text = re.sub(r"\s+", "", "".join(paragraph.itertext()))
        match = re.match(r"^(.+?)[：:]\s*[\[［【]", text)
        if match:
            labels.append(match.group(1))

    # The current cover tables use alternating physical label/value cells.
    # Single-cell title rows are excluded; checkbox options are values, not labels.
    for table in root.xpath(".//w:tbl", namespaces={"w": W_NS}):
        for row in table.findall(f"{W}tr"):
            cells = row.findall(f"{W}tc")
            if len(cells) < 2:
                continue
            for index in range(0, len(cells), 2):
                label = normalized_label(cell_text(cells[index]))
                if not label or label.startswith(("□", "√", "☐", "☑")):
                    continue
                labels.append(label)

    result: list[str] = []
    seen: set[str] = set()
    for label in labels:
        normalized = normalized_label(label)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(label.strip())
    if not result:
        raise RuntimeError("封皮 current 未能提取业务字段清单，禁止跳过完整性确认")
    return result


def validate_cover_controls(
    request: dict[str, Any],
    cover_fields: list[dict[str, Any]],
    blockers: list[str],
    cover_path: Path | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    completion_raw = request.get("cover_completion")
    completion: dict[str, Any] = {}
    if completion_raw is None:
        blockers.append("缺少 cover_completion，封皮全部业务字段必须逐项填写或确认留空")
    else:
        if not isinstance(completion_raw, dict):
            blockers.append("cover_completion 必须是对象")
        else:
            required = completion_raw.get("required_labels")
            blanks = completion_raw.get("intentional_blanks")
            if completion_raw.get("lawyer_confirmed") is not True:
                blockers.append("cover_completion 必须经律师确认")
            if not isinstance(required, list) or not required or any(not str(item).strip() for item in required):
                blockers.append("cover_completion.required_labels 必须是非空字段名数组")
                required = []
            required_normalized = [normalized_label(str(item)) for item in required]
            if len(required_normalized) != len(set(required_normalized)):
                blockers.append("cover_completion.required_labels 归一化后不得重复")
            if not isinstance(blanks, list):
                blockers.append("cover_completion.intentional_blanks 必须是数组")
                blanks = []
            blank_labels: list[str] = []
            normalized_blanks: list[dict[str, Any]] = []
            for item in blanks:
                if (
                    not isinstance(item, dict)
                    or not str(item.get("label", "")).strip()
                    or not str(item.get("reason", "")).strip()
                    or item.get("lawyer_confirmed") is not True
                ):
                    blockers.append("每个封皮留空决定必须包含 label、reason 和 lawyer_confirmed=true")
                    continue
                label = str(item["label"]).strip()
                blank_labels.append(normalized_label(label))
                normalized_blanks.append({
                    "label": label,
                    "reason": str(item["reason"]).strip(),
                    "lawyer_confirmed": True,
                })
            filled_labels = {
                normalized_label(str(field.get("label", "")))
                for field in cover_fields
                if str(field.get("value", "")).strip()
                or field.get("value_source") == "computed_body_page_count"
            }
            missing = [
                str(label) for label in required
                if normalized_label(str(label)) not in filled_labels
                and normalized_label(str(label)) not in set(blank_labels)
            ]
            if missing:
                blockers.append("封皮字段既未填写也无确认留空理由：" + "、".join(missing))
            overlap = filled_labels.intersection(blank_labels)
            if overlap:
                blockers.append("封皮字段不能同时填写并标记留空：" + "、".join(sorted(overlap)))
            template_labels: list[str] = []
            if cover_path is not None:
                try:
                    template_labels = cover_template_business_labels(cover_path)
                except RuntimeError as exc:
                    blockers.append(str(exc))
                template_set = {normalized_label(label) for label in template_labels}
                required_set = set(required_normalized)
                omitted = [label for label in template_labels if normalized_label(label) not in required_set]
                unknown = [str(label) for label in required if normalized_label(str(label)) not in template_set]
                if omitted:
                    blockers.append("cover_completion 未覆盖 current 业务字段：" + "、".join(omitted))
                if unknown:
                    blockers.append("cover_completion 含 current 中不存在的业务字段：" + "、".join(unknown))
            blank_not_required = [
                item["label"] for item in normalized_blanks
                if normalized_label(item["label"]) not in set(required_normalized)
            ]
            if blank_not_required:
                blockers.append("封皮留空决定不在 required_labels 中：" + "、".join(blank_not_required))
            completion = {
                "required_labels": [str(item).strip() for item in required],
                "template_labels": template_labels,
                "intentional_blanks": normalized_blanks,
                "lawyer_confirmed": completion_raw.get("lawyer_confirmed") is True,
            }

    render_raw = request.get("cover_render")
    render: dict[str, Any] = {}
    if render_raw is None:
        blockers.append("缺少 cover_render，必须确认封皮渲染策略和预期页数")
    else:
        if not isinstance(render_raw, dict):
            blockers.append("cover_render 必须是对象")
        else:
            policy = str(render_raw.get("fit_policy", ""))
            expected_pages = render_raw.get("expected_pages")
            if policy not in {"preserve", "fixed_template_rows_compact_values", "balanced_template_rows_one_page"}:
                blockers.append("cover_render.fit_policy 不受支持")
            if not isinstance(expected_pages, int) or isinstance(expected_pages, bool) or expected_pages < 1:
                blockers.append("cover_render.expected_pages 必须是正整数")
            if render_raw.get("lawyer_confirmed") is not True:
                blockers.append("cover_render 必须经律师确认")
            row_height_scale = render_raw.get("row_height_scale")
            if policy == "balanced_template_rows_one_page":
                if expected_pages != 1:
                    blockers.append("balanced_template_rows_one_page 的 expected_pages 必须为 1")
                if (
                    not isinstance(row_height_scale, (int, float))
                    or isinstance(row_height_scale, bool)
                    or not 1 <= float(row_height_scale) <= 1.5
                ):
                    blockers.append("balanced_template_rows_one_page 必须提供 1 至 1.5 的 row_height_scale")
            elif row_height_scale is not None:
                blockers.append("row_height_scale 只能用于 balanced_template_rows_one_page")
            render = {
                "fit_policy": policy,
                "expected_pages": expected_pages,
                "row_height_scale": float(row_height_scale) if isinstance(row_height_scale, (int, float)) and not isinstance(row_height_scale, bool) else None,
                "lawyer_confirmed": render_raw.get("lawyer_confirmed") is True,
            }
    return completion, render


def preflight_directory_template(path: Path) -> list[str]:
    try:
        root = load_docx_document_xml(path)
    except RuntimeError as exc:
        return [str(exc)]
    for table in root.xpath(".//w:tbl", namespaces={"w": W_NS}):
        rows = table.findall(f"{W}tr")
        for index, row in enumerate(rows):
            text = re.sub(r"\s+", "", "".join(row.itertext())).replace("：", "").replace(":", "")
            if all(label in text for label in ["序号", "名称", "页码", "备注"]):
                if index + 1 >= len(rows):
                    return ["目录模板缺少数据行"]
                if len(rows[index + 1].findall(f"{W}tc")) < 4:
                    return ["目录模板数据行少于四个物理单元格，可能存在不受支持的合并单元格"]
                return []
    return ["目录模板未找到序号、名称、页码、备注表头"]


def snapshot_file(root: Path, path: Path) -> dict[str, Any]:
    status, detail = structural_check(path)
    stat = path.stat()
    snapshot = {
        "path": relative(root, path),
        "name": path.name,
        "suffix": path.suffix.lower(),
        "kind": file_kind(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256_file(path),
        "structural_status": status,
        "structural_detail": detail,
    }
    if path.suffix.lower() == ".pdf":
        try:
            snapshot["pdf_security"] = pdf_security_profile(path)
        except Exception:  # noqa: BLE001 - structural status already records the failure
            pass
    return snapshot


def validate_source_roots(root: Path, values: list[Any]) -> list[Path]:
    if not isinstance(values, list) or not values:
        raise ValueError("source_roots 必须是至少包含一个项目内相对目录的数组")
    result: list[Path] = []
    for value in values:
        path = resolve_inside(root, str(value), "source_root", must_exist=True)
        if not path.is_dir():
            raise ValueError(f"source_root 不是目录：{value}")
        if path == root or path.name == ".git" or ".git" in path.parts:
            raise ValueError(f"source_root 不得是项目根或 Git 目录：{value}")
        repo = repository_root(path)
        if repo:
            raise ValueError(f"source_root 不得位于 Git 仓库内：{value}")
        result.append(path)
    return result


def validate_output_root(root: Path, value: Any, sources: list[Path]) -> Path:
    output = resolve_inside(root, str(value), "output_root")
    if output == root or ".git" in output.parts:
        raise ValueError("output_root 不得是项目根或 Git 目录")
    if repository_root(output):
        raise ValueError("output_root 不得位于 Git 仓库内")
    for source in sources:
        if is_within(output, source) or is_within(source, output):
            raise ValueError(f"output_root 必须与 source_root 相互独立：{relative(root, source)}")
    return output


def scan_sources(root: Path, sources: list[Path], output: Path | None = None) -> list[dict[str, Any]]:
    seen: set[Path] = set()
    inventory: list[dict[str, Any]] = []
    for source in sources:
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"源材料包含符号链接：{relative(root, path)}")
            if not path.is_file() or path in seen:
                continue
            if output and is_within(path, output):
                continue
            if ".git" in path.parts or "__pycache__" in path.parts:
                continue
            seen.add(path)
            inventory.append(snapshot_file(root, path))
    return inventory


def verify_snapshot(root: Path, snapshot: Iterable[dict[str, Any]]) -> list[str]:
    failures: list[str] = []
    for item in snapshot:
        path = resolve_inside(root, item["path"], "snapshot path")
        if not path.is_file():
            failures.append(f"源文件不存在：{item['path']}")
            continue
        stat = path.stat()
        if stat.st_size != item["size"] or stat.st_mtime_ns != item["mtime_ns"] or sha256_file(path) != item["sha256"]:
            failures.append(f"源文件自预览后已变化：{item['path']}")
    return failures


def duplicate_groups(inventory: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_hash: dict[str, list[str]] = defaultdict(list)
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in inventory:
        by_hash[item["sha256"]].append(item["path"])
        by_name[item["name"].casefold()].append(item)
    exact = [
        {"sha256": value, "paths": sorted(paths)}
        for value, paths in sorted(by_hash.items()) if len(paths) > 1
    ]
    same_name = []
    for name, items in sorted(by_name.items()):
        hashes = {item["sha256"] for item in items}
        if len(items) > 1 and len(hashes) > 1:
            same_name.append({"name": name, "paths": sorted(item["path"] for item in items), "sha256": sorted(hashes)})
    return exact, same_name


def discover_binary(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if name == "soffice":
        candidate = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
        if candidate.is_file():
            return str(candidate)
    return None


def module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def dependency_report(config_path: Path | None = None) -> dict[str, Any]:
    modules = {
        "pypdf": module_available("pypdf"),
        "reportlab": module_available("reportlab"),
        "lxml": module_available("lxml"),
        "PIL": module_available("PIL"),
    }
    binary_paths = {
        name: discover_binary(name)
        for name in ["soffice", "pdftoppm", "qpdf", "gs", "tesseract"]
    }
    capabilities = {
        "core": modules["pypdf"] and modules["reportlab"],
        "docx": modules["lxml"] and bool(binary_paths["soffice"]),
        "image": modules["PIL"],
        "visual": bool(binary_paths["pdftoppm"]),
        "pdf-compat": bool(binary_paths["qpdf"]),
        "encrypted-pdf": bool(binary_paths["gs"]),
        "ocr": bool(binary_paths["tesseract"]),
    }
    selected = config_path or default_config_path()
    config_status = "missing"
    config_error = ""
    if selected.is_file():
        try:
            configured = read_json(selected)
            if configured.get("config_schema_version") != CONFIG_SCHEMA_VERSION:
                raise ValueError(f"config_schema_version 必须为 {CONFIG_SCHEMA_VERSION}")
            config_status = "available"
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            config_status = "invalid"
            config_error = str(exc)
    return {
        "config_schema_version": CONFIG_SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "status": "ready" if capabilities["core"] else "setup_required",
        "python": {
            "version": platform.python_version(),
            "executable": str(Path(os.sys.executable).resolve()),
            "supported": tuple(map(int, platform.python_version_tuple()[:2])) >= (3, 10),
        },
        "modules": modules,
        "binaries": binary_paths,
        "capabilities": capabilities,
        "config": {"path": str(selected), "status": config_status, "error": config_error},
        "policy": "检测命令不安装依赖、不修改配置、不读取案件目录",
    }


def parse_layers(raw: str | None) -> list[str]:
    requested = {item.strip() for item in (raw or "core").split(",") if item.strip()}
    requested.add("core")
    unknown = sorted(requested - LAYERS)
    if unknown:
        raise ValueError("不支持的功能层：" + "、".join(unknown))
    return sorted(requested)


def normalize_capability_settings(candidate: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    raw_layers = candidate.get("enabled_layers", ["core"])
    if not isinstance(raw_layers, list):
        raise ValueError("enabled_layers 必须是数组")
    layers = parse_layers(",".join(str(item) for item in raw_layers))
    raw_policies = candidate.get("policies") or {}
    if not isinstance(raw_policies, dict):
        raise ValueError("policies 必须是对象")
    policies = {
        "visual_verification": str(raw_policies.get("visual_verification", "automatic_when_available")),
        "ocr": str(raw_policies.get("ocr", "disabled")),
        "encrypted_pdf": str(raw_policies.get("encrypted_pdf", "printable_permissions_only")),
    }
    if policies["visual_verification"] not in {"automatic_when_available", "manual_only"}:
        raise ValueError("policies.visual_verification 不受支持")
    if policies["ocr"] not in {"disabled", "local_only"}:
        raise ValueError("policies.ocr 不受支持")
    if policies["encrypted_pdf"] not in {"block_all", "printable_permissions_only"}:
        raise ValueError("policies.encrypted_pdf 不受支持")
    return layers, policies


def setup_plan(layers: list[str], include_system: bool) -> dict[str, Any]:
    packages = sorted({package for layer in layers for package in PYTHON_LAYER_PACKAGES.get(layer, [])})
    data_dir = default_data_dir()
    venv_dir = data_dir / "runtime" / "venv"
    venv_python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    system_layers = [layer for layer in layers if layer in BINARY_BY_LAYER]
    system_commands: list[list[str]] = []
    system_instructions: list[str] = []
    if system_layers:
        if platform.system() == "Darwin":
            brew = discover_binary("brew")
            formulae = {
                "docx": "libreoffice", "visual": "poppler", "pdf-compat": "qpdf",
                "encrypted-pdf": "ghostscript", "ocr": "tesseract",
            }
            missing = [formulae[layer] for layer in system_layers if not discover_binary(BINARY_BY_LAYER[layer])]
            if missing and include_system and brew:
                system_commands.append([brew, "install", *sorted(set(missing))])
            elif missing and include_system and not brew:
                system_instructions.append("未检测到 Homebrew；不会自动安装 Homebrew，请人工安装所需系统程序")
            elif missing:
                system_instructions.append("系统程序只列入计划；如需调用现有 Homebrew，请加 --include-system")
        elif platform.system() == "Windows" or os.name == "nt":
            system_instructions.append("Windows 为实验性支持；只检测并提示系统程序，不自动安装")
        else:
            system_instructions.append("Linux 只提示系统程序，请使用本机包管理器人工安装")
    core = {
        "plan_version": "1.0", "tool_version": TOOL_VERSION, "status": "preview",
        "layers": layers, "data_directory": str(data_dir), "venv_directory": str(venv_dir),
        "venv_python": str(venv_python), "python_packages": packages,
        "system_commands": system_commands, "system_instructions": system_instructions,
        "effects": ["创建用户级虚拟环境", "安装所选 Python 依赖"]
        + (["调用现有系统包管理器安装所选程序"] if system_commands else []),
    }
    return {**core, "confirmation_token": digest(core)}


def apply_setup(plan: dict[str, Any], confirm: str) -> dict[str, Any]:
    expected = digest({key: value for key, value in plan.items() if key != "confirmation_token"})
    if confirm != expected or plan.get("confirmation_token") != expected:
        raise RuntimeError("setup confirmation_token 不匹配")
    venv_dir = Path(plan["venv_directory"])
    if not venv_dir.exists():
        venv_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([os.sys.executable, "-m", "venv", str(venv_dir)], check=True)
    python = Path(plan["venv_python"])
    packages = list(plan.get("python_packages", []))
    if packages:
        subprocess.run([str(python), "-m", "pip", "install", *packages], check=True)
    for command in plan.get("system_commands", []):
        subprocess.run(command, check=True)
    return {"status": "applied", "layers": plan["layers"], "venv_python": str(python),
            "system_instructions": plan.get("system_instructions", [])}


def template_check(mode: str, cover: Path, directory: Path,
                   expected_cover_pages: int, expected_directory_pages: int) -> dict[str, Any]:
    if mode not in {"pdf", "docx"}:
        raise ValueError("template mode 仅支持 pdf 或 docx")
    for path, label in [(cover, "cover"), (directory, "directory")]:
        if not path.is_file():
            raise ValueError(f"{label} 文件不存在：{path}")
        if path.is_symlink():
            raise ValueError(f"{label} 文件本身不得是符号链接：{path}")
    checks: list[dict[str, Any]] = []
    if mode == "pdf":
        require_core_dependencies()
        for path, label, expected_pages in [
            (cover, "cover", expected_cover_pages),
            (directory, "directory", expected_directory_pages),
        ]:
            if path.suffix.lower() != ".pdf":
                checks.append({"check": label, "ok": False, "detail": "PDF 模式要求 .pdf 文件"})
                continue
            pages = validate_pdf(path)
            checks.append({"check": label, "ok": pages == expected_pages,
                           "detail": {"pages": pages, "expected_pages": expected_pages}})
    else:
        require_docx_dependency()
        try:
            labels = cover_template_business_labels(cover)
            cover_failures: list[str] = [] if labels else ["封皮模板没有可识别业务字段"]
        except RuntimeError as exc:
            cover_failures = [str(exc)]
        directory_failures = preflight_directory_template(directory)
        checks.append({"check": "cover", "ok": not cover_failures, "detail": cover_failures})
        checks.append({"check": "directory", "ok": not directory_failures, "detail": directory_failures})
        if not discover_binary("soffice"):
            checks.append({"check": "soffice", "ok": False,
                           "detail": "缺少 LibreOffice；结构可检查，但不能自动转换"})
    return {
        "status": "compatible" if all(item["ok"] for item in checks) else "incompatible",
        "mode": mode,
        "cover": {"path": str(cover.resolve()), "sha256": sha256_file(cover),
                  "expected_pages": expected_cover_pages},
        "directory": {"path": str(directory.resolve()), "sha256": sha256_file(directory),
                      "expected_pages": expected_directory_pages},
        "checks": checks,
    }


def normalize_local_config(candidate: dict[str, Any], *, validate_templates: bool) -> tuple[dict[str, Any], list[str]]:
    if candidate.get("config_schema_version") != CONFIG_SCHEMA_VERSION:
        raise ValueError(f"config_schema_version 必须为 {CONFIG_SCHEMA_VERSION}")
    layers, policies = normalize_capability_settings(candidate)
    profiles = candidate.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("profiles 必须至少配置一种归档类型")
    normalized_profiles: dict[str, Any] = {}
    blockers: list[str] = []
    if policies["ocr"] == "local_only" and "ocr" not in layers:
        blockers.append("policies.ocr=local_only 时必须启用 ocr 功能层")
    for profile, raw in profiles.items():
        if profile not in PROFILES or not isinstance(raw, dict):
            raise ValueError(f"无效归档 profile：{profile}")
        mode = str(raw.get("template_mode", ""))
        if mode not in {"pdf", "docx"}:
            raise ValueError(f"{profile}.template_mode 仅支持 pdf 或 docx")
        if mode == "docx" and "docx" not in layers:
            blockers.append(f"{profile} 使用 DOCX 模板时必须启用 docx 功能层")
        rule = Path(str((raw.get("rule") or {}).get("path", ""))).expanduser()
        cover_raw = raw.get("cover") or {}
        directory_raw = raw.get("directory") or {}
        cover = Path(str(cover_raw.get("path", ""))).expanduser()
        directory = Path(str(directory_raw.get("path", ""))).expanduser()
        for path, label in [(rule, "rule"), (cover, "cover"), (directory, "directory")]:
            if not path.is_file():
                raise ValueError(f"{profile}.{label} 文件不存在：{path}")
        cover_pages = int(cover_raw.get("expected_pages", 1))
        directory_pages = int(directory_raw.get("expected_pages", 1))
        if validate_templates:
            try:
                check = template_check(mode, cover, directory, cover_pages, directory_pages)
                if check["status"] != "compatible":
                    blockers.append(f"{profile} 模板检查未通过")
            except RuntimeError as exc:
                check = {"status": "deferred_missing_dependency", "reason": str(exc)}
                blockers.append(f"{profile} 模板检查等待安装可选依赖：{exc}")
        else:
            check = {"status": "deferred", "reason": "未加载可选处理依赖"}
        ocr_enabled = bool(raw.get("ocr_enabled", False))
        if ocr_enabled and ("ocr" not in layers or policies["ocr"] != "local_only"):
            blockers.append(f"{profile}.ocr_enabled=true 时必须启用 ocr 功能层并设置 policies.ocr=local_only")
        normalized_profiles[profile] = {
            "template_mode": mode,
            "rule": {"path": str(rule.resolve()), "sha256": sha256_file(rule)},
            "cover": {"path": str(cover.resolve()), "sha256": sha256_file(cover),
                      "expected_pages": cover_pages},
            "directory": {"path": str(directory.resolve()), "sha256": sha256_file(directory),
                          "expected_pages": directory_pages},
            "ocr_enabled": ocr_enabled,
            "template_check": check,
        }
    report = dependency_report()
    return {
        "config_schema_version": CONFIG_SCHEMA_VERSION, "tool_version": TOOL_VERSION,
        "enabled_layers": layers,
        "policies": policies,
        "runtime": {"python_executable": report["python"]["executable"], "binaries": report["binaries"]},
        "profiles": normalized_profiles,
    }, blockers


def configure_plan(candidate_path: Path, target: Path, *, validate_templates: bool) -> dict[str, Any]:
    normalized, blockers = normalize_local_config(read_json(candidate_path), validate_templates=validate_templates)
    core = {
        "plan_version": "1.0", "status": "blocked" if blockers else "preview",
        "target": str(target), "candidate_sha256": sha256_file(candidate_path),
        "config": normalized, "blockers": blockers,
        "effects": ["在用户配置目录写入或替换 legal-matter-archive 配置"],
    }
    return {**core, "confirmation_token": digest(core)}


def apply_configuration(plan: dict[str, Any], confirm: str) -> dict[str, Any]:
    expected = digest({key: value for key, value in plan.items() if key != "confirmation_token"})
    if plan.get("status") != "preview" or confirm != expected or plan.get("confirmation_token") != expected:
        raise RuntimeError("configure confirmation_token 不匹配或配置仍有阻断项")
    target = Path(plan["target"]).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.configure-{uuid.uuid4().hex}"
    temporary.write_text(json.dumps(plan["config"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return {"status": "configured", "config": str(target), "config_schema_version": CONFIG_SCHEMA_VERSION}


def read_local_config(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if value.get("config_schema_version") != CONFIG_SCHEMA_VERSION:
        raise ValueError(f"config_schema_version 必须为 {CONFIG_SCHEMA_VERSION}")
    if not isinstance(value.get("profiles"), dict):
        raise ValueError("配置缺少 profiles")
    return value


def scoped_source_descriptor(
    root: Path,
    value: Any,
    label: str,
    blockers: list[str],
    *,
    require_confirmation: bool = False,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        blockers.append(f"{label} 必须是文件描述对象")
        return None
    raw_path = str(value.get("path", value.get("source", ""))).strip()
    if not raw_path:
        blockers.append(f"{label} 缺少 path")
        return None
    try:
        path = resolve_inside(root, raw_path, label, must_exist=True)
    except ValueError as exc:
        blockers.append(str(exc))
        return None
    if not path.is_file():
        blockers.append(f"{label} 不是文件：{raw_path}")
        return None
    snapshot = snapshot_file(root, path)
    expected = str(value.get("sha256", "")).strip().lower()
    if expected and expected != snapshot["sha256"]:
        blockers.append(f"{label} 哈希不匹配：{raw_path}")
    if require_confirmation and not value.get("lawyer_confirmed"):
        blockers.append(f"{label} 尚未完成律师确认：{raw_path}")
    return {
        "path": snapshot["path"],
        "sha256": snapshot["sha256"],
        "display_name": str(value.get("display_name") or path.stem),
        "reason": str(value.get("reason") or ""),
        "lawyer_confirmed": bool(value.get("lawyer_confirmed", not require_confirmation)),
        "source_snapshot": snapshot,
    }


def validate_engagement_unit(root: Path, value: Any, blockers: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(value, dict):
        blockers.append("engagement 必须是委托合同归档单元对象")
        return {}, []
    basis = str(value.get("basis", "")).strip()
    if basis not in {"written_contract", "written_contract_source_missing", "lawyer_designated_exception"}:
        blockers.append("engagement.basis 必须为 written_contract、written_contract_source_missing 或 lawyer_designated_exception")
    scope_summary = str(value.get("scope_summary", "")).strip()
    if not scope_summary:
        blockers.append("engagement.scope_summary 必须说明合同委托范围或律师指定范围")
    if not value.get("scope_confirmed"):
        blockers.append("委托合同归档范围尚未完成律师确认")

    snapshots: list[dict[str, Any]] = []
    primary: dict[str, Any] | None = None
    if basis == "written_contract":
        primary = scoped_source_descriptor(root, value.get("primary_agreement"), "主委托合同", blockers)
        if primary:
            snapshots.append(primary["source_snapshot"])
    elif basis == "written_contract_source_missing":
        if not str(value.get("agreement_reference", "")).strip():
            blockers.append("主委托合同文件缺失时必须提供 agreement_reference")
    elif basis == "lawyer_designated_exception" and not str(value.get("exception_reason", "")).strip():
        blockers.append("无书面委托合同的例外归档必须说明 exception_reason")

    supplements: list[dict[str, Any]] = []
    raw_supplements = value.get("supplemental_agreements", [])
    if not isinstance(raw_supplements, list):
        blockers.append("engagement.supplemental_agreements 必须是数组")
    else:
        for index, raw in enumerate(raw_supplements):
            descriptor = scoped_source_descriptor(root, raw, f"补充协议[{index}]", blockers)
            if descriptor:
                supplements.append(descriptor)
                snapshots.append(descriptor["source_snapshot"])

    shared_sources: list[dict[str, Any]] = []
    raw_shared = value.get("shared_sources", [])
    if not isinstance(raw_shared, list):
        blockers.append("engagement.shared_sources 必须是数组")
    else:
        for index, raw in enumerate(raw_shared):
            descriptor = scoped_source_descriptor(
                root, raw, f"跨委托共享材料[{index}]", blockers, require_confirmation=True,
            )
            if descriptor:
                if not descriptor["reason"]:
                    blockers.append(f"跨委托共享材料[{index}] 必须说明 reason")
                shared_sources.append(descriptor)
                snapshots.append(descriptor["source_snapshot"])

    normalized = {
        "basis": basis,
        "primary_agreement": primary,
        "supplemental_agreements": supplements,
        "shared_sources": shared_sources,
        "scope_summary": scope_summary,
        "scope_confirmed": bool(value.get("scope_confirmed")),
        "agreement_reference": str(value.get("agreement_reference") or ""),
        "exception_reason": str(value.get("exception_reason") or ""),
    }
    return normalized, snapshots


def validate_internal_close(value: Any, blockers: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        blockers.append("internal_close 必须记录律师确认的内部结案日期")
        return {}
    raw_date = str(value.get("date", "")).strip()
    try:
        date.fromisoformat(raw_date)
    except ValueError:
        blockers.append("internal_close.date 必须是 YYYY-MM-DD")
    if not value.get("lawyer_confirmed"):
        blockers.append("内部结案日期尚未完成律师确认")
    return {"date": raw_date, "lawyer_confirmed": bool(value.get("lawyer_confirmed"))}


def validate_archive_intake(value: Any, blockers: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        blockers.append("archive_intake 必须记录律师确认进入正式结案归档")
        return {}
    status = str(value.get("status", "")).strip()
    confirmed_by = str(value.get("confirmed_by", "")).strip()
    confirmed_on = str(value.get("confirmed_on", "")).strip()
    if status != "confirmed_for_formal_archive":
        blockers.append("archive_intake.status 必须为 confirmed_for_formal_archive")
    if value.get("lawyer_confirmed") is not True:
        blockers.append("尚未取得律师进入正式结案归档的明确确认")
    if not confirmed_by:
        blockers.append("archive_intake.confirmed_by 不得为空")
    try:
        date.fromisoformat(confirmed_on)
    except ValueError:
        blockers.append("archive_intake.confirmed_on 必须是 YYYY-MM-DD")
    return {
        "status": status,
        "lawyer_confirmed": value.get("lawyer_confirmed") is True,
        "confirmed_by": confirmed_by,
        "confirmed_on": confirmed_on,
        "note": str(value.get("note") or ""),
    }


def parse_display_date(value: str) -> str | None:
    compact = re.sub(r"\s+", "", value)
    match = re.fullmatch(r"(\d{4})[-年/.](\d{1,2})[-月/.](\d{1,2})日?", compact)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return None


def validate_procedure_disposition(value: Any, blockers: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        blockers.append("诉讼/仲裁归档必须单独记录 court_or_tribunal_disposition")
        return {}
    status = str(value.get("status", "")).strip()
    allowed = {
        "confirmed_final", "confirmed_non_merits_close", "final_document_missing",
        "pending_or_unknown", "not_applicable",
    }
    if status not in allowed:
        blockers.append("court_or_tribunal_disposition.status 不受支持")
    raw_date = value.get("date")
    if raw_date not in {None, ""}:
        try:
            date.fromisoformat(str(raw_date))
        except ValueError:
            blockers.append("court_or_tribunal_disposition.date 必须是 YYYY-MM-DD 或空值")
    if not value.get("lawyer_confirmed"):
        blockers.append("法院或仲裁机构程序处理状态尚未完成律师确认")
    return {
        "status": status,
        "date": str(raw_date or ""),
        "lawyer_confirmed": bool(value.get("lawyer_confirmed")),
        "note": str(value.get("note") or ""),
    }


def normalize_item(root: Path, raw: dict[str, Any], inventory_map: dict[str, dict[str, Any]], blockers: list[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        blockers.append("items 中每一项必须是对象")
        return None
    path_value = str(raw.get("source", raw.get("path", ""))).strip()
    if not path_value:
        blockers.append("归档条目缺少 source")
        return None
    try:
        path = resolve_inside(root, path_value, "item source", must_exist=True)
    except ValueError as exc:
        blockers.append(str(exc))
        return None
    if not path.is_file():
        blockers.append(f"归档条目不是文件：{path_value}")
        return None
    snapshot = inventory_map.get(path_value) or snapshot_file(root, path)
    policy = material_inclusion_policy(raw.get("category"), raw.get("display_name"), path_value)
    include_explicit = isinstance(raw.get("include"), bool)
    include = bool(raw["include"]) if include_explicit else (policy[1] if policy else False)
    confirmation_explicit = "decision_confirmed" in raw or "lawyer_confirmed" in raw
    if confirmation_explicit:
        confirmed = bool(raw.get("decision_confirmed", raw.get("lawyer_confirmed", False)))
        decision_basis = "explicit_confirmation" if confirmed else "explicitly_unconfirmed"
    elif policy and (not include_explicit or include == policy[1]):
        confirmed = True
        decision_basis = f"confirmed_policy:{policy[0]}"
    else:
        confirmed = False
        decision_basis = "unresolved"
    if not include_explicit and not policy:
        blockers.append(f"材料缺少纳入或排除决定：{path_value}")
    if not confirmed:
        blockers.append(f"材料纳入或排除决定尚未确认：{path_value}")
    order = raw.get("order")
    if include and (not isinstance(order, int) or order < 1):
        blockers.append(f"纳入材料必须有正整数 order：{path_value}")
    category = str(raw.get("category", "")).strip()
    if include and not category:
        blockers.append(f"纳入材料缺少 category：{path_value}")
    if include and snapshot["kind"] == "unsupported":
        blockers.append(f"纳入材料类型不受支持：{path_value}")
    if include and snapshot["structural_status"] in {"invalid", "blocked", "unsupported"}:
        blockers.append(f"纳入材料结构不可执行：{path_value}（{snapshot['structural_detail']}）")
    return {
        "source": path_value,
        "include": include,
        "decision_confirmed": confirmed,
        "decision_basis": decision_basis,
        "inclusion_policy": policy[0] if policy else "",
        "order": order,
        "category": category,
        "display_name": str(raw.get("display_name") or path.stem),
        "stage": str(raw.get("stage") or ""),
        "internal_note": str(raw.get("internal_note") or ""),
        "source_snapshot": snapshot,
    }


def is_lawyer_fee_invoice(category: str, label: str, source: str) -> bool:
    text = " ".join([category, label, source])
    return any(marker in text for marker in INVOICE_MARKERS) and "诉讼费" not in text


def suggest_directory_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a deterministic, lawyer-confirmable semantic grouping proposal.

    The proposal never becomes executable on its own.  It is intentionally
    conservative: only files with the same lawyer-supplied stage/category are
    combined, while agreements and lawyer-fee invoices get their own groups.
    """
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    order: list[tuple[str, str]] = []
    for item in items:
        category = item["category"]
        if is_lawyer_fee_invoice(category, item["display_name"], item["source"]):
            key = ("律师费发票", "收费材料")
        elif "委托" in category or "协议" in category:
            key = ("委托代理协议及补充协议", "委托代理协议")
        else:
            stage = item.get("stage") or ""
            label = f"{stage}{category}" if stage else category
            key = (label, category)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(item)
    return [
        {
            "group_id": f"suggested-{index:02d}",
            "order": index,
            "label": label,
            "category": category,
            "member_sources": [item["source"] for item in buckets[(label, category)]],
            "formal_remark": "",
            "formal_remark_confirmed": False,
        }
        for index, (label, category) in enumerate(order, start=1)
    ]


def normalize_directory_groups(
    raw_groups: Any,
    items: list[dict[str, Any]],
    blockers: list[str],
    risks: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    suggestions = suggest_directory_groups(items)
    if not isinstance(raw_groups, list) or not raw_groups:
        blockers.append("directory_groups 必须由律师确认；已在预览中提供语义分组建议")
        return [], suggestions

    source_map = {item["source"]: item for item in items}
    included_sources = set(source_map)
    groups: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    used_sources: set[str] = set()
    for raw in raw_groups:
        if not isinstance(raw, dict):
            blockers.append("directory_groups 每一项必须是对象")
            continue
        group_id = str(raw.get("group_id", "")).strip()
        label = str(raw.get("label", "")).strip()
        category = str(raw.get("category", "")).strip()
        sequence = raw.get("order")
        members = raw.get("member_sources")
        formal_remark = str(raw.get("formal_remark", "")).strip()
        if not group_id or not re.fullmatch(r"[A-Za-z0-9._-]+", group_id):
            blockers.append("directory_groups.group_id 必须是非空 ASCII 标识")
        if group_id in seen_ids:
            blockers.append(f"directory_groups.group_id 重复：{group_id}")
        seen_ids.add(group_id)
        if not label or not category:
            blockers.append(f"目录组必须包含 label 和 category：{group_id or '[未命名]'}")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            blockers.append(f"目录组必须包含正整数 order：{group_id or '[未命名]'}")
        if not isinstance(members, list) or not members:
            blockers.append(f"目录组必须包含 member_sources：{group_id or '[未命名]'}")
            members = []
        normalized_members = [str(value).strip() for value in members if str(value).strip()]
        if len(normalized_members) != len(members) or len(set(normalized_members)) != len(normalized_members):
            blockers.append(f"目录组成员不得为空或重复：{group_id or '[未命名]'}")
        unknown = [source for source in normalized_members if source not in source_map]
        if unknown:
            blockers.append(f"目录组引用了未纳入材料：{group_id or '[未命名]'}：{'、'.join(unknown)}")
        repeated = sorted(set(normalized_members).intersection(used_sources))
        if repeated:
            blockers.append(f"材料不得同时属于多个目录组：{'、'.join(repeated)}")
        used_sources.update(normalized_members)
        if formal_remark:
            if raw.get("formal_remark_confirmed") is not True:
                blockers.append(f"正式备注必须单独经律师确认：{group_id or '[未命名]'}")
            lowered = formal_remark.casefold()
            forbidden = [marker for marker in FORMAL_INTERNAL_MARKERS if marker.casefold() in lowered]
            if forbidden:
                blockers.append(f"正式备注不得含内部工作流语言：{group_id or '[未命名]'}：{'、'.join(forbidden)}")
        groups.append({
            "group_id": group_id,
            "order": sequence,
            "label": label,
            "category": category,
            "member_sources": normalized_members,
            "formal_remark": formal_remark,
            "formal_remark_confirmed": bool(raw.get("formal_remark_confirmed")) if formal_remark else False,
        })
    orders = [group["order"] for group in groups if isinstance(group.get("order"), int)]
    if len(orders) != len(set(orders)):
        blockers.append("目录组的 order 不得重复")
    missing = sorted(included_sources - used_sources)
    if missing:
        blockers.append("以下纳入材料尚未归入目录组：" + "、".join(missing))
    groups.sort(key=lambda group: (group["order"], group["group_id"]))

    fee_positions = [index for index, group in enumerate(groups) if is_lawyer_fee_invoice(group["category"], group["label"], "")]
    agreement_positions = [index for index, group in enumerate(groups) if "委托" in group["category"] or "协议" in group["category"]]
    if fee_positions and agreement_positions and fee_positions[0] != agreement_positions[-1] + 1:
        risks.append("律师费发票通常建议紧随委托代理协议及补充协议；当前顺序已保留，须由律师在 Gate A 明确确认")
    ranks = [DIRECTORY_CATEGORY_RANK.get(group["category"], 50) for group in groups]
    if any(left > right for left, right in zip(ranks, ranks[1:])):
        risks.append("目录组顺序与诉讼归档建议顺序存在偏离；Tool 不自动改序，须由律师确认")
    return groups, suggestions


def summary_draft(
    profile: str,
    matter_id: str,
    engagement: dict[str, Any],
    internal_close: dict[str, Any],
    procedure_disposition: dict[str, Any],
    engagement_snapshot: list[dict[str, Any]],
) -> str:
    title = "办案小结" if profile == "litigation" else "项目工作小结"
    scope_summary = engagement.get("scope_summary") or "[待补充委托范围]"
    close_date = internal_close.get("date") or "[待确认]"
    procedure_status = procedure_disposition.get("status") or "[待确认或不适用]"
    process_text = "接受委托后，承办律师围绕委托事项开展了[待补充具体办理工作]。"
    result_text = "[待读取并核对裁判文书、办理成果正文或律师确认内容后，补充办理结果。]"
    return f"""# {title}（待律师确认）

## 基本信息

- 项目标识：{matter_id}
- 内部结案日期：{close_date}

## 委托范围

{scope_summary}

## 主要办理过程

{process_text}

## 主要成果与办理结果

{result_text}

## 未完成事项与后续安排

[待补充。]

## 承办律师

- 承办律师：[待填写]
- 日期：[待填写]

## AI 内部提示（定稿前删除）

- 当前委托合同归档范围快照共记录 {len(engagement_snapshot)} 个文件。
- 法院或仲裁机构程序处理状态字段为：`{procedure_status}`。请结合正式结案文书核对正文表述，内部结案日期不能替代程序处理结果。
- 办案或项目小结不重复罗列已归档材料、收费材料、纳入排除决定或页码处理；这些内容由目录和 manifest 承载。只有 current 要求的必要归档材料确有缺失时，才在正文增加“必要归档材料缺失情况”。
- 办理过程和结果须从来源材料或律师确认内容核对；机器元数据不得写作案件事实。
- 如使用本地 OCR 辅助读取扫描件，请逐项核对姓名、日期、金额、案号、裁判结果和页码后再定稿。
- 本提示为内部起草辅助内容，正式归档前应整体删除。
"""


def normalize_public_request(request: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    profile = str(request.get("profile", ""))
    profile_config = (config.get("profiles") or {}).get(profile)
    if not isinstance(profile_config, dict):
        raise ValueError(f"本地配置未启用归档 profile：{profile}")
    matter = request.get("matter") if isinstance(request.get("matter"), dict) else {}
    confirmations = request.get("confirmations") if isinstance(request.get("confirmations"), dict) else {}
    engagement = deepcopy(request.get("engagement") or {})
    if isinstance(engagement, dict):
        engagement["scope_confirmed"] = confirmations.get("scope_confirmed") is True
    confirmed_by = str(confirmations.get("confirmed_by", ""))
    confirmed_on = str(confirmations.get("confirmed_on", ""))
    items = deepcopy(request.get("items") or [])
    for exclusion in request.get("exclusions") or []:
        if not isinstance(exclusion, dict):
            continue
        items.append({
            "source": exclusion.get("source"), "include": False,
            "decision_confirmed": exclusion.get("lawyer_confirmed") is True,
            "display_name": exclusion.get("display_name", ""),
            "internal_note": exclusion.get("reason", ""),
        })
    source_roots = request.get("source_roots")
    if source_roots is None and request.get("source_root") is not None:
        source_roots = [request.get("source_root")]
    enabled_layers, policies = normalize_capability_settings(config)
    execution_policy = {
        "enabled_layers": enabled_layers,
        "policies": policies,
        "profile_ocr_enabled": bool(profile_config.get("ocr_enabled", False)),
    }
    execution_policy["fingerprint"] = digest({
        "config_schema_version": config.get("config_schema_version"),
        "profile": profile,
        "profile_config": profile_config,
        "execution_policy": execution_policy,
    })
    return {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "matter_id": str(matter.get("id", "")),
        "matter_subtype": str(matter.get("subtype", "")),
        "case_numbers": deepcopy(matter.get("case_numbers") or []),
        "stages": deepcopy(matter.get("stages") or []),
        "archive_intake": {
            "status": "confirmed_for_formal_archive" if confirmations.get("formal_archive") is True else "unconfirmed",
            "lawyer_confirmed": confirmations.get("formal_archive") is True,
            "confirmed_by": confirmed_by, "confirmed_on": confirmed_on,
        },
        "engagement": engagement,
        "internal_close": {"date": matter.get("internal_close_date", ""),
                           "lawyer_confirmed": confirmations.get("internal_close") is True},
        "court_or_tribunal_disposition": deepcopy(request.get("court_or_tribunal_disposition")),
        "source_roots": deepcopy(source_roots or []),
        "output_root": request.get("output_root", ""), "run_id": request.get("run_id"),
        "rule_source": deepcopy(profile_config.get("rule")),
        "templates": {"cover": deepcopy(profile_config.get("cover")),
                      "directory": deepcopy(profile_config.get("directory"))},
        "template_mode": profile_config.get("template_mode"),
        "items": items, "summary": deepcopy(request.get("summary")),
        "required_categories": deepcopy(request.get("required_categories") or []),
        "duplicates_reviewed": confirmations.get("duplicates_reviewed") is True,
        "cover_fields_confirmed": confirmations.get("cover_directory_reviewed") is True,
        "cover_fields": deepcopy(request.get("cover_fields") or []),
        "cover_completion": deepcopy(request.get("cover_completion")),
        "cover_render": deepcopy(request.get("cover_render") or {
            "fit_policy": "preserve",
            "expected_pages": int((profile_config.get("cover") or {}).get("expected_pages", 1)),
            "lawyer_confirmed": confirmations.get("cover_directory_reviewed") is True,
        }),
        "page_number_policy": request.get("page_number_policy", "overlay_bottom_right"),
        "existing_page_numbers_reviewed": confirmations.get("existing_page_numbers_reviewed") is True,
        "directory_groups": deepcopy(request.get("directory_groups")),
        "directory_groups_confirmed": confirmations.get("directory_confirmed") is True,
        "missing_material_disposition": deepcopy(request.get("missing_material_disposition")),
        "pending_decisions": deepcopy(request.get("pending_decisions") or []),
        "execution_policy": execution_policy,
        "config_snapshot": {
            "config_schema_version": config.get("config_schema_version"), "profile": profile,
            "profile_fingerprint": digest(profile_config),
            "execution_policy_fingerprint": execution_policy["fingerprint"],
        },
    }


def build_plan(root: Path, request_path: Path, public_request: dict[str, Any], config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    blockers: list[str] = []
    risks: list[str] = []
    pending: list[str] = []
    request_schema = read_json(SCHEMA_DIR / "archive_request.schema.json")
    blockers.extend(
        f"archive request Schema：{error}"
        for error in validate_json_schema_value(public_request, request_schema, request_schema)
    )
    request = normalize_public_request(public_request, config)
    execution_policy = request["execution_policy"]
    enabled_layers = set(execution_policy["enabled_layers"])
    policies = execution_policy["policies"]
    if request.get("schema_version") != SCHEMA_VERSION:
        blockers.append(f"archive request schema_version 必须为 {SCHEMA_VERSION}")
    profile = str(request.get("profile", ""))
    if profile not in PROFILES:
        blockers.append(f"不支持的 profile：{profile}")
    matter_id = str(request.get("matter_id", "")).strip()
    if not matter_id:
        blockers.append("archive request 缺少 matter_id")
    archive_intake = validate_archive_intake(request.get("archive_intake"), blockers)
    engagement, engagement_basis_snapshots = validate_engagement_unit(root, request.get("engagement"), blockers)
    internal_close = validate_internal_close(request.get("internal_close"), blockers)
    if engagement.get("basis") == "written_contract_source_missing":
        risks.append("主委托合同关系已由律师确认，但合同文件尚未取得；必须列入必备材料缺失处理并在卷内披露")
    raw_case_numbers = request.get("case_numbers", [])
    case_numbers: list[str] = []
    if raw_case_numbers is None:
        raw_case_numbers = []
    if not isinstance(raw_case_numbers, list):
        blockers.append("case_numbers 必须是字符串数组")
    else:
        case_numbers = [str(value).strip() for value in raw_case_numbers if str(value).strip()]
        if len(case_numbers) != len(raw_case_numbers):
            blockers.append("case_numbers 不得包含空值")
        if len(set(case_numbers)) != len(case_numbers):
            blockers.append("case_numbers 不得重复")

    procedure_disposition: dict[str, Any] = {}
    if profile == "litigation":
        procedure_disposition = validate_procedure_disposition(
            request.get("court_or_tribunal_disposition"), blockers,
        )

    sources = validate_source_roots(root, request.get("source_roots", []))
    output = validate_output_root(root, request.get("output_root", ""), sources)
    inventory = scan_sources(root, sources, output)
    inventory_map = {item["path"]: item for item in inventory}
    package_duplicates, package_same_name_conflicts = duplicate_groups(inventory)

    rule_path, rule_snapshot, rule_error = resolve_current_source(root, request.get("rule_source"), "归档规则")
    cover_path, cover_snapshot, cover_error = resolve_current_source(root, (request.get("templates") or {}).get("cover"), "卷宗封皮模板")
    directory_path, directory_snapshot, directory_error = resolve_current_source(root, (request.get("templates") or {}).get("directory"), "案卷目录模板")
    for error in [rule_error, cover_error, directory_error]:
        if error:
            blockers.append(error)

    template_mode = str(request.get("template_mode", ""))
    if template_mode not in {"pdf", "docx"}:
        blockers.append("template_mode 必须为 pdf 或 docx")
    if not request.get("cover_fields_confirmed"):
        blockers.append("封皮和目录尚未完成律师确认")
    cover_fields = request.get("cover_fields")
    if not isinstance(cover_fields, list):
        blockers.append("cover_fields 必须是数组")
        cover_fields = []
    if template_mode == "docx":
        if not cover_fields:
            blockers.append("DOCX 模板模式必须提供 cover_fields")
        fillable_cover_fields: list[dict[str, Any]] = []
        for field in cover_fields:
            if not isinstance(field, dict) or not str(field.get("label", "")).strip() or not isinstance(field.get("value", ""), str):
                blockers.append("每个 cover_fields 条目必须包含 label 和字符串 value")
                continue
            value_source = str(field.get("value_source", "literal"))
            if value_source not in {"literal", "computed_body_page_count"}:
                blockers.append("cover_fields.value_source 仅支持 literal 或 computed_body_page_count")
                continue
            if value_source == "computed_body_page_count" and normalized_label(str(field.get("label", ""))) != "卷内页数":
                blockers.append("computed_body_page_count 只能用于封皮“卷内页数”")
                continue
            field_label = normalized_label(str(field.get("label", "")))
            if field_label == "案件编号":
                if str(field.get("value", "")).strip():
                    blockers.append("封皮“案件编号”必须留空，不得用法院案号代替")
                continue
            if field_label == "收案日期" and engagement.get("basis") == "written_contract_source_missing" and str(field.get("value", "")).strip():
                blockers.append("主委托合同文件缺失时，封皮收案日期必须留空")
            fillable_cover_fields.append(field)
        cover_fields = fillable_cover_fields
        cover_completion, cover_render = validate_cover_controls(request, cover_fields, blockers, cover_path)
        for field in cover_fields:
            if normalized_label(str(field.get("label", ""))) == "结案日期":
                cover_close_date = parse_display_date(str(field.get("value", "")))
                if not cover_close_date or cover_close_date != internal_close.get("date"):
                    blockers.append("封皮结案日期必须有效并与 matter.internal_close_date 一致")
        if cover_path:
            if cover_snapshot is not None:
                try:
                    cover_snapshot["layout_fingerprint"] = cover_layout_fingerprint(cover_path)
                except RuntimeError as exc:
                    blockers.append(str(exc))
            blockers.extend(preflight_cover_template(cover_path, cover_fields))
        if directory_path:
            blockers.extend(preflight_directory_template(directory_path))
    else:
        cover_fields = []
        cover_completion = {}
        cover_render = {
            "fit_policy": "prefilled_pdf",
            "expected_pages": int(((request.get("templates") or {}).get("cover") or {}).get("expected_pages", 1)),
            "lawyer_confirmed": request.get("cover_fields_confirmed") is True,
        }
        for path, label in [(cover_path, "封皮"), (directory_path, "目录")]:
            if path is not None and path.suffix.lower() != ".pdf":
                blockers.append(f"PDF 模式的{label}必须是 .pdf 文件")

    dependencies = dependency_report()
    if not dependencies["capabilities"]["core"]:
        blockers.append("缺少基础 PDF 依赖 pypdf 或 reportlab")
    if template_mode == "docx":
        if "docx" not in enabled_layers:
            blockers.append("DOCX 模板模式未在配置中启用 docx 功能层")
        elif not dependencies["capabilities"]["docx"]:
            blockers.append("DOCX 模板模式缺少 lxml 或 LibreOffice")
    policy = request.get("page_number_policy")
    if policy not in {"overlay", "overlay_bottom_right", "keep_confirmed"}:
        blockers.append("page_number_policy 必须为 overlay、overlay_bottom_right 或 keep_confirmed")
    if not request.get("existing_page_numbers_reviewed"):
        blockers.append("尚未确认正文是否已有页码及处理方式")

    request_pending = request.get("pending_decisions", [])
    if not isinstance(request_pending, list):
        blockers.append("pending_decisions 必须是数组")
    elif request_pending:
        pending.extend(str(item) for item in request_pending)
        blockers.append("archive request 仍含未解决的待确认事项")

    values = request.get("items", [])
    raw_items: list[dict[str, Any]] = []
    if not isinstance(values, list):
        blockers.append("items 必须是数组")
    else:
        raw_items = list(values)
        risks.append("收费材料的纳入或排除只按本次经律师确认的 items 和 exclusions 执行")
    items: list[dict[str, Any]] = []
    for raw in raw_items:
        item = normalize_item(root, raw, inventory_map, blockers)
        if item:
            items.append(item)

    if profile in {"litigation", "special_nonlitigation"}:
        summary = request.get("summary") or {}
        if summary.get("status") != "lawyer_confirmed" or not summary.get("path"):
            blockers.append("办案小结或项目工作小结尚未完成律师确认")
        else:
            raw = {
                "source": summary.get("path"), "include": True, "decision_confirmed": True,
                "order": summary.get("order", max([item.get("order") or 0 for item in items] + [0]) + 1),
                "category": "办案小结" if profile == "litigation" else "项目工作小结",
                "display_name": "办案小结" if profile == "litigation" else "项目工作小结",
            }
            item = normalize_item(root, raw, inventory_map, blockers)
            if item:
                expected = str(summary.get("sha256", "")).lower()
                if expected and expected != item["source_snapshot"]["sha256"]:
                    blockers.append("律师确认小结哈希不匹配")
                if not any(existing["source"] == item["source"] for existing in items):
                    items.append(item)

    included = [item for item in items if item["include"]]
    if any(item["source_snapshot"].get("kind") == "image" for item in included):
        if "image" not in enabled_layers:
            blockers.append("归档材料包含图片，但配置未启用 image 功能层")
        elif not dependencies["capabilities"]["image"]:
            blockers.append("归档材料包含图片，但缺少 Pillow 图像能力")
    if any(item["source_snapshot"].get("kind") == "office" for item in included):
        if "docx" not in enabled_layers:
            blockers.append("归档材料包含 Office 文件，但配置未启用 docx 功能层")
        elif not dependencies["binaries"].get("soffice"):
            blockers.append("归档材料包含 Office 文件，但缺少 LibreOffice")
    included_paths = {item["source"] for item in included}
    primary_agreement = engagement.get("primary_agreement")
    if primary_agreement and primary_agreement["path"] not in included_paths:
        blockers.append("主委托合同必须作为本归档单元材料纳入 items")
    for supplement in engagement.get("supplemental_agreements", []):
        if supplement["path"] not in included_paths:
            blockers.append(f"用于界定本归档范围的补充协议必须纳入 items：{supplement['path']}")

    printable_encrypted = [
        item for item in included
        if item["source_snapshot"].get("pdf_security", {}).get("print_conversion_required")
    ]
    if printable_encrypted:
        if policies["encrypted_pdf"] == "block_all":
            blockers.append("本地策略禁止处理任何加密 PDF")
        elif "encrypted-pdf" not in enabled_layers:
            blockers.append("归档材料包含允许打印的权限型加密 PDF，但配置未启用 encrypted-pdf 功能层")
        elif not dependencies["binaries"].get("gs"):
            blockers.append("缺少 Ghostscript/gs，无法为允许打印的权限型加密 PDF 生成派生副本")
        else:
            risks.extend(
                f"权限型加密 PDF 将以打印派生副本入卷，Gate B 必须逐页核对：{item['source']}"
                for item in printable_encrypted
            )

    required = request.get("required_categories")
    missing_categories: list[str] = []
    missing_disposition: dict[str, Any] | None = None
    if not isinstance(required, list) or not required:
        pending.append("未提供经律师确认的必备材料类别")
        blockers.append("required_categories 必须由律师确认并提供")
    else:
        if engagement.get("basis") == "written_contract_source_missing" and not any(
            "委托" in str(category) or "合同" in str(category) or "代理协议" in str(category)
            for category in required
        ):
            blockers.append("主委托合同文件缺失时 required_categories 必须保留对应合同类别")
        included_categories = {item["category"] for item in included}
        missing_categories = [str(category) for category in required if str(category) not in included_categories]
        if missing_categories:
            raw_disposition = request.get("missing_material_disposition")
            if not isinstance(raw_disposition, dict):
                blockers.append("缺少律师确认的必备材料类别，且未提供 missing_material_disposition")
            else:
                status = str(raw_disposition.get("status", "")).strip()
                missing_disposition = {
                    "status": status,
                    "categories": [str(value) for value in raw_disposition.get("categories", [])]
                    if isinstance(raw_disposition.get("categories", []), list) else [],
                    "lawyer_confirmed": bool(raw_disposition.get("lawyer_confirmed")),
                    "note": str(raw_disposition.get("note") or ""),
                }
                covered = set(missing_categories).issubset(set(missing_disposition["categories"]))
                if status != "confirmed_unavailable_with_explanation" or not missing_disposition["lawyer_confirmed"] or not covered:
                    blockers.append("缺失必备材料尚未取得律师确认的缺失说明和继续归档决定")
                else:
                    explanation = raw_disposition.get("explanation")
                    if not isinstance(explanation, dict):
                        blockers.append("确认带缺失材料归档时必须提供 explanation 文件")
                    else:
                        raw = {
                            "source": explanation.get("path", explanation.get("source")),
                            "include": True,
                            "decision_confirmed": True,
                            "order": explanation.get("order"),
                            "category": explanation.get("category", "材料缺失说明"),
                            "display_name": explanation.get("display_name", "材料缺失说明"),
                            "internal_note": "律师确认缺失材料无法补齐并以说明文件继续归档",
                        }
                        item = normalize_item(root, raw, inventory_map, blockers)
                        if item:
                            expected = str(explanation.get("sha256", "")).strip().lower()
                            if expected and expected != item["source_snapshot"]["sha256"]:
                                blockers.append("材料缺失说明哈希不匹配")
                            if not any(existing["source"] == item["source"] for existing in items):
                                items.append(item)
                                included.append(item)
                            missing_disposition["explanation"] = {
                                "path": item["source"], "sha256": item["source_snapshot"]["sha256"],
                                "category": item["category"],
                            }
                            risks.append("律师确认在必备材料缺失情况下以缺失说明继续归档；目录和小结必须披露该缺失")

    inventory_paths = set(inventory_map)
    decided_paths = {item["source"] for item in items}
    unreviewed_inventory = sorted(inventory_paths - decided_paths)
    if unreviewed_inventory:
        blockers.append("材料包存在尚未明确纳入或排除的文件：" + "、".join(unreviewed_inventory))
    out_of_scope_items = sorted(
        item["source"] for item in items
        if not any(is_within(resolve_inside(root, item["source"], "item source", must_exist=True), source) for source in sources)
    )
    if out_of_scope_items:
        blockers.append("items 或 exclusions 引用了 source_roots 之外的文件：" + "、".join(out_of_scope_items))
    if execution_policy.get("profile_ocr_enabled"):
        risks.append("0.1.0-beta 仅检测本地 Tesseract 并记录 OCR 策略；归档 Tool 不自动执行 OCR，OCR 结果须在外部本地流程中逐项校对")

    orders = [item.get("order") for item in included]
    if len(orders) != len(set(orders)):
        blockers.append("纳入材料的 order 不得重复")
    included.sort(key=lambda item: (item.get("order") or 0, item["source"]))

    raw_directory_groups = deepcopy(request.get("directory_groups"))
    if (not isinstance(raw_directory_groups, list) or not raw_directory_groups) and request.get("directory_groups_confirmed") is True:
        raw_directory_groups = suggest_directory_groups(included)
        risks.append("未提供自定义目录分组；按律师已确认的材料类别和顺序生成通用语义分组")
    if isinstance(raw_directory_groups, list) and raw_directory_groups:
        grouped_sources = {
            str(source).strip()
            for group in raw_directory_groups if isinstance(group, dict)
            for source in group.get("member_sources", []) if str(source).strip()
        }
        summary_items = [item for item in included if item["category"] in {"办案小结", "项目工作小结"}]
        for summary_item in summary_items:
            if summary_item["source"] not in grouped_sources:
                next_order = max(
                    [int(group.get("order", 0)) for group in raw_directory_groups if isinstance(group, dict)] + [0]
                ) + 1
                raw_directory_groups.append({
                    "group_id": f"summary-{next_order}",
                    "order": next_order,
                    "label": summary_item["display_name"],
                    "category": summary_item["category"],
                    "member_sources": [summary_item["source"]],
                    "formal_remark": "",
                    "formal_remark_confirmed": False,
                    "auto_added": "confirmed_summary_last",
                })
                grouped_sources.add(summary_item["source"])
    if request.get("directory_groups_confirmed") is not True:
        blockers.append("目录语义分组及顺序尚未完成律师确认")
    directory_groups, grouping_suggestions = normalize_directory_groups(raw_directory_groups, included, blockers, risks)

    engagement_snapshot_by_path: dict[str, dict[str, Any]] = {
        snapshot["path"]: snapshot for snapshot in engagement_basis_snapshots
    }
    for item in items:
        engagement_snapshot_by_path[item["source_snapshot"]["path"]] = item["source_snapshot"]
    engagement_source_snapshot = sorted(engagement_snapshot_by_path.values(), key=lambda value: value["path"])
    exact_duplicates, same_name_conflicts = duplicate_groups(engagement_source_snapshot)

    included_hashes: dict[str, list[str]] = defaultdict(list)
    for item in included:
        included_hashes[item["source_snapshot"]["sha256"]].append(item["source"])
    repeated_included = [paths for paths in included_hashes.values() if len(paths) > 1]
    if repeated_included and not request.get("duplicates_reviewed"):
        blockers.append("纳入材料存在完全重复文件，尚未完成重复处理确认")
    elif repeated_included:
        risks.append("律师确认保留了内容完全相同的多个文件")
    if same_name_conflicts:
        risks.append("发现同名不同内容文件，必须依已确认 order 和显示名称区分")

    stages = request.get("stages", [])
    if profile == "litigation":
        if not isinstance(stages, list) or not stages:
            blockers.append("诉讼 profile 必须提供经确认的 stages")
        elif any(stage not in STAGES for stage in stages):
            blockers.append("stages 含不支持的诉讼阶段")
    subtype = str(request.get("matter_subtype", "")).strip()
    if profile == "special_nonlitigation" and not subtype:
        blockers.append("专项非诉 profile 必须提供 matter_subtype")

    run_id = str(request.get("run_id") or f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{digest({'matter': matter_id, 'request': request, 'inventory': inventory}, 6)}")
    if not SAFE_RUN_ID.fullmatch(run_id):
        raise ValueError("run_id 必须以 run- 开头且只含字母、数字、点、下划线或短横线")
    run_dir = output / run_id
    if run_dir.exists():
        raise ValueError(f"run 已存在，禁止覆盖：{relative(root, run_dir)}")

    core = {
        "schema_version": SCHEMA_VERSION,
        "project_root_fingerprint": hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest(),
        "request_path": relative(root, request_path),
        "request_sha256": sha256_file(request_path),
        "profile": profile,
        "matter_id": matter_id,
        "archive_intake": archive_intake,
        "engagement": engagement,
        "case_numbers": case_numbers,
        "matter_subtype": subtype,
        "stages": stages,
        "run_dir": relative(root, run_dir),
        "output_root": relative(root, output),
        "source_roots": [relative(root, path) for path in sources],
        "package_inventory": inventory,
        "unreviewed_inventory": unreviewed_inventory,
        "out_of_scope_items": out_of_scope_items,
        "engagement_source_snapshot": engagement_source_snapshot,
        "items": included,
        "directory_groups": directory_groups,
        "directory_grouping_suggestions": grouping_suggestions,
        "excluded_items": [item for item in items if not item["include"]],
        "required_categories": required if isinstance(required, list) else [],
        "missing_categories": missing_categories,
        "missing_material_disposition": missing_disposition,
        "duplicates": exact_duplicates,
        "same_name_conflicts": same_name_conflicts,
        "package_duplicates": package_duplicates,
        "package_same_name_conflicts": package_same_name_conflicts,
        "current": {
            "rule": {"path": str(rule_path) if rule_path else "", "snapshot": rule_snapshot},
            "cover": {"path": str(cover_path) if cover_path else "", "snapshot": cover_snapshot},
            "directory": {"path": str(directory_path) if directory_path else "", "snapshot": directory_snapshot},
        },
        "template_mode": template_mode,
        "execution_policy": execution_policy,
        "config_snapshot": request.get("config_snapshot", {}),
        "cover_fields": cover_fields,
        "cover_completion": cover_completion,
        "cover_render": cover_render,
        "internal_close": internal_close,
        "court_or_tribunal_disposition": procedure_disposition,
        "page_number_policy": policy,
        "existing_page_numbers_reviewed": bool(request.get("existing_page_numbers_reviewed")),
        "pending_decisions": sorted(set(pending)),
        "blockers": sorted(set(blockers)),
        "risks": sorted(set(risks)),
    }
    token = digest(core)
    plan = {**core, "status": "blocked" if blockers else "preview", "confirmation_token": token}
    validate_schema_document(plan, "archive_plan.schema.json", "archive plan")
    names = {
        "summary": "办案小结-待律师确认.md" if profile == "litigation" else "项目工作小结-待律师确认.md",
    }
    return plan, names


def write_preview(root: Path, plan: dict[str, Any], names: dict[str, str]) -> Path:
    run_dir = resolve_inside(root, plan["run_dir"], "run_dir")
    preview = run_dir / "00_preview"
    preview.mkdir(parents=True, exist_ok=False)
    plan_path = preview / "archive-plan.json"
    write_new_json(plan_path, plan)

    lines = [
        "# 归档预览", "",
        f"- Profile：`{plan['profile']}`", f"- 项目标识：`{plan['matter_id']}`",
        f"- 案号：{'；'.join(plan.get('case_numbers', [])) or '无或不适用'}",
        f"- 归档单元：以一份主委托合同及属于该合同的补充协议为依据；该合同范围内 {len(plan.get('case_numbers', []))} 个案号不拆分",
        f"- 委托范围：{plan.get('engagement', {}).get('scope_summary', '待确认')}",
        f"- 状态：`{plan['status']}`", f"- 纳入文件：{len(plan['items'])}",
        f"- 材料包盘点文件：{len(plan['package_inventory'])}",
        f"- 本委托范围快照文件：{len(plan['engagement_source_snapshot'])}", "",
        "## 阻断项", "",
    ]
    lines.extend([f"- {item}" for item in plan["blockers"]] or ["- 无"])
    lines.extend(["", "## 风险与人工复核", ""])
    lines.extend([f"- {item}" for item in plan["risks"]] or ["- 无"])
    lines.extend(["", "## 说明", "", "生成本预览不表示归档完成；只有律师确认后才能进入 prepare。", ""])
    write_new_text(preview / "archive-preview.md", "\n".join(lines))

    with (preview / "proposed-order.csv").open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["原始顺序", "类别", "显示名称", "来源相对路径", "内部说明（不进入正式目录）"])
        for item in plan["items"]:
            writer.writerow([
                item["order"], item["category"], item["display_name"], item["source"],
                item.get("internal_note", ""),
            ])

    with (preview / "proposed-directory-groups.csv").open("x", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["目录顺序", "组标识", "正式目录名称", "目录类别", "成员来源相对路径", "正式备注（已确认才可显示）"])
        for group in plan.get("directory_groups", []):
            writer.writerow([
                group["order"], group["group_id"], group["label"], group["category"],
                "；".join(group["member_sources"]), group.get("formal_remark", ""),
            ])

    missing = ["# 缺失材料", ""]
    missing.extend([f"- {item}" for item in plan["missing_categories"]] or ["- 无已识别的必备类别缺失；仍以律师复核为准。"])
    write_new_text(preview / "missing-materials.md", "\n".join(missing) + "\n")

    duplicate_lines = ["# 重复和冲突", "", "## 完全重复", ""]
    for group in plan["duplicates"]:
        duplicate_lines.append(f"- `{group['sha256']}`：" + "；".join(group["paths"]))
    if not plan["duplicates"]:
        duplicate_lines.append("- 无")
    duplicate_lines.extend(["", "## 同名不同内容", ""])
    for group in plan["same_name_conflicts"]:
        duplicate_lines.append(f"- {group['name']}：" + "；".join(group["paths"]))
    if not plan["same_name_conflicts"]:
        duplicate_lines.append("- 无")
    write_new_text(preview / "duplicates-and-conflicts.md", "\n".join(duplicate_lines) + "\n")

    pending_lines = ["# 待确认事项", ""]
    pending_lines.extend([f"- {item}" for item in plan["pending_decisions"]] or ["- 无；仍须完成 plan 的明确律师确认。"])
    write_new_text(preview / "pending-decisions.md", "\n".join(pending_lines) + "\n")
    proposed = """# 拟生成文件

- `10_converted/`：格式统一后的独立副本
- `20_numbered-items/unpaginated/`：按律师确认顺序形成的分类 PDF
- `30_front-matter/`：封皮和目录 DOCX/PDF
- `40_final/matter-archive.pdf`：最终卷宗 PDF
- `50_manifest/`：准备和最终 manifest
- `60_qa/`：技术及逐页视觉核验
"""
    write_new_text(preview / "proposed-output-files.md", proposed)
    if plan["profile"] in {"litigation", "special_nonlitigation"} and any("小结" in blocker for blocker in plan["blockers"]):
        write_new_text(preview / names["summary"], summary_draft(
            plan["profile"], plan["matter_id"], plan["engagement"], plan["internal_close"],
            plan.get("court_or_tribunal_disposition", {}), plan["engagement_source_snapshot"],
        ))
    return plan_path


def safe_slug(value: str, fallback: str = "item") -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "-", value).strip(" .-")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:80] or fallback


def convert_image_to_pdf(source: Path, target: Path) -> None:
    require_image_dependency()
    with Image.open(source) as image:
        frames: list[Image.Image] = []
        count = getattr(image, "n_frames", 1)
        for index in range(count):
            image.seek(index)
            frame = ImageOps.exif_transpose(image.copy()).convert("RGB")
            frames.append(frame)
        first, rest = frames[0], frames[1:]
        first.save(target, "PDF", save_all=bool(rest), append_images=rest, resolution=150.0)


def soffice_convert(source: Path, output_dir: Path) -> tuple[Path, str]:
    binary = discover_binary("soffice")
    if not binary:
        raise RuntimeError("缺少 LibreOffice/soffice，无法转换 Office 文件")
    profile = Path(tempfile.mkdtemp(prefix="legal-archive-lo-"))
    output_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(profile)
    cache = profile / "cache"
    cache.mkdir()
    env["XDG_CACHE_HOME"] = str(cache)
    if platform.system() == "Darwin":
        font_dirs = [
            path for path in [Path("/System/Library/Fonts"), Path("/System/Library/Fonts/Supplemental"), Path("/Library/Fonts")]
            if path.is_dir()
        ]
        if font_dirs:
            fontconfig = profile / "fonts.conf"
            entries = "".join(f"<dir>{path}</dir>" for path in font_dirs)
            fontconfig.write_text(
                f'<?xml version="1.0"?><!DOCTYPE fontconfig SYSTEM "fonts.dtd"><fontconfig>{entries}<cachedir>{cache / "fontconfig"}</cachedir></fontconfig>',
                encoding="utf-8",
            )
            env["FONTCONFIG_FILE"] = str(fontconfig)
    if platform.system() == "Darwin" and Path("/private/tmp").is_dir():
        env["TMPDIR"] = "/private/tmp"
        env["TEMP"] = "/private/tmp"
        env["TMP"] = "/private/tmp"
    command = [binary, "--headless", f"-env:UserInstallation=file://{profile}", "--convert-to", "pdf", "--outdir", str(output_dir), str(source)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180, env=env, check=False)
        target = output_dir / f"{source.stem}.pdf"
        if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
            detail = (result.stderr or result.stdout or "未知转换错误").strip()
            raise RuntimeError(f"LibreOffice 转换失败：{source.name}：{detail}")
        version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20, check=False).stdout.strip()
        return target, version or "LibreOffice"
    finally:
        shutil.rmtree(profile, ignore_errors=True)


def validate_pdf(path: Path) -> int:
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        raise RuntimeError(f"PDF 已加密：{path.name}")
    count = len(reader.pages)
    if count < 1:
        raise RuntimeError(f"PDF 无页面：{path.name}")
    return count


def pdf_page_geometry(path: Path) -> list[dict[str, float | int]]:
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        raise RuntimeError(f"PDF 已加密：{path.name}")
    geometry: list[dict[str, float | int]] = []
    for page in reader.pages:
        geometry.append({
            "media_width": round(float(page.mediabox.width), 3),
            "media_height": round(float(page.mediabox.height), 3),
            "crop_width": round(float(page.cropbox.width), 3),
            "crop_height": round(float(page.cropbox.height), 3),
            "rotation": int(getattr(page, "rotation", 0) or 0),
        })
    return geometry


def pdf_text(path: Path) -> str:
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        raise RuntimeError(f"PDF 已加密：{path.name}")
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def cjk_label_sentinel(value: str) -> str:
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", value))
    return cjk[:4] if cjk else normalized_label(value)[:8]


def ghostscript_print_pdf(source: Path, target: Path) -> str:
    binary = discover_binary("gs")
    if not binary:
        raise RuntimeError("缺少 Ghostscript/gs，无法生成加密 PDF 的打印派生副本")
    security = pdf_security_profile(source)
    if not security["encrypted"] or not security["opens_without_prompt"] or not security["print_allowed"]:
        raise RuntimeError(f"PDF 不符合打印派生条件：{source.name}")
    if target.exists():
        raise RuntimeError(f"转换目标已存在，禁止覆盖：{target}")
    command = [
        binary, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-dPrinted=true",
        "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4", "-dAutoRotatePages=/None",
        "-dPreserveAnnots=false", "-dPreserveMarkedContent=false", "-dBackgroundColor=16#FFFFFF",
        f"-sOutputFile={target}", str(source),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
    if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        detail = (result.stderr or result.stdout or "未知转换错误").strip()
        raise RuntimeError(f"加密 PDF 打印派生失败：{source.name}：{detail}")
    pages = validate_pdf(target)
    if pages != security["page_count"]:
        raise RuntimeError(f"打印派生副本页数与原件不一致：{source.name}")
    source_reader = PdfReader(str(source), strict=False)
    source_reader.decrypt("")
    source_geometry = [
        (round(float(page.mediabox.width), 2), round(float(page.mediabox.height), 2), int(getattr(page, "rotation", 0) or 0))
        for page in source_reader.pages
    ]
    derived_reader = PdfReader(str(target), strict=False)
    derived_geometry = [
        (round(float(page.mediabox.width), 2), round(float(page.mediabox.height), 2), int(getattr(page, "rotation", 0) or 0))
        for page in derived_reader.pages
    ]
    if source_geometry != derived_geometry:
        raise RuntimeError(f"打印派生副本页面尺寸或旋转与原件不一致：{source.name}")
    version = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20, check=False).stdout.strip()
    return f"Ghostscript {version or 'unknown'} print-to-PDF"


def convert_to_pdf(source: Path, target: Path, *, allow_encrypted_pdf: bool = False) -> tuple[int, str]:
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower()
    if suffix == ".pdf":
        security = pdf_security_profile(source)
        if security["encrypted"]:
            if not allow_encrypted_pdf:
                raise RuntimeError("当前执行策略未启用可打印权限型加密 PDF 处理")
            engine = ghostscript_print_pdf(source, target)
        else:
            shutil.copy2(source, target)
            engine = "copy-pdf"
    elif suffix in IMAGE_SUFFIXES:
        convert_image_to_pdf(source, target)
        engine = "Pillow"
    elif suffix in OFFICE_SUFFIXES:
        converted, engine = soffice_convert(source, target.parent)
        if converted != target:
            if target.exists():
                raise RuntimeError(f"转换目标冲突：{target}")
            os.replace(converted, target)
    else:
        raise RuntimeError(f"不支持转换：{source.name}")
    return validate_pdf(target), engine


def merge_pdfs(sources: list[Path], target: Path, *, allow_qpdf: bool = False) -> int:
    if target.exists():
        raise RuntimeError(f"合并目标已存在，禁止覆盖：{target}")
    total = 0
    for source in sources:
        total += validate_pdf(source)
    if total == 0:
        raise RuntimeError("合并 PDF 没有页面")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        writer = PdfWriter()
        for source in sources:
            writer.append(str(source))
        with target.open("xb") as handle:
            writer.write(handle)
        actual = validate_pdf(target)
        if actual != total:
            raise RuntimeError(f"pypdf 合并页数不一致：预期 {total} 页，实际 {actual} 页")
        return actual
    except Exception as primary_error:  # noqa: BLE001 - qpdf is the explicit compatibility fallback
        target.unlink(missing_ok=True)
        if not allow_qpdf:
            raise RuntimeError(f"pypdf 合并失败；当前执行策略未启用 qpdf 兼容层：{primary_error}") from primary_error
        binary = discover_binary("qpdf")
        if not binary:
            raise RuntimeError(f"pypdf 合并失败；请启用 qpdf 兼容层：{primary_error}") from primary_error
    command = [binary, "--warning-exit-0", "--empty", "--pages", *[str(source) for source in sources], "--", str(target)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=300, check=False)
    if result.returncode != 0 or not target.is_file() or target.stat().st_size == 0:
        target.unlink(missing_ok=True)
        detail = (result.stderr or result.stdout or "未知合并错误").strip()
        raise RuntimeError(f"qpdf 合并失败：{detail}")
    actual = validate_pdf(target)
    if actual != total:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"qpdf 合并页数不一致：预期 {total} 页，实际 {actual} 页")
    return actual


def normalize_pdf_resources(path: Path, *, allow_qpdf: bool = False) -> None:
    if not allow_qpdf or not discover_binary("qpdf"):
        return
    normalized = path.with_name(f"{path.stem}.qpdf-normalized.pdf")
    merge_pdfs([path], normalized, allow_qpdf=True)
    os.replace(normalized, path)


def verify_plan_token(plan: dict[str, Any], confirm: str) -> None:
    core = {key: value for key, value in plan.items() if key not in {"status", "confirmation_token"}}
    expected = digest(core)
    if plan.get("confirmation_token") != expected or confirm != expected:
        raise RuntimeError("confirmation_token 不匹配或 plan 已被修改")
    if plan.get("status") != "preview" or plan.get("blockers"):
        raise RuntimeError("plan 存在阻断项或不是可执行预览状态")


def verify_current_snapshots(plan: dict[str, Any]) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    labels = {"rule": "归档规则", "cover": "卷宗封皮模板", "directory": "案卷目录模板"}
    current = plan.get("current")
    if not isinstance(current, dict):
        raise RuntimeError("plan 缺少 current 快照")
    for key, label in labels.items():
        descriptor = current.get(key)
        if not isinstance(descriptor, dict) or not descriptor.get("path") or not isinstance(descriptor.get("snapshot"), dict):
            raise RuntimeError(f"plan 缺少{label} current 快照")
        path = Path(str(descriptor["path"])).expanduser()
        if path.is_symlink():
            raise RuntimeError(f"{label}文件本身不得是符号链接")
        snapshot = descriptor["snapshot"]
        if not path.is_file() or sha256_file(path) != snapshot.get("sha256"):
            raise RuntimeError(f"{label} current 自 plan 后已变化")
        paths[key] = path
    return paths


def prepare_archive(root: Path, plan_path: Path, plan: dict[str, Any], confirm: str) -> dict[str, Any]:
    validate_schema_document(plan, "archive_plan.schema.json", "archive plan")
    verify_plan_token(plan, confirm)
    verify_current_snapshots(plan)
    failures = verify_snapshot(root, plan["engagement_source_snapshot"])
    if failures:
        raise RuntimeError("；".join(failures))
    request_path = resolve_inside(root, plan["request_path"], "request_path", must_exist=True)
    if sha256_file(request_path) != plan["request_sha256"]:
        raise RuntimeError("archive request 自预览后已变化")
    run_dir = resolve_inside(root, plan["run_dir"], "run_dir", must_exist=True)
    execution_policy = plan.get("execution_policy") or {}
    enabled_layers = set(execution_policy.get("enabled_layers", []))
    policies = execution_policy.get("policies") or {}
    allow_qpdf = "pdf-compat" in enabled_layers
    allow_encrypted_pdf = (
        "encrypted-pdf" in enabled_layers
        and policies.get("encrypted_pdf") == "printable_permissions_only"
    )
    detect_incomplete_transactions(run_dir, "prepare")
    for name in ["10_converted", "20_numbered-items", "50_manifest"]:
        if (run_dir / name).exists():
            raise RuntimeError(f"准备输出已存在，禁止覆盖：{name}")
    temp_root = run_dir / f".prepare-{uuid.uuid4().hex}"
    converted_dir = temp_root / "10_converted"
    unpaginated_dir = temp_root / "20_numbered-items" / "unpaginated"
    manifest_dir = temp_root / "50_manifest"
    converted_dir.mkdir(parents=True)
    unpaginated_dir.mkdir(parents=True)
    manifest_dir.mkdir(parents=True)
    converted_entries: list[dict[str, Any]] = []
    converted_by_source: dict[str, dict[str, Any]] = {}
    promoted_directories: list[Path] = []
    promoted_files: list[Path] = []
    marker = write_transaction_marker(
        temp_root,
        "prepare",
        [run_dir / "10_converted", run_dir / "20_numbered-items", run_dir / "50_manifest"],
    )
    try:
        for index, item in enumerate(plan["items"], start=1):
            source = resolve_inside(root, item["source"], "source", must_exist=True)
            if sha256_file(source) != item["source_snapshot"]["sha256"]:
                raise RuntimeError(f"源文件自预览后已变化：{item['source']}")
            name = f"{index:03d}-{safe_slug(item['display_name'])}.pdf"
            target = converted_dir / name
            pages, engine = convert_to_pdf(source, target, allow_encrypted_pdf=allow_encrypted_pdf)
            entry = {
                "source": item["source"], "source_sha256": item["source_snapshot"]["sha256"],
                "converted": f"{plan['run_dir']}/10_converted/{name}", "converted_sha256": sha256_file(target),
                "page_count": pages, "engine": engine, "order": item["order"], "category": item["category"],
                "display_name": item["display_name"], "internal_note": item.get("internal_note", ""),
                "source_pdf_security": item["source_snapshot"].get("pdf_security"),
                "derivation": (
                    "print-to-pdf" if item["source_snapshot"].get("pdf_security", {}).get("print_conversion_required")
                    else "copy-or-format-conversion"
                ),
            }
            converted_entries.append(entry)
            converted_by_source[item["source"]] = entry

        sections: list[dict[str, Any]] = []
        logical_start = 1
        for sequence, group in enumerate(plan["directory_groups"], start=1):
            members = [converted_by_source[source] for source in group["member_sources"]]
            label = group["label"]
            filename = f"{sequence:03d}-{safe_slug(label)}.pdf"
            member_paths = [temp_root / Path(item["converted"]).relative_to(plan["run_dir"]) for item in members]
            section_target = unpaginated_dir / filename
            pages = merge_pdfs(member_paths, section_target, allow_qpdf=allow_qpdf)
            logical_end = logical_start + pages - 1
            sections.append({
                "sequence": sequence, "group_id": group["group_id"], "name": label, "category": group["category"],
                "unpaginated": f"{plan['run_dir']}/20_numbered-items/unpaginated/{filename}",
                "sha256": sha256_file(section_target), "page_count": pages,
                "logical_start": logical_start, "logical_end": logical_end,
                "page_range": str(logical_start) if logical_start == logical_end else f"{logical_start}-{logical_end}",
                "formal_remark": group.get("formal_remark", ""),
                "members": [item["source"] for item in members],
            })
            logical_start = logical_end + 1

        body_page_count = logical_start - 1
        current_paths = plan.get("current") or {}
        cover_source = Path(str((current_paths.get("cover") or {}).get("path", "")))
        directory_source = Path(str((current_paths.get("directory") or {}).get("path", "")))
        if not cover_source.is_file() or not directory_source.is_file():
            raise RuntimeError("prepare 阶段无法读取已核验的封皮或目录 current")
        preview_front = temp_root / "30_front-matter-preview"
        preview_front.mkdir(parents=True)
        template_mode = plan.get("template_mode")
        cover_preview_docx: Path | None = None
        directory_preview_docx: Path | None = None
        if template_mode == "pdf":
            resolved_cover_fields = []
            cover_preview_pdf = preview_front / "cover.pdf"
            directory_preview_pdf = preview_front / "directory.pdf"
            shutil.copy2(cover_source, cover_preview_pdf)
            shutil.copy2(directory_source, directory_preview_pdf)
            cover_engine = directory_engine = "prefilled-pdf"
            cover_integrity = {"mode": "prefilled_pdf", "source_sha256": sha256_file(cover_source)}
            cover_pages = validate_pdf(cover_preview_pdf)
            directory_pages = validate_pdf(directory_preview_pdf)
            expected_cover_pages = int(((current_paths.get("cover") or {}).get("snapshot") or {}).get("expected_pages", 1))
            expected_directory_pages = int(((current_paths.get("directory") or {}).get("snapshot") or {}).get("expected_pages", 1))
            if cover_pages != expected_cover_pages or directory_pages != expected_directory_pages:
                raise RuntimeError("已填写 PDF 的实际页数与本地配置不一致")
            layout_core = {
                "directory_pdf_sha256": sha256_file(directory_preview_pdf),
                "directory_pages": directory_pages, "page_entries": [], "warnings": [],
                "requires_lawyer_acceptance": False,
            }
            directory_layout = {**layout_core, "acceptance_token": digest(layout_core)}
        else:
            resolved_cover_fields = resolve_cover_fields(plan["cover_fields"], body_page_count)
            cover_preview_docx = preview_front / "cover.docx"
            cover_integrity = fill_cover_template(
                cover_source, cover_preview_docx, resolved_cover_fields, plan.get("cover_render"),
            )
            cover_preview_pdf, cover_engine = soffice_convert(cover_preview_docx, preview_front)
            if cover_preview_pdf.name != "cover.pdf":
                os.replace(cover_preview_pdf, preview_front / "cover.pdf")
                cover_preview_pdf = preview_front / "cover.pdf"
            normalize_pdf_resources(cover_preview_pdf, allow_qpdf=allow_qpdf)
            cover_pages = validate_pdf(cover_preview_pdf)
            expected_cover_pages = int((plan.get("cover_render") or {}).get("expected_pages", 0))
            if cover_pages != expected_cover_pages:
                raise RuntimeError(f"候选封皮页数为 {cover_pages}，与 expected_pages={expected_cover_pages} 不一致")
            directory_preview_docx = preview_front / "directory.docx"
            fill_directory_template(directory_source, directory_preview_docx, sections)
            directory_preview_pdf, directory_engine = soffice_convert(directory_preview_docx, preview_front)
            if directory_preview_pdf.name != "directory.pdf":
                os.replace(directory_preview_pdf, preview_front / "directory.pdf")
                directory_preview_pdf = preview_front / "directory.pdf"
            normalize_pdf_resources(directory_preview_pdf, allow_qpdf=allow_qpdf)
            directory_pages = validate_pdf(directory_preview_pdf)
            validate_directory_rendered_values(directory_preview_pdf, sections)
            directory_layout = inspect_directory_layout(directory_preview_pdf, sections)

        prepared_core = {
            "schema_version": SCHEMA_VERSION, "status": "prepared", "profile": plan["profile"],
            "matter_id": plan["matter_id"], "case_numbers": plan.get("case_numbers", []), "run_dir": plan["run_dir"],
            "archive_intake": plan.get("archive_intake", {}),
            "engagement": plan["engagement"], "internal_close": plan["internal_close"],
            "court_or_tribunal_disposition": plan.get("court_or_tribunal_disposition", {}),
            "plan_path": relative(root, plan_path), "plan_sha256": sha256_file(plan_path),
            "source_roots": plan["source_roots"], "output_root": plan["output_root"],
            "execution_policy": execution_policy,
            "engagement_source_snapshot": plan["engagement_source_snapshot"], "converted_items": converted_entries,
            "sections": sections, "body_page_count": body_page_count,
            "template_mode": template_mode,
            "resolved_cover_fields": resolved_cover_fields,
            "front_matter_preview": {
                "cover_docx": f"{plan['run_dir']}/30_front-matter-preview/cover.docx" if cover_preview_docx else "",
                "cover_pdf": f"{plan['run_dir']}/30_front-matter-preview/cover.pdf",
                "cover_sha256": sha256_file(cover_preview_pdf),
                "cover_pages": cover_pages,
                "directory_docx": f"{plan['run_dir']}/30_front-matter-preview/directory.docx" if directory_preview_docx else "",
                "directory_pdf": f"{plan['run_dir']}/30_front-matter-preview/directory.pdf",
                "directory_sha256": sha256_file(directory_preview_pdf),
                "directory_pages": directory_pages,
                "directory_layout": directory_layout,
                "converter": cover_engine or directory_engine,
                "cover_integrity": cover_integrity,
            },
            "missing_categories": plan.get("missing_categories", []),
            "missing_material_disposition": plan.get("missing_material_disposition"),
            "page_number_policy": plan["page_number_policy"],
        }
        final_token = digest(prepared_core)
        prepared = {**prepared_core, "final_confirmation_token": final_token}
        validate_schema_document(prepared, "prepared_manifest.schema.json", "prepared manifest")
        write_new_json(manifest_dir / "prepared-manifest.json", prepared)

        final_preview = ["# 最终化预览", "", f"- 正文总页数：{prepared['body_page_count']}", f"- 候选封皮：`{prepared['front_matter_preview']['cover_pdf']}`", f"- 候选目录：`{prepared['front_matter_preview']['directory_pdf']}`", "", "## 目录条目", ""]
        final_preview.extend(f"- {item['sequence']}. {item['name']}：{item['page_range']}" for item in sections)
        if directory_layout["requires_lawyer_acceptance"]:
            final_preview.extend([
                "", "## 目录跨页版式警示", "",
                "- 候选目录存在需要律师明确接受的跨页版式：" + "、".join(
                    str(item["code"]) for item in directory_layout["warnings"]
                ),
                "- 最终化时除 `final_confirmation_token` 外，还必须提供与本候选目录哈希绑定的 `directory_layout.acceptance_token` 及复核律师。",
            ])
        final_preview.extend(["", "使用 `final_confirmation_token` 确认后才可生成封皮、目录、页码和最终卷宗。", ""])
        final_preview_path = run_dir / "00_preview" / "final-preview.md"
        finalization_plan_path = run_dir / "00_preview" / "finalization-plan.json"
        if final_preview_path.exists() or finalization_plan_path.exists():
            raise RuntimeError("最终化预览已存在，禁止覆盖")

        promote_directory(converted_dir, run_dir / "10_converted", promoted_directories)
        promote_directory(temp_root / "20_numbered-items", run_dir / "20_numbered-items", promoted_directories)
        promote_directory(preview_front, run_dir / "30_front-matter-preview", promoted_directories)
        promote_directory(manifest_dir, run_dir / "50_manifest", promoted_directories)
        write_new_text(final_preview_path, "\n".join(final_preview))
        promoted_files.append(final_preview_path)
        write_new_json(finalization_plan_path, prepared)
        promoted_files.append(finalization_plan_path)
        marker.unlink(missing_ok=True)
        temp_root.rmdir()
        return {
            "status": "prepared", "prepared_manifest": f"{plan['run_dir']}/50_manifest/prepared-manifest.json",
            "final_confirmation_token": final_token, "body_page_count": prepared["body_page_count"], "sections": sections,
        }
    except BaseException:
        rollback_promotions(promoted_files, promoted_directories)
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


def cell_text(cell: etree._Element) -> str:
    return "".join(cell.itertext())


def normalized_label(value: str) -> str:
    return re.sub(r"\s+", "", value).replace("：", "").replace(":", "")


def replace_cell_text(cell: etree._Element, value: str) -> None:
    paragraphs = cell.findall(f"{W}p")
    p_pr = deepcopy(paragraphs[0].find(f"{W}pPr")) if paragraphs and paragraphs[0].find(f"{W}pPr") is not None else None
    first_run = paragraphs[0].find(f"{W}r") if paragraphs else None
    r_pr = deepcopy(first_run.find(f"{W}rPr")) if first_run is not None and first_run.find(f"{W}rPr") is not None else None
    for paragraph in paragraphs:
        cell.remove(paragraph)
    paragraph = etree.Element(f"{W}p")
    if p_pr is not None:
        paragraph.append(p_pr)
    run = etree.SubElement(paragraph, f"{W}r")
    if r_pr is not None:
        run.append(r_pr)
    parts = value.split("\n")
    for index, part in enumerate(parts):
        if index:
            etree.SubElement(run, f"{W}br")
        text = etree.SubElement(run, f"{W}t")
        text.text = part
        if part[:1].isspace() or part[-1:].isspace():
            text.set(XML_SPACE, "preserve")
    cell.append(paragraph)


def apply_cover_render_policy(
    root: etree._Element,
    fields: list[dict[str, Any]],
    render: dict[str, Any] | None,
) -> None:
    policy = str((render or {}).get("fit_policy", "preserve"))
    if policy in {"", "preserve"}:
        return
    if policy not in {"fixed_template_rows_compact_values", "balanced_template_rows_one_page"}:
        raise RuntimeError(f"不支持的封皮版式适配策略：{policy}")

    balanced = policy == "balanced_template_rows_one_page"
    scale = float((render or {}).get("row_height_scale", 0)) if balanced else 1.0
    if balanced and not 1 <= scale <= 1.5:
        raise RuntimeError("balanced_template_rows_one_page 的 row_height_scale 必须为 1 至 1.5")

    # The compact policy fixes only taller rows at their retained heights.  The
    # balanced policy keeps every current row's relative proportion, applies a
    # lawyer-confirmed scale, and fixes the scaled heights so LibreOffice cannot
    # redistribute them under CJK font substitution.  Borders, column widths,
    # fixed text, margins, and page size remain unchanged.
    for row in root.xpath(".//w:tr", namespaces={"w": W_NS}):
        tr_pr = row.find(f"{W}trPr")
        if tr_pr is None:
            continue
        height = tr_pr.find(f"{W}trHeight")
        value = height.get(f"{W}val") if height is not None else None
        if value and balanced:
            height.set(f"{W}val", str(round(int(value) * scale)))
            height.set(f"{W}hRule", "exact")
        elif value and int(value) >= 600:
            height.set(f"{W}hRule", "exact")

    # Compact the paragraph box for all fixed and editable table text without
    # changing its font size.  This is what keeps wrapped fixed labels visible
    # inside the retained row geometry under CJK font substitution.
    for paragraph in root.xpath(".//w:tbl//w:p", namespaces={"w": W_NS}):
        p_pr = paragraph.find(f"{W}pPr")
        if p_pr is None:
            p_pr = etree.Element(f"{W}pPr")
            paragraph.insert(0, p_pr)
        spacing = p_pr.find(f"{W}spacing")
        if spacing is None:
            spacing = etree.SubElement(p_pr, f"{W}spacing")
        spacing.set(f"{W}before", "0")
        spacing.set(f"{W}after", "0")
        spacing.set(f"{W}line", "280" if balanced else "240")
        spacing.set(f"{W}lineRule", "exact")

    for field in fields:
        if normalized_label(str(field.get("label", ""))) == "案件编号":
            continue
        mode, target = locate_cover_field_target(root, field)
        paragraphs = [target] if target.tag == f"{W}p" else target.xpath(".//w:p", namespaces={"w": W_NS})
        for paragraph in paragraphs:
            p_pr = paragraph.find(f"{W}pPr")
            if p_pr is None:
                p_pr = etree.Element(f"{W}pPr")
                paragraph.insert(0, p_pr)
            spacing = p_pr.find(f"{W}spacing")
            if spacing is None:
                spacing = etree.SubElement(p_pr, f"{W}spacing")
            spacing.set(f"{W}before", "0")
            spacing.set(f"{W}after", "0")
            spacing.set(f"{W}line", "280" if balanced else "220")
            spacing.set(f"{W}lineRule", "exact")
        if mode != "inline_brackets":
            for run in target.xpath(".//w:r", namespaces={"w": W_NS}):
                r_pr = run.find(f"{W}rPr")
                if r_pr is None:
                    r_pr = etree.Element(f"{W}rPr")
                    run.insert(0, r_pr)
                for name in ["sz", "szCs"]:
                    size = r_pr.find(f"{W}{name}")
                    if size is None:
                        size = etree.SubElement(r_pr, f"{W}{name}")
                    size.set(f"{W}val", "21")


def fill_cover_cell_preserving_layout(cell: etree._Element, value: str, expected_text: str | None = None) -> None:
    actual = normalized_label(cell_text(cell))
    if expected_text is None and actual:
        raise RuntimeError("封皮目标单元格含既有文字，禁止修改模板内容")
    if expected_text is not None and actual != normalized_label(expected_text):
        raise RuntimeError("封皮目标单元格与 expected_text 不一致，禁止覆盖")
    paragraphs = cell.findall(f"{W}p")
    if not paragraphs:
        raise RuntimeError("封皮目标单元格缺少既有段落，禁止重建模板结构")
    for paragraph in paragraphs:
        for run in paragraph.findall(f"{W}r"):
            for child in list(run):
                if child.tag in {f"{W}t", f"{W}br"}:
                    run.remove(child)
    parts = value.split("\n")
    for index, part in enumerate(parts):
        paragraph = paragraphs[min(index, len(paragraphs) - 1)]
        runs = paragraph.findall(f"{W}r")
        target_run = runs[0] if runs else etree.SubElement(paragraph, f"{W}r")
        insert_at = 1 if target_run.find(f"{W}rPr") is not None else 0
        if index >= len(paragraphs):
            target_run.insert(insert_at, etree.Element(f"{W}br"))
            insert_at += 1
        text = etree.Element(f"{W}t")
        text.text = part
        if part[:1].isspace() or part[-1:].isspace():
            text.set(XML_SPACE, "preserve")
        target_run.insert(insert_at, text)


def fill_inline_brackets_preserving_layout(paragraph: etree._Element, value: str, expected_text: str | None = None) -> None:
    candidates: list[tuple[etree._Element, re.Match[str]]] = []
    for node in paragraph.xpath(".//w:t", namespaces={"w": W_NS}):
        match = re.search(r"\[([^\[\]]*)\]", node.text or "")
        if match:
            candidates.append((node, match))
    if len(candidates) != 1:
        raise RuntimeError("封皮行内字段必须且只能有一个方括号占位区")
    node, match = candidates[0]
    inner = match.group(1)
    if expected_text is None and inner.strip():
        raise RuntimeError("封皮方括号占位区已有内容，禁止覆盖")
    if expected_text is not None and inner != expected_text:
        raise RuntimeError("封皮方括号占位内容与 expected_text 不一致")
    node.text = f"{(node.text or '')[:match.start()]}[{value}]{(node.text or '')[match.end():]}"


def patch_docx(source: Path, target: Path, transform) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source, "r") as zin, zipfile.ZipFile(target, "x") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                root = etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False))
                transform(root)
                data = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            zout.writestr(item, data)
    with zipfile.ZipFile(target, "r") as check:
        bad = check.testzip()
        if bad:
            raise RuntimeError(f"生成的 DOCX 容器损坏：{bad}")


def fill_cover_template(
    source: Path,
    target: Path,
    fields: list[dict[str, Any]],
    render: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_source_root = load_docx_document_xml(source)
    normalized_source_root = deepcopy(raw_source_root)
    apply_cover_render_policy(normalized_source_root, fields, render)
    source_layout = cover_layout_fingerprint_root(normalized_source_root)
    raw_source_layout = cover_layout_fingerprint_root(raw_source_root)
    source_other_members = docx_package_hashes(source, exclude={"word/document.xml"})

    def transform(root: etree._Element) -> None:
        apply_cover_render_policy(root, fields, render)
        fixed_text_before = "".join(root.xpath(".//w:t/text()", namespaces={"w": W_NS}))
        target_paths: list[str] = []
        for field in fields:
            if normalized_label(str(field.get("label", ""))) == "案件编号":
                if str(field.get("value", "")).strip():
                    raise RuntimeError("封皮“案件编号”由律所归档部门填写，归档工作流必须留空")
                continue
            mode, target = locate_cover_field_target(root, field)
            target_paths.append(root.getroottree().getpath(target))
            if mode == "inline_brackets":
                fill_inline_brackets_preserving_layout(target, str(field["value"]), field.get("expected_text"))
            else:
                expected = str(field["expected_text"]) if mode == "adjacent_placeholder" else None
                fill_cover_cell_preserving_layout(target, str(field["value"]), expected)
        check = deepcopy(root)
        for path in target_paths:
            matches = check.xpath(path, namespaces={"w": W_NS})
            if matches:
                for text_node in matches[0].xpath(".//w:t", namespaces={"w": W_NS}):
                    text_node.text = ""
        original = load_docx_document_xml(source)
        for path in target_paths:
            matches = original.xpath(path, namespaces={"w": W_NS})
            if matches:
                for text_node in matches[0].xpath(".//w:t", namespaces={"w": W_NS}):
                    text_node.text = ""
        fixed_text_after = "".join(check.xpath(".//w:t/text()", namespaces={"w": W_NS}))
        fixed_text_original = "".join(original.xpath(".//w:t/text()", namespaces={"w": W_NS}))
        if fixed_text_after != fixed_text_original:
            raise RuntimeError("封皮模板固定文字发生变化，已阻断输出")
        if not fixed_text_before and fields:
            raise RuntimeError("封皮模板缺少可核验的固定文字")
    patch_docx(source, target, transform)
    target_layout = cover_layout_fingerprint(target)
    if target_layout != source_layout:
        raise RuntimeError("封皮模板段落、表格或页面结构发生变化，已阻断输出")
    if docx_package_hashes(target, exclude={"word/document.xml"}) != source_other_members:
        raise RuntimeError("封皮 DOCX 除正文 XML 外的模板部件发生变化，已阻断输出")
    return {
        "raw_source_layout_fingerprint": raw_source_layout,
        "source_layout_fingerprint": source_layout,
        "output_layout_fingerprint": target_layout,
        "non_document_members_unchanged": True,
        "render_policy": render or {},
    }


def resolve_cover_fields(fields: list[dict[str, Any]], body_page_count: int) -> list[dict[str, Any]]:
    """Resolve the only permitted computed cover field after prepare."""
    resolved: list[dict[str, Any]] = []
    for raw in fields:
        field = deepcopy(raw)
        if field.get("value_source") == "computed_body_page_count":
            if normalized_label(str(field.get("label", ""))) != "卷内页数":
                raise RuntimeError("只有封皮“卷内页数”允许使用 computed_body_page_count")
            field["value"] = str(body_page_count)
        resolved.append(field)
    return resolved


def fill_directory_template(source: Path, target: Path, entries: list[dict[str, Any]]) -> None:
    def transform(root: etree._Element) -> None:
        selected = None
        header_index = -1
        for table in root.xpath(".//w:tbl", namespaces={"w": W_NS}):
            rows = table.findall(f"{W}tr")
            for index, row in enumerate(rows):
                text = normalized_label(cell_text(row))
                if all(label in text for label in ["序号", "名称", "页码", "备注"]):
                    selected, header_index = table, index
                    break
            if selected is not None:
                break
        if selected is None:
            raise RuntimeError("目录模板未找到序号、名称、页码、备注表头")
        rows = selected.findall(f"{W}tr")
        if header_index + 1 >= len(rows):
            raise RuntimeError("目录模板缺少数据行")
        template_row = deepcopy(rows[header_index + 1])
        note_row = next((row for row in rows[header_index + 1:] if "注意事项" in cell_text(row)), rows[-1])
        for row in list(selected.findall(f"{W}tr"))[header_index + 1:]:
            if row is note_row:
                continue
            selected.remove(row)
        for entry in entries:
            new_row = deepcopy(template_row)
            cells = new_row.findall(f"{W}tc")
            if len(cells) < 4:
                raise RuntimeError("目录模板数据行少于四个物理单元格")
            values = [str(entry["sequence"]), entry["name"], entry["page_range"], entry.get("formal_remark", "")]
            for cell, value in zip(cells[:4], values):
                replace_cell_text(cell, value)
            note_index = list(selected).index(note_row)
            selected.insert(note_index, new_row)
    patch_docx(source, target, transform)


def validate_directory_rendered_values(path: Path, entries: list[dict[str, Any]]) -> None:
    """Reject a directory whose final PDF loses, clips, or mutates a planned cell value."""
    text = normalized_label(pdf_text(path))
    for entry in entries:
        required = [str(entry["sequence"]), str(entry["name"]), str(entry["page_range"])]
        if entry.get("formal_remark"):
            required.append(str(entry["formal_remark"]))
        missing = [value for value in required if normalized_label(value) not in text]
        if missing:
            raise RuntimeError(
                f"目录 PDF 未完整呈现第 {entry['sequence']} 项字段，可能存在截断或字体异常：{'、'.join(missing)}"
            )
    forbidden = [marker for marker in FORMAL_INTERNAL_MARKERS if marker.casefold() in text.casefold()]
    if forbidden:
        raise RuntimeError("正式目录出现禁止的内部工作流语言：" + "、".join(forbidden))


def inspect_directory_layout(path: Path, entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Describe Gate B directory pagination without changing the lawyer's grouping.

    This is deliberately conservative: it reports a warning when a row cannot
    be located on exactly one rendered PDF page.  The warning is not silently
    "fixed" by changing row order or shrinking the official template.
    """
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        raise RuntimeError(f"PDF 已加密：{path.name}")
    locations: dict[int, list[int]] = {int(entry["sequence"]): [] for entry in entries}
    page_entries: list[dict[str, Any]] = []
    header_labels = [normalized_label(value) for value in ["序号", "名称", "页码", "备注"]]
    for page_number, page in enumerate(reader.pages, start=1):
        text = normalized_label(page.extract_text() or "")
        sequences: list[int] = []
        for entry in entries:
            sequence = int(entry["sequence"])
            name = normalized_label(str(entry["name"]))
            page_range = normalized_label(str(entry["page_range"]))
            if name and page_range and name in text and page_range in text:
                sequences.append(sequence)
                locations[sequence].append(page_number)
        page_entries.append({
            "page": page_number,
            "sequences": sequences,
            "header_present": all(label in text for label in header_labels),
        })

    warnings: list[dict[str, Any]] = []
    unlocatable = sorted(sequence for sequence, pages in locations.items() if len(pages) != 1)
    if unlocatable:
        warnings.append({"code": "directory_row_split_or_unlocatable", "sequences": unlocatable})
    missing_continuation_headers = [
        item["page"] for item in page_entries[1:] if not item["header_present"]
    ]
    if missing_continuation_headers:
        warnings.append({"code": "continuation_header_missing", "pages": missing_continuation_headers})
    single_entry_continuations = [
        item["page"] for item in page_entries[1:] if len(item["sequences"]) == 1
    ]
    if single_entry_continuations:
        warnings.append({"code": "single_entry_continuation", "pages": single_entry_continuations})

    core = {
        "directory_pdf_sha256": sha256_file(path),
        "directory_pages": len(reader.pages),
        "page_entries": page_entries,
        "warnings": warnings,
        "requires_lawyer_acceptance": bool(warnings),
    }
    return {**core, "acceptance_token": digest(core)}


def verify_directory_layout_acceptance(
    layout: dict[str, Any], acceptance_token: str | None, reviewer: str | None,
) -> dict[str, Any]:
    """Require a candidate-bound Gate B acceptance only when layout warnings exist."""
    required = {
        "directory_pdf_sha256", "directory_pages", "page_entries", "warnings",
        "requires_lawyer_acceptance", "acceptance_token",
    }
    if not isinstance(layout, dict) or not required.issubset(layout):
        raise RuntimeError("prepared manifest 缺少目录版式 Gate B 记录")
    core = {key: layout[key] for key in required if key != "acceptance_token"}
    expected = digest(core)
    if layout.get("acceptance_token") != expected:
        raise RuntimeError("目录版式接受令牌与候选目录不匹配")
    if layout["requires_lawyer_acceptance"]:
        if acceptance_token != expected:
            raise RuntimeError("目录存在跨页版式警示，必须提供匹配的目录版式接受令牌")
        reviewer = str(reviewer or "").strip()
        if not reviewer:
            raise RuntimeError("目录存在跨页版式警示，必须记录律师复核人")
        return {
            "required": True,
            "accepted": True,
            "accepted_by": reviewer,
            "accepted_at": datetime.now().isoformat(timespec="seconds"),
            "acceptance_token": expected,
        }
    if acceptance_token or reviewer:
        raise RuntimeError("目录没有跨页版式警示，不应提供目录版式接受参数")
    return {"required": False, "accepted": False, "accepted_by": "", "accepted_at": None, "acceptance_token": ""}


def number_pdf(source: Path, target: Path, start: int, policy: str) -> int:
    if policy == "keep_confirmed":
        shutil.copy2(source, target)
        return validate_pdf(target)
    reader = PdfReader(str(source), strict=False)
    writer = PdfWriter()
    for offset, original in enumerate(reader.pages):
        writer.add_page(original)
        page = writer.pages[-1]
        if getattr(page, "rotation", 0):
            page.transfer_rotation_to_content()
        media = page.mediabox
        width, height = float(media.width), float(media.height)
        crop = page.cropbox
        if policy == "overlay_bottom_right":
            x = float(crop.right) - 24
        else:
            x = float(crop.left) + float(crop.width) / 2
        y = float(crop.bottom) + 18
        packet = io.BytesIO()
        overlay_canvas = canvas.Canvas(packet, pagesize=(width, height))
        overlay_canvas.setFont("Helvetica", 9)
        if policy == "overlay_bottom_right":
            overlay_canvas.drawRightString(x, y, str(start + offset))
        else:
            overlay_canvas.drawCentredString(x, y, str(start + offset))
        overlay_canvas.save()
        packet.seek(0)
        overlay = PdfReader(packet).pages[0]
        page.merge_page(overlay)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as handle:
        writer.write(handle)
    return len(reader.pages)


def verify_logical_page_numbers(path: Path, first_physical_page: int, body_page_count: int) -> list[int]:
    """Return body logical page numbers whose overlay cannot be recovered from final PDF text."""
    reader = PdfReader(str(path), strict=False)
    failures: list[int] = []
    for logical in range(1, body_page_count + 1):
        physical = first_physical_page + logical - 1
        text = (reader.pages[physical - 1].extract_text() or "").strip()
        if not text.endswith(str(logical)):
            failures.append(logical)
    return failures


def natural_page_key(path: Path) -> tuple[int, str]:
    match = re.search(r"(\d+)$", path.stem)
    return (int(match.group(1)) if match else 0, path.name)


def render_pdf_for_review(
    source: Path, output_dir: Path, expected_pages: int, preferred_engine: str | None = None,
) -> tuple[list[Path], dict[str, Any]]:
    """Render with Poppler. Missing visual capability is an explicit, non-fatal downgrade."""
    del preferred_engine
    output_dir.mkdir(parents=True, exist_ok=True)
    renderer = discover_binary("pdftoppm")
    diagnostics: dict[str, Any] = {"engine": "", "warnings": ""}
    if renderer:
        result = subprocess.run(
            [renderer, "-png", "-scale-to", "1200", str(source), str(output_dir / "page")],
            capture_output=True, text=True, timeout=300, check=False,
        )
        paths = sorted(output_dir.glob("page-*.png"), key=natural_page_key)
        stderr = (result.stderr or "").strip()
        diagnostics["warnings"] = stderr
        font_problem = any(marker in stderr.casefold() for marker in [
            "no font", "unknown font", "missing language pack", "cmap",
        ])
        if result.returncode == 0 and len(paths) == expected_pages and not font_problem:
            diagnostics["engine"] = "pdftoppm"
            return paths, diagnostics
    diagnostics["error"] = diagnostics["warnings"] or "缺少 pdftoppm 或 Poppler 渲染未通过"
    return [], diagnostics


def render_first_page_raw(source: Path, output_prefix: Path) -> Path:
    renderer = discover_binary("pdftoppm")
    if not renderer:
        raise RuntimeError("缺少 pdftoppm，不能比较封皮与最终首页")
    output_prefix.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [renderer, "-f", "1", "-l", "1", "-singlefile", str(source), str(output_prefix)],
        capture_output=True, text=True, timeout=300, check=False,
    )
    target = output_prefix.with_suffix(".ppm")
    if result.returncode != 0 or not target.is_file():
        raise RuntimeError((result.stderr or result.stdout or "首页原始渲染失败").strip())
    return target
def verify_prepared_token(prepared: dict[str, Any], confirm: str) -> None:
    core = {key: value for key, value in prepared.items() if key != "final_confirmation_token"}
    expected = digest(core)
    if prepared.get("final_confirmation_token") != expected or confirm != expected:
        raise RuntimeError("final_confirmation_token 不匹配或 prepared manifest 已被修改")
    if prepared.get("status") != "prepared":
        raise RuntimeError("prepared manifest 状态错误")


def finalize_archive(
    root: Path,
    prepared_path: Path,
    prepared: dict[str, Any],
    confirm: str,
    directory_layout_acceptance: str | None = None,
    directory_layout_reviewer: str | None = None,
) -> dict[str, Any]:
    validate_schema_document(prepared, "prepared_manifest.schema.json", "prepared manifest")
    verify_prepared_token(prepared, confirm)
    plan_path = resolve_inside(root, prepared["plan_path"], "plan_path", must_exist=True)
    if sha256_file(plan_path) != prepared["plan_sha256"]:
        raise RuntimeError("archive plan 自 prepare 后已变化")
    plan = read_json(plan_path)
    verify_current_snapshots(plan)
    failures = verify_snapshot(root, prepared["engagement_source_snapshot"])
    if failures:
        raise RuntimeError("；".join(failures))
    run_dir = resolve_inside(root, prepared["run_dir"], "run_dir", must_exist=True)
    execution_policy = prepared.get("execution_policy") or {}
    allow_qpdf = "pdf-compat" in set(execution_policy.get("enabled_layers", []))
    ensure_run_active(run_dir)
    detect_incomplete_transactions(run_dir, "finalize")
    front_dir = run_dir / "30_front-matter"
    final_dir = run_dir / "40_final"
    numbered_dir = run_dir / "20_numbered-items" / "final"
    for path in [front_dir, final_dir, numbered_dir]:
        if path.exists():
            raise RuntimeError(f"最终化输出已存在，禁止覆盖：{relative(root, path)}")

    temp_root = run_dir / f".finalize-{uuid.uuid4().hex}"
    temp_front = temp_root / "30_front-matter"
    temp_final = temp_root / "40_final"
    temp_numbered = temp_root / "numbered"
    temp_front.mkdir(parents=True)
    temp_final.mkdir(parents=True)
    temp_numbered.mkdir(parents=True)
    promoted_directories: list[Path] = []
    promoted_files: list[Path] = []
    marker = write_transaction_marker(temp_root, "finalize", [front_dir, numbered_dir, final_dir])
    try:
        preview = prepared.get("front_matter_preview") or {}
        template_mode = str(prepared.get("template_mode", "docx"))
        cover_preview_docx = None
        directory_preview_docx = None
        if template_mode == "docx":
            cover_preview_docx = resolve_inside(root, str(preview.get("cover_docx", "")), "候选封皮 DOCX", must_exist=True)
            directory_preview_docx = resolve_inside(root, str(preview.get("directory_docx", "")), "候选目录 DOCX", must_exist=True)
        cover_preview_pdf = resolve_inside(root, str(preview.get("cover_pdf", "")), "候选封皮 PDF", must_exist=True)
        directory_preview_pdf = resolve_inside(root, str(preview.get("directory_pdf", "")), "候选目录 PDF", must_exist=True)
        if sha256_file(cover_preview_pdf) != preview.get("cover_sha256"):
            raise RuntimeError("候选封皮自 Gate B 预览后已变化")
        if sha256_file(directory_preview_pdf) != preview.get("directory_sha256"):
            raise RuntimeError("候选目录自 Gate B 预览后已变化")
        cover_docx = temp_front / "cover.docx"
        cover_pdf = temp_front / "cover.pdf"
        directory_docx = temp_front / "directory.docx"
        directory_pdf = temp_front / "directory.pdf"
        if cover_preview_docx:
            shutil.copy2(cover_preview_docx, cover_docx)
        shutil.copy2(cover_preview_pdf, cover_pdf)
        if directory_preview_docx:
            shutil.copy2(directory_preview_docx, directory_docx)
        shutil.copy2(directory_preview_pdf, directory_pdf)
        cover_integrity = deepcopy(preview.get("cover_integrity") or {})
        cover_engine = str(preview.get("converter") or "")
        directory_engine = cover_engine
        cover_pages = validate_pdf(cover_pdf)
        directory_pages = validate_pdf(directory_pdf)
        cover_render = plan.get("cover_render") or {}
        expected_pages = int(cover_render.get("expected_pages", 0))
        if cover_pages != expected_pages:
            raise RuntimeError(f"封皮应为 {expected_pages} 页，实际为 {cover_pages} 页")
        cover_integrity["page_count_matches_expected"] = True
        directory_layout = (preview.get("directory_layout") or {})
        if template_mode == "docx":
            cover_pdf_text = normalized_label(pdf_text(cover_pdf))
            cover_pdf_cjk = "".join(re.findall(r"[\u3400-\u9fff]", cover_pdf_text))
            missing_cover_labels = []
            for field in plan["cover_fields"]:
                label = str(field["label"])
                sentinel = cjk_label_sentinel(label)
                haystack = cover_pdf_cjk if re.search(r"[\u3400-\u9fff]", sentinel) else cover_pdf_text
                if sentinel and sentinel not in haystack:
                    missing_cover_labels.append(label)
            if missing_cover_labels:
                raise RuntimeError("封皮 PDF 字体或文本转换异常，缺少字段标签：" + "、".join(missing_cover_labels))
            validate_directory_rendered_values(directory_pdf, prepared["sections"])
            actual_directory_layout = inspect_directory_layout(directory_pdf, prepared["sections"])
            comparable_keys = [
                "directory_pdf_sha256", "directory_pages", "page_entries", "warnings",
                "requires_lawyer_acceptance", "acceptance_token",
            ]
            if any(directory_layout.get(key) != actual_directory_layout.get(key) for key in comparable_keys):
                raise RuntimeError("候选目录跨页版式记录与 Gate B 实际渲染不一致")
        directory_layout_record = verify_directory_layout_acceptance(
            directory_layout, directory_layout_acceptance, directory_layout_reviewer,
        )

        numbered_sections: list[dict[str, Any]] = []
        for section in prepared["sections"]:
            source = resolve_inside(root, section["unpaginated"], "unpaginated", must_exist=True)
            if sha256_file(source) != section["sha256"]:
                raise RuntimeError(f"分类 PDF 自 prepare 后已变化：{section['unpaginated']}")
            target = temp_numbered / Path(section["unpaginated"]).name
            pages = number_pdf(source, target, section["logical_start"], prepared["page_number_policy"])
            if pages != section["page_count"]:
                raise RuntimeError(f"编号后页数变化：{section['name']}")
            numbered_sections.append({
                **section,
                "numbered": f"{prepared['run_dir']}/20_numbered-items/final/{target.name}",
                "numbered_sha256": sha256_file(target),
                "physical_start": cover_pages + directory_pages + section["logical_start"],
                "physical_end": cover_pages + directory_pages + section["logical_end"],
            })

        final_pdf = temp_final / "matter-archive.pdf"
        final_pages = merge_pdfs(
            [cover_pdf, directory_pdf] + [temp_numbered / Path(item["numbered"]).name for item in numbered_sections],
            final_pdf,
            allow_qpdf=allow_qpdf,
        )
        expected_pages = cover_pages + directory_pages + prepared["body_page_count"]
        if final_pages != expected_pages:
            raise RuntimeError("最终卷宗页数与封皮、目录和正文之和不一致")
        if prepared["page_number_policy"] != "keep_confirmed":
            number_failures = verify_logical_page_numbers(
                final_pdf, cover_pages + directory_pages + 1, prepared["body_page_count"],
            )
            if number_failures:
                raise RuntimeError("最终卷宗连续页码缺失或错位：" + "、".join(map(str, number_failures[:20])))

        promote_directory(temp_front, front_dir, promoted_directories)
        promote_directory(temp_numbered, numbered_dir, promoted_directories)
        promote_directory(temp_final, final_dir, promoted_directories)

        manifest_core = {
            "schema_version": SCHEMA_VERSION, "status": "technical_complete_manual_visual_review_required",
            "run_disposition": "active",
            "profile": prepared["profile"], "matter_id": prepared["matter_id"],
            "case_numbers": prepared.get("case_numbers", []), "run_dir": prepared["run_dir"],
            "source_roots": prepared["source_roots"], "output_root": prepared["output_root"],
            "execution_policy": execution_policy, "page_number_policy": prepared["page_number_policy"],
            "archive_intake": prepared.get("archive_intake", {}),
            "engagement": prepared.get("engagement", {}),
            "internal_close": prepared.get("internal_close", {}),
            "court_or_tribunal_disposition": prepared.get("court_or_tribunal_disposition", {}),
            "prepared_manifest": relative(root, prepared_path), "prepared_sha256": sha256_file(prepared_path),
            "template_mode": template_mode,
            "front_matter": {
                "cover_docx": f"{prepared['run_dir']}/30_front-matter/cover.docx" if cover_preview_docx else "",
                "cover_pdf": f"{prepared['run_dir']}/30_front-matter/cover.pdf", "cover_pages": cover_pages,
                "directory_docx": f"{prepared['run_dir']}/30_front-matter/directory.docx" if directory_preview_docx else "",
                "directory_pdf": f"{prepared['run_dir']}/30_front-matter/directory.pdf", "directory_pages": directory_pages,
                "converter": cover_engine or directory_engine,
                "cover_integrity": {
                    **cover_integrity,
                    "page_geometry_matches_template": True,
                    "field_labels_extractable": True,
                },
                "cover_completion": plan.get("cover_completion", {}),
                "directory_labels_extractable": True,
                "directory_layout": directory_layout,
                "directory_layout_acceptance": directory_layout_record,
            },
            "sections": numbered_sections, "body_page_count": prepared["body_page_count"],
            "missing_categories": prepared.get("missing_categories", []),
            "missing_material_disposition": prepared.get("missing_material_disposition"),
            "final_pdf": f"{prepared['run_dir']}/40_final/matter-archive.pdf",
            "final_pdf_sha256": sha256_file(final_dir / "matter-archive.pdf"), "final_page_count": final_pages,
            "engagement_source_snapshot": prepared["engagement_source_snapshot"],
        }
        final_manifest = run_dir / "50_manifest" / "archive-manifest.json"
        if final_manifest.exists():
            raise RuntimeError("最终 archive manifest 已存在，禁止覆盖")
        validate_schema_document(manifest_core, "archive_manifest.schema.json", "archive manifest")
        write_new_json(final_manifest, manifest_core)
        promoted_files.append(final_manifest)
        disposition_path = run_disposition_path(run_dir)
        write_new_json(disposition_path, {
            "disposition_version": "1.0",
            "status": "active",
            "archive_manifest": relative(root, final_manifest),
            "archive_manifest_sha256": sha256_file(final_manifest),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })
        promoted_files.append(disposition_path)
        marker.unlink(missing_ok=True)
        temp_root.rmdir()
        return {
            "status": manifest_core["status"], "archive_manifest": relative(root, final_manifest),
            "final_pdf": manifest_core["final_pdf"], "final_page_count": final_pages,
        }
    except BaseException:
        rollback_promotions(promoted_files, promoted_directories)
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


def run_disposition_path(run_dir: Path) -> Path:
    return run_dir / "50_manifest" / "run-disposition.json"


def read_run_disposition(run_dir: Path) -> dict[str, Any]:
    path = run_disposition_path(run_dir)
    if not path.exists():
        return {"status": "active", "source": "implicit_for_legacy_run"}
    value = read_json(path)
    if value.get("status") not in {"active", "invalidated", "superseded"}:
        raise RuntimeError("run-disposition.json 状态无效")
    return value


def ensure_run_active(run_dir: Path) -> None:
    disposition = read_run_disposition(run_dir)
    if disposition.get("status") != "active":
        raise RuntimeError(f"该归档 run 已{disposition.get('status')}，禁止继续核验、交付或重复执行")


def verify_archive(root: Path, manifest_path: Path, manifest: dict[str, Any], visual_confirmed: bool, reviewer: str | None) -> dict[str, Any]:
    validate_schema_document(manifest, "archive_manifest.schema.json", "archive manifest")
    run_dir = resolve_inside(root, manifest["run_dir"], "run_dir", must_exist=True)
    ensure_run_active(run_dir)
    execution_policy = manifest.get("execution_policy") or {}
    enabled_layers = set(execution_policy.get("enabled_layers", []))
    policies = execution_policy.get("policies") or {}
    automatic_visual_enabled = (
        "visual" in enabled_layers
        and policies.get("visual_verification") == "automatic_when_available"
    )
    if visual_confirmed and not automatic_visual_enabled:
        raise RuntimeError("当前配置未启用自动视觉核验，不能提升为 ready_for_oa_submission")
    checks: list[dict[str, Any]] = []
    source_failures = verify_snapshot(root, manifest.get("engagement_source_snapshot", []))
    checks.append({"check": "source_immutability", "ok": not source_failures, "detail": source_failures})
    final_pdf = resolve_inside(root, manifest["final_pdf"], "final_pdf", must_exist=True)
    final_hash_ok = sha256_file(final_pdf) == manifest["final_pdf_sha256"]
    checks.append({"check": "final_pdf_hash", "ok": final_hash_ok, "detail": ""})
    actual_pages = validate_pdf(final_pdf)
    checks.append({"check": "final_page_count", "ok": actual_pages == manifest["final_page_count"], "detail": actual_pages})
    expected = manifest["front_matter"]["cover_pages"] + manifest["front_matter"]["directory_pages"] + manifest["body_page_count"]
    checks.append({"check": "page_sum", "ok": expected == manifest["final_page_count"], "detail": expected})
    for section in manifest["sections"]:
        path = resolve_inside(root, section["numbered"], "numbered section", must_exist=True)
        checks.append({
            "check": f"section:{section['sequence']}",
            "ok": sha256_file(path) == section["numbered_sha256"] and validate_pdf(path) == section["page_count"],
            "detail": section["page_range"],
        })
    if manifest.get("page_number_policy") == "keep_confirmed":
        checks.append({
            "check": "existing_page_numbers_visual_review",
            "ok": visual_confirmed,
            "detail": "已由律师逐页视觉确认" if visual_confirmed else "保留既有页码，等待律师逐页视觉确认",
        })
    else:
        logical_failures = verify_logical_page_numbers(
            final_pdf,
            manifest["front_matter"]["cover_pages"] + manifest["front_matter"]["directory_pages"] + 1,
            manifest["body_page_count"],
        )
        checks.append({"check": "logical_page_numbers", "ok": not logical_failures, "detail": logical_failures})
    directory_pdf = resolve_inside(root, manifest["front_matter"]["directory_pdf"], "directory_pdf", must_exist=True)
    if manifest.get("template_mode") == "pdf":
        directory_ok, directory_detail = True, "已填写 PDF 目录由律师在 Gate B 确认"
    else:
        try:
            validate_directory_rendered_values(directory_pdf, manifest["sections"])
            directory_ok, directory_detail = True, "正式目录字段完整且未出现内部说明"
        except RuntimeError as exc:
            directory_ok, directory_detail = False, str(exc)
    checks.append({"check": "directory_semantic_values", "ok": directory_ok, "detail": directory_detail})
    expected_layout = manifest["front_matter"].get("directory_layout") or {}
    layout_keys = [
        "directory_pdf_sha256", "directory_pages", "page_entries", "warnings",
        "requires_lawyer_acceptance", "acceptance_token",
    ]
    acceptance = manifest["front_matter"].get("directory_layout_acceptance") or {}
    actual_layout = expected_layout if manifest.get("template_mode") == "pdf" else inspect_directory_layout(directory_pdf, manifest["sections"])
    layout_ok = all(expected_layout.get(key) == actual_layout.get(key) for key in layout_keys)
    if expected_layout.get("requires_lawyer_acceptance"):
        layout_ok = layout_ok and acceptance.get("required") is True and acceptance.get("accepted") is True and bool(acceptance.get("accepted_by"))
    checks.append({
        "check": "directory_layout_gate_b",
        "ok": layout_ok,
        "detail": actual_layout.get("warnings", []),
    })

    qa_dir = run_dir / "60_qa"
    qa_dir.mkdir(parents=True, exist_ok=True)
    report_name = "visual-review-record.md" if visual_confirmed else "verification-report.md"
    json_name = report_name.replace(".md", ".json")
    if (qa_dir / report_name).exists() or (qa_dir / json_name).exists():
        raise RuntimeError(f"核验输出已存在，禁止覆盖：{report_name}")
    render_dir = qa_dir / ("rendered-visual" if visual_confirmed else "rendered-technical")
    if automatic_visual_enabled:
        rendered_paths, render_diagnostics = render_pdf_for_review(final_pdf, render_dir, actual_pages)
    else:
        rendered_paths, render_diagnostics = [], {
            "engine": "",
            "error": "自动视觉核验未启用；仅可保留人工视觉复核状态",
        }
    render_ok = len(rendered_paths) == actual_pages and not render_diagnostics.get("error")
    checks.append({"check": "render_pages", "ok": render_ok, "detail": render_diagnostics.get("error") or render_diagnostics.get("engine", "")})

    cover_match, cover_match_detail = False, ""
    if rendered_paths:
        try:
            cover_pdf = resolve_inside(root, manifest["front_matter"]["cover_pdf"], "cover_pdf", must_exist=True)
            raw_dir = qa_dir / ("raw-cover-compare-visual" if visual_confirmed else "raw-cover-compare-technical")
            cover_raw = render_first_page_raw(cover_pdf, raw_dir / "cover")
            final_raw = render_first_page_raw(final_pdf, raw_dir / "final")
            cover_match = sha256_file(cover_raw) == sha256_file(final_raw)
            if not cover_match:
                cover_match_detail = "独立封皮与最终 PDF 首页原始像素渲染不一致"
        except (OSError, ValueError, RuntimeError) as exc:
            cover_match_detail = str(exc)
    else:
        cover_match_detail = render_diagnostics.get("error", "最终 PDF 未生成可比对首页")
    checks.append({"check": "front_cover_render_matches_final_first_page", "ok": cover_match, "detail": cover_match_detail or "一致"})
    technical_ok = all(
        check["ok"] for check in checks
        if check["check"] not in {
            "render_pages", "front_cover_render_matches_final_first_page", "existing_page_numbers_visual_review",
        }
    )
    automatic_visual_ok = render_ok and cover_match and (
        manifest.get("page_number_policy") != "keep_confirmed" or visual_confirmed
    )

    if visual_confirmed and not reviewer:
        raise ValueError("确认视觉复核时必须提供 reviewer")
    if visual_confirmed and not (technical_ok and automatic_visual_ok):
        raise RuntimeError("技术检查或自动视觉核验未全部通过，不能确认视觉复核")
    status = "ready_for_oa_submission" if visual_confirmed else "technical_complete_manual_visual_review_required"
    result = {
        "schema_version": SCHEMA_VERSION, "status": status, "technical_ok": technical_ok,
        "automatic_visual_ok": automatic_visual_ok,
        "render_ok": render_ok, "rendered_pages": [relative(root, path) for path in rendered_paths],
        "visual_review_confirmed": visual_confirmed, "reviewer": reviewer or "",
        "reviewed_at": datetime.now().isoformat(timespec="seconds") if visual_confirmed else "",
        "archive_manifest": relative(root, manifest_path), "archive_manifest_sha256": sha256_file(manifest_path),
        "review_artifacts": {
            "render_engine": render_diagnostics.get("engine", ""),
            "render_diagnostics": render_diagnostics,
            "rendered_pages": [relative(root, path) for path in rendered_paths],
        },
        "checks": checks,
    }
    report = ["# 归档技术与视觉核验", "", f"- 状态：`{status}`", f"- 技术检查：{'通过' if technical_ok else '失败'}", f"- 页面渲染：{'通过' if render_ok else '失败'}", f"- 视觉复核：{'已确认' if visual_confirmed else '待逐页确认'}", "", "## 检查项", ""]
    report.extend(f"- [{'x' if item['ok'] else ' '}] {item['check']}：{item['detail']}" for item in checks)
    if status == "ready_for_oa_submission":
        report.extend(["", "本记录只说明卷宗已达到 OA 提交前准备状态；交付到项目结案归档目录必须另行预览、确认并执行。", ""])
    else:
        report.extend(["", "本记录表示技术处理已完成，但尚未达到 OA 提交前准备状态，仍需完成规定的视觉复核。", ""])
    result["verification_report"] = relative(root, qa_dir / report_name)
    validate_schema_document(result, "archive_verification.schema.json", "archive verification")
    write_new_text(qa_dir / report_name, "\n".join(report))
    write_new_json(qa_dir / json_name, result)
    return result


def ready_verification_for_manifest(root: Path, run_dir: Path, manifest_path: Path) -> tuple[Path, dict[str, Any]]:
    """Return the single lawyer-confirmed visual review tied to this immutable manifest."""
    manifest_hash = sha256_file(manifest_path)
    qa_dir = run_dir / "60_qa"
    candidates: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(qa_dir.glob("*.json")):
        try:
            value = read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if (
            value.get("status") == "ready_for_oa_submission"
            and value.get("visual_review_confirmed") is True
            and value.get("technical_ok") is True
            and value.get("render_ok") is True
            and value.get("automatic_visual_ok") is True
            and value.get("archive_manifest") == relative(root, manifest_path)
            and value.get("archive_manifest_sha256") == manifest_hash
        ):
            candidates.append((path, value))
    if len(candidates) != 1:
        raise RuntimeError("交付前必须存在且仅存在一份与当前 manifest 匹配的律师视觉复核记录")
    return candidates[0]


def delivery_target(root: Path, manifest: dict[str, Any], destination: str, filename: str) -> tuple[Path, Path]:
    if not filename or filename != Path(filename).name or filename in {".", ".."}:
        raise ValueError("交付文件名必须是单一文件名，不得包含目录")
    if Path(filename).suffix.lower() != ".pdf":
        raise ValueError("交付文件名必须以 .pdf 结尾")
    destination_dir = resolve_inside(root, destination, "交付目录")
    run_dir = resolve_inside(root, manifest["run_dir"], "run_dir", must_exist=True)
    if is_within(destination_dir, run_dir):
        raise ValueError("交付目录不得位于归档 run 内部")
    output_root = resolve_inside(root, manifest["output_root"], "output_root", must_exist=True)
    if not is_within(destination_dir, output_root):
        raise ValueError("交付目录必须位于获批 output_root 内")
    for source in manifest.get("source_roots", []):
        source_path = resolve_inside(root, str(source), "source_root", must_exist=True)
        if is_within(destination_dir, source_path) or is_within(source_path, destination_dir):
            raise ValueError("交付目录不得位于源材料目录内或覆盖源材料目录")
    return destination_dir, destination_dir / filename


def preview_delivery(root: Path, manifest_path: Path, manifest: dict[str, Any], destination: str, filename: str) -> dict[str, Any]:
    validate_schema_document(manifest, "archive_manifest.schema.json", "archive manifest")
    run_dir = resolve_inside(root, manifest["run_dir"], "run_dir", must_exist=True)
    ensure_run_active(run_dir)
    final_pdf = resolve_inside(root, manifest["final_pdf"], "final_pdf", must_exist=True)
    if sha256_file(final_pdf) != manifest["final_pdf_sha256"]:
        raise RuntimeError("最终 PDF 哈希与 manifest 不一致，禁止交付预览")
    verification_path, verification = ready_verification_for_manifest(root, run_dir, manifest_path)
    destination_dir, target = delivery_target(root, manifest, destination, filename)
    if destination_dir.exists() and any(destination_dir.iterdir()):
        raise RuntimeError(f"交付目录必须为空，确保仅放入最终 PDF：{relative(root, destination_dir)}")
    if target.exists():
        raise RuntimeError(f"交付目标已存在，禁止覆盖：{relative(root, target)}")
    qa_dir = run_dir / "60_qa"
    delivery_plan_path = qa_dir / "delivery-plan.json"
    if delivery_plan_path.exists():
        raise RuntimeError("交付预览已存在；如需更换目标，请先明确使当前 run 失效后重新生成")
    core = {
        "schema_version": SCHEMA_VERSION,
        "status": "preview",
        "archive_manifest": relative(root, manifest_path),
        "archive_manifest_sha256": sha256_file(manifest_path),
        "verification_record": relative(root, verification_path),
        "verification_sha256": sha256_file(verification_path),
        "final_pdf": manifest["final_pdf"],
        "final_pdf_sha256": manifest["final_pdf_sha256"],
        "destination_directory": relative(root, destination_dir),
        "destination_pdf": relative(root, target),
        "filename": filename,
        "visual_reviewer": str(verification.get("reviewer", "")),
        "delivery_scope": "only_final_pdf",
    }
    plan = {**core, "confirmation_token": digest(core)}
    validate_schema_document(plan, "delivery_plan.schema.json", "delivery plan")
    write_new_json(delivery_plan_path, plan)
    return {"status": "preview", "delivery_plan": relative(root, delivery_plan_path), **plan}


def copy_final_pdf_no_overwrite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.parent / f".{target.name}.delivery-{uuid.uuid4().hex}"
    created_target = False
    try:
        shutil.copy2(source, temp)
        try:
            os.link(temp, target)
            created_target = True
        except FileExistsError as exc:
            raise RuntimeError(f"交付目标已存在，禁止覆盖：{target}") from exc
        except OSError:
            with target.open("xb") as handle, temp.open("rb") as copied:
                shutil.copyfileobj(copied, handle)
            created_target = True
        if sha256_file(target) != sha256_file(source):
            raise RuntimeError("交付后的 PDF 哈希与最终 PDF 不一致")
    except BaseException:
        if created_target:
            target.unlink(missing_ok=True)
        raise
    finally:
        temp.unlink(missing_ok=True)


def deliver_archive(root: Path, delivery_plan_path: Path, delivery_plan: dict[str, Any], confirm: str) -> dict[str, Any]:
    validate_schema_document(delivery_plan, "delivery_plan.schema.json", "delivery plan")
    core = {key: value for key, value in delivery_plan.items() if key != "confirmation_token"}
    expected = digest(core)
    if delivery_plan.get("status") != "preview" or delivery_plan.get("confirmation_token") != expected or confirm != expected:
        raise RuntimeError("delivery confirmation_token 不匹配、plan 已被修改或不是可执行预览")
    manifest_path = resolve_inside(root, delivery_plan["archive_manifest"], "archive_manifest", must_exist=True)
    manifest = read_json(manifest_path)
    validate_schema_document(manifest, "archive_manifest.schema.json", "archive manifest")
    if sha256_file(manifest_path) != delivery_plan["archive_manifest_sha256"]:
        raise RuntimeError("archive manifest 自交付预览后已变化")
    run_dir = resolve_inside(root, manifest["run_dir"], "run_dir", must_exist=True)
    ensure_run_active(run_dir)
    verification_path = resolve_inside(root, delivery_plan["verification_record"], "verification_record", must_exist=True)
    verification = read_json(verification_path)
    if sha256_file(verification_path) != delivery_plan["verification_sha256"]:
        raise RuntimeError("视觉复核记录自交付预览后已变化")
    ready_verification_for_manifest(root, run_dir, manifest_path)
    final_pdf = resolve_inside(root, delivery_plan["final_pdf"], "final_pdf", must_exist=True)
    if sha256_file(final_pdf) != delivery_plan["final_pdf_sha256"]:
        raise RuntimeError("最终 PDF 自交付预览后已变化")
    destination_dir, target = delivery_target(root, manifest, delivery_plan["destination_directory"], delivery_plan["filename"])
    if relative(root, target) != delivery_plan["destination_pdf"]:
        raise RuntimeError("交付目标与获批 delivery plan 不一致")
    if destination_dir.exists() and any(destination_dir.iterdir()):
        raise RuntimeError("交付目录在确认后出现其他内容，禁止再交付")
    if target.exists():
        raise RuntimeError(f"交付目标已存在，禁止覆盖：{delivery_plan['destination_pdf']}")
    qa_dir = run_dir / "60_qa"
    record_path = qa_dir / "delivery-record.json"
    if record_path.exists():
        raise RuntimeError("该 run 已存在交付记录，禁止重复交付")
    created: list[Path] = []
    try:
        copy_final_pdf_no_overwrite(final_pdf, target)
        record = {
            "delivery_version": "1.0", "status": "delivered", "delivered_at": datetime.now().isoformat(timespec="seconds"),
            "delivery_plan": relative(root, delivery_plan_path), "delivery_plan_sha256": sha256_file(delivery_plan_path),
            "archive_manifest": relative(root, manifest_path), "archive_manifest_sha256": sha256_file(manifest_path),
            "verification_record": relative(root, verification_path), "verification_sha256": sha256_file(verification_path),
            "final_pdf": manifest["final_pdf"], "final_pdf_sha256": manifest["final_pdf_sha256"],
            "destination_pdf": relative(root, target), "destination_pdf_sha256": sha256_file(target),
            "delivery_scope": "only_final_pdf",
        }
        write_new_json(record_path, record)
        created.append(record_path)
        return {"status": "delivered", "destination_pdf": relative(root, target), "delivery_record": relative(root, record_path)}
    except BaseException:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise


def preview_invalidate(root: Path, manifest_path: Path, manifest: dict[str, Any], reason: str, confirmed_by: str) -> dict[str, Any]:
    validate_schema_document(manifest, "archive_manifest.schema.json", "archive manifest")
    if not reason.strip() or not confirmed_by.strip():
        raise ValueError("使归档 run 失效必须写明 reason 和 confirmed_by")
    run_dir = resolve_inside(root, manifest["run_dir"], "run_dir", must_exist=True)
    ensure_run_active(run_dir)
    qa_dir = run_dir / "60_qa"
    path = qa_dir / "invalidation-plan.json"
    if path.exists():
        raise RuntimeError("失效预览已存在，禁止覆盖")
    core = {
        "invalidation_version": "1.0", "status": "preview", "archive_manifest": relative(root, manifest_path),
        "archive_manifest_sha256": sha256_file(manifest_path), "reason": reason.strip(), "confirmed_by": confirmed_by.strip(),
    }
    plan = {**core, "confirmation_token": digest(core)}
    validate_schema_document(plan, "invalidation_plan.schema.json", "invalidation plan")
    write_new_json(path, plan)
    return {"status": "preview", "invalidation_plan": relative(root, path), **plan}


def invalidate_archive(root: Path, plan_path: Path, plan: dict[str, Any], confirm: str) -> dict[str, Any]:
    validate_schema_document(plan, "invalidation_plan.schema.json", "invalidation plan")
    core = {key: value for key, value in plan.items() if key != "confirmation_token"}
    expected = digest(core)
    if plan.get("status") != "preview" or plan.get("confirmation_token") != expected or confirm != expected:
        raise RuntimeError("invalidation confirmation_token 不匹配、plan 已被修改或不是可执行预览")
    manifest_path = resolve_inside(root, str(plan.get("archive_manifest", "")), "archive_manifest", must_exist=True)
    if sha256_file(manifest_path) != plan.get("archive_manifest_sha256"):
        raise RuntimeError("archive manifest 自失效预览后已变化")
    manifest = read_json(manifest_path)
    run_dir = resolve_inside(root, manifest["run_dir"], "run_dir", must_exist=True)
    ensure_run_active(run_dir)
    disposition = run_disposition_path(run_dir)
    if disposition.exists():
        active = read_run_disposition(run_dir)
        if active.get("status") != "active":
            raise RuntimeError("run disposition 已不是 active")
    replacement = disposition.with_name(f".{disposition.name}.invalidate-{uuid.uuid4().hex}")
    write_new_json(replacement, {
        "disposition_version": "1.0", "status": "invalidated", "archive_manifest": relative(root, manifest_path),
        "archive_manifest_sha256": sha256_file(manifest_path), "reason": str(plan["reason"]),
        "confirmed_by": str(plan["confirmed_by"]), "invalidated_at": datetime.now().isoformat(timespec="seconds"),
    })
    try:
        os.replace(replacement, disposition)
    except BaseException:
        replacement.unlink(missing_ok=True)
        raise
    return {"status": "invalidated", "run_disposition": relative(root, disposition)}


def main() -> int:
    parser = argparse.ArgumentParser(description="通用法律业务正式归档 Tool")
    parser.add_argument("--project-root")
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    sub.add_parser("check")
    setup = sub.add_parser("setup")
    setup_mode = setup.add_mutually_exclusive_group(required=True)
    setup_mode.add_argument("--plan", action="store_true")
    setup_mode.add_argument("--apply", action="store_true")
    setup.add_argument("--layers", default="core")
    setup.add_argument("--include-system", action="store_true")
    setup.add_argument("--confirm")
    configure = sub.add_parser("configure")
    configure_mode = configure.add_mutually_exclusive_group(required=True)
    configure_mode.add_argument("--plan", action="store_true")
    configure_mode.add_argument("--apply", action="store_true")
    configure.add_argument("--input", required=True)
    configure.add_argument("--confirm")
    template = sub.add_parser("template-check")
    template.add_argument("--mode", choices=["pdf", "docx"], required=True)
    template.add_argument("--cover", required=True)
    template.add_argument("--directory", required=True)
    template.add_argument("--cover-pages", type=int, default=1)
    template.add_argument("--directory-pages", type=int, default=1)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--source-root", action="append", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--input", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--plan", required=True)
    prepare.add_argument("--confirm", required=True)
    finalize = sub.add_parser("finalize")
    prepared_group = finalize.add_mutually_exclusive_group(required=True)
    prepared_group.add_argument("--prepared")
    prepared_group.add_argument("--prepared-manifest")
    finalize.add_argument("--confirm", required=True)
    finalize.add_argument("--accept-directory-layout")
    finalize.add_argument("--directory-layout-reviewer")
    verify = sub.add_parser("verify")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--visual-review-confirmed", action="store_true")
    verify.add_argument("--reviewer")
    preview_deliver = sub.add_parser("preview-deliver")
    preview_deliver.add_argument("--manifest", required=True)
    preview_deliver.add_argument("--destination", required=True)
    preview_deliver.add_argument("--filename", required=True)
    deliver = sub.add_parser("deliver")
    deliver.add_argument("--plan", required=True)
    deliver.add_argument("--confirm", required=True)
    preview_invalidation = sub.add_parser("preview-invalidate")
    preview_invalidation.add_argument("--manifest", required=True)
    preview_invalidation.add_argument("--reason", required=True)
    preview_invalidation.add_argument("--confirmed-by", required=True)
    invalidate = sub.add_parser("invalidate")
    invalidate.add_argument("--plan", required=True)
    invalidate.add_argument("--confirm", required=True)
    args = parser.parse_args()
    try:
        config_path = selected_config_path(args.config)
        if args.command in {"doctor", "check"}:
            result = dependency_report(config_path)
        elif args.command == "setup":
            candidate = setup_plan(parse_layers(args.layers), args.include_system)
            if args.plan:
                result = candidate
            else:
                if not args.confirm:
                    raise ValueError("setup --apply 必须提供 --confirm")
                result = apply_setup(candidate, args.confirm)
        elif args.command == "configure":
            candidate_path = Path(args.input).expanduser().resolve()
            candidate = configure_plan(candidate_path, config_path, validate_templates=True)
            if args.plan:
                result = candidate
            else:
                if not args.confirm:
                    raise ValueError("configure --apply 必须提供 --confirm")
                result = apply_configuration(candidate, args.confirm)
        elif args.command == "template-check":
            result = template_check(
                args.mode, Path(args.cover).expanduser(), Path(args.directory).expanduser(),
                args.cover_pages, args.directory_pages,
            )
        else:
            if not args.project_root:
                raise ValueError(f"{args.command} 必须提供 --project-root")
            root = Path(args.project_root).expanduser().resolve()
            if not root.is_dir():
                raise ValueError(f"项目根目录不存在：{root}")
            require_core_dependencies()
            if args.command == "inspect":
                sources = validate_source_roots(root, args.source_root)
                inventory = scan_sources(root, sources)
                exact, same_name = duplicate_groups(inventory)
                result = {"schema_version": SCHEMA_VERSION, "status": "inspected", "inventory": inventory, "duplicates": exact, "same_name_conflicts": same_name}
            elif args.command == "plan":
                request_path = resolve_inside(root, args.input, "input", must_exist=True)
                request = read_json(request_path)
                config = read_local_config(config_path)
                archive_plan, names = build_plan(root, request_path, request, config)
                saved = write_preview(root, archive_plan, names)
                result = {"plan": relative(root, saved), **archive_plan}
            elif args.command == "prepare":
                path = resolve_inside(root, args.plan, "plan", must_exist=True)
                result = prepare_archive(root, path, read_json(path), args.confirm)
            elif args.command == "finalize":
                prepared_value = args.prepared or args.prepared_manifest
                path = resolve_inside(root, prepared_value, "prepared", must_exist=True)
                result = finalize_archive(
                    root, path, read_json(path), args.confirm,
                    args.accept_directory_layout, args.directory_layout_reviewer,
                )
            elif args.command == "verify":
                path = resolve_inside(root, args.manifest, "manifest", must_exist=True)
                result = verify_archive(root, path, read_json(path), args.visual_review_confirmed, args.reviewer)
            elif args.command == "preview-deliver":
                path = resolve_inside(root, args.manifest, "manifest", must_exist=True)
                result = preview_delivery(root, path, read_json(path), args.destination, args.filename)
            elif args.command == "deliver":
                path = resolve_inside(root, args.plan, "delivery_plan", must_exist=True)
                result = deliver_archive(root, path, read_json(path), args.confirm)
            elif args.command == "preview-invalidate":
                path = resolve_inside(root, args.manifest, "manifest", must_exist=True)
                result = preview_invalidate(root, path, read_json(path), args.reason, args.confirmed_by)
            else:
                path = resolve_inside(root, args.plan, "invalidation_plan", must_exist=True)
                result = invalidate_archive(root, path, read_json(path), args.confirm)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, RuntimeError, KeyError, OSError, zipfile.BadZipFile) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
