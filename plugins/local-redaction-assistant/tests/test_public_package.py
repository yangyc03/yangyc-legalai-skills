from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PLUGIN_ROOT / "skills" / "local-redaction-assistant"
MANIFEST = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"


class PublicPackageTests(unittest.TestCase):
    def test_manifest_and_required_files(self):
        data = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(data["name"], "local-redaction-assistant")
        self.assertEqual(data["version"], "1.1.1-beta")
        self.assertEqual(data["license"], "Apache-2.0")
        self.assertTrue((SKILL_ROOT / "SKILL.md").is_file())
        self.assertTrue((PLUGIN_ROOT / "README.md").is_file())
        self.assertTrue((PLUGIN_ROOT / "THIRD_PARTY_NOTICES.md").is_file())
        self.assertTrue((PLUGIN_ROOT / "requirements-pdf.txt").is_file())

    def test_no_private_path_or_local_config_names(self):
        forbidden = re.compile(
            r"(?:/" + "Users/|/" + "Volumes/|[A-Za-z]:\\\\|" +
            "OneDrive" + "-个人|skills" + "-backups)",
            re.IGNORECASE,
        )
        for path in PLUGIN_ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            self.assertIsNone(forbidden.search(text), str(path))

    def test_launchers_use_runtime_then_plugin_source(self):
        mac = (PLUGIN_ROOT / "tools" / "启动本地脱敏网页.command").read_text(encoding="utf-8")
        win = (PLUGIN_ROOT / "tools" / "启动本地脱敏网页.bat").read_text(encoding="utf-8")
        self.assertIn(".codex/skills/local-redaction-assistant", mac)
        self.assertIn("skills/local-redaction-assistant/scripts/web_app.py", mac)
        self.assertIn(".codex\\skills\\local-redaction-assistant", win)
        self.assertIn("skills\\local-redaction-assistant\\scripts\\web_app.py", win)

    def test_web_app_help_runs_without_pdf_dependency(self):
        script = SKILL_ROOT / "scripts" / "web_app.py"
        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--port", result.stdout)


if __name__ == "__main__":
    unittest.main()
