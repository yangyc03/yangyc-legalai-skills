from __future__ import annotations

import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "legal-network-verification"


class CreditChinaEntryContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        cls.profile = (SKILL_ROOT / "references" / "site-profiles.md").read_text(
            encoding="utf-8"
        )
        cls.auth = (
            SKILL_ROOT / "references" / "authenticated-browser-workflow.md"
        ).read_text(encoding="utf-8")

    def test_creditchina_profile_uses_verified_official_contract(self) -> None:
        self.assertIn("https://www.creditchina.gov.cn/xinyongxinxi/index.html", self.profile)
        self.assertIn("credit_xyzx_tyshxydm", self.profile)
        self.assertIn("config.FEIP", self.profile)
        self.assertIn("2026-08-13", self.profile)

    def test_repair_overlay_is_not_treated_as_submission(self) -> None:
        self.assertIn("信用修复", self.profile)
        self.assertIn("/xyxf/xfzn/", self.profile)
        self.assertIn("submit_state=not_submitted", self.profile)
        self.assertIn("human_verification_required", self.profile)

    def test_occlusion_guard_is_fail_closed_and_archive_safe(self) -> None:
        self.assertIn("只读命中检查", self.auth)
        self.assertIn("执行一次", self.auth)
        self.assertIn("不得猜测路径、参数或批量尝试候选网址", self.auth)
        self.assertIn("去除查询参数和片段", self.auth)
        self.assertIn("v2.0.1-beta", self.skill)


if __name__ == "__main__":
    unittest.main()
