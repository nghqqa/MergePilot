# Architecture Overview

The sample project separates the control plane from the execution plane.
The control plane owns task orchestration, authorization, and audit;
the execution plane performs the actual checking, fixing, and verification.

## Layers

The first layer is the access layer. It handles external event entry and
signature verification. Every external event must carry a valid signature;
events that fail verification are dropped and recorded in the audit log.

The second layer is the orchestration layer. It maintains the task state
machine. Every state transition must write an audit event including the
initiator, the reason, and a timestamp.

The third layer is the execution layer. It contains three roles: the
reviewer discovers problems, the fixer produces patches, and the verifier
performs independent verification. The three roles are isolated from each
other, and the verifier only accepts evidence from an independent test
harness.

## Key Constraints

The control plane holds exclusive ticket ownership. No execution role may
create or close tickets directly; ticket creation and lifecycle transitions
belong to the control plane alone.

Outputs of the execution layer are always suggestions. Whether to adopt them
is a human decision. The auto-merge channel is disabled by default; enabling
it requires an explicit human authorization grant.
