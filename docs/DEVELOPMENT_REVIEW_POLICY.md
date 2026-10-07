# Development review identity policy

This policy applies only to development integration. It does not qualify a
release, change the release signer policy, or weaken any production or verified
channel gate.

Every development change still requires all of the following before merge:

1. exact-head project checks and applicable privacy checks pass;
2. every introduced commit has an accepted SSH signature and matching DCO
   trailer; and
3. an independent technical review-worker records its result against the exact
   candidate head and tree.

“Independent” describes the review execution and evidence boundary, not the
GitHub login identity. The review worker must be a separate worker context from
the implementation worker and inspect the exact candidate; it need not be a
different GitHub account or repository collaborator.

For development-only pull requests, a GitHub approval from the same account
that authored the topic commits is acceptable after the independent review
worker has approved the exact head. No additional authorized-maintainer or
second-GitHub-account approval is required. The approval must not substitute
for the independent technical review, exact-head checks, signatures, DCO, or
privacy checks.

The verified release channel remains governed by its existing release policy,
including independent verification, signed artifacts, provenance, and release
publication gates. This development exception must never be used as release
evidence.
