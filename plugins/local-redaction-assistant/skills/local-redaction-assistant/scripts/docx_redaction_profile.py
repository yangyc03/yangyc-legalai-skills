#!/usr/bin/env python3
"""DOCX profile candidates and compatibility-preserving OOXML helpers."""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import unquote
from xml.etree import ElementTree


AI_SHARE_PROFILE = "ai-share"
LEGAL_TEMPLATE_PROFILE = "legal-template"
REDACTION_PROFILES = (AI_SHARE_PROFILE, LEGAL_TEMPLATE_PROFILE)

TERM_CATEGORIES = (
    "persons",
    "companies",
    "company_aliases",
    "projects",
    "institutions",
    "meeting_locations",
    "other_locations",
    "template_years",
    "template_dates",
    "headcounts",
    "share_counts",
    "vote_counts",
    "ownership_ratios",
    "service_fees",
    "other_amounts",
    "service_terms",
    "fund_manager_registration_numbers",
    "registration_numbers",
    "lawyer_phones",
    "phones",
    "emails",
    "identity_numbers",
    "unified_social_credit_codes",
    "bank_accounts",
    "case_numbers",
    "contract_numbers",
    "addresses",
    "other_terms",
)

TERM_CATEGORY_LABELS = {
    "persons": "姓名",
    "companies": "主体全称",
    "company_aliases": "主体简称",
    "projects": "基金/项目名称",
    "institutions": "机构/单位",
    "meeting_locations": "会议地点",
    "other_locations": "其他地点/地名",
    "template_years": "项目年份",
    "template_dates": "项目日期",
    "headcounts": "人数",
    "share_counts": "股份数",
    "vote_counts": "票数/表决权总数",
    "ownership_ratios": "表决/持股比例",
    "service_fees": "服务费/报价",
    "other_amounts": "价格/贷款/薪酬等金额",
    "service_terms": "服务/合同期限",
    "fund_manager_registration_numbers": "基金管理人登记编号",
    "registration_numbers": "登记/备案编号",
    "lawyer_phones": "律师手机号",
    "phones": "电话/手机号",
    "emails": "邮箱",
    "identity_numbers": "证件号码",
    "unified_social_credit_codes": "统一社会信用代码",
    "bank_accounts": "银行账号",
    "case_numbers": "案号",
    "contract_numbers": "合同编号",
    "addresses": "地址",
    "other_terms": "其他词项",
}

COMPANY_PATTERN = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9（）()·]{2,60}"
    r"(?:有限责任公司|股份有限公司|有限公司|合伙企业（有限合伙）|合伙企业)"
)
FUND_PATTERN = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9（）()·]{2,60}"
    r"(?:私募股权投资基金|股权投资基金|创业投资基金|投资基金|基金)"
)
PERSON_PATTERN = re.compile(
    r"(?:姓名|原告|被告|第三人|法定代表人|联系人|委托代理人|代理人|申请人|被申请人|上诉人|被上诉人|"
    r"股东|董事|监事|授权代表|会议主持人|记录人|计票人|监票人)"
    r"[：:，,\s]{0,6}([\u4e00-\u9fff]{2,4})(?![\u4e00-\u9fff])"
)
LAWYER_PATTERN = re.compile(
    r"(?:经办律师|承办律师|见证律师|律师姓名|律师)[：:，,\s]{0,6}"
    r"([\u4e00-\u9fff]{2,4})(?![\u4e00-\u9fff])"
)
ADDRESS_PATTERN = re.compile(
    r"(?:住址|住所地|地址|送达地址|通讯地址)[：:，,\s]{0,6}"
    r"([\u4e00-\u9fffA-Za-z0-9（）()\-]{6,80})"
)
ALIAS_PATTERN = re.compile(
    r"(?:以下简称|下称|简称)[：:]?[\s“\"《]?"
    r"([\u4e00-\u9fffA-Za-z0-9（）()·_-]{1,30})[”\"》]?"
)
MEETING_LOCATION_PATTERN = re.compile(
    r"(?:会议地点|开会地点|见证地点|召开地点)[：:]\s*([^\n\r；;。]{2,80})"
)
OTHER_LOCATION_PATTERN = re.compile(r"(?:^|[\n\r；;。])地点[：:]\s*([^\n\r；;。]{2,80})")
YEAR_PATTERN = re.compile(r"(?<!\d)(20\d{2}年)")
DATE_PATTERN = re.compile(r"(?<!\d)(20\d{2}年\d{1,2}月\d{1,2}日)(?!\d)")
HEADCOUNT_PATTERN = re.compile(r"(?<!\d)(\d{1,6}(?:人|位|名))(?!\d)")
SHARE_COUNT_PATTERN = re.compile(r"(?<!\d)(\d[\d,，]*(?:\.\d+)?(?:万|亿)?股)(?!\d)")
VOTE_COUNT_PATTERN = re.compile(r"(?<!\d)(\d[\d,，]*(?:\.\d+)?(?:万|亿)?(?:票|份表决权))(?!\d)")
RATIO_PATTERN = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?%)(?![\d.])")
MONEY_PATTERN = re.compile(
    r"(?:人民币|RMB|CNY|￥|¥)?\s*\d[\d,，]*(?:\.\d+)?\s*(?:元|万元|亿元)"
)
FUND_REGISTRATION_PATTERN = re.compile(
    r"(?:基金管理人登记编号|私募基金管理人登记编号)[：:]\s*"
    r"([A-Za-z0-9_-]{4,40})"
)
PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?86[- ]?)?(1[3-9]\d{9})(?!\d)")
LANDLINE_PATTERN = re.compile(r"(?<!\d)(0\d{2,3}[- ]?\d{7,8})(?!\d)")
EMAIL_PATTERN = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![A-Za-z0-9.-])")
IDENTITY_CONTEXT_PATTERN = re.compile(
    r"(?:身份证(?:号码|号)?|证件(?:号码|号)?|统一证件号码)[：:]?\s*([0-9A-Za-z*]{15,20})"
)
IDENTITY_NUMBER_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])([1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
    r"(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx])(?![0-9A-Za-z])"
)
IDENTITY_15_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])([1-9]\d{5}\d{6}\d{3})(?![0-9A-Za-z])"
)
UNIFIED_SOCIAL_CREDIT_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])[159Y][0-9A-Z]{17}(?![0-9A-Za-z])"
)
INSTITUTION_PATTERN = re.compile(
    r"(?:机构|单位|银行|法院|医院|学校|律所|律师事务所|事务所名称)[：:]?\s*"
    r"([^\n\r，,；;。]{2,80})"
)
BANK_ACCOUNT_PATTERN = re.compile(
    r"(?:银行账号|银行账户|开户账号|账号)[：:]?\s*([0-9][0-9\- ]{8,29}[0-9])"
)
CASE_NUMBER_PATTERN = re.compile(
    r"(?:案号|案件编号)[：:]?\s*([^\s，,；;。]{4,40})"
)
CONTRACT_NUMBER_PATTERN = re.compile(
    r"(?:合同编号|合同号)[：:]?\s*([^\s，,；;。]{4,40})"
)
REGISTRATION_NUMBER_PATTERN = re.compile(
    r"(?:登记编号|备案编号|登记号|备案号)[：:]\s*([A-Za-z0-9_-]{4,40})"
)
TERM_PATTERN = re.compile(
    r"(?:服务期限|顾问期限|合同期限|项目期限|委托期限|聘用期限)[：:]?\s*"
    r"(\d+(?:年|个月|月|日))"
)

LEGAL_YEAR_PROTECTORS = ("修订", "施行", "颁布", "发布", "实施", "法规", "公司法", "民法典", "条例", "办法")
RATIO_PROTECTORS = ("利率", "年利率", "法定", "法律规定", "法规规定", "应当", "不得低于", "不得超过")
RATIO_CONTEXT = ("表决", "持股", "股权", "股份", "出资", "份额", "占比")
HEADCOUNT_CONTEXT = ("出席", "参会", "与会", "到会", "股东", "代表", "律师", "员工")
SERVICE_FEE_CONTEXT = ("服务费", "律师费", "法律服务费", "报价", "服务报价", "顾问费")
OTHER_AMOUNT_CONTEXT = ("贷款", "借款", "价格", "价款", "薪酬", "津贴", "报酬", "工资", "奖金")
PHONE_CONTEXT = ("电话", "手机", "联系方式", "联系人", "律师")
PERSON_FALSE_POSITIVES = {
    "电话", "手机", "姓名", "信息", "方式", "地址", "会议", "大会",
    "名册", "人数", "代表", "资格", "情况", "数量", "事务",
}

UNIFIED_CREDIT_ALPHABET = "0123456789ABCDEFGHJKLMNPQRTUWXY"
UNIFIED_CREDIT_WEIGHTS = (1, 3, 9, 7, 1, 3, 9, 7, 1, 3, 9, 7, 1, 3, 9, 7, 1)


def is_valid_identity_number(value: str) -> bool:
    """Validate mainland resident identity numbers without accepting placeholders."""
    value = value.upper()
    if IDENTITY_15_PATTERN.fullmatch(value):
        try:
            date(1900 + int(value[6:8]), int(value[8:10]), int(value[10:12]))
        except ValueError:
            return False
        return True
    if not IDENTITY_NUMBER_PATTERN.fullmatch(value):
        return False
    try:
        date(int(value[6:10]), int(value[10:12]), int(value[12:14]))
    except ValueError:
        return False
    weights = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
    checks = "10X98765432"
    try:
        check = sum(int(char) * weight for char, weight in zip(value[:17], weights)) % 11
    except ValueError:
        return False
    return value[-1] == checks[check]


def is_valid_unified_social_credit_code(value: str) -> bool:
    """Validate the 18-character Chinese unified social credit code checksum."""
    value = value.upper()
    if not UNIFIED_SOCIAL_CREDIT_PATTERN.fullmatch(value):
        return False
    try:
        total = sum(UNIFIED_CREDIT_ALPHABET.index(char) * weight for char, weight in zip(value[:17], UNIFIED_CREDIT_WEIGHTS))
    except ValueError:
        return False
    check_value = (31 - total % 31) % 31
    return value[-1] == UNIFIED_CREDIT_ALPHABET[check_value]


def is_high_confidence_candidate(category: str, value: str) -> bool:
    """Return whether a candidate is eligible for explicit user-enabled auto handling."""
    if category == "phones":
        return bool(PHONE_PATTERN.fullmatch(value))
    if category == "emails":
        return bool(EMAIL_PATTERN.fullmatch(value))
    if category == "identity_numbers":
        return is_valid_identity_number(value)
    if category == "unified_social_credit_codes":
        return is_valid_unified_social_credit_code(value)
    return False


def normalise_redaction_dictionary(payload: object) -> dict[str, list[str]]:
    """Validate the local JSON dictionary without persisting source values."""
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("redaction_dictionary_schema_error")
    terms_payload = payload.get("terms", {})
    allowlist_payload = payload.get("allowlist", [])
    if not isinstance(terms_payload, dict) or not isinstance(allowlist_payload, list):
        raise ValueError("redaction_dictionary_schema_error")
    allowed_keys = set(TERM_CATEGORIES) | {"other_terms"}
    result: dict[str, list[str]] = {category: [] for category in TERM_CATEGORIES}
    result["allowlist"] = []

    def normalise_values(values: object, limit: int) -> list[str]:
        if not isinstance(values, list) or len(values) > limit:
            raise ValueError("redaction_dictionary_schema_error")
        normalised: list[str] = []
        for value in values:
            if not isinstance(value, str):
                raise ValueError("redaction_dictionary_schema_error")
            value = re.sub(r"\s+", " ", value.strip())
            if not (2 <= len(value) <= 80) or any(ord(char) < 32 for char in value):
                raise ValueError("redaction_dictionary_schema_error")
            if value not in normalised:
                normalised.append(value)
        return normalised

    for key, values in terms_payload.items():
        if key not in allowed_keys:
            raise ValueError("redaction_dictionary_category_error")
        target_key = "other_terms" if key == "other_terms" else key
        result.setdefault(target_key, []).extend(normalise_values(values, 100))
        result[target_key] = list(dict.fromkeys(result[target_key]))[:100]
    result["allowlist"] = normalise_values(allowlist_payload, 500)
    return result


def _line(text: str, start: int, end: int) -> str:
    line_start = max(text.rfind("\n", 0, start), text.rfind("\r", 0, start)) + 1
    next_breaks = [position for position in (text.find("\n", end), text.find("\r", end)) if position >= 0]
    line_end = min(next_breaks) if next_breaks else len(text)
    return text[line_start:line_end]


def _clause(text: str, start: int, end: int) -> str:
    left_boundaries = [text.rfind(marker, 0, start) for marker in ("\n", "\r", "，", ",", "；", ";", "。")]
    clause_start = max(left_boundaries) + 1
    right_boundaries = [
        position
        for marker in ("\n", "\r", "，", ",", "；", ";", "。")
        for position in (text.find(marker, end),)
        if position >= 0
    ]
    clause_end = min(right_boundaries) if right_boundaries else len(text)
    return text[clause_start:clause_end]


def _nearest_named_subject(text: str, alias_start: int) -> str | None:
    prefix_start = max(0, alias_start - 140)
    prefix = text[prefix_start:alias_start]
    subjects = list(COMPANY_PATTERN.finditer(prefix)) + list(FUND_PATTERN.finditer(prefix))
    if not subjects:
        return None
    return max(subjects, key=lambda item: item.end()).group(0).strip()


def extract_profile_candidates(
    text: str,
    profile: str = AI_SHARE_PROFILE,
    limit_per_type: int = 50,
) -> dict:
    """Return local-sensitive candidates; no candidate is auto-approved."""
    if profile not in REDACTION_PROFILES:
        raise ValueError("unsupported_redaction_profile")

    candidates: dict[str, list[str]] = {category: [] for category in TERM_CATEGORIES}
    seen: dict[str, set[str]] = {category: set() for category in TERM_CATEGORIES}
    associations: list[dict[str, str]] = []
    association_seen: set[tuple[str, str]] = set()

    def add(category: str, value: str) -> None:
        normalised = re.sub(r"\s+", " ", value.strip(" \t\r\n，,；;。"))
        if len(normalised) < 2 or normalised in seen[category] or len(candidates[category]) >= limit_per_type:
            return
        seen[category].add(normalised)
        candidates[category].append(normalised)

    for match in COMPANY_PATTERN.finditer(text):
        add("companies", match.group(0))
    for match in PERSON_PATTERN.finditer(text):
        if match.group(1) not in PERSON_FALSE_POSITIVES:
            add("persons", match.group(1))
    for match in ADDRESS_PATTERN.finditer(text):
        add("addresses", match.group(1))

    for match in FUND_PATTERN.finditer(text):
        add("projects", match.group(0))
    for match in LAWYER_PATTERN.finditer(text):
        add("persons", match.group(1))
    for match in ALIAS_PATTERN.finditer(text):
        alias = match.group(1).strip()
        add("company_aliases", alias)
        subject = _nearest_named_subject(text, match.start())
        if subject:
            key = (subject, alias)
            if key not in association_seen and len(associations) < limit_per_type:
                association_seen.add(key)
                associations.append({"full_name": subject, "alias": alias})
    for match in INSTITUTION_PATTERN.finditer(text):
        add("institutions", match.group(1))
    for match in MEETING_LOCATION_PATTERN.finditer(text):
        add("meeting_locations", match.group(1))
    for match in OTHER_LOCATION_PATTERN.finditer(text):
        add("other_locations", match.group(1))
    for match in DATE_PATTERN.finditer(text):
        add("template_dates", match.group(1))
    for match in YEAR_PATTERN.finditer(text):
        context = _clause(text, match.start(), match.end())
        if not any(protector in context for protector in LEGAL_YEAR_PROTECTORS):
            add("template_years", match.group(1))
    for match in HEADCOUNT_PATTERN.finditer(text):
        context = _clause(text, match.start(), match.end())
        if any(marker in context for marker in HEADCOUNT_CONTEXT):
            add("headcounts", match.group(1))
    for match in SHARE_COUNT_PATTERN.finditer(text):
        add("share_counts", match.group(1))
    for match in VOTE_COUNT_PATTERN.finditer(text):
        add("vote_counts", match.group(1))
    for match in RATIO_PATTERN.finditer(text):
        context = _clause(text, match.start(), match.end())
        if any(marker in context for marker in RATIO_CONTEXT) and not any(
            protector in context for protector in RATIO_PROTECTORS
        ):
            add("ownership_ratios", match.group(1))
    for match in MONEY_PATTERN.finditer(text):
        context = _clause(text, match.start(), match.end())
        if any(marker in context for marker in SERVICE_FEE_CONTEXT):
            add("service_fees", match.group(0))
        elif any(marker in context for marker in OTHER_AMOUNT_CONTEXT):
            add("other_amounts", match.group(0))
    for match in TERM_PATTERN.finditer(text):
        add("service_terms", match.group(1))
    for match in FUND_REGISTRATION_PATTERN.finditer(text):
        add("fund_manager_registration_numbers", match.group(1))
    for match in REGISTRATION_NUMBER_PATTERN.finditer(text):
        add("registration_numbers", match.group(1))
    for match in CASE_NUMBER_PATTERN.finditer(text):
        add("case_numbers", match.group(1))
    for match in CONTRACT_NUMBER_PATTERN.finditer(text):
        add("contract_numbers", match.group(1))
    for match in PHONE_PATTERN.finditer(text):
        context = _line(text, match.start(), match.end())
        if "律师" in context:
            add("lawyer_phones", match.group(1))
        add("phones", match.group(1))
    for match in LANDLINE_PATTERN.finditer(text):
        add("phones", match.group(1))
    for match in EMAIL_PATTERN.finditer(text):
        add("emails", match.group(1))
    for match in IDENTITY_CONTEXT_PATTERN.finditer(text):
        add("identity_numbers", match.group(1))
    for match in IDENTITY_NUMBER_PATTERN.finditer(text):
        add("identity_numbers", match.group(1).upper())
    for match in IDENTITY_15_PATTERN.finditer(text):
        add("identity_numbers", match.group(1))
    for match in UNIFIED_SOCIAL_CREDIT_PATTERN.finditer(text):
        if is_valid_unified_social_credit_code(match.group(0)):
            add("unified_social_credit_codes", match.group(0))
    for match in BANK_ACCOUNT_PATTERN.finditer(text):
        add("bank_accounts", re.sub(r"[- ]", "", match.group(1)))

    return {
        "profile": profile,
        "candidate_terms": {category: sorted(values) for category, values in candidates.items()},
        "candidate_associations": associations,
        "auto_apply_redaction": False,
        "manual_lawyer_confirmation_required": True,
    }


@dataclass(frozen=True)
class XmlTextSlice:
    start: int
    end: int
    text: str


@dataclass
class _OpenTag:
    name: str
    content_start: int
    paragraph: list[XmlTextSlice] | None = None
    safe_text: bool = False


def _markup_end(data: bytes, start: int) -> int:
    if data.startswith(b"<!--", start):
        end = data.find(b"-->", start + 4)
        return len(data) if end < 0 else end + 3
    if data.startswith(b"<![CDATA[", start):
        end = data.find(b"]]>", start + 9)
        return len(data) if end < 0 else end + 3
    if data.startswith(b"<?", start):
        end = data.find(b"?>", start + 2)
        return len(data) if end < 0 else end + 2
    quote = 0
    index = start + 1
    while index < len(data):
        value = data[index]
        if quote:
            if value == quote:
                quote = 0
        elif value in (34, 39):
            quote = value
        elif value == 62:
            return index + 1
        index += 1
    return len(data)


def _tag_details(raw: bytes) -> tuple[str, str, bool]:
    stripped = raw[1:-1].strip()
    if not stripped or stripped.startswith((b"!", b"?")):
        return "special", "", False
    kind = "end" if stripped.startswith(b"/") else "start"
    if kind == "end":
        stripped = stripped[1:].lstrip()
    self_closing = kind == "start" and stripped.rstrip().endswith(b"/")
    name = re.split(rb"[\s/>]", stripped, maxsplit=1)[0]
    local_name = name.rsplit(b":", 1)[-1].decode("ascii", "ignore")
    return kind, local_name, self_closing


def _decode_text(raw: bytes) -> str:
    wrapper = b"<x>" + raw + b"</x>"
    return ElementTree.fromstring(wrapper).text or ""


def xml_escape_text(value: str) -> bytes:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").encode("utf-8")


def extract_safe_docx_text_slices(
    document_xml: bytes,
    excluded_ancestors: set[str],
    required_ancestor: str | None = "body",
) -> tuple[list[list[XmlTextSlice]], list[str]]:
    """Locate safe w:t content spans without serialising document.xml."""
    ElementTree.fromstring(document_xml)
    paragraphs: list[list[XmlTextSlice]] = []
    stack: list[_OpenTag] = []
    errors: set[str] = set()
    index = 0
    while index < len(document_xml):
        start = document_xml.find(b"<", index)
        if start < 0:
            break
        end = _markup_end(document_xml, start)
        raw = document_xml[start:end]
        kind, name, self_closing = _tag_details(raw)
        if kind == "start":
            ancestors = tuple(tag.name for tag in stack)
            paragraph: list[XmlTextSlice] | None = None
            required_present = required_ancestor is None or required_ancestor in ancestors
            if name == "p" and required_present and not excluded_ancestors.intersection(ancestors):
                paragraph = []
            active_paragraph = next((tag.paragraph for tag in reversed(stack) if tag.paragraph is not None), None)
            safe_text = bool(
                name == "t"
                and active_paragraph is not None
                and required_present
                and "p" in ancestors
                and "r" in ancestors
                and not excluded_ancestors.intersection(ancestors)
            )
            if not self_closing:
                stack.append(_OpenTag(name=name, content_start=end, paragraph=paragraph, safe_text=safe_text))
        elif kind == "end":
            if not stack or stack[-1].name != name:
                errors.add("docx_xml_token_structure_error")
                index = end
                continue
            opened = stack.pop()
            if opened.safe_text:
                try:
                    value = _decode_text(document_xml[opened.content_start:start])
                except ElementTree.ParseError:
                    errors.add("docx_text_node_decode_error")
                else:
                    active_paragraph = next(
                        (tag.paragraph for tag in reversed(stack) if tag.paragraph is not None),
                        None,
                    )
                    if active_paragraph is not None:
                        active_paragraph.append(XmlTextSlice(opened.content_start, start, value))
            if opened.paragraph is not None and opened.paragraph:
                paragraphs.append(opened.paragraph)
        index = end
    if stack:
        errors.add("docx_xml_token_structure_error")
    return paragraphs, sorted(errors)


@dataclass(frozen=True)
class DocxTextUnit:
    """A paragraph plus safe table coordinates; raw XML offsets stay in slices."""

    paragraph_index: int
    slices: tuple[XmlTextSlice, ...]
    table_index: int | None = None
    row_index: int | None = None
    cell_index: int | None = None
    part_scope: str | None = None
    table_id: str | None = None

    @property
    def logical_text(self) -> str:
        return "".join(item.text for item in self.slices)

    @property
    def text_slices(self) -> tuple[XmlTextSlice, ...]:
        return self.slices


def extract_safe_docx_text_units(
    document_xml: bytes,
    excluded_ancestors: set[str],
    required_ancestor: str | None = "body",
    *,
    part_scope: str | None = None,
    table_ids: list[str] | None = None,
) -> tuple[list[DocxTextUnit], dict[str, int], list[str]]:
    """Attach table row/cell coordinates to the existing byte-safe text slices."""
    paragraphs, errors = extract_safe_docx_text_slices(
        document_xml,
        excluded_ancestors,
        required_ancestor,
    )
    root = ElementTree.fromstring(document_xml)
    contexts: list[tuple[int | None, int | None, int | None]] = []
    next_table = 0
    row_next: dict[int, int] = {}
    cell_next: dict[tuple[int, int], int] = {}

    def safe_text_exists(element: ElementTree.Element, ancestors: tuple[str, ...]) -> bool:
        name = element.tag.rsplit("}", 1)[-1]
        current = ancestors + (name,)
        if (
            name == "t"
            and element.text
            and (required_ancestor is None or required_ancestor in ancestors)
            and "p" in ancestors
            and "r" in ancestors
            and not excluded_ancestors.intersection(ancestors)
        ):
            return True
        return any(safe_text_exists(child, current) for child in element)

    def walk(
        element: ElementTree.Element,
        ancestors: tuple[str, ...] = (),
        tables: tuple[int, ...] = (),
        rows: tuple[int, ...] = (),
        cells: tuple[int, ...] = (),
    ) -> None:
        nonlocal next_table
        name = element.tag.rsplit("}", 1)[-1]
        current = ancestors + (name,)
        current_tables = tables
        current_rows = rows
        current_cells = cells
        if name == "tbl":
            table_index = next_table
            next_table += 1
            row_next[table_index] = 0
            current_tables = tables + (table_index,)
        elif name == "tr" and tables:
            table_index = tables[-1]
            row_index = row_next.get(table_index, 0)
            row_next[table_index] = row_index + 1
            cell_next[(table_index, row_index)] = 0
            current_rows = rows + (row_index,)
        elif name == "tc" and tables and rows:
            key = (tables[-1], rows[-1])
            cell_index = cell_next.get(key, 0)
            cell_next[key] = cell_index + 1
            current_cells = cells + (cell_index,)

        required_present = required_ancestor is None or required_ancestor in ancestors
        if (
            name == "p"
            and required_present
            and not excluded_ancestors.intersection(ancestors)
            and safe_text_exists(element, ancestors)
        ):
            contexts.append(
                (
                    current_tables[-1] if current_tables else None,
                    current_rows[-1] if current_rows else None,
                    current_cells[-1] if current_cells else None,
                )
            )
        for child in element:
            walk(child, current, current_tables, current_rows, current_cells)

    walk(root)
    if len(contexts) != len(paragraphs):
        errors = sorted(set(errors) | {"docx_text_unit_context_mismatch"})
    units: list[DocxTextUnit] = []
    for index, slices in enumerate(paragraphs):
        table_index, row_index, cell_index = contexts[index] if index < len(contexts) else (None, None, None)
        units.append(
            DocxTextUnit(
                paragraph_index=index,
                slices=tuple(slices),
                table_index=table_index,
                row_index=row_index,
                cell_index=cell_index,
                part_scope=part_scope,
                table_id=(
                    table_ids[table_index]
                    if table_index is not None and table_ids and table_index < len(table_ids)
                    else None
                ),
            )
        )
    coverage = {
        "text_units": len(units),
        "text_nodes": sum(len(unit.slices) for unit in units),
        "tables": len({unit.table_index for unit in units if unit.table_index is not None}),
        "rows": len({(unit.table_index, unit.row_index) for unit in units if unit.table_index is not None}),
        "cells": len({(unit.table_index, unit.row_index, unit.cell_index) for unit in units if unit.table_index is not None}),
    }
    return units, coverage, errors


def apply_byte_patches(data: bytes, patches: list[tuple[int, int, bytes]]) -> bytes:
    output = data
    previous_start = len(data) + 1
    for start, end, replacement in sorted(patches, reverse=True):
        if not (0 <= start <= end <= len(data)) or end > previous_start:
            raise ValueError("overlapping_or_invalid_xml_patch")
        output = output[:start] + replacement + output[end:]
        previous_start = start
    return output


CORE_CLEAR_FIELDS = {
    "title",
    "subject",
    "keywords",
    "description",
    "category",
    "contentStatus",
}
CORE_AUTHOR_FIELDS = {"creator", "lastModifiedBy"}
APP_TEXT_FIELDS = {"Template", "Manager", "Company", "HyperlinkBase"}
DATE_FIELDS = {"created", "modified", "lastPrinted"}
CUSTOM_NUMERIC_TYPES = {"i1", "i2", "i4", "i8", "int", "ui1", "ui2", "ui4", "ui8", "uint", "r4", "r8", "decimal"}
CUSTOM_DATE_TYPES = {"date", "filetime"}


def _register_source_namespaces(payload: bytes) -> None:
    """Preserve namespace prefixes used by lexical QName attribute values."""
    for _event, (prefix, uri) in ElementTree.iterparse(io.BytesIO(payload), events=("start-ns",)):
        if prefix in {"xml", "xmlns"}:
            continue
        try:
            ElementTree.register_namespace(prefix, uri)
        except ValueError:
            # Reserved generated prefixes are never valid lexical QName dependencies.
            continue


def sanitise_docx_metadata_part(part_name: str, payload: bytes) -> tuple[bytes, int]:
    """Rewrite only a docProps part; never serialise word/document.xml."""
    if part_name not in {"docProps/core.xml", "docProps/app.xml", "docProps/custom.xml"}:
        return payload, 0
    _register_source_namespaces(payload)
    root = ElementTree.fromstring(payload)
    changed = 0
    if part_name == "docProps/custom.xml":
        property_index = 0
        for element in root.iter():
            local_name = element.tag.rsplit("}", 1)[-1]
            if local_name == "property":
                property_index += 1
                if element.attrib.get("name") != f"RedactedProperty{property_index}":
                    element.attrib["name"] = f"RedactedProperty{property_index}"
                    changed += 1
            elif element is not root and element.text:
                replacement = ""
                if local_name in CUSTOM_NUMERIC_TYPES:
                    replacement = "0"
                elif local_name == "bool":
                    replacement = "false"
                elif local_name in CUSTOM_DATE_TYPES:
                    replacement = "2000-01-01T00:00:00Z"
                elif local_name == "clsid":
                    replacement = "{00000000-0000-0000-0000-000000000000}"
                if element.text != replacement:
                    element.text = replacement
                    changed += 1
    else:
        text_fields = CORE_CLEAR_FIELDS if part_name == "docProps/core.xml" else APP_TEXT_FIELDS
        for element in root.iter():
            local_name = element.tag.rsplit("}", 1)[-1]
            if local_name in text_fields and (element.text or "") != "":
                element.text = ""
                changed += 1
            elif part_name == "docProps/core.xml" and local_name in CORE_AUTHOR_FIELDS:
                if element.text != "yangyc":
                    element.text = "yangyc"
                    changed += 1
            elif part_name == "docProps/core.xml" and local_name in DATE_FIELDS:
                replacement = "2000-01-01T00:00:00Z"
                if element.text != replacement:
                    element.text = replacement
                    changed += 1
            elif part_name == "docProps/core.xml" and local_name == "revision" and element.text != "1":
                element.text = "1"
                changed += 1
    if not changed:
        return payload, 0
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True), changed


def _declared_namespace_prefixes(payload: bytes) -> set[str]:
    prefixes: set[str] = set()
    for _event, (prefix, _uri) in ElementTree.iterparse(io.BytesIO(payload), events=("start-ns",)):
        prefixes.add(prefix)
    prefixes.add("xml")
    return prefixes


def _lexical_qname_errors(payload: bytes, root: ElementTree.Element) -> set[str]:
    """Validate namespace prefixes stored as text inside QName-valued attributes."""
    errors: set[str] = set()
    prefixes = _declared_namespace_prefixes(payload)
    xsi_type = "{http://www.w3.org/2001/XMLSchema-instance}type"
    mc_ignorable = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable"
    for element in root.iter():
        type_value = element.attrib.get(xsi_type, "").strip()
        if ":" in type_value and type_value.split(":", 1)[0] not in prefixes:
            errors.add("docx_unbound_xsi_type_prefix")
        for prefix in element.attrib.get(mc_ignorable, "").split():
            if prefix not in prefixes:
                errors.add("docx_unbound_mc_ignorable_prefix")
    return errors


def _relationship_target(rels_name: str, target: str) -> str | None:
    target = unquote(target.split("#", 1)[0]).replace("\\", "/")
    if not target:
        return None
    if target.startswith("/"):
        candidate = target.lstrip("/")
    else:
        rels_path = PurePosixPath(rels_name)
        if rels_name == "_rels/.rels":
            base = ""
        else:
            parts = rels_path.parts
            try:
                rel_index = parts.index("_rels")
            except ValueError:
                return None
            base = "/".join(parts[:rel_index])
        candidate = posixpath.join(base, target)
    normalised = posixpath.normpath(candidate)
    if normalised in {"", "."} or normalised == ".." or normalised.startswith("../"):
        return None
    return normalised.lstrip("/")


def validate_docx_ooxml_package(path: Path) -> tuple[bool, list[str]]:
    """Reject packages likely to trigger Office repair before success is reported."""
    errors: set[str] = set()
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            name_set = set(names)
            if len(name_set) != len(names):
                errors.add("docx_duplicate_package_part")
            if archive.testzip() is not None:
                errors.add("docx_zip_crc_error")
            for required in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
                if required not in name_set:
                    errors.add("docx_required_package_part_missing")
            for name in names:
                if not (name.endswith(".xml") or name.endswith(".rels")):
                    continue
                try:
                    payload = archive.read(name)
                    root = ElementTree.fromstring(payload)
                except (ElementTree.ParseError, KeyError):
                    errors.add("docx_ooxml_part_parse_error")
                    continue
                try:
                    errors.update(_lexical_qname_errors(payload, root))
                except (ElementTree.ParseError, ValueError):
                    errors.add("docx_namespace_declaration_parse_error")
                if name.endswith(".rels"):
                    for relationship in root.iter():
                        if relationship.tag.rsplit("}", 1)[-1] != "Relationship":
                            continue
                        if relationship.attrib.get("TargetMode", "").lower() == "external":
                            continue
                        resolved = _relationship_target(name, relationship.attrib.get("Target", ""))
                        if resolved is None or resolved not in name_set:
                            errors.add("docx_relationship_target_missing")
            if "[Content_Types].xml" in name_set:
                try:
                    content_types = ElementTree.fromstring(archive.read("[Content_Types].xml"))
                except ElementTree.ParseError:
                    errors.add("docx_content_types_invalid")
                else:
                    for element in content_types.iter():
                        if element.tag.rsplit("}", 1)[-1] != "Override":
                            continue
                        part_name = element.attrib.get("PartName", "").lstrip("/")
                        if part_name and part_name not in name_set:
                            errors.add("docx_content_type_part_missing")
    except (OSError, zipfile.BadZipFile):
        return False, ["redacted_docx_reopen_failed"]
    return not errors, sorted(errors)
