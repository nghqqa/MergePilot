#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Showcase material tests — presentation-only media contract.

Media strategy (media-cleanup round): the twelve @2x presentation PNGs are
the ONLY screenshot assets; the former canonical 1x PNGs under
docs/showcase/screenshots/ were deleted. The suite pins:

  - docs/showcase/screenshots/ contains no PNGs (and stays absent);
  - docs/showcase/presentation/ holds exactly 12 PNGs: 8 desktop at
    2880x1800 and 4 mobile at 780x1688 (DPR=2 renders of 1440x900 /
    390x844 CSS viewports), frozen by hash;
  - README displays @2x images only, never references screenshots/ or
    "canonical" assets, and sizes them as CSS viewport + DPR=2 pixels;
  - the architecture SVG parses, keeps its components and the four
    honesty annotations, and its preflight-box / boundary-annotations
    regions do not overlap (layout fix);
  - the README embeds the diagram as a default-open <details> preview;
  - demo-script links resolve (now under presentation/);
  - no secrets, machine paths, evidence writes, 9th page, new API,
    verified fields, or M8 content anywhere in the materials.

Pure static checks — no Docker, no network, no browser.
"""

from __future__ import annotations

import hashlib
import os
import re
import struct
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

_HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = _HERE.parent.parent
for _p in (str(_HERE), str(ROOT), str(ROOT / "tools" / "demo_console")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from bundle_builder import scan_secrets  # noqa: E402

DOCS = ROOT / "docs" / "showcase"
SHOTS = DOCS / "screenshots"
PRESENT = DOCS / "presentation"
README = ROOT / "README.md"
SVG = DOCS / "architecture.svg"
SCRIPT = DOCS / "demo-script.md"

DESKTOP = tuple("desktop-%02d-%s.png" % (i, name) for i, name in enumerate((
    "overview", "timeline", "findings", "rag", "trace", "safety",
    "evidence", "benchmark"), start=1))
MOBILE = ("mobile-01-overview.png", "mobile-02-timeline.png",
          "mobile-03-safety.png", "mobile-04-evidence.png")
AT2X = tuple(n[:-4] + "@2x.png" for n in DESKTOP + MOBILE)

# Hash-of-hashes of the 12 presentation PNG blob ids in canonical
# file-name order — the same sequence the git hash-object pipeline uses.
# Frozen by the media-cleanup round: @2x assets must never be regenerated
# or modified by documentation work.
PRESENT_HASH_OF_HASHES = "cee16f1c544fd4fbedbb19b7090c5d6c068c41c2"

README_TEXT = README.read_text(encoding="utf-8")
SCRIPT_TEXT = SCRIPT.read_text(encoding="utf-8")
SVG_TEXT = SVG.read_text(encoding="utf-8")

_SVG_NS = {"s": "http://www.w3.org/2000/svg"}


def _png_size(path: Path):
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("not a PNG: %s" % path.name)
    return struct.unpack(">II", data[16:24])


# ── 1: deliverable set (presentation-only) ─────────────────────────────────

class TestDeliverableSet(unittest.TestCase):

    def test_readme_svg_script_exist(self):
        for path in (README, SVG, SCRIPT):
            self.assertTrue(path.is_file(), path)

    def test_no_png_under_screenshots_dir(self):
        if SHOTS.exists():
            self.assertEqual(list(SHOTS.glob("*.png")), [])
            self.assertEqual(list(SHOTS.iterdir()), [])

    def test_presentation_has_exactly_twelve_pngs(self):
        actual = sorted(p.name for p in PRESENT.glob("*.png"))
        self.assertEqual(actual, sorted(AT2X))
        self.assertEqual(len(AT2X), 12)
        self.assertEqual(len(DESKTOP), 8)
        self.assertEqual(len(MOBILE), 4)

    def test_no_stray_files_in_showcase_dir(self):
        allowed = set(AT2X) | {"architecture.svg", "demo-script.md"}
        extra = [p.name for p in DOCS.rglob("*")
                 if p.is_file() and p.name not in allowed]
        self.assertEqual(extra, [])

    def test_no_materials_inside_evidence_or_verification(self):
        for forbidden in (ROOT / "evidence", ROOT / "verification"):
            if forbidden.exists():
                leaked = [p for p in forbidden.rglob("*.png")
                          if p.name in AT2X]
                self.assertEqual(leaked, [])
                self.assertEqual(
                    [p for p in forbidden.rglob("architecture.svg")], [])


# ── 2: architecture SVG ─────────────────────────────────────────────────────

class TestArchitectureSvg(unittest.TestCase):

    def _root(self):
        return ET.fromstring(SVG_TEXT)

    def test_parses_as_xml(self):
        root = self._root()
        self.assertEqual(root.tag.split("}")[-1], "svg")
        self.assertGreater(len(list(root.iter())), 40)

    def test_required_components_present(self):
        for component in (
                "PR / 开发者入口", "Policy Gateway", "L2 approval",
                "Controller", "MCP orchestration", "PostgreSQL",
                "只读 snapshot", "console-edge", "8 页面",
                "Deterministic", "showcase seed", "internal backend network",
                "publication bridge", "audit / evidence 边界"):
            self.assertIn(component, SVG_TEXT, component)

    def test_four_mandatory_annotations(self):
        for note in (
                "不是第五个应用服务",
                "不是外部客户数据，不是生产证据",
                "不等于 revision producer integration",
                "production_verified=false"):
            self.assertIn(note, SVG_TEXT, note)

    def test_component_names_match_repository(self):
        for name in ("policy-gateway", "controller", "demo-console",
                     "console-edge", "postgres", "preflight"):
            self.assertIn(name, SVG_TEXT, name)

    def test_not_drawn_as_production_topology(self):
        self.assertIn("不是生产部署、云架构或多租户系统", SVG_TEXT)
        self.assertNotRegex(SVG_TEXT, r"云服务|AWS|Azure|多租户集群|Kubernetes")

    def _box(self, element_id):
        box = self._root().find('.//s:rect[@id="%s"]' % element_id, _SVG_NS)
        self.assertIsNotNone(box, element_id)
        return (float(box.get("x")), float(box.get("y")),
                float(box.get("width")), float(box.get("height")))

    def test_stable_ids_present(self):
        for element_id in ("preflight-box", "boundary-annotations"):
            self._box(element_id)

    def test_preflight_box_geometry(self):
        x, y, w, h = self._box("preflight-box")
        self.assertEqual((x, y, w, h), (640.0, 420.0, 270.0, 44.0))

    def test_preflight_does_not_overlap_postgresql_or_console(self):
        # PostgreSQL block: x=640 y=160 h=250 (ends y=410); Demo Console
        # starts at y=470. Preflight must sit strictly in the gap.
        _, py, _, ph = self._box("preflight-box")
        self.assertGreaterEqual(py, 160 + 250)
        self.assertLessEqual(py + ph, 470)

    def test_preflight_does_not_overlap_boundary_annotations(self):
        _, py, _, ph = self._box("preflight-box")
        _, by, _, bh = self._box("boundary-annotations")
        self.assertLessEqual(py + ph, by)
        self.assertGreaterEqual(by + bh, 820)

    def test_preflight_text_inside_box(self):
        # the two preflight text lines sit inside the 420..464 band
        ys = [int(m.group(1)) for m in re.finditer(
            r'<text x="775" y="(\d+)"[^>]*>(?:Preflight|10 门)', SVG_TEXT)]
        self.assertEqual(len(ys), 2, ys)
        for y in ys:
            self.assertGreaterEqual(y, 420)
            self.assertLessEqual(y, 464)


# ── 3: presentation screenshot assets ──────────────────────────────────────

class TestPresentationAssets(unittest.TestCase):

    def test_desktop_at2x_are_2880x1800(self):
        for n in (n for n in AT2X if n.startswith("desktop")):
            self.assertEqual(_png_size(PRESENT / n), (2880, 1800), n)

    def test_mobile_at2x_are_780x1688(self):
        for n in (n for n in AT2X if n.startswith("mobile")):
            self.assertEqual(_png_size(PRESENT / n), (780, 1688), n)

    def test_metadata_free(self):
        for n in AT2X:
            raw = (PRESENT / n).read_bytes()
            for marker in (b"tEXt", b"iTXt", b"zTXt"):
                self.assertNotIn(marker, raw[:8192], (n, marker))

    def test_blobs_frozen(self):
        blob_ids = []
        for n in AT2X:
            raw = (PRESENT / n).read_bytes()
            header = b"blob " + str(len(raw)).encode("ascii") + bytes([0])
            blob_ids.append(hashlib.sha1(header + raw).hexdigest())
        joined = ("\n".join(blob_ids) + "\n").encode("utf-8")
        self.assertEqual(hashlib.sha1(joined).hexdigest(),
                         PRESENT_HASH_OF_HASHES,
                         "presentation @2x assets changed")

    def test_page_mapping_stable(self):
        for n in AT2X:
            base = n[:-7]  # strip '@2x.png'
            self.assertTrue(base.startswith(("desktop", "mobile")), n)
            idx = int(base.split("-")[1])
            if base.startswith("desktop"):
                self.assertIn((idx, base.split("-", 2)[2]), list(enumerate(
                    ("overview", "timeline", "findings", "rag", "trace",
                     "safety", "evidence", "benchmark"), start=1)))
            else:
                self.assertIn(base, {m[:-4] for m in MOBILE}, n)


# ── 4: README media contract ───────────────────────────────────────────────

class TestReadmeMedia(unittest.TestCase):

    def test_all_displayed_images_are_at2x_presentation(self):
        imgs = re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", README_TEXT)
        self.assertEqual(len(imgs), 12)
        for img in imgs:
            self.assertTrue(img.startswith("docs/showcase/presentation/"), img)
            self.assertIn("@2x.png", img)
            self.assertTrue((ROOT / img).exists(), img)

    def test_no_screenshots_dir_references(self):
        self.assertNotIn("docs/showcase/screenshots/", README_TEXT)

    def test_no_canonical_wording(self):
        self.assertNotIn("canonical", README_TEXT.lower())

    def test_no_click_to_canonical_wording(self):
        self.assertNotIn("点击图片打开对应的", README_TEXT)
        self.assertNotIn("点击打开 canonical", README_TEXT)

    def test_viewport_and_dpr_sizing_wording(self):
        self.assertIn("CSS viewport 为 1440×900", README_TEXT)
        self.assertIn("CSS viewport 为 390×844", README_TEXT)
        self.assertIn("DPR=2", README_TEXT)
        self.assertIn("2880×1800 / 780×1688", README_TEXT)

    def test_dpr_disclosure_present(self):
        self.assertIn("deviceScaleFactor=2", README_TEXT)
        self.assertIn("deterministic showcase seed", README_TEXT.lower())
        self.assertIn("非外部客户数据", README_TEXT)
        self.assertIn("非生产证据", README_TEXT)

    def test_relative_links_resolve(self):
        refs = re.findall(r"\]\(([^)#\s]+)\)", README_TEXT)
        self.assertGreater(len(refs), 10)
        for ref in refs:
            if ref.startswith(("http://", "https://", "mailto:")):
                continue
            self.assertTrue((ROOT / ref).exists(), ref)

    def test_external_links_are_https(self):
        for ref in re.findall(r"\]\((https?://[^)\s]+)\)", README_TEXT):
            self.assertTrue(ref.startswith("https://"), ref)

    def test_architecture_diagram_default_open(self):
        self.assertIn("<details open>", README_TEXT)
        self.assertIn("<summary>", README_TEXT)
        self.assertIn('width="960"', README_TEXT)
        html_src = re.findall(r'src="([^"]+)"', README_TEXT)
        self.assertEqual(html_src, ["docs/showcase/architecture.svg"])
        self.assertIn('<a href="docs/showcase/architecture.svg">', README_TEXT)
        svg_refs = re.findall(r"docs/showcase/architecture\.svg", README_TEXT)
        self.assertEqual(len(svg_refs), 2, svg_refs)

    def test_architecture_boundary_notes_retained(self):
        for note in ("不是第五个应用服务", "不是外部客户数据，不是生产证据",
                     "M8-A1 不等于 revision producer integration",
                     "production_verified=false"):
            self.assertIn(note, README_TEXT, note)


# ── 5: README structure and truth boundaries ────────────────────────────────

class TestReadmeStructure(unittest.TestCase):

    def test_required_sections(self):
        for section in (
                "一句话定位", "解决什么问题", "架构", "三个确定性演示案例",
                "8 页面控制台", "Mobile 布局", "Quick Start",
                "测试与真实性边界"):
            self.assertIn(section, README_TEXT, section)

    def test_positioning_is_honest(self):
        self.assertIn("fail-closed", README_TEXT)
        for banned in ("生产部署完成", "已正式上线", "已上线运营",
                       "M8 已完成", "真实客户在用", "production ready"):
            self.assertNotIn(banned, README_TEXT)

    def test_all_eight_pages_documented_with_shots(self):
        for page in ("overview", "timeline", "findings", "rag", "trace",
                     "safety", "evidence", "benchmark"):
            self.assertIn(page, README_TEXT)
        for n in AT2X:
            self.assertIn(n, README_TEXT)

    def test_three_cases_with_stable_ids_and_shas(self):
        for run_id, case_id, pr in (
                ("run-showcase-a", "case-showcase-protected-merge-success",
                 "#101"),
                ("run-showcase-b", "case-showcase-failclosed-policy-rejection",
                 "#102"),
                ("run-showcase-c", "case-showcase-revision-drift-recovery",
                 "#103")):
            self.assertIn(run_id, README_TEXT)
            self.assertIn(case_id, README_TEXT)
            self.assertIn(pr, README_TEXT)
        for sha in ("73686f77636173652d632d686561640000000000",
                    "73686f77636173652d632d647269667400000000",
                    "73686f77636173652d632d7265636f7665726564"):
            self.assertIn(sha, README_TEXT)

    def test_case_outcomes_documented(self):
        for outcome in ("MERGED", "FAIL", "ROLLED_BACK", "RECOVERED",
                        "PROTECTED_PATH_PREFIX", "REVISION_DRIFT"):
            self.assertIn(outcome, README_TEXT)

    def test_truth_boundaries(self):
        for boundary in (
                "application_integration_verified=false",
                "database_verified=false",
                "production_verified=false",
                "revision_producer_contract=NOT_VERIFIED",
                "audit_producer_contract=NOT_VERIFIED",
                "M8-A2 未实现"):
            self.assertIn(boundary, README_TEXT, boundary)

    def test_regression_numbers(self):
        for number in ("1195 passed / 13 skipped / 0 failed", "12 → 12",
                       "60 passed", "PREFLIGHT_OK"):
            self.assertIn(number, README_TEXT)

    def test_mobile_section_residual_disclosure(self):
        self.assertIn("390×844", README_TEXT)
        self.assertIn("不声称完整 WCAG 合规", README_TEXT)
        self.assertIn("residual validation", README_TEXT)

    def test_no_real_credentials_or_machine_paths(self):
        self.assertEqual(scan_secrets(README_TEXT), 0)
        self.assertNotIn("D:\\", README_TEXT)
        self.assertNotIn("C:\\Users", README_TEXT)
        self.assertNotIn("/mnt/d/", README_TEXT)
        for placeholder in ("<postgres.env 路径>", "<controller.env 路径>",
                            "<demo_console.env 路径>",
                            "<postgres 容器桥接 IP>", "<postgres 容器>"):
            self.assertIn(placeholder, README_TEXT)

    def test_no_ninth_page_or_new_api_claims(self):
        self.assertNotIn("第 9 页", README_TEXT)
        self.assertNotIn("第 9 页面", README_TEXT)
        self.assertNotIn("新增 API", README_TEXT)


# ── 6: demo script ─────────────────────────────────────────────────────────

class TestDemoScript(unittest.TestCase):

    def test_segments_present(self):
        for stamp in ("0:00", "0:20", "1:20", "2:10", "3:10", "4:00"):
            self.assertIn(stamp, SCRIPT_TEXT)

    def test_covers_three_cases_and_boundary_close(self):
        for token in ("run-showcase-a", "run-showcase-b", "run-showcase-c",
                      "MERGED", "FAIL", "ROLLED_BACK", "RECOVERED",
                      "PROTECTED_PATH_PREFIX", "REVISION_DRIFT",
                      "PREFLIGHT_OK", "1195 passed / 13 skipped / 0 failed"):
            self.assertIn(token, SCRIPT_TEXT, token)

    def test_script_links_resolve(self):
        for ref in re.findall(r"\]\(([^)#\s]+)\)", SCRIPT_TEXT):
            if ref.startswith(("http://", "https://")):
                continue
            self.assertTrue((SCRIPT.parent / ref).exists(), ref)

    def test_script_references_presentation_not_screenshots(self):
        self.assertNotIn("](screenshots/)", SCRIPT_TEXT)
        self.assertIn("](presentation/)", SCRIPT_TEXT)

    def test_script_forbidden_claims_only_in_prohibition_note(self):
        note = "不得向观众声明"
        self.assertIn(note, SCRIPT_TEXT)
        for token in ("production ready", "生产已部署", "M8 完成",
                      "verified=true", "真实客户"):
            for m in re.finditer(re.escape(token), SCRIPT_TEXT):
                context = SCRIPT_TEXT[max(0, m.start() - 120):m.end() + 120]
                self.assertIn(note, context,
                              "'%s' outside the prohibition note" % token)

    def test_script_discloses_showcase_seed(self):
        self.assertIn("deterministic showcase seed", SCRIPT_TEXT.lower())
        self.assertIn("非生产证据", SCRIPT_TEXT)


# ── 7: materials hygiene ────────────────────────────────────────────────────

class TestMaterialsHygiene(unittest.TestCase):

    def test_no_secrets_in_any_material(self):
        for path in (README, SVG, SCRIPT):
            self.assertEqual(scan_secrets(path.read_text(encoding="utf-8")),
                             0, path)

    def test_no_third_party_asset_claims(self):
        for text in (README_TEXT, SCRIPT_TEXT):
            self.assertNotIn("SigmaMentor", text)

    def test_no_machine_paths_in_svg_or_script(self):
        for text in (SVG_TEXT, SCRIPT_TEXT):
            self.assertNotIn("C:\\Users", text)
            self.assertNotIn("/mnt/d/", text)
            self.assertNotIn("127.0.0.1:5432", text)

    def test_no_new_verified_fields_or_m8_content(self):
        for text in (README_TEXT, SVG_TEXT, SCRIPT_TEXT):
            for banned in ("application_integration_verified=true",
                           "database_verified=true",
                           "production_verified=true",
                           "M8 已完成", "M8-A2 已实现"):
                self.assertNotIn(banned, text)


if __name__ == "__main__":
    unittest.main()
