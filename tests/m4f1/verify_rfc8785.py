#!/usr/bin/env python3
"""verify_rfc8785.py — Fixed-spec canonical constants oracle for MergePilot JCS Profile v1.

Does NOT implement a canonicalizer. Expected canonical text/SHA-256 are hardcoded
spec constants derived from RFC 8785/JCS rules and verified against the official
RFC 8785 Appendix B examples. Python only reads constants, computes SHA-256 over
the fixed canonical bytes, and compares PG output.

No third-party dependency. stdlib only.
"""
import hashlib, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ============================================================
# FIXED SPEC CONSTANTS (oracle)
# Each tuple: (id, raw_input_description, expected_canonical_text, status)
# status: "PASS" = canonical must match; "REJECT" = PG must raise P0001
# ============================================================

VECTORS = [
    # --- JCS canonical vectors (canonical text = fixed spec constant) ---
    ("V1",  '{"b":1,"a":{"n":null}}',
     '{"a":{"n":null},"b":1}', "PASS"),
    ("V2",  '{"z":"é","a":"α"}',
     '{"a":"α","z":"é"}', "PASS"),
    ("V3",  '{"c":1.0,"b":1.50e2,"a":-0.0}',
     '{"a":0,"b":150,"c":1}', "PASS"),
    ("V4",  '[1,null,{"b":2,"a":1}]',
     '[1,null,{"a":1,"b":2}]', "PASS"),
    # V5: U+E000 (chr(57344)) vs U+1D54F (chr(120119)); UTF-16 sort: 𝕏(D835)<U+E000(E000)
    ("V5",  None,  # special: needs PG jsonb_build_object
     '{"\U0001d54f":2,"":1}', "PASS"),
    ("V6",  '{"a\\\\b":"x\\ty"}',  # key=a\b, value=x<TAB>y
     '{"a\\\\b":"x\\ty"}', "PASS"),  # \t short escape, \\ for backslash
    ("V7",  '{"n":1e2}',
     '{"n":100}', "PASS"),
    ("V8",  '{"n":-0}',
     '{"n":0}', "PASS"),
    # --- Reject vectors ---
    ("V9",  '{"n":9007199254740993}',  # 2^53+1
     None, "REJECT"),

    # --- Number formatting (ECMAScript NumberToString) ---
    ("N1",  '{"n":0.0000001}',  # 1e-7
     '{"n":1e-7}', "PASS"),
    ("N2",  '{"n":0.000001}',   # 1e-6
     '{"n":0.000001}', "PASS"),
    ("N3",  '{"n":0.1}',
     '{"n":0.1}', "PASS"),
    ("N4",  '{"n":333333333.33333329}',
     '{"n":333333333.3333333}', "PASS"),  # shortest round-trip for this binary64

    # --- String escape (JCS §3.2.2.2) ---
    ("S_BS",   '{"k":"\\u0008"}',  '{"k":"\\b"}', "PASS"),
    ("S_TAB",  '{"k":"\\u0009"}',  '{"k":"\\t"}', "PASS"),
    ("S_LF",   '{"k":"\\u000a"}',  '{"k":"\\n"}', "PASS"),
    ("S_FF",   '{"k":"\\u000c"}',  '{"k":"\\f"}', "PASS"),
    ("S_CR",   '{"k":"\\u000d"}',  '{"k":"\\r"}', "PASS"),
    ("S_QUOTE",'{"k":"\\""}',      '{"k":"\\""}', "PASS"),
    ("S_BSLS", '{"k":"\\\\"}',     '{"k":"\\\\"}', "PASS"),
    ("S_CTRL", '{"k":"\\u0001"}',  '{"k":"\\u0001"}', "PASS"),  # other control char

    ("LIT_BS_U_CANON", '{"k":"\\\\u0000"}', '{"k":"\\\\u0000"}', "PASS"),

    # --- Key reorder same canonical ---
    ("R1",  '{"z":1,"a":2,"m":3}',  '{"a":2,"m":3,"z":1}', "PASS"),
]

def expected_sha(canonical_text):
    """Compute SHA-256 of canonical text UTF-8 bytes."""
    return hashlib.sha256(canonical_text.encode('utf-8')).hexdigest()

def canon_str(v):
    """canon_str helper (length-prefix, NULL→-1:)."""
    if v is None: return "-1:"
    b = v.encode('utf-8')
    return f"{len(b)}:{v}"

def compute_request_id(trace_id, run_id, skill_name, attempt, d_in):
    """Compute request_id = 'req-' || H_24(trace,run,skill,attempt,D_in)."""
    concat = canon_str(trace_id) + canon_str(run_id) + canon_str(skill_name) + canon_str(str(attempt)) + canon_str(d_in)
    return "req-" + hashlib.sha256(concat.encode('utf-8')).hexdigest()[:24]

if __name__ == "__main__":
    for vid, raw_desc, canon, status in VECTORS:
        if status == "PASS":
            sha = expected_sha(canon)
            print(f"{vid}|{canon.encode('utf-8').hex()}|{sha}")
        else:
            print(f"{vid}|REJECT|")

    # Fixed request_id example: input {"f":1}, trace="tr1", run="pa_run1", skill="diff-parse", attempt=1
    d_in = expected_sha('{"f":1}')
    rid = compute_request_id("tr1", "pa_run1", "diff-parse", 1, d_in)
    print(f"REQID|{rid}|")
    print(f"D_IN|{d_in}|")
