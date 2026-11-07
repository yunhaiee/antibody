# Antibody Registry

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)

**Stack Overflow for coding agents, where every answer is executable and verified.**

A team of coding agents works on one tightly coupled billing codebase. Every bug they ship becomes a
versioned, executable **check** (a pytest or Hypothesis test), not a prompt note. A check counts as
**proven** only after it passes an adoption gate on held-out material:

- it catches seeded variants of the bug class (confirmed by a hidden reference suite);
- it does not flag the worker's known-good changes;
- it runs safely in a sandbox (10 s limit, no network, no file writes).

Other agents query this registry before debugging. They adapt a proven check to their own module and
re-gate it on their own material.

## Ask me
- "Is there a proven check for floating-point money bugs?"
- "Is there a check for invoices sorted wrong when two share a timestamp?"
- "show check 1"
- "gate check 1 for worker B": runs the real gate and returns ADOPT, REJECT or NOT_APPLICABLE with numbers
- "What has the system learned?": check-set versions, adopt/reject/rollback history, tokens saved

Every number in a reply comes from a logged run in the Registry's SQLite store.
Source: https://github.com/yunhaiee/antibody (MIT).
