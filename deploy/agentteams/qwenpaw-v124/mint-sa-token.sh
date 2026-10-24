#!/usr/bin/env bash
# mint-sa-token.sh — 从 embedded ctrl 的 /data/pki/sa.key 签发 service-account JWT（幂等）。
# 用途：ctrl 管理 API（:8090 /api/v1/*）的认证凭据——生产形态由 at-auth-proxy 注入同源 JWT，
# 隔离/直连形态由本脚本铸造。token 只写文件 0600，零回显。
# 用法：QP_CTRL_NAME=qwp2-controller bash mint-sa-token.sh [输出文件，默认 ./ctrl-sa-token]
set -euo pipefail
export MSYS_NO_PATHCONV=1
QP_CTRL_NAME="${QP_CTRL_NAME:-qwp2-controller}"
OUT="${1:-ctrl-sa-token}"
SUB_NAME="${QP_SA_NAME:-qwp2-admin}"

umask 077  # 177 会把 mktemp 目录削成 d600（无 x）——目录内建文件必被拒
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
docker exec "$QP_CTRL_NAME" cat /data/pki/sa.key > "$TMP/sa.key"
docker exec "$QP_CTRL_NAME" cat /data/pki/sa.pub > "$TMP/sa.pub"

python3 - "$TMP" "$SUB_NAME" "$OUT" <<'PY'
import base64, hashlib, json, sys, time, uuid, subprocess, os
tmp, sub_name, out = sys.argv[1], sys.argv[2], sys.argv[3]
NUL = bytes([0])
def b64u(b): return base64.urlsafe_b64encode(b).rstrip(b'=').decode()
head = open(f'{tmp}/sa.key').read(40)
if 'EC PRIVATE KEY' in head:
    # ES256（v1.2.4 embedded ctrl 的 sa.key=EC P-256）
    pub = subprocess.run(['openssl','ec','-in',f'{tmp}/sa.key','-pubout'], capture_output=True, text=True, check=True).stdout
    point = subprocess.run(['openssl','ec','-in',f'{tmp}/sa.key','-noout','-text'],
                           capture_output=True, text=True, check=True).stdout
    hx = ''.join(line.strip().replace(':', '') for line in point.splitlines()[1:]
                 if all(c in '0123456789abcdefABCDEF:' for c in line.strip()))
    # 04||X(32)||Y(32)——取 65 字节非压缩点
    raw = bytes.fromhex(hx)
    raw = raw[-65:]
    assert raw[0] == 4, 'expect uncompressed EC point'
    x_b, y_b = raw[1:33], raw[33:65]
    jwk = json.dumps({'crv': 'P-256', 'kty': 'EC', 'x': b64u(x_b), 'y': b64u(y_b)},
                     sort_keys=True, separators=(',',':'))
    kid = b64u(hashlib.sha256(jwk.encode()).digest())
    now = int(time.time())
    header = {'alg': 'ES256', 'kid': kid, 'typ': 'JWT'}
    payload = {
        'aud': ['agentteams-controller'],
          'iss': 'https://agentteams.local',
        'exp': now + 10*365*24*3600, 'iat': now - 60, 'nbf': now - 60,
        'jti': str(uuid.uuid4()),
        'kubernetes.io': {'namespace': 'default',
                          'serviceaccount': {'name': sub_name, 'uid': str(uuid.uuid4())}},
        'sub': f'system:serviceaccount:default:{sub_name}',
    }
    si = b64u(json.dumps(header, separators=(',',':')).encode()) + '.' + b64u(json.dumps(payload, separators=(',',':')).encode())
    open(f'{tmp}/si', 'w').write(si)
    subprocess.run(['openssl','dgst','-sha256','-sign',f'{tmp}/sa.key','-out',f'{tmp}/sig.der',f'{tmp}/si'], check=True)
    der = open(f'{tmp}/sig.der','rb').read()
    # DER ECDSA-Sig → JOSE raw r||s(64)
    i = 2
    assert der[0] == 0x30 and der[1] == len(der) - 2
    def read_int(i):
        l = der[i+1]; v = der[i+2:i+2+l]; return v, i+2+l
    r_b, i = read_int(i)
    s_b, i = read_int(i)
    r_b = r_b.lstrip(NUL).rjust(32, NUL)
    s_b = s_b.lstrip(NUL).rjust(32, NUL)
    token = si + '.' + b64u(r_b + s_b)
else:
    # RS256（223ddc2 旧线 pki=RSA；保留兼容）
    n_hex = subprocess.run(['openssl','rsa','-in',f'{tmp}/sa.key','-noout','-modulus'],
                           capture_output=True, text=True, check=True).stdout.strip()
    n = int(n_hex.split('=',1)[1], 16)
    jwk = json.dumps({'e': b64u((65537).to_bytes(3,'big')), 'kty': 'RSA', 'n': b64u(n.to_bytes((n.bit_length()+7)//8,'big'))},
                     sort_keys=True, separators=(',',':'))
    kid = b64u(hashlib.sha256(jwk.encode()).digest())
    now = int(time.time())
    header = {'alg': 'RS256', 'kid': kid, 'typ': 'JWT'}
    payload = {
        'aud': ['agentteams-controller'],
          'iss': 'https://agentteams.local',
        'exp': now + 10*365*24*3600, 'iat': now - 60, 'nbf': now - 60,
        'jti': str(uuid.uuid4()),
        'kubernetes.io': {'namespace': 'default',
                          'serviceaccount': {'name': sub_name, 'uid': str(uuid.uuid4())}},
        'sub': f'system:serviceaccount:default:{sub_name}',
    }
    si = b64u(json.dumps(header, separators=(',',':')).encode()) + '.' + b64u(json.dumps(payload, separators=(',',':')).encode())
    open(f'{tmp}/si', 'w').write(si)
    subprocess.run(['openssl','dgst','-sha256','-sign',f'{tmp}/sa.key','-out',f'{tmp}/sig',f'{tmp}/si'], check=True)
    token = si + '.' + b64u(open(f'{tmp}/sig','rb').read())
open(out, 'w').write(token)
os.chmod(out, 0o600)
print(f'minted token -> {out} (len={len(token)}, alg={header["alg"]}, kid={kid[:8]}..., sub={sub_name})')
PY

# 自证：带 token 打 worker 列表（只报状态码）
CODE=$(docker exec "$QP_CTRL_NAME" sh -c 'curl -s -o /tmp/w.json -w "%{http_code}" -H "Authorization: Bearer $0" http://127.0.0.1:8090/api/v1/workers' _ "$(cat "$OUT")")
echo "workers-api-with-minted-token=$CODE"
[ "$CODE" = "200" ] || { echo "FAIL: 铸造 token 未通过认证（HTTP $CODE）" >&2; exit 1; }
