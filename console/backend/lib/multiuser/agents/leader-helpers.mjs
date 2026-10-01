// PR-D 内部装配层：re-export leader 原语 + attempt 收尾薄封装（单一 import 面）。
export { advanceAfterVerify, MAX_FIX_ROUNDS, retryClaim } from './leader.mjs';
import { finishAttempt } from '../orchestration.mjs';

export async function finishAttemptOrSkip(pool, attemptId, status, errorCode = null,
  outputDigest = null, latencyMs = null) {
  return finishAttempt(pool, { attemptId, status, errorCode, outputDigest, latencyMs,
    evidenceRef: `attempt:${attemptId}` });
}
