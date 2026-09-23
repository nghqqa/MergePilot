# -*- coding: utf-8 -*-
"""model_gateway.smoke — 隔离切换验收(八点检查;零 GitHub 写入)。

用途:模型切换(如 deepseek-chat → deepseek-flash)前的隔离 smoke。
跑法(生产同路径:worker 容器内 → higress 网关 → 上游):
  docker exec elemiso-worker-reviewer python3 /path/smoke.py --model deepseek-flash
安全边界:
  * 只发最小非敏感提示词(固定 "Reply with the single word: ok");
  * 不携带任何仓库/PR/案例内容;不打印 key;
  * 客户端只与配置的 model base_url 通信,结构性不接触 GitHub。
429/5xx 不做在线触发(避免制造限频/故障),由单测覆盖分类逻辑。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

try:
    from .config import ModelCallConfig, load_model_config
    from .client import ModelGatewayClient, TIMEOUT_KIND
except ImportError:   # 直跑(scripts 路径):按平铺模块导入
    from config import ModelCallConfig, load_model_config
    from client import ModelGatewayClient, TIMEOUT_KIND

SMOKE_PROMPT = "Reply with the single word: ok"
ROLLBACK_NOTE = ("rollback = revert model config to the previously verified value "
                 "(see README switch procedure); one-line change, no code change")


def _check(name: str, status: str, detail: Any = None) -> Dict[str, Any]:
    return {"check": name, "status": status, "detail": detail}


def check_response_shape(resp: Dict[str, Any]) -> Dict[str, Any]:
    """点3:响应形状与 OpenAI-completions adapter 兼容性。"""
    problems = []
    if not isinstance(resp.get("content"), str):
        problems.append("choices[0].message.content missing/not-string")
    if resp.get("finish_reason") is None:
        problems.append("choices[0].finish_reason missing")
    usage = resp.get("usage") or {}
    for f in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if not isinstance(usage.get(f), int):
            problems.append("usage.%s missing/not-int" % f)
    if not isinstance(resp.get("model"), str) or not resp["model"]:
        problems.append("response model id missing")
    return {"compatible": not problems, "problems": problems,
            "usage": usage, "response_model": resp.get("model"),
            "raw_keys": resp.get("raw_keys")}


def run_smoke(cfg: ModelCallConfig, api_key: Optional[str],
              target_model: str, old_model: str,
              client: Optional[ModelGatewayClient] = None) -> Dict[str, Any]:
    """执行八点检查,返回 JSON 可序列化报告。不发任何 GitHub 请求。"""
    c = client or ModelGatewayClient(cfg.base_url, api_key)
    checks: List[Dict[str, Any]] = []

    # 点1: provider health / model-list 能力检查
    cat = c.list_models(timeout_s=min(10.0, cfg.timeout_s))
    target_in_catalog = bool(cat.get("ok")) and target_model in (cat.get("models") or [])
    checks.append(_check(
        "1.provider-model-list",
        "pass" if cat.get("ok") and target_in_catalog else
        ("warn" if cat.get("ok") else "fail"),
        {"http": cat.get("http"), "latency_ms": cat.get("latency_ms"),
         "models": cat.get("models"), "target_present": target_in_catalog,
         "kind": cat.get("kind"), "detail": cat.get("detail")}))

    # 点2: 最小非敏感请求
    chat = c.chat(target_model, [{"role": "user", "content": SMOKE_PROMPT}],
                  timeout_s=cfg.timeout_s, max_tokens=16, temperature=0.0)
    checks.append(_check(
        "2.minimal-request",
        "pass" if chat.get("ok") else "fail",
        {"http": chat.get("http"), "latency_ms": chat.get("latency_ms"),
         "content": (chat.get("content") or "")[:40] if chat.get("ok") else None,
         "kind": chat.get("kind"), "detail": (chat.get("detail") or "")[:200]}))

    # 点3: 响应格式兼容性(以点2的真实响应判)
    shape = check_response_shape(chat) if chat.get("ok") else {"compatible": False,
                                                               "problems": ["no successful response to check"]}
    checks.append(_check("3.response-shape-compat",
                         "pass" if shape["compatible"] else "fail", shape))

    # 点4: 错误分类(在线只触发 401 / permanent / timeout;429/5xx 留给单测)
    # 坏 key 探测器复用注入的传输函数(离线可测),仅换 key 值。
    bad_key = ModelGatewayClient(cfg.base_url, "sk-invalid-classification-probe",
                                 urlopen_fn=c.urlopen_fn)
    r401 = bad_key.chat(target_model, [{"role": "user", "content": SMOKE_PROMPT}],
                        timeout_s=cfg.timeout_s, max_tokens=8)
    checks.append(_check(
        "4a.error-classification-401",
        "pass" if (not r401.get("ok") and r401.get("kind") == "auth") else "fail",
        {"kind": r401.get("kind"), "http": r401.get("http")}))
    bogus = c.chat("nonexistent-model-zz", [{"role": "user", "content": SMOKE_PROMPT}],
                   timeout_s=cfg.timeout_s, max_tokens=8)
    checks.append(_check(
        "4b.error-classification-permanent",
        "pass" if (not bogus.get("ok") and bogus.get("kind") == "permanent") else "fail",
        {"kind": bogus.get("kind"), "http": bogus.get("http"),
         "detail": (bogus.get("detail") or "")[:160]}))
    tmo = c.chat(target_model, [{"role": "user", "content": SMOKE_PROMPT}],
                 timeout_s=0.001, max_tokens=8)
    checks.append(_check(
        "4c.error-classification-timeout",
        "pass" if (not tmo.get("ok") and tmo.get("kind") in (TIMEOUT_KIND, "transport")) else "fail",
        {"kind": tmo.get("kind"), "note": "local socket timeout; request-side effect unlikely (connect phase)"}))
    checks.append(_check(
        "4d.error-classification-429-5xx",
        "skipped",
        {"note": "not live-triggered by design (no artificial rate-limit/fault injection); "
                 "classifier covered by tests/model_gateway"}))

    # 点5: token/usage 计量字段(点2成功时已在点3验收)
    checks.append(_check(
        "5.usage-metering",
        "pass" if (chat.get("ok") and shape["compatible"]) else
        ("fail" if chat.get("ok") else "skipped"),
        {"usage": chat.get("usage"), "note": "usage fields verified in check 3"}))

    # 点6: audit 中的实际模型标识(本次 smoke 记录响应 model id;
    # 生产链的 gap:派发后 worker 响应侧模型 id 不回传审计——如实标注)
    resp_model = chat.get("model")
    checks.append(_check(
        "6.audit-model-identity",
        "pass" if resp_model == target_model else
        ("warn" if chat.get("ok") else "skipped"),
        {"requested": target_model, "response_model": resp_model,
         "note": "smoke records response-side model id; production run audit "
                 "records config-side identity at dispatch (manifest) — response-side "
                 "capture inside worker remains a known gap (R5 follow-up)"}))

    # 点7: 旧模型回滚探测(一次最小请求;结果如实记录,不猜测别名)
    #   old ok                         → pass(可一键回滚,已验证)
    #   old 失败 且 不在实时目录(已退役) → warn(回滚目标上游已不可用——单向切换风险)
    #   old 失败 且 仍在实时目录         → fail(回滚路径坏在配置/网关层,必须先修)
    old = c.chat(old_model, [{"role": "user", "content": SMOKE_PROMPT}],
                 timeout_s=cfg.timeout_s, max_tokens=16)
    old_in_catalog = bool(cat.get("ok")) and old_model in (cat.get("models") or [])
    if old.get("ok"):
        old_status, old_note = "pass", "rollback probe ok (one-line config revert)"
    elif not old_in_catalog:
        old_status = "warn"
        old_note = ("old model NOT in live catalog (retired upstream) and probe "
                    "failed — rollback target currently unavailable; switching "
                    "is effectively one-way until upstream restores it")
    else:
        old_status = "fail"
        old_note = "old model in catalog but probe failed — rollback path broken"
    checks.append(_check(
        "7.rollback-old-model", old_status,
        {"old_model": old_model, "probe": "ok" if old.get("ok") else old.get("kind"),
         "old_in_catalog": old_in_catalog,
         "http": old.get("http"), "kind": old.get("kind"),
         "detail": (old.get("detail") or "")[:200],
         "note": ROLLBACK_NOTE + "; " + old_note}))

    # 点8: 零 GitHub 写入(结构性:本客户端只连 model base_url;逐项声明)
    checks.append(_check(
        "8.no-github-writes",
        "pass",
        {"note": "client only speaks to base_url=%s; no GitHub endpoints, no tokens, "
                 "no check-run calls anywhere in this module" % cfg.base_url}))

    hard_fail = any(x["status"] == "fail" for x in checks)
    return {"smoke_version": 1, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "target_model": target_model, "old_model": old_model,
            "base_url": cfg.base_url, "config": cfg.as_manifest_dict(),
            "data_mode": "LIVE_ISOLATED_SMOKE",
            "verdict": "FAIL" if hard_fail else "PASS",
            "checks": checks}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="isolated model switch smoke (no GitHub)")
    ap.add_argument("--model", required=True, help="target model id (e.g. deepseek-flash)")
    ap.add_argument("--old-model", default=None,
                    help="previous verified model for rollback probe (default: config default)")
    ap.add_argument("--key-file", default=None,
                    help="file holding the gateway API key (value never printed); "
                         "default: env var named by MERGEPILOT_MODEL_API_KEY_ENV")
    ap.add_argument("--key-from-openclaw", metavar="ROLE", default=None,
                    help="read the plaintext gateway key from the worker's live config "
                         "(/root/.copaw-worker/<ROLE>/openclaw.json, in-container); "
                         "provider-json api_key is ENC-encrypted at rest and will 401")
    ap.add_argument("--out", default=None, help="write JSON report to this path")
    a = ap.parse_args(argv)

    cfg = load_model_config()
    key: Optional[str] = None
    if a.key_from_openclaw:
        oc_path = "/root/.copaw-worker/%s/openclaw.json" % a.key_from_openclaw
        with open(oc_path, encoding="utf-8") as f:
            oc = json.load(f)
        key = ((oc.get("models") or {}).get("providers") or {})
        key = (key.get("agentteams-gateway") or {}).get("apiKey")
        if not key:
            print("no agentteams-gateway apiKey in %s" % oc_path, file=sys.stderr)
            return 2
    elif a.key_file:
        with open(a.key_file, encoding="utf-8") as f:
            key = f.read().strip()
    else:
        key = os.environ.get(cfg.api_key_env)
    if not key:
        print("no API key available (set %s or pass --key-file); aborting" % cfg.api_key_env,
              file=sys.stderr)
        return 2
    report = run_smoke(cfg, key, a.model, a.old_model or cfg.model)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    print(text)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
