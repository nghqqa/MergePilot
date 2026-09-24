# -*- coding: utf-8 -*-
"""model_gateway 契约测试:配置默认值/校验、错误分类、smoke 离线驱动。

HTTP 全部离线注入(fake urlopen,按 Authorization/model/timeout 路由),
不触网、不触 GitHub。
"""
import importlib.util
import json
import os
import socket
import sys
import unittest
import urllib.error
from io import BytesIO

_HERE = os.path.dirname(os.path.abspath(__file__))
_MG = os.path.normpath(os.path.join(_HERE, "..", "..", "tools", "model_gateway"))


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_MG, file))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


cfg_mod = _load("mg_config", "config.py")
cli_mod = _load("mg_client", "client.py")
# smoke 的平铺回退导入按模块名 config/client 解析;测试内预注册(仓库无同名顶层模块)
sys.modules.setdefault("config", cfg_mod)
sys.modules.setdefault("client", cli_mod)
smoke_mod = _load("mg_smoke", "smoke.py")

BAD_KEY = "sk-invalid-classification-probe"
USAGE = {"prompt_tokens": 12, "completion_tokens": 2, "total_tokens": 14}


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(url, code, reason):
    return urllib.error.HTTPError(url, code, reason, {}, BytesIO(b'{"error":"%s"}' % reason.encode()))


def make_route(catalog, *, chat_models=("deepseek-flash", "deepseek-chat")):
    """离线路由:Authorization 决定 401;model 决定 400;timeout<=0.001 决定超时。"""
    def route(url, body, timeout=None):
        auth = (json.dumps({}) and None)
        if url.endswith("/models"):
            return (200, {"data": [{"id": m} for m in catalog]})
        model = (body or {}).get("model", "")
        if model == "nonexistent-model-zz":
            raise _http_error(url, 400, "model_not_found")
        if model not in chat_models:
            raise _http_error(url, 404, "model_not_found")
        if timeout is not None and timeout <= 0.001:
            raise socket.timeout("timed out")
        return (200, {"model": model,
                      "choices": [{"message": {"content": "ok"},
                                   "finish_reason": "stop"}],
                      "usage": USAGE,
                      "id": "cmpl-x", "created": 1, "object": "chat.completion"})
    return route


def make_client(route, key="sk-good"):
    def urlopen(req, timeout=None):
        auth = req.headers.get("Authorization", "")
        if auth == "Bearer " + BAD_KEY:
            raise _http_error(req.full_url, 401, "Unauthorized")
        status, payload = route(req.full_url,
                                json.loads(req.data.decode()) if req.data else None,
                                timeout)
        if isinstance(payload, Exception):
            raise payload
        return _FakeResp(payload, status)
    return cli_mod.ModelGatewayClient("http://gw:8080/v1", key, urlopen_fn=urlopen)


class TestConfigDefaults(unittest.TestCase):
    def test_defaults_pin_current_verified_model(self):
        c = cfg_mod.load_model_config({})
        self.assertEqual(c.model, "deepseek-chat")   # 代码默认 = 已验证模型
        self.assertEqual(c.base_url, "http://elemiso-controller:8080/v1")
        self.assertEqual(c.api_key_env, "AGENTTEAMS_GATEWAY_API_KEY")
        self.assertEqual(c.max_attempts, 3)
        self.assertIsNone(c.temperature)   # 与现链一致:默认不发送
        self.assertIsNone(c.max_tokens)

    def test_env_overrides(self):
        c = cfg_mod.load_model_config({
            cfg_mod.MODEL_ENV: "deepseek-flash",
            cfg_mod.BASE_URL_ENV: "http://gw:8080/v1",
            cfg_mod.API_KEY_ENV_NAME_ENV: "MY_KEY_VAR",
            cfg_mod.TIMEOUT_ENV: "30",
            cfg_mod.MAX_ATTEMPTS_ENV: "2",
            cfg_mod.TEMPERATURE_ENV: "0.0",
            cfg_mod.MAX_TOKENS_ENV: "512",
        })
        self.assertEqual(c.model, "deepseek-flash")
        self.assertEqual(c.base_url, "http://gw:8080/v1")
        self.assertEqual(c.api_key_env, "MY_KEY_VAR")
        self.assertEqual(c.timeout_s, 30.0)
        self.assertEqual(c.max_attempts, 2)
        self.assertEqual(c.temperature, 0.0)
        self.assertEqual(c.max_tokens, 512)

    def test_invalid_values_rejected(self):
        with self.assertRaises(ValueError):
            cfg_mod.load_model_config({cfg_mod.TIMEOUT_ENV: "0"})
        with self.assertRaises(ValueError):
            cfg_mod.load_model_config({cfg_mod.MAX_ATTEMPTS_ENV: "abc"})
        with self.assertRaises(ValueError):
            cfg_mod.load_model_config({cfg_mod.TEMPERATURE_ENV: "3.0"})
        with self.assertRaises(ValueError):
            cfg_mod.load_model_config({cfg_mod.MAX_TOKENS_ENV: "-1"})

    def test_manifest_dict_has_no_secret_value(self):
        c = cfg_mod.load_model_config({cfg_mod.MODEL_ENV: "deepseek-flash"})
        blob = json.dumps(c.as_manifest_dict())
        self.assertNotIn("sk-", blob)
        self.assertEqual(c.as_manifest_dict()["api_key_env"],
                         "AGENTTEAMS_GATEWAY_API_KEY")   # 只有变量名


class TestClassify(unittest.TestCase):
    def test_categories(self):
        f = cli_mod.classify_model_error
        self.assertEqual(f(401), "auth")
        self.assertEqual(f(429), "rate_limited")
        self.assertEqual(f(500), "retryable")
        self.assertEqual(f(503), "retryable")
        self.assertEqual(f(403, "API rate limit exceeded"), "rate_limited")
        self.assertEqual(f(403, "forbidden"), "permanent")
        self.assertEqual(f(400), "permanent")
        self.assertEqual(f(404), "permanent")
        self.assertEqual(f(422), "permanent")
        self.assertEqual(f(None), "unknown")


class TestClientOffline(unittest.TestCase):
    def test_list_models_ok(self):
        c = make_client(make_route(["deepseek-flash", "deepseek-v4-pro"]))
        r = c.list_models(timeout_s=5)
        self.assertTrue(r["ok"])
        self.assertEqual(r["models"], ["deepseek-flash", "deepseek-v4-pro"])

    def test_chat_shape_extracted(self):
        c = make_client(make_route(["deepseek-flash"]))
        r = c.chat("deepseek-flash", [{"role": "user", "content": "hi"}],
                   max_tokens=16, temperature=0.0)
        self.assertTrue(r["ok"])
        self.assertEqual(r["content"], "ok")
        self.assertEqual(r["model"], "deepseek-flash")
        self.assertEqual(r["usage"]["total_tokens"], 14)
        self.assertIn("model", r["raw_keys"])

    def test_request_body_omits_unset_generation_params(self):
        seen = {}

        def route(url, body, timeout=None):
            seen.update(body or {})
            return (200, {"model": "m", "choices": [{"message": {"content": "ok"},
                                                     "finish_reason": "stop"}],
                          "usage": dict(USAGE)})
        c = make_client(route)
        c.chat("m", [{"role": "user", "content": "hi"}])
        self.assertNotIn("temperature", seen)
        self.assertNotIn("max_tokens", seen)
        c.chat("m", [{"role": "user", "content": "hi"}], max_tokens=8, temperature=0.5)
        self.assertEqual(seen.get("max_tokens"), 8)
        self.assertEqual(seen.get("temperature"), 0.5)

    def test_error_kinds(self):
        c = make_client(make_route(["deepseek-flash"]))
        r = c.chat("nonexistent-model-zz", [{"role": "user", "content": "x"}], max_tokens=8)
        self.assertEqual(r["kind"], "permanent")           # 400 → permanent
        r = c.chat("deepseek-flash", [{"role": "user", "content": "x"}],
                   timeout_s=0.001, max_tokens=8)
        self.assertEqual(r["kind"], cli_mod.TIMEOUT_KIND)  # 本地超时=结果未知


class TestSmokeOffline(unittest.TestCase):
    def _run(self, catalog, target="deepseek-flash", old="deepseek-chat"):
        client = make_client(make_route(catalog))
        cfg = cfg_mod.ModelCallConfig(base_url="http://gw:8080/v1", model=old)
        report = smoke_mod.run_smoke(cfg, None, target_model=target,
                                     old_model=old, client=client)
        return report, {c["check"]: c for c in report["checks"]}

    def test_full_pass_with_live_rollback_target(self):
        report, by = self._run(["deepseek-flash", "deepseek-v4-pro", "deepseek-chat"])
        self.assertEqual(report["verdict"], "PASS", json.dumps(report["checks"], ensure_ascii=False))
        for name in ("1.provider-model-list", "2.minimal-request",
                     "3.response-shape-compat", "4a.error-classification-401",
                     "4b.error-classification-permanent", "4c.error-classification-timeout",
                     "5.usage-metering", "6.audit-model-identity",
                     "7.rollback-old-model", "8.no-github-writes"):
            self.assertEqual(by[name]["status"], "pass", name)
        self.assertEqual(by["4d.error-classification-429-5xx"]["status"], "skipped")
        self.assertEqual(by["6.audit-model-identity"]["detail"]["response_model"],
                         "deepseek-flash")

    def test_retired_old_model_is_warn_not_fail(self):
        """旧模型已从上游目录消失(当前 deepseek-chat 现实):点7=warn,总判定仍 PASS,
        但回滚说明必须明示单向切换风险。"""
        client = make_client(make_route(["deepseek-flash", "deepseek-v4-pro"],
                                        chat_models=("deepseek-flash",)))
        cfg = cfg_mod.ModelCallConfig(base_url="http://gw:8080/v1", model="deepseek-chat")
        report = smoke_mod.run_smoke(cfg, None, target_model="deepseek-flash",
                                     old_model="deepseek-chat", client=client)
        by = {c["check"]: c for c in report["checks"]}
        self.assertEqual(by["1.provider-model-list"]["status"], "pass")
        self.assertEqual(by["7.rollback-old-model"]["status"], "warn")
        self.assertFalse(by["7.rollback-old-model"]["detail"]["old_in_catalog"])
        self.assertIn("one-way", by["7.rollback-old-model"]["detail"]["note"])
        self.assertEqual(report["verdict"], "PASS")

    def test_target_absent_from_catalog_is_warn(self):
        report, by = self._run(["deepseek-v4-pro"])   # 目录无 target
        self.assertEqual(by["1.provider-model-list"]["status"], "warn")
        self.assertFalse(by["1.provider-model-list"]["detail"]["target_present"])

    def test_broken_rollback_when_old_listed_but_dead(self):
        """旧模型仍在目录但探测失败(路由只对 chat_models 放行)→ 点7=fail。"""
        client = make_client(make_route(["deepseek-flash", "deepseek-v4-pro",
                                         "deepseek-chat"],
                                        chat_models=("deepseek-flash",)))
        cfg = cfg_mod.ModelCallConfig(base_url="http://gw:8080/v1", model="deepseek-chat")
        report = smoke_mod.run_smoke(cfg, None, target_model="deepseek-flash",
                                     old_model="deepseek-chat", client=client)
        by = {c["check"]: c for c in report["checks"]}
        self.assertEqual(by["7.rollback-old-model"]["status"], "fail")
        self.assertEqual(report["verdict"], "FAIL")

    def test_report_carries_no_key_material(self):
        report, _ = self._run(["deepseek-flash"])
        blob = json.dumps(report)
        self.assertNotIn("sk-good", blob)
        self.assertNotIn(BAD_KEY, blob)


if __name__ == "__main__":
    unittest.main()
