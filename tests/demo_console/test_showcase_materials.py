"""Contracts for the public README (finals edition), architecture, and media assets."""

from __future__ import annotations

import hashlib
import re
import struct
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
DOCS = ROOT / "docs" / "showcase"
SVG = DOCS / "architecture.svg"
SCRIPT = DOCS / "demo-script.md"
PRESENT = DOCS / "presentation"

DESKTOP = tuple("desktop-%02d-%s@2x.png" % item for item in (
    (1, "overview"), (2, "timeline"), (3, "findings"), (4, "rag"),
    (5, "trace"), (6, "safety"), (7, "evidence"), (8, "benchmark")))
MOBILE = (
    "mobile-01-overview@2x.png", "mobile-02-timeline@2x.png",
    "mobile-03-safety@2x.png", "mobile-04-evidence@2x.png")
AT2X = DESKTOP + MOBILE
PRESENTATION_HASH = "cee16f1c544fd4fbedbb19b7090c5d6c068c41c2"

README_TEXT = README.read_text(encoding="utf-8")
SVG_TEXT = SVG.read_text(encoding="utf-8")
SCRIPT_TEXT = SCRIPT.read_text(encoding="utf-8")
NS = {"s": "http://www.w3.org/2000/svg"}


def _png_size(path: Path) -> tuple[int, int]:
    raw = path.read_bytes()
    if raw[:8] != b"\x89PNG\r\n\x1a\n" or raw[12:16] != b"IHDR":
        raise AssertionError("not a PNG: %s" % path)
    return struct.unpack(">II", raw[16:24])


def _hash_of_hashes(paths: list[Path]) -> str:
    blob_ids = []
    for path in paths:
        raw = path.read_bytes()
        header = b"blob " + str(len(raw)).encode("ascii") + b"\0"
        blob_ids.append(hashlib.sha1(header + raw).hexdigest())
    return hashlib.sha1(("\n".join(blob_ids) + "\n").encode()).hexdigest()


def _rect(root: ET.Element, element_id: str) -> tuple[float, float, float, float]:
    element = root.find('.//s:rect[@id="%s"]' % element_id, NS)
    if element is None:
        group = root.find('.//s:g[@id="%s"]' % element_id, NS)
        element = None if group is None else group.find("s:rect", NS)
    if element is None:
        raise AssertionError("missing rect/group: %s" % element_id)
    return tuple(float(element.get(key)) for key in ("x", "y", "width", "height"))


def _overlap(a: tuple[float, float, float, float],
             b: tuple[float, float, float, float]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)


def _refs(text: str) -> set[str]:
    refs = set(re.findall(r"\]\(([^)#\s]+)", text))
    refs.update(re.findall(r'(?:src|href)="([^"]+)"', text))
    return refs


class TestDeliverableSet(unittest.TestCase):

    def test_core_files_exist(self):
        for path in (README, SVG, SCRIPT):
            self.assertTrue(path.is_file(), path)

    def test_presentation_directory_exists(self):
        self.assertTrue(PRESENT.is_dir())

    def test_exactly_twelve_pngs(self):
        self.assertEqual(sorted(p.name for p in PRESENT.glob("*.png")),
                         sorted(AT2X))

    def test_no_low_dpi_screenshot_directory(self):
        low = DOCS / "screenshots"
        self.assertFalse(low.exists() and any(low.glob("*.png")))

    def test_no_stray_showcase_files(self):
        allowed = set(AT2X) | {"architecture.svg", "demo-script.md"}
        actual = {p.name for p in DOCS.rglob("*") if p.is_file()}
        self.assertEqual(actual, allowed)

    def test_no_materials_in_evidence(self):
        for root in (ROOT / "evidence", ROOT / "verification"):
            if root.exists():
                self.assertEqual(list(root.rglob("*@2x.png")), [])

    def test_only_expected_repo_files_are_images(self):
        self.assertEqual(sorted(p.name for p in DOCS.rglob("*.png")),
                         sorted(AT2X))


class TestPngAssets(unittest.TestCase):

    def test_desktop_count(self):
        self.assertEqual(len(DESKTOP), 8)

    def test_mobile_count(self):
        self.assertEqual(len(MOBILE), 4)

    def test_desktop_dimensions(self):
        for name in DESKTOP:
            self.assertEqual(_png_size(PRESENT / name), (2880, 1800), name)

    def test_mobile_dimensions(self):
        for name in MOBILE:
            self.assertEqual(_png_size(PRESENT / name), (780, 1688), name)

    def test_png_magic(self):
        for name in AT2X:
            self.assertEqual((PRESENT / name).read_bytes()[:8],
                             b"\x89PNG\r\n\x1a\n")

    def test_no_text_metadata_chunks(self):
        for name in AT2X:
            raw = (PRESENT / name).read_bytes()
            for chunk in (b"tEXt", b"iTXt", b"zTXt"):
                self.assertNotIn(chunk, raw, "%s contains %r" % (name, chunk))

    def test_presentation_hash_frozen(self):
        paths = [PRESENT / name for name in AT2X]
        self.assertEqual(_hash_of_hashes(paths), PRESENTATION_HASH)

    def test_names_map_all_pages(self):
        joined = " ".join(AT2X)
        for page in ("overview", "timeline", "findings", "rag", "trace",
                     "safety", "evidence", "benchmark"):
            self.assertIn(page, joined)


class TestArchitectureSvg(unittest.TestCase):

    def _root(self):
        return ET.fromstring(SVG_TEXT)

    def test_parses_as_xml(self):
        self.assertEqual(self._root().tag.split("}")[-1], "svg")

    def test_viewbox_is_expanded(self):
        self.assertEqual(self._root().get("viewBox"), "0 0 1440 900")

    def test_accessible_title_and_description(self):
        self.assertIn('aria-labelledby="title desc"', SVG_TEXT)
        self.assertIsNotNone(self._root().find("s:title", NS))
        self.assertIsNotNone(self._root().find("s:desc", NS))

    def test_required_nodes_present(self):
        for node in ("pr-entry", "policy-gateway", "controller", "postgres-state",
                     "demo-console", "preflight", "showcase-seed", "console-edge",
                     "browser", "audit-boundary"):
            self.assertIsNotNone(self._root().find('.//s:g[@id="%s"]' % node, NS), node)

    def test_repository_component_names_present(self):
        for text in ("policy-gateway", "controller", "postgres", "demo-console",
                     "console-edge", "Preflight", "8 pages"):
            self.assertIn(text, SVG_TEXT)

    def test_truth_boundary_strings_present(self):
        for text in ("不是第五个应用服务", "不是外部客户数据", "不是生产证据",
                     "不等于 revision producer integration",
                     "application_integration_verified=false",
                     "database_verified=false", "production_verified=false"):
            self.assertIn(text, SVG_TEXT)

    def test_not_presented_as_production_topology(self):
        self.assertIn("不是生产部署、云架构或多租户系统", SVG_TEXT)
        self.assertNotRegex(SVG_TEXT, r"AWS|Azure|Kubernetes|多租户集群")

    def test_minimum_font_size_is_twelve(self):
        sizes = [int(x) for x in re.findall(r"font:(?:\d+\s+)?(\d+)px", SVG_TEXT)]
        self.assertTrue(sizes)
        self.assertGreaterEqual(min(sizes), 12)

    def test_no_rotated_text(self):
        self.assertNotIn("rotate(", SVG_TEXT)

    def test_primary_nodes_do_not_overlap(self):
        root = self._root()
        ids = ("pr-entry", "policy-gateway", "controller", "postgres-state",
               "demo-console", "preflight", "showcase-seed", "console-edge",
               "browser", "audit-boundary")
        boxes = {node: _rect(root, node) for node in ids}
        for i, left in enumerate(ids):
            for right in ids[i + 1:]:
                self.assertFalse(_overlap(boxes[left], boxes[right]),
                                 "%s overlaps %s" % (left, right))

    def test_preflight_has_dedicated_row(self):
        self.assertEqual(_rect(self._root(), "preflight-box"),
                         (300.0, 560.0, 660.0, 58.0))

    def test_truth_panel_is_separate(self):
        truth = _rect(self._root(), "boundary-annotations")
        self.assertEqual(truth, (40.0, 690.0, 1280.0, 180.0))
        self.assertFalse(_overlap(truth, _rect(self._root(), "preflight-box")))

    def test_internal_and_publication_boundaries_present(self):
        for element_id in ("internal-network", "publication-boundary",
                           "boundary-annotations"):
            _rect(self._root(), element_id)

    def test_no_external_fonts_or_images(self):
        body = SVG_TEXT.replace("http://www.w3.org/2000/svg", "")
        self.assertNotRegex(body, r"https?://|@font-face|<image\b")

    def test_svg_has_no_machine_paths(self):
        for value in ("C:\\Users", "/mnt/", "file://"):
            self.assertNotIn(value, SVG_TEXT)


class TestReadmeMedia(unittest.TestCase):

    def test_architecture_image_is_referenced_and_exists(self):
        match = re.search(r"!\[[^\]]*\]\((docs/assets/[^)]+)\)", README_TEXT)
        self.assertIsNotNone(match, "architecture image not referenced from README")
        self.assertTrue((ROOT / match.group(1)).exists(), match.group(1))

    def test_architecture_image_is_visible_by_default(self):
        for tag in ("<details", "<summary", "</details"):
            self.assertNotIn(tag, README_TEXT)
        self.assertIn("architecture-preview4.png", README_TEXT)

    def test_download_links_point_to_the_current_release(self):
        for asset in ("MergePilot-demo.zip", "MergePilot-demo.mp4"):
            self.assertIn("releases/download/v0.2.0/%s" % asset, README_TEXT)

    def test_no_stale_semifinal_release_links(self):
        self.assertNotIn("fudai-semifinal-demo-20260831", README_TEXT)

    def test_all_local_links_resolve(self):
        for ref in _refs(README_TEXT):
            if ref.startswith(("http://", "https://", "#")):
                continue
            self.assertTrue((ROOT / ref).exists(), ref)

    def test_no_low_dpi_or_canonical_references(self):
        self.assertNotIn("docs/showcase/screenshots/", README_TEXT)
        self.assertNotIn("canonical", README_TEXT.lower())

    def test_truth_boundary_badges_are_present(self):
        for badge in ("badge/Database_Branch-SIMULATED-yellow",
                      "badge/PolarDB-NOT_CONNECTED-red",
                      "badge/PR_Auto_Merge-DISABLED-red",
                      "badge/RAG-SYNTHETIC_DEMO-blue"):
            self.assertIn(badge, README_TEXT)

    def test_selftest_claim_is_stated(self):
        self.assertIn("54 项", README_TEXT)
        self.assertIn("actions/workflows/selftest.yml", README_TEXT)



class TestReadmeStructure(unittest.TestCase):

    def test_required_sections(self):
        for section in ("演示", "要解决的问题", "三条安全决策路径", "Agent 协同设计",
                        "AgentLoop 云端 Trace", "RAG 检索能力", "PolarDB 与 Branch 边界",
                        "快速开始", "仓库结构", "评估方法", "技术栈", "提交材料", "许可"):
            self.assertIn(section, README_TEXT)

    def test_title_uses_the_registered_project_name(self):
        self.assertTrue(README_TEXT.startswith("# MergePilot：多 Agent PR 审修闭环"))

    def test_positioning_is_honest(self):
        self.assertIn("fail-closed", README_TEXT)
        for banned in ("生产部署完成", "已正式上线", "真实客户在用",
                       "production ready", "M8 已完成"):
            self.assertNotIn(banned, README_TEXT)

    def test_three_safety_paths_are_documented(self):
        for value in ("自主完成", "批准后完成", "拒绝后停止", "PR #1", "PR #2",
                      "PR #3", "CWE-22", "CWE-78", "409 终态", "LOCKED"):
            self.assertIn(value, README_TEXT)

    def test_authoritative_trace_is_documented(self):
        for value in ("fbf4a3cec0493990d76e10a102418be1", "17.6", "51,890"):
            self.assertIn(value, README_TEXT)

    def test_truth_boundaries_are_frozen(self):
        for value in ("SYNTHETIC/REDACTED", "SIMULATED", "NOT CONNECTED", "DISABLED"):
            self.assertIn(value, README_TEXT)

    def test_agentteams_baseline_is_disclosed(self):
        self.assertIn("AgentTeams", README_TEXT)
        self.assertIn("CoPaw", README_TEXT)
        self.assertIn("HiClaw", README_TEXT)

    def test_test_count_matches_the_documented_command(self):
        self.assertIn("1490 tests", README_TEXT)  # documented command collects 1490 (verified 2026-09-14 on main and finals branch)



class TestDemoScript(unittest.TestCase):

    def test_script_links_resolve(self):
        for ref in _refs(SCRIPT_TEXT):
            if not ref.startswith(("http://", "https://", "#")):
                self.assertTrue((DOCS / ref).exists() or (ROOT / ref).exists(), ref)

    def test_script_discloses_showcase_seed(self):
        self.assertIn("deterministic showcase seed", SCRIPT_TEXT.lower())
        self.assertIn("非生产证据", SCRIPT_TEXT)


class TestMaterialsHygiene(unittest.TestCase):

    def test_no_credentials_or_machine_paths(self):
        combined = "\n".join((README_TEXT, SVG_TEXT, SCRIPT_TEXT))
        for pattern in (r"postgresql://[^<\s]+", r"ghp_[A-Za-z0-9]+",
                        r"sk-[A-Za-z0-9]+", r"C:\\Users", r"/mnt/[a-z]/"):
            self.assertIsNone(re.search(pattern, combined), pattern)

    def test_no_third_party_asset_claims_or_new_verified_state(self):
        combined = "\n".join((README_TEXT, SVG_TEXT, SCRIPT_TEXT))
        self.assertNotIn("SigmaMentor", combined)
        for value in ("application_integration_verified=true",
                      "database_verified=true", "production_verified=true"):
            self.assertNotIn(value, combined)


if __name__ == "__main__":
    unittest.main()
