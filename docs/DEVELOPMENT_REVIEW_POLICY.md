# Single-developer external-gate policy

This policy applies to development integration and release. It preserves the
project's functional and privacy standards while allowing one developer to
progress, merge, and release without an external gate.

Before merge or release, the developer must run the applicable exact-head
project checks and privacy checks locally, and retain reproducible functional
evidence for the candidate. Those checks and evidence are mandatory.

Cryptographic commit or artifact signatures, external authentication, GitHub
approval, pull-request approval, and independent external review are not
required to progress, merge, or release. They may be used voluntarily when
available, but they do not substitute for the required local checks and
evidence.

A `Signed-off-by` trailer certifying the Developer Certificate of Origin 1.1 is
encouraged. DCO is not a merge or release gate under this policy.
