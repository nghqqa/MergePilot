"""Fresh install vs. upgrade must apply the same effective m9 contract (pre-merge review Q5/Q6).

Gated: runs only when DBVERIFY_PG_PORT / DBVERIFY_PG_PASSWORD_FILE point at a disposable
PostgreSQL whose superuser is `mergepilot` (the dbverify temp container). It creates two
throw-away databases and drops them again:

  fresh    = release/offline/db-init/001-init.sql applied to an empty database (the offline
             install path; 001-init embeds the whole audit-db chain including m9)
  upgrade  = the 12 pre-m9 chain files applied one by one, the legacy role script's grant on
             the OLD 5-parameter l2_claim_ticket, then tools/audit-db/m9_migration_verification.sql
             applied standalone (the upgrade path an existing deployment takes)

Both must end with byte-identical function definitions, owners, ACLs, tables, triggers and
the same runtime behaviour (single claim signature, defaulted 6th parameter, gateway role can
execute, approver role cannot). A skip is reported as a skip, never as a pass."""
import hashlib
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PORT = os.environ.get("DBVERIFY_PG_PORT")
PWFILE = os.environ.get("DBVERIFY_PG_PASSWORD_FILE")
HOST = os.environ.get("DBVERIFY_PG_HOST", "127.0.0.1")

CHAIN_PRE_M9 = [
    "init.sql", "m3_state.sql", "m3b_policy.sql", "m3b_b4.sql", "m3b_b4c.sql", "m3b_b4c1.sql",
    "m3b_b4c1_1.sql", "m3b_b4d1.sql", "m3c_state.sql", "m4f1_state.sql", "m4f1_hotfix_1.sql",
    "m8gh1_github_ingress.sql",
]
M9 = ROOT / "tools" / "audit-db" / "m9_migration_verification.sql"
OFFLINE_INIT = ROOT / "release" / "offline" / "db-init" / "001-init.sql"
ROLES_SCRIPT = ROOT / "tools" / "m3b-b4-create-roles.sh"

M9_FUNCTIONS = ("l2_claim_ticket", "db_release_gate", "l2_bind_verification", "mv_register_baseline",
                "mv_register_candidate", "mv_record_verification", "mv_run_status", "_mv_candidate_guard")
M9_TABLES = ("data_baselines", "migration_candidates", "migration_verifications", "approval_verification_bindings")


def _roles_script_claim_blocks():
    """The two DO blocks of tools/m3b-b4-create-roles.sh that touch l2_claim_ticket, unescaped."""
    text = ROLES_SCRIPT.read_text(encoding="utf-8")
    start = text.index("-- B4a.3 P1#A")
    end = text.index("GRANT EXECUTE ON FUNCTION l2_complete_ticket")
    return text[start:end].replace("\\$", "$")


@unittest.skipUnless(PORT and PWFILE, "DBVERIFY_PG_PORT / DBVERIFY_PG_PASSWORD_FILE not set")
class TestFreshVsUpgradeParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg2
        cls.psycopg2 = psycopg2
        cls.password = Path(PWFILE).read_text(encoding="utf-8").strip()
        cls.suffix = hashlib.sha256(os.urandom(8)).hexdigest()[:8]
        cls.fresh_db = "parity_fresh_" + cls.suffix
        cls.upgrade_db = "parity_upgrade_" + cls.suffix
        cls.created_roles = []
        try:
            cls._provision()
        except Exception:
            cls.tearDownClass()  # unittest skips tearDownClass when setUpClass raises
            raise

    @classmethod
    def _provision(cls):
        admin = cls._connect("mergepilot_audit")
        with admin.cursor() as cur:
            # Phase-0 prerequisite roles exactly as tools/cli/mergepilot.py::PREREQUISITE_ROLE_SQL
            # creates them before the chain runs (the pure docker-entrypoint offline init does not
            # create them; then m9's conditional GRANT is skipped and the legacy role script grants).
            for role in ("policy_gateway_l2", "mergepilot_approver"):
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
                if cur.fetchone() is None:
                    cur.execute("CREATE ROLE %s NOLOGIN" % role)
                    cls.created_roles.append(role)
            cur.execute('CREATE DATABASE "%s"' % cls.fresh_db)
            cur.execute('CREATE DATABASE "%s"' % cls.upgrade_db)
        admin.close()
        # fresh: the offline install file, as the docker entrypoint / CLI chain would run it
        conn = cls._connect(cls.fresh_db)
        with conn.cursor() as cur:
            cur.execute(OFFLINE_INIT.read_text(encoding="utf-8"))
            cur.execute(_roles_script_claim_blocks())  # maintenance re-run of the role script
        conn.close()
        # upgrade: pre-m9 chain, legacy grant on the OLD signature, then m9 standalone
        conn = cls._connect(cls.upgrade_db)
        with conn.cursor() as cur:
            for name in CHAIN_PRE_M9:
                cur.execute((ROOT / "tools" / "audit-db" / name).read_text(encoding="utf-8"))
            cur.execute("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname='public' AND p.proname='l2_claim_ticket' "
                        "AND pg_get_function_identity_arguments(p.oid)='p_ticket_id text, p_action text, p_repo text, p_pr_number integer, p_args_hash text'")
            assert cur.fetchone()[0] == 1, "pre-m9 chain must yield the 5-parameter claim function"
            # what the legacy role script did on a pre-m9 deployment (and must still be able to do)
            cur.execute(_roles_script_claim_blocks())
            cur.execute(M9.read_text(encoding="utf-8"))
            # the role script re-run after the upgrade (maintenance path) must succeed as well
            cur.execute(_roles_script_claim_blocks())
        conn.close()

    @classmethod
    def tearDownClass(cls):
        admin = cls._connect("mergepilot_audit")
        with admin.cursor() as cur:
            for db in (cls.fresh_db, cls.upgrade_db):
                cur.execute('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % db)
            for role in cls.created_roles:
                cur.execute("DROP ROLE IF EXISTS %s" % role)
        admin.close()

    @classmethod
    def _connect(cls, dbname):
        conn = cls.psycopg2.connect(host=HOST, port=int(PORT), user="mergepilot", password=cls.password,
                                    dbname=dbname, connect_timeout=5)
        conn.autocommit = True
        return conn

    # ── contract snapshot ────────────────────────────────────────────────────────

    def _snapshot(self, dbname):
        conn = self._connect(dbname)
        snap = {}
        with conn.cursor() as cur:
            cur.execute("""
                SELECT p.proname, pg_get_function_identity_arguments(p.oid), pg_get_function_result(p.oid),
                       r.rolname, p.prosecdef, p.proconfig,
                       md5(pg_get_functiondef(p.oid)),
                       (SELECT string_agg(g.rolname || ':' || a.privilege_type, ',' ORDER BY g.rolname, a.privilege_type)
                          FROM aclexplode(p.proacl) a JOIN pg_roles g ON g.oid = a.grantee)
                  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_roles r ON r.oid = p.proowner
                 WHERE n.nspname = 'public' AND p.proname = ANY(%s) ORDER BY 1, 2""", (list(M9_FUNCTIONS),))
            snap["functions"] = cur.fetchall()
            cur.execute("""
                SELECT c.relname, r.rolname,
                       (SELECT string_agg(g.rolname || ':' || a.privilege_type, ',' ORDER BY g.rolname, a.privilege_type)
                          FROM aclexplode(c.relacl) a JOIN pg_roles g ON g.oid = a.grantee),
                       (SELECT string_agg(a.attname || ' ' || format_type(a.atttypid, a.atttypmod) || ' ' || a.attnotnull
                                          || ' ' || coalesce(pg_get_expr(d.adbin, d.adrelid), ''), '|' ORDER BY a.attnum)
                          FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                         WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped),
                       (SELECT string_agg(conname || ' ' || pg_get_constraintdef(oid), '|' ORDER BY conname)
                          FROM pg_constraint WHERE conrelid = c.oid),
                       (SELECT string_agg(tgname || ' ' || pg_get_triggerdef(t.oid), '|' ORDER BY tgname)
                          FROM pg_trigger t WHERE t.tgrelid = c.oid AND NOT t.tgisinternal)
                  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_roles r ON r.oid = c.relowner
                 WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname = ANY(%s) ORDER BY 1""", (list(M9_TABLES),))
            snap["tables"] = cur.fetchall()
        conn.close()
        return snap

    def test_effective_contract_is_identical(self):
        fresh, upgrade = self._snapshot(self.fresh_db), self._snapshot(self.upgrade_db)
        self.assertEqual(len(fresh["tables"]), 4)
        self.assertEqual({f[0] for f in fresh["functions"]}, set(M9_FUNCTIONS))
        self.assertEqual(fresh["functions"], upgrade["functions"])
        self.assertEqual(fresh["tables"], upgrade["tables"])

    def test_single_claim_signature_and_no_dangling_overloads(self):
        for db in (self.fresh_db, self.upgrade_db):
            conn = self._connect(db)
            with conn.cursor() as cur:
                cur.execute("SELECT pg_get_function_identity_arguments(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                            "WHERE n.nspname='public' AND p.proname='l2_claim_ticket'")
                rows = [r[0] for r in cur.fetchall()]
                self.assertEqual(rows, ["p_ticket_id text, p_action text, p_repo text, p_pr_number integer, p_args_hash text, p_target_data_digest text"], db)
                cur.execute("SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                            "WHERE n.nspname='public' AND p.proname='db_release_gate'")
                self.assertEqual(cur.fetchone()[0], 1, db)
            conn.close()

    def test_gateway_role_can_claim_and_approver_cannot(self):
        for db in (self.fresh_db, self.upgrade_db):
            conn = self._connect(db)
            with conn.cursor() as cur:
                cur.execute("SET ROLE policy_gateway_l2")
                cur.execute("SELECT count(*) FROM public.l2_claim_ticket('no-such-ticket','merge','o/r',1,'h','%s')" % ("0" * 64))
                self.assertEqual(cur.fetchone()[0], 0, db)
                cur.execute("SELECT count(*) FROM public.l2_claim_ticket('no-such-ticket','merge','o/r',1,'h')")  # 5-arg callers still resolve
                self.assertEqual(cur.fetchone()[0], 0, db)
                cur.execute("RESET ROLE")
                cur.execute("SET ROLE mergepilot_approver")
                with self.assertRaises(self.psycopg2.errors.InsufficientPrivilege, msg=db):
                    cur.execute("SELECT count(*) FROM public.l2_claim_ticket('x','merge','o/r',1,'h')")
                cur.execute("RESET ROLE")
            conn.close()

    def test_roles_script_no_longer_hardcodes_a_single_claim_signature(self):
        text = ROLES_SCRIPT.read_text(encoding="utf-8")
        self.assertNotRegex(text, re.compile(r"^GRANT EXECUTE ON FUNCTION l2_claim_ticket\(", re.M))
        self.assertIn("to_regprocedure(f)", text)
