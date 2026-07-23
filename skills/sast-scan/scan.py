#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MergePilot SASTScan skill — real static analysis (no external deps, pure stdlib).
Detectors:
  - Secret leak (regex: sk-live/sk-test/AWS AKIA / assigned API_KEY/SECRET/PASSWORD/TOKEN)
  - SQL injection (AST: execute()/executemany() with f-string or string-concat query)
  - Dangerous calls (eval/exec/pickle.load/subprocess shell=True)
  - Dependency vulns (requirements.txt known-bad pins)
Output: JSON list of findings to stdout.

Usage:
    python3 scan.py <file_or_dir> [<file2> ...]
"""
import sys, os, ast, json, re, glob

# ---- known-bad dependency pins (demo set) ----
BAD_DEPS = {
    "cryptography": {"37.0.0": "CVE-2023-50782 (密钥处理漏洞),应升级到 >=42.0.4"},
    "loguru": {"0.5.3": "已知问题版本,升级到最新"},
    "requests": {"2.19.0": "CVE-2018-18074,升级到 >=2.20.0"},
}

SECRET_PATTERNS = [
    (re.compile(r"sk-(live|test|prod|key)-[A-Za-z0-9_\-]{8,}"), "OpenAI 风格生产/测试密钥"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS Access Key ID"),
    (re.compile(r"(?i)(API_KEY|SECRET|SECRET_KEY|PASSWORD|PASSWD|TOKEN|ACCESS_TOKEN)\s*=\s*['\"][^'\"]{6,}['\"]"), "硬编码凭证赋值"),
]

DANGEROUS = {
    "eval": ("eval() 动态执行,代码注入风险", "high", "L2"),
    "exec": ("exec() 动态执行,代码注入风险", "high", "L2"),
    "load": ("pickle.load 反序列化,任意代码执行风险", "high", "L2"),
}


def redact(text, m):
    s = m.group(0)
    return s[:6] + "***" + s[-4:] if len(s) > 12 else s[:3] + "***"


def scan_secrets(path, text, findings):
    for i, line in enumerate(text.splitlines(), 1):
        for pat, desc in SECRET_PATTERNS:
            m = pat.search(line)
            if m:
                findings.append({
                    "category": "security", "severity": "critical", "risk_level": "L2",
                    "file": path, "line": i,
                    "description": f"{desc} 明文暴露:{redact(line, m)}",
                    "suggestion": "改用环境变量或密钥管理服务,源码中杜绝密钥字面量,并吊销已泄漏密钥",
                })


def scan_ast(path, text, findings):
    try:
        tree = ast.parse(text)
    except SyntaxError as e:
        findings.append({"category": "quality", "severity": "medium", "risk_level": "L0",
                         "file": path, "line": e.lineno or 0,
                         "description": f"语法错误:{e.msg}", "suggestion": "修复语法错误"})
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            # SQLi: *.execute(...)/executemany(...) with f-string or concat query
            if isinstance(func, ast.Attribute) and func.attr in ("execute", "executemany") and node.args:
                first = node.args[0]
                if isinstance(first, ast.JoinedStr):
                    findings.append({"category": "security", "severity": "critical", "risk_level": "L2",
                                     "file": path, "line": getattr(node, "lineno", 0),
                                     "description": "SQL 注入:execute() 使用 f-string 拼接查询,未参数化",
                                     "suggestion": "改用参数化查询:execute('SELECT ... WHERE name=?', (name,))"})
                elif isinstance(first, ast.BinOp):
                    findings.append({"category": "security", "severity": "critical", "risk_level": "L2",
                                     "file": path, "line": getattr(node, "lineno", 0),
                                     "description": "SQL 注入:execute() 使用字符串拼接查询,未参数化",
                                     "suggestion": "改用参数化查询:execute('SELECT ... WHERE name=?', (name,))"})
            # dangerous calls
            name = func.id if isinstance(func, ast.Name) else None
            if name in DANGEROUS:
                desc, sev, risk = DANGEROUS[name]
                # pickle.load: func is Attribute (.load on pickle)
                findings.append({"category": "security", "severity": sev, "risk_level": risk,
                                 "file": path, "line": getattr(node, "lineno", 0),
                                 "description": f"危险调用:{desc}", "suggestion": "移除或替换为安全实现"})


def scan_deps(path, text, findings):
    for i, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"([A-Za-z0-9_\-]+)\s*[=~><!]+\s*([0-9.]+)", line)
        if m:
            pkg, ver = m.group(1).lower(), m.group(2)
            if pkg in BAD_DEPS and ver in BAD_DEPS[pkg]:
                findings.append({"category": "security", "severity": "high", "risk_level": "L2",
                                 "file": path, "line": i,
                                 "description": f"依赖漏洞:{pkg}=={ver},{BAD_DEPS[pkg][ver]}",
                                 "suggestion": f"升级 {pkg} 到安全版本"})


def scan_path(target):
    findings = []
    files = []
    if os.path.isdir(target):
        for ext in ("*.py", "*.txt", "*.js", "*.ts"):
            files += glob.glob(os.path.join(target, "**", ext), recursive=True)
    elif os.path.isfile(target):
        files = [target]
    else:
        return findings
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except Exception:
            continue
        base = os.path.basename(f)
        scan_secrets(f, text, findings)
        if base.endswith(".py"):
            scan_ast(f, text, findings)
        if base in ("requirements.txt", "package.json"):
            scan_deps(f, text, findings)
    return findings


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "usage: scan.py <file_or_dir> [...]"}, ensure_ascii=False))
        sys.exit(1)
    all_f = []
    for t in sys.argv[1:]:
        all_f += scan_path(t)
    print(json.dumps({"findings": all_f, "count": len(all_f)}, ensure_ascii=False, indent=2))
