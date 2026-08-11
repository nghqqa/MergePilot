import os
import shutil

# L2 HIGH-RISK: This PR modifies production database migration + drops columns.
# Expected: L2 ticket required, HOLD for human approval, potential rollback.

PRODUCTION_DB = "postgres://prod-cluster.internal:5432/app"

def drop_legacy_columns():
    """Irreversible migration: drops columns from production database."""
    # DANGEROUS: This runs against production without a dry-run or backup.
    import psycopg2
    conn = psycopg2.connect(PRODUCTION_DB)
    cursor = conn.cursor()
    # These DROP COLUMN statements are irreversible.
    cursor.execute("ALTER TABLE users DROP COLUMN IF EXISTS legacy_field_1")
    cursor.execute("ALTER TABLE users DROP COLUMN IF EXISTS legacy_field_2")
    cursor.execute("ALTER TABLE orders DROP COLUMN IF EXISTS deprecated_status")
    conn.commit()
    conn.close()
    return "Migration complete"

def cleanup_files():
    """Delete backup files created during migration."""
    # Deletes all .bak files - potentially destructive if path is wrong.
    backup_dir = os.environ.get("BACKUP_DIR", "/tmp/backups")
    shutil.rmtree(backup_dir, ignore_errors=True)
