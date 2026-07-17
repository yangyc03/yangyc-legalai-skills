from __future__ import annotations

import copy
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree
from PIL import Image, PngImagePlugin


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PLUGIN_ROOT / "skills" / "legal-network-verification"
MODULE_PATH = SKILL_ROOT / "scripts" / "network_workpaper.py"
SPEC = importlib.util.spec_from_file_location("network_workpaper", MODULE_PATH)
network_workpaper = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(network_workpaper)

WATERMARK_MODULE_PATH = SKILL_ROOT / "scripts" / "watermark_capture.py"
WATERMARK_SPEC = importlib.util.spec_from_file_location(
    "watermark_capture", WATERMARK_MODULE_PATH
)
watermark_capture = importlib.util.module_from_spec(WATERMARK_SPEC)
assert WATERMARK_SPEC.loader is not None
WATERMARK_SPEC.loader.exec_module(watermark_capture)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"w": W_NS, "r": R_NS, "pr": PKG_REL_NS}
BUNDLED_TEMPLATE = SKILL_ROOT / "assets" / "generic-network-query-record.docx"
SYNTHETIC_FULL_ID = "1101011990" + "03071234"


def make_template(path: Path) -> None:
    document = Document()
    document.add_heading("网络查询记录", level=1)
    document.add_paragraph("项目名称：【项目名称】")
    document.add_paragraph("查验事项：【查验事项】")
    document.add_paragraph("核查期间：【核查期间】")
    document.add_paragraph("查询时间：【查询时间】")
    document.add_paragraph("查询地点：【查询地点】")
    document.add_paragraph("查询人：【查询人】")
    table = document.add_table(rows=2, cols=3)
    table.cell(0, 0).text = "主体角色"
    table.cell(0, 1).text = "查询对象"
    table.cell(0, 2).text = "身份号码"
    table.cell(1, 0).text = "【SUBJECT_ROWS】"
    marker = document.add_paragraph("【QUERY_RESULT_ITEMS】")
    marker.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    document.add_paragraph("附件：查询网址截图底稿另行保存。")
    document.add_paragraph("查询律师签署：")
    footer = document.sections[0].footer.paragraphs[0]
    field_run = footer.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = " PAGE "
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    field_run._r.extend((begin, instruction, end))
    document.save(path)


def anonymous_data() -> dict:
    return {
        "schema_version": "1.0",
        "run": {
            "run_id": "NQ-20260717-TEST",
            "timezone": "Asia/Shanghai",
            "profile": "custom",
            "project": {
                "name": "匿名网络核查测试项目",
                "short_name": "匿名测试",
                "matter": "公开网络核查",
                "period": "2023年1月1日至查询日",
            },
            "formal_record": {
                "query_date": "2026年7月17日",
                "query_location": "北京市朝阳区",
                "query_people": ["测试律师"],
            },
        },
        "subjects": [
            {
                "subject_id": "SUB-001",
                "type": "natural_person",
                "name": "甲某",
                "role": "自然人核查对象",
                "associated_entity": "示例科技有限公司",
                "masked_id_number": "1101011990********",
            },
            {
                "subject_id": "SUB-002",
                "type": "company",
                "name": "示例科技有限公司",
                "role": "企业核查对象",
                "associated_entity": "",
                "credit_code": "91110000TEST000001",
            },
        ],
        "queries": [
            {
                "evidence_id": "NQ-01-01-01",
                "subject_id": "SUB-001",
                "site_id": "court",
                "site_name": "示例裁判文书公开网站",
                "url": "https://example.invalid/case/search?name=%E7%94%B2%E6%9F%90&from=2023-01-01",
                "query_time": "2026-07-17T09:10:11+08:00",
                "query_terms": "姓名及关联机构",
                "filters": "2023年1月1日以后公开记录",
                "status": "identity_match",
                "result_summary": "页面显示1条与姓名及关联机构相符的公开记录",
                "identity_assessment": "姓名和关联机构两项身份要素一致",
                "screenshot_path": "01-内部底稿/截图/NQ-01-01-01.png",
                "capture_kind": "watermarked_page_only",
                "follow_up": "复核记录详情",
            },
            {
                "evidence_id": "NQ-01-02-01",
                "subject_id": "SUB-001",
                "site_id": "enforcement",
                "site_name": "示例执行信息公开网站",
                "url": "https://example.invalid/enforcement/list/search/result/detail/path.with.long.segment?keyword=%E7%94%B2%E6%9F%90&page=1&size=20",
                "query_time": "2026-07-17T09:20:21+08:00",
                "query_terms": "姓名及本案材料所载身份要素",
                "filters": "全部公开栏目",
                "status": "no_match_displayed",
                "result_summary": "页面显示检索结果0条，未显示可确认匹配记录",
                "identity_assessment": "在本次查询条件下未显示可确认匹配的结果",
                "screenshot_path": "01-内部底稿/截图/NQ-01-02-01.png",
                "capture_kind": "watermarked_page_only",
                "follow_up": "",
            },
            {
                "evidence_id": "NQ-01-03-01",
                "subject_id": "SUB-001",
                "site_id": "same-name",
                "site_name": "示例公开检索网站",
                "url": "https://example.invalid/public/search?query=%E7%94%B2%E6%9F%90&category=all",
                "query_time": "2026-07-17T09:30:31+08:00",
                "query_terms": "姓名",
                "filters": "2023年1月1日以后",
                "status": "same_name_candidates",
                "result_summary": "页面显示3条姓名相同的候选记录",
                "identity_assessment": "公开页面信息不足以确认候选记录归属于核查对象",
                "screenshot_path": "01-内部底稿/截图/NQ-01-03-01.png",
                "capture_kind": "watermarked_page_only",
                "follow_up": "结合其他身份要素进一步核对",
            },
            {
                "evidence_id": "NQ-01-04-01",
                "subject_id": "SUB-001",
                "site_id": "company-only",
                "site_name": "示例企业信用网站",
                "url": "https://example.invalid/company/search?key=%E7%94%B2%E6%9F%90",
                "query_time": "2026-07-17T09:40:41+08:00",
                "query_terms": "姓名",
                "filters": "企业名称查询入口",
                "status": "not_applicable",
                "result_summary": "该入口仅接受企业名称或统一社会信用代码",
                "identity_assessment": "该入口不适用于普通自然人姓名核查",
                "screenshot_path": "01-内部底稿/截图/NQ-01-04-01.png",
                "capture_kind": "watermarked_page_only",
                "follow_up": "",
            },
            {
                "evidence_id": "NQ-02-01-01",
                "subject_id": "SUB-002",
                "site_id": "limited",
                "site_name": "示例监管查询网站",
                "url": "https://example.invalid/regulator/search?company=%E7%A4%BA%E4%BE%8B%E7%A7%91%E6%8A%80",
                "query_time": "2026-07-17T09:50:51+08:00",
                "query_terms": "企业名称",
                "filters": "公开查询入口",
                "status": "access_limited",
                "result_summary": "网站触发访问频率限制，未能完整加载查询结果",
                "identity_assessment": "本项查询未完整完成",
                "screenshot_path": "",
                "capture_kind": "",
                "follow_up": "恢复访问后补充查询",
            },
            {
                "evidence_id": "NQ-02-02-01",
                "subject_id": "SUB-002",
                "site_id": "failed",
                "site_name": "示例公告查询网站",
                "url": "https://example.invalid/notice/search?company=%E7%A4%BA%E4%BE%8B%E7%A7%91%E6%8A%80",
                "query_time": "2026-07-17T10:00:01+08:00",
                "query_terms": "企业名称",
                "filters": "公告全文",
                "status": "failed",
                "result_summary": "网站返回错误页面，本次查询未完成",
                "identity_assessment": "无法形成查询结果",
                "screenshot_path": "",
                "capture_kind": "",
                "follow_up": "另行补充查询",
            },
        ],
        "opinion_wording_requested": False,
    }


class NetworkWorkpaperV12Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.template = self.root / "template.docx"
        make_template(self.template)
        screenshot_dir = self.root / "01-内部底稿" / "截图"
        screenshot_dir.mkdir(parents=True)
        for evidence_id in ("NQ-01-01-01", "NQ-01-02-01", "NQ-01-03-01", "NQ-01-04-01"):
            Image.new("RGB", (80, 50), "white").save(
                screenshot_dir / f"{evidence_id}.png", format="PNG"
            )
        self.data = anonymous_data()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_two_layer_build_and_docx_contract(self) -> None:
        paths = network_workpaper.build_outputs(
            self.data,
            workpaper_root=self.root,
            output_dir=self.root,
            template_docx=self.template,
            formal_mode="draft",
            layout="two-layer",
            overwrite=False,
        )
        markdown_paths = sorted(path for path in paths if path.suffix == ".md")
        docx_path = next(path for path in paths if path.suffix == ".docx")
        self.assertTrue(all(path.parent.name == "01-内部底稿" for path in markdown_paths))
        self.assertEqual(docx_path.parent.name, "02-正式记录")
        self.assertTrue(docx_path.name.endswith("_草稿.docx"))

        person_markdown = next(path for path in markdown_paths if path.name.startswith("甲某"))
        markdown_text = person_markdown.read_text(encoding="utf-8")
        self.assertIn("(截图/NQ-01-01-01.png)", markdown_text)
        initial_conclusion = markdown_text.split("## 四、初步结论", 1)[1].split(
            "## 五、建议补核事项", 1
        )[0]
        self.assertNotIn("身份判断为", initial_conclusion)
        self.assertNotIn("结论级别为", initial_conclusion)
        self.assertNotIn("公开页面信息不足以确认候选记录归属于核查对象", initial_conclusion)

        with zipfile.ZipFile(docx_path) as archive:
            document_xml = etree.fromstring(archive.read("word/document.xml"))
            rels_xml = etree.fromstring(archive.read("word/_rels/document.xml.rels"))

        formal_text = "".join(document_xml.xpath(".//w:t/text()", namespaces=NS))
        for forbidden in ("身份判断", "结论级别", "NQ-", "复核人"):
            self.assertNotIn(forbidden, formal_text)
        self.assertIn("经查询示例执行信息公开网站", formal_text)
        self.assertIn("仅反映本次查询条件及该网站公开可查询范围", formal_text)

        result_paragraphs = document_xml.xpath(
            ".//w:p[.//w:t[contains(., '就') and contains(., '查询')]]",
            namespaces=NS,
        )
        self.assertEqual(len(result_paragraphs), len(self.data["queries"]))
        for paragraph in result_paragraphs:
            self.assertEqual(paragraph.xpath("string(./w:pPr/w:jc/@w:val)", namespaces=NS), "both")
            self.assertEqual(
                paragraph.xpath("string(./w:pPr/w:wordWrap/@w:val)", namespaces=NS),
                "1",
            )

        relationships = {
            item.get("Id"): item.get("Target")
            for item in rels_xml.xpath("./pr:Relationship", namespaces=NS)
            if item.get("Type", "").endswith("/hyperlink")
        }
        hyperlink_targets = []
        hyperlink_displays = []
        for hyperlink in document_xml.xpath(".//w:hyperlink", namespaces=NS):
            hyperlink_targets.append(relationships[hyperlink.get(f"{{{R_NS}}}id")])
            hyperlink_displays.append(
                "".join(hyperlink.xpath(".//w:t/text()", namespaces=NS))
            )
        expected_urls = [query["url"] for query in self.data["queries"]]
        self.assertCountEqual(hyperlink_targets, expected_urls)
        self.assertCountEqual(
            [display.replace(network_workpaper.ZERO_WIDTH_SPACE, "") for display in hyperlink_displays],
            expected_urls,
        )
        self.assertTrue(all(network_workpaper.ZERO_WIDTH_SPACE in item for item in hyperlink_displays))

    def test_flat_layout_remains_available(self) -> None:
        flat_root = self.root / "flat"
        flat_root.mkdir()
        data = copy.deepcopy(self.data)
        for query in data["queries"]:
            screenshot = query.get("screenshot_path")
            if screenshot:
                source = self.root / screenshot
                destination = flat_root / screenshot
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
        paths = network_workpaper.build_outputs(
            data,
            workpaper_root=flat_root,
            output_dir=flat_root,
            template_docx=self.template,
            formal_mode="draft",
            layout="flat",
            overwrite=False,
        )
        self.assertTrue(all(path.parent == flat_root for path in paths))
        self.assertTrue(any(path.name.endswith("_草稿.docx") for path in paths))

    def test_final_mode_requires_location_people_and_identifiers(self) -> None:
        cases = [
            ("query_location", ""),
            ("query_people", []),
        ]
        for field, value in cases:
            with self.subTest(field=field):
                data = copy.deepcopy(self.data)
                data["run"]["formal_record"][field] = value
                with self.assertRaises(network_workpaper.ValidationError):
                    network_workpaper.validate_run(
                        data,
                        workpaper_root=self.root,
                        formal_mode="final",
                    )

        data = copy.deepcopy(self.data)
        data["subjects"][0]["masked_id_number"] = ""
        with self.assertRaises(network_workpaper.ValidationError):
            network_workpaper.validate_run(data, workpaper_root=self.root, formal_mode="final")

    def test_restricted_and_failed_queries_cannot_claim_no_record(self) -> None:
        for status in ("access_limited", "failed"):
            with self.subTest(status=status):
                data = copy.deepcopy(self.data)
                query = next(item for item in data["queries"] if item["status"] == status)
                query["result_summary"] = "页面未发现记录"
                with self.assertRaises(network_workpaper.ValidationError):
                    network_workpaper.validate_run(
                        data,
                        workpaper_root=self.root,
                        formal_mode="draft",
                    )

    def test_masked_id_contract_and_sensitive_values(self) -> None:
        for invalid in (
            "110***********001X",
            "1101011990*******",
            "1101011990*********",
            SYNTHETIC_FULL_ID,
        ):
            with self.subTest(invalid=invalid):
                data = copy.deepcopy(self.data)
                data["subjects"][0]["masked_id_number"] = invalid
                with self.assertRaises(network_workpaper.ValidationError):
                    network_workpaper.validate_run(
                        data,
                        workpaper_root=self.root,
                        formal_mode="draft",
                    )

        for key in ("cookie", "token", "password", "authorization"):
            with self.subTest(key=key):
                data = copy.deepcopy(self.data)
                data[key] = "not-a-real-value"
                with self.assertRaises(network_workpaper.ValidationError):
                    network_workpaper.validate_run(
                        data,
                        workpaper_root=self.root,
                        formal_mode="draft",
                    )

    def test_sensitive_page_may_be_recorded_without_screenshot(self) -> None:
        data = copy.deepcopy(self.data)
        query = data["queries"][0]
        query["screenshot_path"] = ""
        query["capture_kind"] = "not_retained_sensitive_page"
        query["result_summary"] = "页面显示匹配结果，因敏感信息保护未留存该页面截图"
        network_workpaper.validate_run(
            data,
            workpaper_root=self.root,
            formal_mode="draft",
        )

        query["result_summary"] = "页面显示匹配结果"
        query["follow_up"] = ""
        with self.assertRaises(network_workpaper.ValidationError):
            network_workpaper.validate_run(
                data,
                workpaper_root=self.root,
                formal_mode="draft",
            )

    def test_template_priority_and_markdown_only_fallback(self) -> None:
        explicit_root = self.root / "explicit"
        paths = network_workpaper.build_outputs(
            self.data,
            workpaper_root=self.root,
            output_dir=explicit_root,
            template_docx=self.template,
            formal_mode="draft",
            layout="two-layer",
            overwrite=False,
        )
        docx_path = next(path for path in paths if path.suffix == ".docx")
        explicit_text = "\n".join(p.text for p in Document(docx_path).paragraphs)
        self.assertIn("查询律师签署", explicit_text)
        self.assertNotIn("查询人员签名", explicit_text)

        original_bundled = network_workpaper.BUNDLED_TEMPLATE
        network_workpaper.BUNDLED_TEMPLATE = self.root / "missing-template.docx"
        try:
            markdown_root = self.root / "markdown-only"
            markdown_paths = network_workpaper.build_outputs(
                self.data,
                workpaper_root=self.root,
                output_dir=markdown_root,
                template_docx=None,
                formal_mode="draft",
                layout="two-layer",
                overwrite=False,
            )
            self.assertTrue(markdown_paths)
            self.assertTrue(all(path.suffix == ".md" for path in markdown_paths))

            with self.assertRaises(FileNotFoundError):
                network_workpaper.build_outputs(
                    self.data,
                    workpaper_root=self.root,
                    output_dir=self.root / "missing-final-template",
                    template_docx=None,
                    formal_mode="final",
                    layout="two-layer",
                    overwrite=False,
                )
        finally:
            network_workpaper.BUNDLED_TEMPLATE = original_bundled

    def test_bundled_template_is_generic_and_structurally_valid(self) -> None:
        report = network_workpaper.template_check(BUNDLED_TEMPLATE)
        self.assertTrue(report["privacy_safe"])
        with zipfile.ZipFile(BUNDLED_TEMPLATE) as archive:
            package_text = "\n".join(
                archive.read(name).decode("utf-8", errors="ignore")
                for name in archive.namelist()
                if name.endswith((".xml", ".rels"))
            )
        for forbidden in (
            "云亭",
            "律所地址",
            SYNTHETIC_FULL_ID,
            "/Users/",
            "OneDrive",
        ):
            self.assertNotIn(forbidden, package_text)
        self.assertRegex(package_text, r"\bPAGE\b")
        self.assertIn("身份证号码（脱敏）", package_text)

        document = Document(BUNDLED_TEMPLATE)
        properties = document.core_properties
        self.assertFalse(properties.author)
        self.assertFalse(properties.last_modified_by)
        self.assertFalse(properties.subject)
        self.assertFalse(properties.keywords)
        self.assertFalse(properties.comments)

    def test_doctor_reports_capability_degradation(self) -> None:
        self.assertEqual(
            network_workpaper.doctor_report("available")["mode"],
            "full-workflow",
        )
        self.assertEqual(
            network_workpaper.doctor_report("unavailable")["mode"],
            "local-processing-only",
        )

    def test_artifact_audit_checks_text_docx_and_png_metadata(self) -> None:
        clean_root = self.root / "clean-audit"
        clean_root.mkdir()
        (clean_root / "record.md").write_text("匿名核查记录", encoding="utf-8")
        make_template(clean_root / "record.docx")
        Image.new("RGB", (20, 20), "white").save(clean_root / "record.png")
        self.assertTrue(network_workpaper.artifact_audit(clean_root)["ok"])

        bad_root = self.root / "bad-audit"
        bad_root.mkdir()
        (bad_root / "record.md").write_text(
            "身份证号" + SYNTHETIC_FULL_ID, encoding="utf-8"
        )
        bad_docx = Document()
        bad_docx.add_paragraph("password=example")
        bad_docx.save(bad_root / "record.docx")
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("Subject", SYNTHETIC_FULL_ID)
        Image.new("RGB", (20, 20), "white").save(
            bad_root / "record.png", pnginfo=metadata
        )
        report = network_workpaper.artifact_audit(bad_root)
        self.assertFalse(report["ok"])
        self.assertGreaterEqual(len(report["issues"]), 3)

    def test_watermark_preserves_timezone_and_rejects_sensitive_text(self) -> None:
        source = self.root / "source.png"
        output = self.root / "watermarked.png"
        Image.new("RGB", (640, 360), "white").save(source)
        queried_at = watermark_capture.parse_timestamp("2026-07-17T09:10:11+02:00")
        watermark_capture.add_watermark(
            source,
            output,
            evidence_id="NQ-01-01-01",
            subject="甲某",
            source="示例网站",
            queried_at=queried_at,
        )
        with Image.open(output) as image:
            self.assertEqual(image.info["QueriedAt"], "2026-07-17T09:10:11+02:00")
            self.assertEqual(image.info["CaptureKind"], "watermarked_page_only")

        with self.assertRaises(ValueError):
            watermark_capture.add_watermark(
                source,
                self.root / "bad.png",
                evidence_id="NQ-01-01-01",
                subject=SYNTHETIC_FULL_ID,
                source="示例网站",
                queried_at=queried_at,
            )
        with self.assertRaises(ValueError):
            watermark_capture.add_watermark(
                source,
                source,
                evidence_id="NQ-01-01-01",
                subject="甲某",
                source="示例网站",
                queried_at=queried_at,
                overwrite=True,
            )

    def test_public_package_has_one_core_and_thin_adapter(self) -> None:
        plugin_root = PLUGIN_ROOT
        manifest = json.loads(
            (plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["version"], "1.2.0-beta")
        self.assertEqual(manifest["license"], "Apache-2.0")
        self.assertTrue((plugin_root / "LICENSE").is_file())
        self.assertTrue((plugin_root / "PRIVACY.md").is_file())
        self.assertEqual(
            list((plugin_root / "skills").glob("*/SKILL.md")),
            [SKILL_ROOT / "SKILL.md"],
        )

        skill_text = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        for forbidden in (
            "/Users/",
            "OneDrive",
            ".workbuddy/",
            ".codex/",
            "node_repl",
            "computer-use",
        ):
            self.assertNotIn(forbidden, skill_text)


if __name__ == "__main__":
    unittest.main()
