# -*- coding: utf-8 -*-
"""RAG_CONTROLLED_PREFLIGHT — live fail-closed probes (read-only inspection;
no downloads, no registration, no shared resources)."""
import sys, os, tempfile, json
sys.path.insert(0, 'D:/goai/mp-worktrees/integration')
os.chdir('D:/goai/mp-worktrees/integration')

# P1: empty cache dir -> model not located (offline gate premise)
from skills.case_retrieval.embedding import offline_gate
with tempfile.TemporaryDirectory() as td:
    found = offline_gate.locate_model_dir('BAAI/bge-small-zh-v1.5', td)
    print('P1 empty-cache locate ->', found or 'None (model absent)')
    auth = offline_gate.download_authorized({})
    print('P1b download_authorized(empty env) =', auth, '(must be False)')

real = offline_gate.locate_model_dir('BAAI/bge-small-zh-v1.5', offline_gate.resolve_cache_dir())
print('P1c real default-cache locate ->', real or 'None (no approved cache deployed)')

# P1d/P1e: approved-manifest verification fail-closed paths
from skills.case_retrieval import approved_cache
with tempfile.TemporaryDirectory() as td:
    try:
        approved_cache.verify_approved_cache(os.path.join(td, 'missing.json'), td)
        print('P1d FAIL: missing manifest accepted')
    except Exception as e:
        print('P1d PASS: missing manifest ->', type(e).__name__)
    manifest = {"model": "m", "version": "1", "dimension": 512,
                "files": [{"path": "model.bin", "sha256": "0" * 64, "bytes": 10}]}
    mp = os.path.join(td, 'manifest.json')
    json.dump(manifest, open(mp, 'w'))
    open(os.path.join(td, 'model.bin'), 'wb').write(b'x' * 10)
    try:
        approved_cache.verify_approved_cache(mp, td)
        print('P1e FAIL: hash mismatch accepted')
    except Exception as e:
        print('P1e PASS: hash mismatch ->', type(e).__name__)

# P2: registration decisions
from skills.case_retrieval.metadata_registry import registration_decision, PgAuthoritativeMetadataSource
allow, code, detail = registration_decision('production', source=None)
print('P2 no-source -> allow=%s code=%s' % (allow, code))
import inspect
print('P2c PgAuthoritativeMetadataSource exists; init params:',
      list(inspect.signature(PgAuthoritativeMetadataSource.__init__).parameters))
# 隔离 PG 上 case_provider_metadata 表是否部署（mp-cc-pg 一次性隔离库）
import psycopg2
try:
    conn = psycopg2.connect('host=127.0.0.1 port=45434 user=mpcc password=mp-cc-staging-pw dbname=mpcc')
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='case_provider_metadata'")
    n = cur.fetchone()[0]
    conn.close()
    print('P2d case_provider_metadata table on isolated staging PG: present=%s (0=not deployed)' % n)
except Exception as e:
    print('P2d table check error:', type(e).__name__)
