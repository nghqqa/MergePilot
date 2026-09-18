"""Offline image delivery contract tests.

Static, hermetic checks over the delivery artifacts that live in the repo
(release/images/*, docker-compose.yml, Dockerfile.*). They guarantee that a
bundle built by release/images/export-images.sh is:

  - ASCII-named (GitHub Release asset contract)
  - fully covered by its manifest and SHA256SUMS
  - a superset of every image docker-compose.yml references
  - free of secret-shaped strings and dev-machine absolute paths
  - safe by default: the compose stack never calls the real GitHub API and
    never performs write operations on its own

Docker is NOT required: these tests never touch the engine. Runtime checks
(digest/arch/count after docker load) live in release/images/load-images.*
and run on the adopter's machine.
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
IMAGES_DIR = ROOT / "release" / "images"
sys.path.insert(0, str(IMAGES_DIR))
import bundle_verify  # noqa: E402  (local module: shared delivery verifier)

COMPOSE = ROOT / "docker-compose.yml"
IMAGE_SET = IMAGES_DIR / "image-set.json"
TEMPLATE = IMAGES_DIR / "manifest.template.json"

COMPOSE_TEXT = COMPOSE.read_text(encoding="utf-8")
COMPOSE_DATA = yaml.safe_load(COMPOSE_TEXT)
PROJECT = COMPOSE_DATA["name"]
IMAGE_SET_DATA = json.loads(IMAGE_SET.read_text(encoding="utf-8"))
TEMPLATE_DATA = json.loads(TEMPLATE.read_text(encoding="utf-8"))

DOCKERFILES = sorted(
    str(p) for p in ROOT.glob("Dockerfile.*")
)
DELIVERY_TEXT_FILES = sorted(
    str(p) for p in IMAGES_DIR.iterdir()
    if p.suffix in (".sh", ".ps1", ".py", ".json", ".md")
    and p.name != "bundle_verify.py"  # 扫描器自身的规则字面量不参与扫描
)

BUILT_IMAGE_NAMES = [i["image"] for i in IMAGE_SET_DATA["images"]["built"]]
PINNED = IMAGE_SET_DATA["images"]["pinned_remote"][0]
TEMPLATE_IMAGE_REFS = {i["ref"] for i in TEMPLATE_DATA["images"]}


def _compose_services():
    return COMPOSE_DATA.get("services", {})


def _built_service_names():
    return sorted(s for s, cfg in _compose_services().items() if "build" in cfg)


# ---------------------------------------------------------------- asset names

class TestReleaseAssetNames:
    def test_bundle_and_archive_names_are_ascii(self):
        for value in (TEMPLATE_DATA["bundle"], TEMPLATE_DATA["archive"]):
            assert bundle_verify.ASSET_NAME_RE.match(value), value
            assert value == "MergePilot-images-linux-amd64" or value.startswith(
                "MergePilot-images-linux-amd64."), value

    def test_canonical_asset_names_are_ascii(self):
        for name in ("MergePilot-images-linux-amd64.tar.zst",
                     "MergePilot-images-manifest.json",
                     "SHA256SUMS"):
            assert bundle_verify.ASSET_NAME_RE.match(name), name


# ------------------------------------------------------- manifest contract

class TestManifestTemplate:
    def test_required_top_level_fields(self):
        for field in ("schema_version", "bundle", "archive", "platform",
                      "source_commit", "release_version", "created_at", "images"):
            assert field in TEMPLATE_DATA, field

    def test_every_image_has_required_fields(self):
        for img in TEMPLATE_DATA["images"]:
            for field in ("image", "tag", "ref", "digest", "architecture"):
                assert field in img, (img.get("ref"), field)
            assert img["digest"].startswith("sha256:")
            if "<" not in img["digest"]:  # 占位符（模板形态）跳过格式断言
                assert len(img["digest"]) == 71
            assert img["tag"] != "latest", "floating 'latest' tag is forbidden"

    def test_no_floating_latest_anywhere(self):
        assert "latest" not in COMPOSE_TEXT.lower()
        for name in BUILT_IMAGE_NAMES:
            assert not name.endswith(":latest")

    def test_digest_pinning_matches_compose(self):
        # f167762: compose runs the offline-loadable TAG; the registry digest is declared
        # to preflight (MERGEPILOT_DECLARED_PG_IMAGE) and gate-checked at start-up. The
        # image-set must agree with BOTH halves, so a re-pin never drifts silently.
        services = _compose_services()
        assert services["postgres"]["image"] == "%s:%s" % (PINNED["image"], PINNED["tag"]), \
            "compose tag and image-set tag must be the same pgvector image"
        declared = services["preflight"]["environment"]["MERGEPILOT_DECLARED_PG_IMAGE"]
        assert declared == PINNED["ref"], "declared digest and image-set must pin the same pgvector digest"
        assert PINNED["manifest_digest"] in declared

    def test_image_set_matches_manifest_template(self):
        built = {i["image"] for i in TEMPLATE_DATA["images"] if i["source"].startswith("built")}
        for i in IMAGE_SET_DATA["images"]["built"]:
            bare = i["image"].rsplit(":", 1)[0]
            assert bare in built, bare
        assert PINNED["ref"] in TEMPLATE_IMAGE_REFS


# --------------------------------------- compose images ⊆ manifest

class TestComposeImagesCoveredByManifest:
    def test_every_built_service_image_is_in_manifest(self):
        for svc in _built_service_names():
            expected = f"{PROJECT}-{svc}:local"
            assert expected in BUILT_IMAGE_NAMES, f"{expected} missing from image-set.json"

    def test_pinned_remote_image_is_in_manifest(self):
        assert PINNED["ref"] in TEMPLATE_IMAGE_REFS

    def test_compose_service_set_subset_of_image_set(self):
        compose_built = {f"{PROJECT}-{svc}:local" for svc in _built_service_names()}
        declared = {i["image"] for i in IMAGE_SET_DATA["images"]["built"]}
        assert compose_built <= declared, (compose_built - declared)
        extras = declared - compose_built
        assert extras == {"mergepilot-isolated-gh-proxy:local",
                          "mergepilot-isolated-mcp-bridge:local"}, extras


# ------------------------------------------------------- secrets / hygiene

class TestNoSecretsOrMachinePaths:
    def test_dockerfiles_have_no_secret_shaped_strings(self):
        hits = bundle_verify.scan_secrets(DOCKERFILES)
        assert hits == [], hits

    def test_compose_has_no_secret_literals(self):
        banned = re.compile(
            r"(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|LTAI[A-Za-z0-9]{12,}"
            r"|BEGIN [A-Z ]*PRIVATE KEY|AKIA[0-9A-Z]{16})")
        m = banned.search(COMPOSE_TEXT)
        assert m is None, m.group(0) if m else ""

    def test_compose_has_no_literal_env_passwords(self):
        for svc, cfg in _compose_services().items():
            env = (cfg.get("environment") or {})
            if isinstance(env, dict):
                for key in env:
                    assert "PASSWORD" not in key.upper() or env[key] in ("", None), (
                        svc, key)
            for f in (cfg.get("env_file") or []):
                assert ".env" not in str(f), f"{svc}: env_file must be orchestrator-created"

    def test_dockerfiles_never_copy_env_files(self):
        for df in DOCKERFILES:
            text = Path(df).read_text(encoding="utf-8")
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("COPY") or s.startswith("ADD"):
                    assert ".env" not in s, (df, s)
                    assert "secret" not in s.lower(), (df, s)

    def test_delivery_scripts_have_no_secret_or_machine_paths(self):
        secret_hits = bundle_verify.scan_secrets(DELIVERY_TEXT_FILES)
        path_hits = bundle_verify.scan_host_paths(DELIVERY_TEXT_FILES)
        assert secret_hits == [], secret_hits
        assert path_hits == [], path_hits

    def test_dockerfiles_have_no_machine_paths(self):
        for df in DOCKERFILES:
            text = Path(df).read_text(encoding="utf-8")
            for pat in (r"D:\\goai", r"C:\\Users\\", r"/d/goai", r"/mnt/[a-z]/goai"):
                assert re.search(pat, text) is None, (df, pat)


# --------------------------------------- default startup boundaries

class TestDefaultStartupBoundaries:
    def test_no_real_github_api_endpoint_in_compose(self):
        assert "api.github.com" not in COMPOSE_TEXT
        assert "https://github.com/" not in COMPOSE_TEXT

    def test_real_github_chain_is_not_in_the_default_stack(self):
        services = _compose_services()
        assert "gh-proxy" not in services, "real-chain service must stay optional"
        assert "mcp-bridge" not in services, "real-chain service must stay optional"

    def test_gateway_upstream_is_the_in_container_stub(self):
        upstream = _compose_services()["policy-gateway"]["environment"]["UPSTREAM_URL"]
        assert upstream == "http://127.0.0.1:8084/sse", "gateway must default to the in-network stub"

    def test_github_ingress_is_off_by_default(self):
        assert "GITHUB_INGRESS_ENABLED" not in COMPOSE_TEXT, \
            "ingress must only be enabled via the E2E 20-key config, never by compose defaults"

    def test_no_docker_socket_mounted(self):
        assert "/var/run/docker.sock" not in COMPOSE_TEXT

    def test_no_auto_merge_switch(self):
        assert "auto_merge" not in COMPOSE_TEXT.lower()
        assert "AUTOMERGE" not in COMPOSE_TEXT

    def test_port_publication_is_loopback_backed_and_minimal(self):
        published = {}
        for svc, cfg in _compose_services().items():
            ports = cfg.get("ports") or []
            if ports:
                published[svc] = list(ports)
        assert set(published) == {"gh-webhook", "console-edge"}, published
        for svc, plist in published.items():
            for p in plist:
                assert p in ("0.0.0.0:8090:8090", "0.0.0.0:8600:8600"), (svc, p)

    def test_console_runs_in_isolated_live_mode(self):
        env = _compose_services()["demo-console"]["environment"]
        assert env["MERGEPILOT_MODE"] == "isolated_live"

    def test_no_persistent_volumes_declared(self):
        assert not COMPOSE_DATA.get("volumes"), "the stack is one-shot; volumes are forbidden by contract"


# --------------------------------------- bundle verifier behaviour

class TestBundleVerifier:
    def _make_bundle(self, tmp_path: Path, *, tamper: bool = False, secret: bool = False):
        import hashlib
        manifest = {
            "schema_version": 1,
            "bundle": "MergePilot-images-linux-amd64",
            "archive": "MergePilot-images-linux-amd64.tar.zst",
            "platform": "linux/amd64",
            "source_commit": "a" * 40,
            "release_version": "v0.2.1",
            "created_at": "2026-09-12T00:00:00Z",
            "images": [{
                "image": "mergepilot-isolated-controller", "tag": "local",
                "ref": "mergepilot-isolated-controller:local",
                "digest": "sha256:" + "a" * 64,
                "architecture": "linux/amd64",
            }],
        }
        (tmp_path / "MergePilot-images-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8")
        payload = b"fake-archive-bytes"
        (tmp_path / "MergePilot-images-linux-amd64.tar.zst").write_bytes(payload)
        extra = tmp_path / "README-runtime-images.md"
        extra.write_text("# runtime images\n", encoding="utf-8")
        if secret:
            extra.write_text("# runtime images\n token=ghp_" + "a" * 35 + "\n", encoding="utf-8")
        digest = hashlib.sha256(
            (tmp_path / "MergePilot-images-linux-amd64.tar.zst").read_bytes()).hexdigest()
        if tamper:
            digest = hashlib.sha256(b"tampered").hexdigest()
        lines = [
            f"{digest}  MergePilot-images-linux-amd64.tar.zst",
            f"{hashlib.sha256((tmp_path / 'MergePilot-images-manifest.json').read_bytes()).hexdigest()}  MergePilot-images-manifest.json",
            f"{hashlib.sha256(extra.read_bytes()).hexdigest()}  README-runtime-images.md",
        ]
        (tmp_path / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_valid_bundle_passes(self, tmp_path):
        self._make_bundle(tmp_path)
        ok, problems = bundle_verify.verify_bundle(tmp_path)
        assert ok, problems

    def test_tampered_archive_fails(self, tmp_path):
        self._make_bundle(tmp_path, tamper=True)
        ok, problems = bundle_verify.verify_bundle(tmp_path)
        assert not ok
        assert any("hash mismatch" in p for p in problems)

    def test_secret_in_shipped_text_fails(self, tmp_path):
        self._make_bundle(tmp_path, secret=True)
        ok, problems = bundle_verify.verify_bundle(tmp_path)
        assert not ok
        assert any("secrets:" in p for p in problems)

    def test_missing_required_field_fails(self, tmp_path):
        manifest = {
            "schema_version": 1,
            "images": [],
        }
        (tmp_path / "MergePilot-images-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8")
        ok, problems = bundle_verify.verify_bundle(tmp_path, check_files=False)
        assert not ok
        assert any("source_commit" in p for p in problems)
