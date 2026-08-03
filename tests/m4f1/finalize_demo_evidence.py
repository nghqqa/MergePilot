#!/usr/bin/env python3
"""Add runner-owned cleanup evidence after every disposable container is gone."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence")
    parser.add_argument("--demo-rc", type=int, required=True)
    parser.add_argument("--containers", type=int, required=True)
    parser.add_argument("--networks", type=int, required=True)
    parser.add_argument("--temp-dirs", type=int, required=True)
    parser.add_argument("--migration-r1", type=int, required=True)
    parser.add_argument("--migration-r2", type=int, required=True)
    parser.add_argument("--revision-cut", type=int, required=True)
    parser.add_argument("--purge-race", type=int, required=True)
    args = parser.parse_args()

    path = Path(args.evidence)
    data = json.loads(path.read_text(encoding="utf-8"))
    residue = {
        "containers": args.containers,
        "networks": args.networks,
        "temp_dirs": args.temp_dirs,
    }
    runner = {
        "finalized_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "demo_rc": args.demo_rc,
        "migration_round_1_rc": args.migration_r1,
        "migration_round_2_rc": args.migration_r2,
        "revision_cut_rc": args.revision_cut,
        "complete_purge_race_rc": args.purge_race,
    }
    data["residue"] = residue
    data["runner"] = runner
    data["all_passed"] = bool(
        data.get("all_passed")
        and args.demo_rc == 0
        and args.migration_r1 == 0
        and args.migration_r2 == 0
        and args.revision_cut == 0
        and args.purge_race == 0
        and all(value == 0 for value in residue.values())
    )
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if data["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
