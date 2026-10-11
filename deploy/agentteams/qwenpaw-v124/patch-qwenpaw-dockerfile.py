#!/usr/bin/env python
"""patch-qwenpaw-dockerfile.py — qwenpaw worker Dockerfile 注入锁定 CAS 依赖。

幂等锚定编辑：在 wrapt/loongsuite 依赖 RUN 之后插入 boto3 锁定安装。
botocore>=1.35.49 的 S3 服务模型才带 PutObject If-Match/If-None-Match；
1.35.0（早期候选）没有该参数，传入会 ParamValidationError。锁定的
1.35.99 已对 embedded MinIO 真机验证（正确 ETag 写入成功 / 错误 ETag
PreconditionFailed / IfNoneMatch 防覆盖）。
"""
import argparse
from pathlib import Path

ANCHOR = '''        "wrapt<2.0" \\
        loongsuite-site-bootstrap==0.6.1 \\
        loongsuite-distro==0.6.0 \\
        loongsuite-otel-util-genai==0.6.1 \\
        loongsuite-instrumentation-qwenpaw==0.6.1 \\
        loongsuite-instrumentation-agentscope==0.6.1 \\
        opentelemetry-exporter-otlp
'''

INJECT = ANCHOR + '''
# TeamHarness task-meta CAS conditional-write dependency (pinned, verified).
# botocore >= 1.35.49 carries PutObject If-Match/If-None-Match in its S3
# service model; 1.35.0 does NOT (ParamValidationError on IfMatch). The
# pinned pair below was verified against the embedded MinIO: correct ETag
# write succeeds, wrong ETag -> PreconditionFailed, If-None-Match guards
# concurrent creates.
RUN /opt/venv/qwenpaw/bin/pip install \\
        --retries 5 \\
        --timeout "${PIP_TIMEOUT}" \\
        --index-url "${PIP_INDEX_URL}" \\
        --trusted-host "$(echo ${PIP_INDEX_URL} | sed -E 's|^https?://||;s|/.*||')" \\
        "boto3==1.35.99" \\
        "botocore==1.35.99"
'''


APT_ANCHOR_OLD = "apt-get update && apt-get install -y --no-install-recommends \\"
APT_ANCHOR_NEW = (
    "# Acquire::Retries: mirror 502s are transient (tuna observed); retry instead\n"
    "# of failing the whole build.\n"
    "apt-get update && apt-get install -y --no-install-recommends -o Acquire::Retries=10 \\\n"
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dockerfile", required=True)
    args = ap.parse_args()
    path = Path(args.dockerfile)
    text = path.read_text(encoding="utf-8")
    if "boto3==1.35.99" in text:
        print("already patched (idempotent no-op)")
        return
    if text.count(ANCHOR) != 1:
        raise SystemExit(f"anchor not unique in {path}")
    if text.count(APT_ANCHOR_OLD) == 1:
        text = text.replace(APT_ANCHOR_OLD, APT_ANCHOR_NEW)
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text.replace(ANCHOR, INJECT))
    print(f"injected pinned CAS dependency into {path}")


if __name__ == "__main__":
    main()
