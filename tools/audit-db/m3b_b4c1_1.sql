-- m3b_b4c1_1.sql — B4c.1.1 修正 migration(独立,幂等;不改冻结的 m3b_b4.sql/m3b_b4c.sql)。
--
-- 修复(B4c.1 复审 minor):
--   l2_reject_approved(NULL) 绕过 allowlist —— 旧 `IF p_reason_code NOT IN (...)` 对 NULL 得 NULL(不 RAISE),
--   随后 UPDATE 用 NULL reason 写 error。改为显式拒 NULL(IS NULL OR NOT IN → 22023)。
-- 签名不变(CREATE OR REPLACE),owner/REVOKE/GRANT 收敛(同 B4a 模板)。

CREATE OR REPLACE FUNCTION l2_reject_approved(p_ticket_id TEXT, p_reason_code TEXT)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  -- B4c.1.1:显式拒 NULL(旧 NOT IN 对 NULL 得 NULL,绕过 allowlist)
  IF p_reason_code IS NULL OR p_reason_code NOT IN ('CLAIM_MISMATCH','REPO_NOT_ALLOWED','L2_TICKET_REQUIRED','INVALID_ACTION') THEN
    RAISE EXCEPTION 'l2_reject_approved: reason_code 不在 allowlist(%)', p_reason_code
      USING ERRCODE = '22023';
  END IF;
  UPDATE public.approvals SET
    status='FAILED', error='preclaim denied:' || p_reason_code
  WHERE ticket_id = p_ticket_id
    AND status='APPROVED'
    AND execution_id IS NULL
    AND expires_at > now();
  RETURN FOUND;
END $$;

DO $$ BEGIN
  ALTER FUNCTION l2_reject_approved(text,text) OWNER TO mergepilot_l2_owner;
END $$;
ALTER ROLE mergepilot_l2_owner NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;
REVOKE ALL ON FUNCTION l2_reject_approved(TEXT,TEXT) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION l2_reject_approved(TEXT,TEXT) TO mergepilot;
