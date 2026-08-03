#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence")
    parser.add_argument("--run-rc", type=int, required=True)
    parser.add_argument("--migration-r1", type=int, required=True)
    parser.add_argument("--migration-r2", type=int, required=True)
    parser.add_argument("--containers", type=int, required=True)
    parser.add_argument("--networks", type=int, required=True)
    parser.add_argument("--temp-dirs", type=int, required=True)
    parser.add_argument("--secret-leaks", type=int, required=True)
    parser.add_argument("--delivery-digest", required=True)
    parser.add_argument("--delivery-files", type=int, required=True)
    args = parser.parse_args()
    path = pathlib.Path(args.evidence)
    data = json.loads(path.read_text(encoding="utf-8"))
    residue = {
        "containers": args.containers,
        "networks": args.networks,
        "temp_dirs": args.temp_dirs,
    }
    data["residue"] = residue
    data["secret_leaks"] = args.secret_leaks
    data["delivery"] = {
        "digest": args.delivery_digest,
        "files": args.delivery_files,
        "scope": "M4-F delivery surface (schema/runtime/controller/gateway/worker/tests-m4f1)",
    }
    data["runner"] = {
        "finalized_at": dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "run_rc": args.run_rc,
        "migration_round_1_rc": args.migration_r1,
        "migration_round_2_rc": args.migration_r2,
    }
    data["all_passed"] = bool(
        data.get("all_passed")
        and args.run_rc == 0
        and args.migration_r1 == 0
        and args.migration_r2 == 0
        and args.secret_leaks == 0
        and all(value == 0 for value in residue.values())
    )
    path.write_text(
        json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    return 0 if data["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
