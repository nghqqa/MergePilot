#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PR-V3 — portfolio showcase material tests.

Covers the README / architecture.svg / demo-script / screenshot package:

  - fixed deliverable set exists at the canonical paths (12 screenshots,
    1 SVG, 1 demo script, README);
  - SVG parses as XML and carries the required components AND the four
    mandatory honesty annotations;
  - screenshots are real PNGs at the exact documented viewports
    (8 × desktop 1440×900, 4 × mobile 390×844) with stable file names;
  - every relative link / image reference in README resolves to a repo
    file; image references are repo-local (no external image URLs);
  - README carries the required sections, case facts, disclosures and
    truth boundaries; demo-script matches the same case/page names;
  - materials contain no secrets and no internal machine paths;
  - no screenshot or material is written into evidence/ or verification/.

Pure static checks — no Docker, no network, no browser.
"""

from __future__ import annotations

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
README = ROOT / "README.md"
SVG = DOCS / "architecture.svg"
SCRIPT = DOCS / "demo-script.md"

DESKTOP = tuple("desktop-%02d-%s.png" % (i, name) for i, name in enumerate((
    "overview", "timeline", "findings", "rag", "trace", "safety",
    "evidence", "benchmark"), start=1))
MOBILE = ("mobile-01-overview.png", "mobile-02-timeline.png",
          "mobile-03-safety.png", "mobile-04-evidence.png")
ALL_SHOTS = DESKTOP + MOBILE
PRESENT = DOCS / "presentation"
AT2X = tuple(n[:-4] + "@2x.png" for n in ALL_SHOTS)
# Hash-of-hashes of the 12 canonical PNG blob OIDs (git blob ids equal
# content hashes). Frozen by the polish round: media presentation work
# must never modify the canonical validation assets.
CANONICAL_HASH_OF_HASHES = ("702ea4f947009b26b08c093952f0fdd4106fa404")

README_TEXT = README.read_text(encoding="utf-8")
SCRIPT_TEXT = SCRIPT.read_text(encoding="utf-8")
SVG_TEXT = SVG.read_text(encoding="utf-8")

# ── 1: fixed deliverable set ────────────────────────────────────────────────

class TestDeliverableSet(unittest.TestCase):

    def test_readme_svg_script_exist(self):
        for path in (README, SVG, SCRIPT):
            self.assertTrue(path.is_file(), path)

    def test_twelve_screenshots_with_stable_names(self):
        actual = sorted(p.name for p in SHOTS.glob("*.png"))
        self.assertEqual(actual, sorted(ALL_SHOTS))
        self.assertEqual(len(actual), 12)
        self.assertEqual(len(DESKTOP), 8)
        self.assertGreaterEqual(len(MOBILE), 4)

    def test_no_stray_files_in_showcase_dir(self):
        allowed = set(ALL_SHOTS) | set(AT2X) | {
            "architecture.svg", "demo-script.md"}
        extra = [p.name for p in DOCS.rglob("*")
                 if p.is_file() and p.name not in allowed]
        self.assertEqual(extra, [])
        self.assertEqual(
            sorted(p.name for p in PRESENT.glob("*.png")), sorted(AT2X))

    def test_no_materials_inside_evidence_or_verification(self):
        for forbidden in (ROOT / "evidence", ROOT / "verification"):
            if forbidden.exists():
                leaked = [p for p in forbidden.rglob("*.png")
                          if p.name in ALL_SHOTS]
                self.assertEqual(leaked, [])
                leaked_svg = [p for p in forbidden.rglob("architecture.svg")]
                self.assertEqual(leaked_svg, [])


# ── 2: architecture SVG ─────────────────────────────────────────────────────

class TestArchitectureSvg(unittest.TestCase):

    def test_parses_as_xml(self):
        root = ET.fromstring(SVG_TEXT)
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
        # Real service/container names, not invented ones.
        for name in ("policy-gateway", "controller", "demo-console",
                     "console-edge", "postgres", "preflight"):
            self.assertIn(name, SVG_TEXT, name)

    def test_not_drawn_as_production_topology(self):
        self.assertIn("不是生产部署、云架构或多租户系统", SVG_TEXT)


# ── 3: screenshot dimensions ────────────────────────────────────────────────

def _png_size(path: Path):
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("not a PNG: %s" % path.name)
    return struct.unpack(">II", data[16:24])


class TestScreenshotDimensions(unittest.TestCase):

    def test_desktop_shots_are_1440x900(self):
        for name in DESKTOP:
            w, h = _png_size(SHOTS / name)
            self.assertEqual((w, h), (1440, 900), name)

    def test_mobile_shots_are_390x844(self):
        for name in MOBILE:
            w, h = _png_size(SHOTS / name)
            self.assertEqual((w, h), (390, 844), name)

    def test_no_text_metadata_chunks(self):
        # tEXt/iTXt/zTXt chunks could carry machine paths or secrets.
        for name in ALL_SHOTS:
            raw = (SHOTS / name).read_bytes()
            for marker in (b"tEXt", b"iTXt", b"zTXt"):
                self.assertNotIn(marker, raw[:8192], (name, marker))


# ── 3b: high-DPI presentation set ───────────────────────────────────────────

class TestPresentationSet(unittest.TestCase):
    """@2x presentation copies: real DPR=2 renders, distinct from the
    canonical verification screenshots, same page/case mapping."""

    def test_presentation_set_exact(self):
        self.assertEqual(
            sorted(p.name for p in PRESENT.glob("*.png")), sorted(AT2X))
        self.assertEqual(len(AT2X), 12)

    def test_presentation_dims(self):
        for name in AT2X:
            w, h = _png_size(PRESENT / name)
            if name.startswith("desktop"):
                self.assertEqual((w, h), (2880, 1800), name)
            else:
                self.assertEqual((w, h), (780, 1688), name)

    def test_presentation_metadata_free(self):
        for name in AT2X:
            raw = (PRESENT / name).read_bytes()
            for marker in (b"tEXt", b"iTXt", b"zTXt"):
                self.assertNotIn(marker, raw[:8192], (name, marker))

    def test_presentation_maps_canonical_pages(self):
        # stripping @2x yields the canonical name for every file
        for at2x in AT2X:
            canonical = at2x[:-7] + ".png"
            self.assertIn(canonical, ALL_SHOTS, at2x)

    def test_readme_displays_at2x_and_links_canonical(self):
        imgs = re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", README_TEXT)
        self.assertEqual(sorted(imgs),
                         sorted("docs/showcase/presentation/%s" % n
                                for n in AT2X))
        for name in ALL_SHOTS:
            self.assertIn(
                "(docs/showcase/screenshots/%s)" % name, README_TEXT, name)
        self.assertIn("高 DPI presentation 副本", README_TEXT)
        self.assertIn("真实验证资产", README_TEXT)

    def test_canonical_blob_hashes_frozen(self):
        import hashlib
        blob_ids = []
        for name in ALL_SHOTS:
            raw = (SHOTS / name).read_bytes()
            header = b"blob " + str(len(raw)).encode("ascii") + bytes([0])
            blob_ids.append(hashlib.sha1(header + raw).hexdigest())
        # Deterministic order: canonical file-name order (ALL_SHOTS), the
        # same sequence the historical git hash-object pipeline used.
        joined = ("\n".join(blob_ids) + "\n").encode("utf-8")
        self.assertEqual(
            hashlib.sha1(joined).hexdigest(), CANONICAL_HASH_OF_HASHES,
            "canonical screenshot binaries changed")


# ── 3c: README media unification (polish round) ────────────────────────────

class TestReadmeMediaUnification(unittest.TestCase):
    """Polish contract: every DISPLAYED png is an @2x presentation copy;
    canonical pngs appear only as click-targets of the @2x displays."""

    def test_no_canonical_png_is_displayed(self):
        for m in re.finditer(r"!\[[^\]]*\]\(([^)\s]+)\)", README_TEXT):
            self.assertFalse(
                m.group(1).startswith("docs/showcase/screenshots/"),
                "canonical png displayed directly: %s" % m.group(1))

    def test_all_displayed_pngs_are_at2x_presentation(self):
        png_imgs = [t for t in re.findall(
            r"!\[[^\]]*\]\(([^)\s]+)\)", README_TEXT)
            if t.endswith(".png")]
        self.assertEqual(len(png_imgs), 12)
        for t in png_imgs:
            self.assertIn("@2x.png", t, t)
            self.assertTrue(t.startswith("docs/showcase/presentation/"), t)

    def test_canonical_paths_only_as_click_targets(self):
        canonical_refs = re.findall(
            r"\]\((docs/showcase/screenshots/[^)\s]+)\)", README_TEXT)
        self.assertEqual(sorted(canonical_refs),
                         sorted("docs/showcase/screenshots/%s" % n
                                for n in ALL_SHOTS))
        # each is wrapped around an @2x display of the same stem
        for n in ALL_SHOTS:
            pattern = ("[![%s](docs/showcase/presentation/%s@2x.png)]"
                       "(docs/showcase/screenshots/%s)"
                       % (n[:-4], n[:-4], n))
            self.assertIn(pattern, README_TEXT, n)

    def test_architecture_svg_collapsed_not_expanded(self):
        # exactly one <details> block; svg appears as link + preview img only
        self.assertIn("<details>", README_TEXT)
        self.assertIn("<summary>", README_TEXT)
        self.assertIn('width="960"', README_TEXT)
        svg_refs = re.findall(r"docs/showcase/architecture\.svg", README_TEXT)
        self.assertEqual(len(svg_refs), 2, svg_refs)
        self.assertIn('<a href="docs/showcase/architecture.svg">', README_TEXT)
        self.assertIn('src="docs/showcase/architecture.svg"', README_TEXT)
        # no other markdown-expanded svg outside the details block
        self.assertNotIn("![MergePilot ISOLATED_LIVE 展示拓扑]", README_TEXT)

    def test_architecture_boundary_notes_retained(self):
        for note in ("不是第五个应用服务", "不是外部客户数据，不是生产证据",
                     "M8-A1 不等于 revision producer integration"):
            self.assertIn(note, README_TEXT, note)

    def test_at2x_presentation_set_unchanged(self):
        self.assertEqual(
            sorted(p.name for p in PRESENT.glob("*.png")), sorted(AT2X))


    def test_presentation_not_claimed_as_new_verification(self):
        # The note must frame @2x as presentation copies of the SAME
        # verification captures, not new results.
        self.assertIn("仅为清晰展示", README_TEXT)


# ── 4: README structure, links, disclosures ────────────────────────────────

class TestReadmeStructure(unittest.TestCase):

    def test_required_sections(self):
        for section in (
                "一句话定位", "解决什么问题", "架构", "三个确定性演示案例",
                "8 页面控制台", "Mobile 布局", "Quick Start",
                "测试与真实性边界"):
            self.assertIn(section, README_TEXT, section)

    def test_positioning_is_honest(self):
        self.assertIn("fail-closed", README_TEXT)
        self.assertIn("不是", README_TEXT)
        # Only unambiguous POSITIVE claims are banned; the README's own
        # negations ("不是已上线的 SaaS") must stay legal.
        for banned in ("生产部署完成", "已正式上线", "已上线运营",
                       "M8 已完成", "真实客户在用", "production ready"):
            self.assertNotIn(banned, README_TEXT)

    def test_all_relative_links_resolve(self):
        refs = re.findall(r"\]\(([^)#\s]+)\)", README_TEXT)
        self.assertGreater(len(refs), 10)
        for ref in refs:
            if ref.startswith(("http://", "https://", "mailto:")):
                continue
            target = (ROOT / ref).resolve()
            self.assertTrue(target.exists(), ref)

    def test_image_references_are_local(self):
        imgs = re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", README_TEXT)
        self.assertEqual(len(imgs), 12)
        for img in imgs:
            self.assertFalse(img.startswith(("http://", "https://")),
                             "external image URL: %s" % img)
            self.assertTrue((ROOT / img).exists(), img)
        # the architecture diagram ships as an HTML <img> inside <details>
        html_src = re.findall(r'src="([^"]+)"', README_TEXT)
        self.assertEqual(html_src, ["docs/showcase/architecture.svg"])
        self.assertTrue((ROOT / html_src[0]).exists())

    def test_external_links_are_https(self):
        for ref in re.findall(r"\]\((https?://[^)\s]+)\)", README_TEXT):
            self.assertTrue(ref.startswith("https://"), ref)

    def test_all_eight_pages_documented_with_shots(self):
        for page in ("overview", "timeline", "findings", "rag", "trace",
                     "safety", "evidence", "benchmark"):
            self.assertIn(page, README_TEXT)
        for name in DESKTOP:
            self.assertIn(name, README_TEXT)

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

    def test_disclosure_banner_present(self):
        self.assertIn(
            "Deterministic showcase seed — not external customer data — "
            "not production evidence", README_TEXT)

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


# ── 5: demo script ─────────────────────────────────────────────────────────

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
        # Script links are relative to the script's own directory.
        for ref in re.findall(r"\]\(([^)#\s]+)\)", SCRIPT_TEXT):
            if ref.startswith(("http://", "https://")):
                continue
            self.assertTrue((SCRIPT.parent / ref).exists(), ref)

    def test_script_forbidden_claims_only_in_prohibition_note(self):
        # The forbidden phrases appear ONLY inside the speaker-notes line
        # that explicitly prohibits saying them to the audience.
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


# ── 6: materials hygiene ────────────────────────────────────────────────────

class TestMaterialsHygiene(unittest.TestCase):

    def test_no_secrets_in_any_material(self):
        for path in (README, SVG, SCRIPT):
            self.assertEqual(scan_secrets(path.read_text(encoding="utf-8")),
                             0, path)

    def test_no_third_party_asset_claims(self):
        # Materials must be first-party; no references to other projects'
        # code/copy/visual assets as sources of the screenshots or SVG.
        for text in (README_TEXT, SCRIPT_TEXT):
            self.assertNotIn("SigmaMentor", text)

    def test_no_machine_paths_in_svg_or_script(self):
        for text in (SVG_TEXT, SCRIPT_TEXT):
            self.assertNotIn("C:\\Users", text)
            self.assertNotIn("/mnt/d/", text)
            self.assertNotIn("127.0.0.1:5432", text)  # internal DB never published


if __name__ == "__main__":
    unittest.main()
