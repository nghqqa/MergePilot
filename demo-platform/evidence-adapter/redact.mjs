// evidence-adapter/redact.mjs
// Redaction layer — EVERY API response passes through redact().
// Constraints (Phase 14.2H-WD-DEMO-PLATFORM-BUILD):
//   - never output password / token / API key / Cookie / Authorization values
//   - PR #2 demo placeholder strings ARE allowed (explicitly permitted by spec)
//   - sha256 digests are display-safe (integrity panel) and must survive redaction

export const REDACTED = '[REDACTED]';

// Display-safe key names whose values are hashes/ids/status results, not secrets.
const SAFE_KEY_RE = /(sha ?256|sha1|digest|checksum|hash|event_id|trace_id|id$|_id$|path|file|name|ref|url|link|secret[_-]?scan|scan[_-]?result|residual)/i;

// Keys whose VALUE is a secret when present.
const SECRET_KEY_RE = /(pass(word|wd)?|secret|token|api[_-]?key|apikey|cookie|authorization|bearer|credential|private[_-]?key|access[_-]?key(?![a-z]*name)|client[_-]?secret)/i;

// Free-string patterns: redact token-shaped substrings.
const STRING_PATTERNS = [
  /\bBearer\s+[A-Za-z0-9._~+/=-]{8,}/gi,
  /\b(?:ghp|gho|ghu|ghs|ghr|xox[bposa]|sk-|sk_|AKIA|ASIA)[A-Za-z0-9_\-]{10,}\b/g,
  /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{8,}\b/g, // JWT
  /\b(pass(word|wd)?|secret|token|api[_-]?key|apikey|cookie)\s*[:=]\s*("[^"]{4,}"|\S{6,})/gi,
];

// Allow-listed demo placeholders that come from the PR #2 repo fixtures.
// The spec explicitly permits showing them; they are NOT credentials.
const ALLOW_TERMS = [
  'TOP-SECRET-OUTSIDE-BASE',
  'WELCOME-LEGIT-DEMO',
  'outside-secret.txt',
  'deep-secret.txt',
  'welcome.txt',
  'LEAKED_OUTSIDE_SECRET',
  'TOP-SECRET',
];

function isAllowTerm(s) {
  if (typeof s !== 'string') return false;
  return ALLOW_TERMS.some((t) => s.includes(t));
}

export function redactString(s) {
  if (typeof s !== 'string') return s;
  if (isAllowTerm(s) && s.length <= 64) return s; // pure placeholder token
  let out = s;
  for (const re of STRING_PATTERNS) {
    out = out.replace(re, (m) => {
      // keep the leading keyword for readability, mask the value
      const eq = m.indexOf('=');
      const colon = m.indexOf(':');
      const cut = eq >= 0 ? eq : colon;
      if ((eq >= 0 || colon >= 0) && cut > 0) {
        const head = m.slice(0, cut + 1);
        if (isAllowTerm(m)) return m;
        return head + REDACTED;
      }
      return REDACTED;
    });
  }
  return out;
}

export function redact(value, keyName = '') {
  if (value === null || value === undefined) return value;
  if (Array.isArray(value)) return value.map((v) => redact(v, keyName));
  if (typeof value === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(value)) out[k] = redact(v, k);
    return out;
  }
  if (typeof value === 'string') {
    // key-based masking first
    if (SECRET_KEY_RE.test(keyName) && !SAFE_KEY_RE.test(keyName)) {
      return isAllowTerm(value) && value.length <= 64 ? value : REDACTED;
    }
    return redactString(value);
  }
  return value; // numbers / booleans
}

// Scan a text/JSON payload for residual secret-like leaks (used by selftest & audit).
export function scanForSecrets(text) {
  const hits = [];
  const push = (why, sample) => hits.push({ why, sample: sample.slice(0, 48) });
  if (/\bBearer\s+[A-Za-z0-9._~+/=-]{8,}/.test(text)) push('bearer-token', text.match(/\bBearer\s+\S{8,}/)[0]);
  const kv = text.match(/\b(pass(word|wd)?|api[_-]?key|apikey|client[_-]?secret)\s*[:=]\s*(?!\[REDACTED\])("\S{4,}"|\S{6,})/gi) || [];
  for (const m of kv) {
    if (ALLOW_TERMS.some((t) => m.includes(t))) continue;
    push('secret-assignment', m);
  }
  if (/\b(?:ghp|gho|xox[bposa]|sk-|sk_|AKIA)[A-Za-z0-9_\-]{10,}\b/.test(text)) push('token-shaped', '');
  if (/\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{8,}\b/.test(text)) push('jwt', '');
  return hits;
}

export const redactionMeta = {
  applied: 'every API response body',
  key_rules: 'secret-like keys -> [REDACTED] (sha/digest/event_id/id/path exempt)',
  string_rules: 'bearer / gh* / sk-* / AKIA* / JWT / key=value secret assignments masked',
  allow_list: ALLOW_TERMS,
  note: 'PR #2 demo placeholders are repo fixtures, not credentials (permitted by build spec)',
};
