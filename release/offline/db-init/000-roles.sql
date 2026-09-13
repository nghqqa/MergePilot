-- offline delivery: pre-create roles referenced by migrations (idempotent)
DO $$ BEGIN
IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'envelope_maint') THEN
  CREATE ROLE envelope_maint NOLOGIN;
END IF;
END $$;
DO $$ BEGIN
IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'gate_owner') THEN
  CREATE ROLE gate_owner NOLOGIN;
END IF;
END $$;
DO $$ BEGIN
IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'github_check_publisher') THEN
  CREATE ROLE github_check_publisher NOLOGIN;
END IF;
END $$;
DO $$ BEGIN
IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'github_checks_publisher') THEN
  CREATE ROLE github_checks_publisher NOLOGIN;
END IF;
END $$;
DO $$ BEGIN
IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'github_event_ingress') THEN
  CREATE ROLE github_event_ingress NOLOGIN;
END IF;
END $$;
DO $$ BEGIN
IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'github_ingress_writer') THEN
  CREATE ROLE github_ingress_writer NOLOGIN;
END IF;
END $$;
DO $$ BEGIN
IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mergepilot') THEN
  CREATE ROLE mergepilot NOLOGIN;
END IF;
END $$;
DO $$ BEGIN
IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mergepilot_approver') THEN
  CREATE ROLE mergepilot_approver NOLOGIN;
END IF;
END $$;
DO $$ BEGIN
IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mergepilot_l2_owner') THEN
  CREATE ROLE mergepilot_l2_owner NOLOGIN;
END IF;
END $$;
DO $$ BEGIN
IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'purge_operator') THEN
  CREATE ROLE purge_operator NOLOGIN;
END IF;
END $$;
DO $$ BEGIN
IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'runtime_owner') THEN
  CREATE ROLE runtime_owner NOLOGIN;
END IF;
END $$;
DO $$ BEGIN
IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'skill_runner') THEN
  CREATE ROLE skill_runner NOLOGIN;
END IF;
END $$;
DO $$ BEGIN
IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'snapshot_worker') THEN
  CREATE ROLE snapshot_worker NOLOGIN;
END IF;
END $$;
DO $$ BEGIN
IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mergepilot_reader') THEN
  CREATE ROLE mergepilot_reader NOLOGIN;
END IF;
END $$;
