#!/usr/bin/env python3
"""CaseRetrieval fixture entry using the deterministic embedding provider.

The production PgVectorAdapter and the normal Skill CLI/envelope path remain
in use.  Only model inference is deterministic so the disposable E2E does not
need a model download or external network.
"""

from skills.case_retrieval import core
from skills.case_retrieval import run as skill_run
from skills.case_retrieval.embedding.fastembed_provider import DeterministicFakeProvider


_real_run = core.run


def _fixture_run(inp, *, adapter=None, embedding_provider=None, trusted_env=None, deadline=None):
    return _real_run(
        inp,
        adapter=adapter,
        embedding_provider=DeterministicFakeProvider(),
        trusted_env=trusted_env,
        deadline=deadline,
    )


def main() -> int:
    core.run = _fixture_run
    return skill_run.main()


if __name__ == "__main__":
    raise SystemExit(main())
