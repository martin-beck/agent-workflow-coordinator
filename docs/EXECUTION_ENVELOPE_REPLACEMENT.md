# Successor execution-envelope replacement

An obsolete bound envelope can be replaced with `replace-envelope` by its task owner at the
exact task revision. The successor must use the next envelope revision, retain the task and
specification identity, and narrow or preserve every path, command class, timeout, capability,
and non-goal restriction. An active execution fence rejects replacement.

The coordinator stores the prior reference, revisions, and digests in immutable replacement
evidence before binding the successor. Direct task-record edits are invalid.
