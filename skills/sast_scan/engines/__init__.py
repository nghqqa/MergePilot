"""SASTScan detection engines (framework-neutral, stdlib only).

Each engine exposes ``scan(path, content, rules) -> list[raw_finding]`` where a
raw_finding is a plain dict carrying ``evidence_text`` (the matched snippet).
``core`` is responsible for redacting evidence, computing digests, dedup and
deterministic ordering -- engines never emit a raw secret into output fields.
"""
