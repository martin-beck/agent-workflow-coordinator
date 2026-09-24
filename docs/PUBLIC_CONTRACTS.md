# Public contract inventory

This inventory names tracked contracts that downstream projects may consume. Each entry is
strictly versioned and must reject unknown fields before a caller treats it as valid.

| Contract | Schema | Validator | Evidence |
| --- | --- | --- | --- |
| Coordinator role | `schema/role.schema.json` | `tools/role_registry.py` | `tests/test_role_registry.py` |
| Coordinator role registry | `schema/role-registry.schema.json` | `tools/role_registry.py` | `tests/test_role_registry.py` |
| Coordinator role assignment | `schema/role-assignment.schema.json` | `tools/role_assignment.py` | `tests/test_role_assignment.py` |
| Capability matrix formal contract | `formal/roles/CapabilityMatrix.tla` and `formal/roles/CapabilityMatrix.cfg` | `tools/capability_matrix_correspondence.py` | `tests/test_capability_matrix_formal.py` |

The role registry is descriptive authorization input. It does not itself authorize a mutation,
release, rollback, or Dispatch operation; those decisions remain owned by the Coordinator
runtime and its durable state.
