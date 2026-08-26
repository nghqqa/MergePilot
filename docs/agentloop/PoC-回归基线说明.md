# PoC 回归基线说明（WinError 10013 · 环境性失败存证）

> 日期：2026-08-27　分支：`feat/agentloop-otel-genai-poc`　基线提交：`a5a33a0`

## 失败清单（tests/otel 既有用例，5 个）

| 用例 | 错误 |
|---|---|
| `test_exporter_timeout.py::TestExporterTimeoutSlowReceiver::test_business_continues_after_timeout` | `PermissionError: [WinError 10013] 以一种访问权限不允许的方式做了一个访问套接字的尝试。`（`test_exporter_timeout.py:39` `sock.bind(("127.0.0.1", port))`） |
| `test_exporter_timeout.py::TestExporterTimeoutSlowReceiver::test_slow_receiver_timeout_bounded` | 同上 |
| `test_sls_exporter.py::TestFakeSLSReceiver::test_receive_spans` | 同上（`socketserver.py:466` bind） |
| `test_sls_exporter.py::TestBatching::test_batch_under_max_uses_timeout` | 同上 |
| `test_sls_exporter.py::TestRedactionInSLS::test_pat_not_in_sls_payload` | 同上 |

## 成因判定

五个用例都需要在测试进程内**起本地监听套接字**（慢速接收器/假 SLS 接收器）。当前执行环境的沙箱策略禁止 `bind()` 监听端口（10013），失败发生在 fixture 启动阶段，早于任何被测逻辑。

## 基线对照证据（stash 法）

```
$ git stash push -u -m "poc-wip"          # 暂存本轮全部改动（含未跟踪文件）
$ python -m pytest <上述代表用例×2> -q
FAILED ...test_business_continues_after_timeout
FAILED ...test_pat_not_in_sls_payload
2 failed in 0.16s                          # 与带改动时同型同因
$ git stash pop                            # 干净恢复
```

无任何本轮改动的干净树上，同样以 WinError 10013 起监听失败 ⇒ **非本次实现引入**。

## 处置纪律

- 按任务要求**不修改、不跳过、不注解绕过**这五个用例；它们在被授权开放端口绑定的环境中预期照常通过（M6A 冻结文档记录的历史门禁为 57 tests ×2 stable，本机当时具备 bind 权限）。
- 回归结论口径：除上述 5 个环境性失败外，`pytest tests/otel` 其余 **99 用例全部通过**（含新增 PoC 套件 25 例与评估器 5 例）。
