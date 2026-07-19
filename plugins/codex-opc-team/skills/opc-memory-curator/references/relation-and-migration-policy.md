# Relation and Migration Policy

## Deterministic recall boundary

Use `query-context`, not Provider rank, to decide what may enter execution context. The fixed hard-filter order is scope/project identity, approved status, current-HEAD commit and hash, sensitivity permission, explicit applicability, then invalidation/supersession. Missing project context means global-only; never infer a project ID from an absolute path.

An unresolved conflict withholds both records. Show both canonical citations and no body. Missing targets, invalid relation data, ineligible targets, and directed cycles fail only the related records.

## Schema migration

Schema 1 remains readable but must be migrated before relation, applicability, or sensitivity curation. Create an external private backup directory first. Run `migrate-schema --dry-run --backup-root <root>` for inventory, select one record, then rerun preview with `--record-id <id>`. Apply only with that single-record preview's unchanged record ID, backup root, and exact plan token. Review and commit only returned transition paths. Never batch-migrate implicitly or put private backups in the public plugin checkout.

## Exact curation

Use `curate --dry-run` with the complete manager approval reference, target status, validation/reason, relations, applicability, and sensitivity. Ask the manager to approve that exact preview. Apply with identical arguments and its `plan_token`; changed inputs or canonical source require a new preview.

Relations are strict JSON objects passed through repeatable `--relation` arguments and require an explicit kind, target ID, scope, and `project_id` (`null` for global). Curation writes only canonical File/Git paths and never writes the optional Provider. A successful apply is still unpublished until the exact paths are committed and verified at current HEAD. Optional reindex remains a separate preview and approval.
