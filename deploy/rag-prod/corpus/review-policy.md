# Review Integration Safety Boundary

The integration between retrieval results and the review flow must obey the
following hard boundaries. None of them may be relaxed.

## Auxiliary Evidence Position

Retrieval results serve only as auxiliary cited evidence for the reviewer.
Auxiliary evidence is marked reference-only, has a trust flag of false, and
carries an exclusion list: finding, ticket, gate, VERIFIED, fixer patch
input, and verifier evidence.

## Fixer Boundary

The fixer must never modify code based on retrieval text alone. The fixer
input filter strips every retrieval-typed evidence item; if the remaining
input is empty, the fixer must not start.

## Verifier Boundary

The verifier accepts only independent test harness evidence. Test reports,
harness outputs, and independent run logs are the only three acceptable
kinds; retrieval-typed evidence is always rejected.

## Promotion Paths

There is no automatic promotion path from retrieval evidence to findings or
tickets. Even when non-retrieval evidence is mixed in, the promotion decision
must be completed by human review.
