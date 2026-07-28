-- m3b_b4d1.sql — B4d.1 hardening:l2_approve 用 session_user 作 approved_by(强身份认证)。
--
-- 动机(B4d 复审 P1):原 l2_approve(p_ticket_id, p_approved_by) 的 approved_by 由调用方传入,
--   持 approver 密码者可绕过 CLI 直调 SELECT l2_approve('tkt','evil@forged') 冒名。
-- 修复:函数体改用 session_user(认证后的 DB 登录角色)写 approved_by,**忽略** p_approved_by。
--   ⇒ 冒名需受害者的 DB 密码;CLI 仅作便捷入口 + 严格参数校验。按人分配 DB 登录即可得到
--   逐人审批身份(每人一个 LOGIN 角色授予 EXECUTE l2_approve)。
--
-- **不改签名**(仍 l2_approve(text,text),仅给 p_approved_by 加 DEFAULT NULL 便于 1-arg 调用),
--   故 B4a frozen allowlist(m3b_b4.sql 里 l2_approve(text,text) 的 OWNER/REVOKE/GRANT 引用)仍有效。
-- 幂等;OWNER/REVOKE/GRANT 收敛(同 B4a 模板)。

CREATE OR REPLACE FUNCTION l2_approve(p_ticket_id TEXT, p_approved_by TEXT DEFAULT NULL)
RETURNS BOOLEAN LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_ttl INT;
BEGIN
  -- p_approved_by 参数**保留但忽略**(向后兼容 + 签名不变);approved_by 一律取 session_user。
  -- SECURITY DEFINER 下 current_user=owner, session_user=调用方登录角色 ⇒ 用 session_user。
  SELECT exec_ttl_hours INTO v_ttl FROM public.approvals WHERE ticket_id=p_ticket_id;
  UPDATE public.approvals SET
    status='APPROVED', approved_by=session_user, approved_at=now(),
    expires_at = now() + make_interval(hours => COALESCE(v_ttl,1))
  WHERE ticket_id=p_ticket_id AND status='PENDING' AND approval_expires_at > now();
  RETURN FOUND;
END $$;

-- OWNER 收敛(签名不变;REPLACE 保留原 owner,显式再收敛防漂移)
DO $$ BEGIN
  ALTER FUNCTION l2_approve(text,text) OWNER TO mergepilot_l2_owner;
END $$;
ALTER ROLE mergepilot_l2_owner NOLOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE NOREPLICATION;
REVOKE ALL ON FUNCTION l2_approve(TEXT,TEXT) FROM PUBLIC;
-- 生产:每名审批人建独立 LOGIN 角色并 GRANT EXECUTE(此处授予现有 approver + 任何已存在同名角色)
GRANT EXECUTE ON FUNCTION l2_approve(TEXT,TEXT) TO mergepilot_approver;
