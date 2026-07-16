# Third-Party Notices

Legal Matter Archive is licensed under Apache-2.0. It can interoperate with the following separately distributed libraries and programs. They are not bundled in the release ZIP, and each remains subject to its own license.

| Component | Purpose | License | Project |
|---|---|---|---|
| pypdf | Core PDF reading and writing | BSD-3-Clause | https://github.com/py-pdf/pypdf |
| ReportLab | PDF page-number overlays | BSD-style license | https://www.reportlab.com/opensource/ |
| lxml | Optional DOCX XML processing | BSD-3-Clause | https://github.com/lxml/lxml |
| Pillow | Optional image input | HPND | https://python-pillow.github.io/ |
| LibreOffice | Optional DOCX and Office conversion | MPL-2.0, with applicable secondary license terms | https://www.libreoffice.org/ |
| Poppler (`pdftoppm`) | Optional automatic PDF rendering | GPL-2.0-or-later | https://poppler.freedesktop.org/ |
| qpdf | Optional PDF compatibility fallback | Apache-2.0 | https://github.com/qpdf/qpdf |
| Ghostscript | Optional printable-permission encrypted PDF conversion | AGPL-3.0-or-later or commercial license | https://www.ghostscript.com/ |
| Tesseract OCR | Optional local OCR | Apache-2.0 | https://github.com/tesseract-ocr/tesseract |

Users are responsible for installing only the optional components they choose and for complying with the licenses of the versions they install. `doctor` detects these programs; `setup --plan` only proposes changes, and `setup --apply` requires an explicit confirmation token.
