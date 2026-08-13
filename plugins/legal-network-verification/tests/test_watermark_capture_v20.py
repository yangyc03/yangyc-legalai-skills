from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw


SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "legal-network-verification"
SCRIPT_PATH = SKILL_ROOT / "scripts" / "watermark_capture.py"
SPEC = importlib.util.spec_from_file_location("watermark_capture", SCRIPT_PATH)
watermark_capture = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(watermark_capture)


class WatermarkCaptureTests(unittest.TestCase):
    @staticmethod
    def make_page_like_source(path: Path, *, mode: str = "RGB") -> None:
        image = Image.new(mode, (800, 480), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 799, 62), fill=(35, 71, 112))
        draw.rectangle((48, 100, 610, 148), fill=(244, 247, 250), outline=(116, 126, 138), width=2)
        draw.rectangle((628, 100, 748, 148), fill=(36, 106, 168))
        draw.rectangle((48, 190, 748, 390), fill=(252, 252, 252), outline=(178, 184, 190), width=2)
        for top, width in ((218, 520), (260, 640), (302, 410), (344, 575)):
            draw.rectangle((72, top, 72 + width, top + 9), fill=(69, 76, 84))
        image.save(path)

    def test_adds_beijing_timestamp_and_evidence_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_text:
            root = Path(temp_text)
            source = root / "raw.png"
            output = root / "watermarked.png"
            self.make_page_like_source(source)
            queried_at = watermark_capture.parse_timestamp("2026-07-16T06:30:00+00:00")
            watermark_capture.add_watermark(
                source,
                output,
                evidence_id="NQ-03-01-01",
                subject="王某丙",
                source="示例官方平台",
                queried_at=queried_at,
            )
            self.assertTrue(source.exists(), "helper must not mutate or delete the temporary source")
            with Image.open(source) as original, Image.open(output) as image:
                self.assertEqual(image.size, (800, 480))
                self.assertEqual(image.info["EvidenceId"], "NQ-03-01-01")
                self.assertEqual(image.info["Subject"], "王某丙")
                self.assertEqual(image.info["Source"], "示例官方平台")
                self.assertEqual(image.info["CaptureKind"], "watermarked_page_only")
                self.assertEqual(
                    image.info["SourceContentVerified"],
                    "heuristic_pre_watermark_gate_passed",
                )
                self.assertTrue(image.info["QueriedAt"].startswith("2026-07-16T14:30:00"))
                self.assertIsNotNone(ImageChops.difference(original.convert("RGB"), image.convert("RGB")).getbbox())

    def test_rejects_non_png_output_and_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temp_text:
            root = Path(temp_text)
            source = root / "raw.png"
            self.make_page_like_source(source)
            queried_at = watermark_capture.parse_timestamp("2026-07-16T14:30:00+08:00")
            with self.assertRaises(ValueError):
                watermark_capture.add_watermark(
                    source,
                    root / "bad.jpg",
                    evidence_id="NQ-03-01-01",
                    subject="王某丙",
                    source="示例官方平台",
                    queried_at=queried_at,
                )
            output = root / "watermarked.png"
            watermark_capture.add_watermark(
                source,
                output,
                evidence_id="NQ-03-01-01",
                subject="王某丙",
                source="示例官方平台",
                queried_at=queried_at,
            )
            with self.assertRaises(FileExistsError):
                watermark_capture.add_watermark(
                    source,
                    output,
                    evidence_id="NQ-03-01-01",
                    subject="王某丙",
                    source="示例官方平台",
                    queried_at=queried_at,
                )

    def test_requires_timezone_offset(self) -> None:
        with self.assertRaises(ValueError):
            watermark_capture.parse_timestamp("2026-07-16T14:30:00")

    def test_rejects_white_black_transparent_and_near_solid_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_text:
            root = Path(temp_text)
            cases = {
                "white": Image.new("RGB", (800, 480), "white"),
                "black": Image.new("RGB", (800, 480), "black"),
                "transparent": Image.new("RGBA", (800, 480), (117, 33, 201, 0)),
                "near-solid": Image.new("RGB", (800, 480), (248, 248, 248)),
            }
            near_solid = cases["near-solid"]
            near_solid_draw = ImageDraw.Draw(near_solid)
            near_solid_draw.rectangle((10, 10, 17, 17), fill=(220, 220, 220))
            queried_at = watermark_capture.parse_timestamp("2026-07-16T14:30:00+08:00")

            for name, image in cases.items():
                with self.subTest(name=name):
                    source = root / f"{name}.png"
                    image.save(source)
                    with self.assertRaises(ValueError):
                        watermark_capture.add_watermark(
                            source,
                            root / f"{name}-watermarked.png",
                            evidence_id="NQ-03-01-01",
                            subject="王某丙",
                            source="示例官方平台",
                            queried_at=queried_at,
                        )

    def test_rejects_too_small_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_text:
            root = Path(temp_text)
            source = root / "small.png"
            output = root / "watermarked.png"
            Image.new("RGB", (319, 179), "white").save(source)
            with self.assertRaisesRegex(ValueError, "too small"):
                watermark_capture.add_watermark(
                    source,
                    output,
                    evidence_id="NQ-03-01-01",
                    subject="王某丙",
                    source="示例官方平台",
                    queried_at=watermark_capture.parse_timestamp(
                        "2026-07-16T14:30:00+08:00"
                    ),
                )


if __name__ == "__main__":
    unittest.main()
