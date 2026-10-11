#!/usr/bin/env python
"""build-qwenpaw-plugin.py — plugins/teamharness qwenpaw 适配 zip 打包。

Ruby 原实现（plugins/teamharness/adapters/qwenpaw/scripts/build-qwenpaw-plugin.rb）
的 Python 等价复刻：宿主无 ruby 时由 build-candidate.sh 调用。结构与原脚本
逐项一致（含 meta 块、qwenpaw-skills 摊平、__pycache__ 修剪），zip 内路径为
LF、ZIP_DEFLATED。

用法: build-qwenpaw-plugin.py --plugin-root <src>/plugins/teamharness --out-dir <src>/dist/adapters/qwenpaw
"""
import argparse
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path


def parse_plugin_yaml_version(plugin_root: Path) -> tuple[str, str]:
    text = (plugin_root / "plugin.yaml").read_text(encoding="utf-8")
    name = version = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("name:") and name is None:
            name = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("version:") and version is None:
            version = stripped.split(":", 1)[1].strip()
        if name and version:
            break
    if not name or not version:
        raise SystemExit("cannot parse name/version from plugin.yaml")
    return name, version


def prune_generated(root: Path) -> None:
    for item in list(root.rglob("*")):
        if item.name in {"__pycache__", ".DS_Store"} or item.name.endswith(".pyc"):
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)


def copy_entry(src_root: Path, target_root: Path, entry: str) -> None:
    src = src_root / entry
    if not src.exists():
        raise SystemExit(f"missing plugin source: {src}")
    dst = target_root / entry
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for child in src.iterdir():
            shutil.copytree(child, dst / child.name, dirs_exist_ok=True) \
                if child.is_dir() else shutil.copy2(child, dst / child.name)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plugin-root", required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()
    plugin_root = Path(args.plugin_root).resolve()
    out_dir = Path(args.out_dir).resolve()
    adapter_root = plugin_root / "adapters" / "qwenpaw"
    if not (plugin_root / "plugin.yaml").is_file():
        raise SystemExit(f"missing manifest: {plugin_root/'plugin.yaml'}")
    if not adapter_root.is_dir():
        raise SystemExit(f"missing adapter: {adapter_root}")

    name, version = parse_plugin_yaml_version(plugin_root)
    package_name = f"{name}-qwenpaw-{version}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_zip = out_dir / f"{package_name}.zip"
    stable_zip = out_dir / f"{name}-qwenpaw.zip"

    with tempfile.TemporaryDirectory(prefix=f"{name}-qwenpaw-") as tmp:
        staging = Path(tmp) / package_name
        # 与上游 ruby 脚本一致：asset 目录名 = 插件名（teamharness/workerflow 各自包目录）
        asset_dir = staging / name
        asset_dir.mkdir(parents=True)
        for entry in ("plugin.yaml", "prompts", "skills", "mcp"):
            copy_entry(plugin_root, asset_dir, entry)
        public_skills = asset_dir / "qwenpaw-skills"
        public_skills.mkdir()
        for group in ("agent", "team"):
            group_dir = plugin_root / "skills" / group
            if not group_dir.is_dir():
                continue
            for skill in group_dir.iterdir():
                target = public_skills / skill.name
                shutil.copytree(skill, target) if skill.is_dir() else shutil.copy2(skill, target)
        copy_entry(adapter_root, staging, "plugin.py")
        # teamharness 携带 task_trace.py；workerflow 适配无此文件，缺省跳过
        if (adapter_root / "task_trace.py").exists():
            copy_entry(adapter_root, staging, "task_trace.py")

        # 与上游 ruby 脚本一致：manifest 由脚本构造（adapter 目录未必携带 plugin.json）
        if name == "teamharness":
            manifest = {
                "id": "teamharness", "name": "TeamHarness", "version": version,
                "type": "general",
                "description": "AgentTeams TeamHarness runtime plugin for QwenPaw.",
                "author": "AgentTeams",
                "entry": {"backend": "plugin.py"},
                "dependencies": [],
                "min_version": "2.2.0",
                "qwenpaw_version": {"min": "2.2.0", "max": "2.3.0"},
                "meta": {
                    "category": "teamharness",
                    "features": [
                        "team-context",
                        "tool-output-sanitizer",
                        "teamharness-skills",
                        "teamharness-mcp",
                    ],
                },
            }
        else:
            manifest = {
                "id": name, "name": "WorkerFlow", "version": version,
                "type": "general",
                "description": "Worker-local workflow plugin for QwenPaw agents and subagents.",
                "author": "AgentTeams",
                "entry": {"backend": "plugin.py"},
                "dependencies": [],
                "min_version": "2.2.0",
                "qwenpaw_version": {"min": "2.2.0", "max": "2.3.0"},
                "meta": {
                    "category": "workerflow",
                    "features": [
                        "worker-internal-workflow",
                        "temporary-agent-lifecycle",
                        "workerflow-mcp",
                    ],
                },
            }
        (staging / "plugin.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

        prune_generated(staging)

        # ruby zip_dir 等价：以 staging 父目录为根、包目录为实体
        root = staging.parent
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for dirpath, _dirnames, filenames in os.walk(staging):
                for filename in filenames:
                    path = Path(dirpath) / filename
                    zf.write(path, path.relative_to(root).as_posix())
        shutil.copy2(out_zip, stable_zip)

    print(out_zip)


if __name__ == "__main__":
    main()
