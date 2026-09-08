# Security policy

Report suspected vulnerabilities privately through GitHub's security-advisory interface. Do not
open a public issue containing an exploit, credential, private path or sensitive task state.

The supported boundary is cooperating processes, local POSIX flock semantics, trusted repository
source/history, and correctly protected runtime configuration. NFS locking, hostile same-UID
processes, malicious repository rewrites, arbitrary wrapped-command safety and power-loss atomicity
are not claimed. Binding prevents accidental cross-project use; it is not a sandbox or cryptographic
defense against an attacker who can rewrite the tool and its trusted files.
