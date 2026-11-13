# FirstWork Compliance Service

## Overview

FirstWork is a tenant-scoped compliance service. A tenant manages workers, companies,
contracts, versioned rules, and compliance checks. The service can evaluate one contract
synchronously, query contracts that satisfy a rule or check, or evaluate a tenant's
contract set asynchronously.

The application is built with Django REST Framework, PostgreSQL, Redis, and a pure-Python
rule engine. Temporal runs bulk evaluations. The API uses tenant-scoped bearer API keys.

## Architecture and diagrams

- [High-level design](docs/firstwork_HLD.drawio)
- [Entity-relationship diagram](docs/firstwork_ERD.drawio)
- Interactive API documentation: `/api/docs/`
- OpenAPI schema: `/api/schema/`

The main code boundaries are:

| Area | Responsibility |
| --- | --- |
| `apps/directory` | Tenant-owned workers, companies, and contracts |
| `apps/rules` | Rule definitions, immutable versions, references, and validation |
| `domain/engine` | Persistence-independent rule validation, interpretation, and SQL compilation |
| `apps/evaluation` | Checks, pinned check versions, synchronous evaluation, and bulk-job APIs |
| `apps/tenants` | API-key authentication, tenant context, errors, cursors, and throttling |
| `workflows` | Temporal bulk-evaluation workflow, worker, and activities |

## Data model and ownership

Customer data is tenant-scoped. The authenticated API key selects the tenant; clients do
not choose a tenant in request data.

| Entity | Purpose |
| --- | --- |
| Tenant and API key | Customer boundary and credential used by the API |
| Worker | Person subject to compliance rules; the assignment's `User` entity is represented as `Worker` |
| Company | Tenant-owned company with name, industry, country, and `bank_deposit` |
| Contract | Connects one worker with one company and records the dates and country for that engagement |
| Rule and rule version | Rule metadata plus each immutable, schema-versioned definition |
| Compliance check and check version | Named group of rules; each check version pins exact rule-version records in order |
| Evaluation result | Persisted single-contract outcome, trace, `as_of`, and idempotency metadata |
| Evaluation job and job result | Bulk-job progress and one persisted result per contract in that job |

The contract relationship permits a worker to have multiple contracts with the same
company. Rules and checks belong to a tenant; there are no shared system rules in the
current implementation. Foreign keys and database constraints protect ownership and
basic data integrity. PostgreSQL row-level security (RLS) is also enabled for tenant-owned
tables.

Rule and check changes create new versions instead of rewriting existing definitions.
Updating a check resolves its selected active rules to their current versions and pins
those versions. Existing checks therefore remain reproducible after a rule is changed or
archived.

## Rule definitions and evaluation

Rules are JSON expression trees. A condition names a registered property, operator, and
typed value; groups combine child nodes with `AND` or `OR`. Validation rejects unknown
properties, invalid values, unsupported operators, malformed trees, and definitions
exceeding the depth or node limits.

Registered properties include worker, company, and contract attributes. Property names
are case-insensitive by entity prefix; `user.` is accepted as an alias for `worker.` and
is canonicalized before storage and in API traces. The allowed operators depend on the
property type: booleans, strings, and country codes support equality and inequality;
ordered types also support `>`, `<`, `>=`, and `<=`.

Rule values are typed: decimal amounts are supplied as strings, dates use ISO date
format, timestamps must be UTC, and country codes are ISO 3166-1 alpha-2.

Age and contract activity are derived rather than stored:

- `worker.age` is completed years from `date_of_birth` at the evaluation instant.
  February 29 birthdays are observed on February 28 in non-leap years.
- `contract.is_active` is true when `start_date <= as_of < end_date`.
- An evaluation uses one `as_of` timestamp for its rules. A bulk job uses the job's
  creation timestamp for all its contracts.

Missing context values evaluate to `UNKNOWN`. A check is:

- `COMPLIANT` when every pinned rule passes;
- `NON_COMPLIANT` when at least one pinned rule fails;
- `INDETERMINATE` when none fail but at least one is unknown.

The response also includes `compliant`, which is true only for `COMPLIANT`; false does
not distinguish non-compliance from an indeterminate result. Evaluation details include
rule statuses and condition traces with expected and actual values, reasons, and paths.
Referenced conditions include a `via` chain.

### Rule references

A rule can include a reference to another rule version. If the reference omits a version,
creation resolves and pins the target's current version. Later target edits do not change
the referring rule; create a new referring-rule version to change its pin. Definitions
written with references use schema version 2; schema-version-1 definitions remain
readable. Reference expansion is bounded by the same 20-level and 200-node limits.

Reference creation is disabled by default (`RULE_REFERENCES_ENABLED=false`). Enabling it
requires deploying code that can read expanded references first. Before rolling that
reader back, disable reference creation and verify that no stored definition uses one.

## Evaluation paths

### One contract

`POST /api/v1/compliance-checks/{id}/evaluate` loads the tenant's current check version
and contract context, evaluates its pinned rule versions in the Python engine, persists
the result, and returns the outcome and trace. Requests support `Idempotency-Key`; a
reused key with a different request returns a conflict. The response includes the
evaluation ID and the `as_of` timestamp.

### Eligible-contract query

`GET /api/v1/companies/{id}/eligible-contracts` compiles registry-approved rule
predicates into parameterized PostgreSQL SQL. This SQL path supports selecting matching
contracts without running the interpreter over every row. SQL identifiers come from the
property registry, and caller-supplied values are parameters.

### Bulk evaluation

`POST /api/v1/compliance-checks/{id}/evaluate-bulk` returns `202 Accepted` and a job
location. The job pins the check version current at submission, records the maximum
contract ID in its initial scope, and processes tenant contracts in ascending ID batches
of 250. Contracts created after submission are excluded.

The Temporal workflow retries activities, while each batch writes contract results and
advances the job cursor in one database transaction. Results are upserted by
`(job, contract)` so activity retries do not duplicate them. Poll
`GET /api/v1/evaluation-jobs/{id}` for job status and progress. Run the worker and outbox
dispatcher with the async Compose profile.

## API and tenant security

All `/api/v1` endpoints require a bearer API key issued for the tenant. The database
stores a hash of the complete key; the secret is shown only when the key is created.
The API supports directory creation, listing and detail reads; rule and check
versioning; single and bulk evaluation; and eligible-contract queries.

Collection endpoints use signed keyset cursors. Cursors are tenant- and resource-bound;
the default page size is 100 and the maximum is 1,000. API errors use
`application/problem+json`.

The PostgreSQL `migrator` role owns the schema and performs migrations. Runtime
processes use the restricted `app_user` role, which does not own tables and does not
have `BYPASSRLS`. Tenant context is transaction-local. The tenancy tests exercise the
runtime role, including raw SQL, to verify isolation. Credentials in the Compose
initialization script are for local development only.

## Running and validating locally

Requirements and the complete command reference are in [README.md](README.md). With
Docker Compose v2 and `make` installed:

```sh
make init
make up
```

The API is available at `http://localhost:8000`; health and readiness endpoints are
`/healthz` and `/readyz`. Start background evaluation services with:

```sh
make up-async
```

Useful project checks are:

```sh
make test
make lint
```

The tests cover unit behavior, API workflows, interpreter/SQL parity, tenancy/RLS, and
Temporal workflow behavior. The Temporal workflow integration test uses the SDK test
server rather than the Compose development service.

## Benchmarking

`make benchmark` runs the Temporal bulk benchmark. Its default fixture creates a
dedicated benchmark tenant with 100,000 rules, 1,000 workers, 20 companies, and 2,000
contracts. Two checks each pin 20 rules; the rules fixture is for scale testing, not a
requirement to evaluate every rule. The benchmark checks persisted job results, reports
throughput and latency, and includes an additional-contract scenario. It leaves
generated tenants, jobs, and results in the database.

`generate_benchmark_data` can also create the larger fixture of 100 tenants, 1,000 rules
per tenant, and 10,000 contracts per tenant. `run_compliance_benchmark` measures check
evaluation and eligible-contract query plans, including PostgreSQL `EXPLAIN ANALYZE`.
See [bench/README.md](bench/README.md) and [README.md](README.md) for command details.

## Assumptions and scope

- A tenant is the customer account; companies and their contracts are scoped to it.
- Rules are tenant-owned; cross-tenant or system-wide rule sharing is not implemented.
- `bank_deposit` is a decimal amount without a currency field. The model assumes a single
  reporting currency; multi-currency behavior is not defined.
- Missing values are reported as `INDETERMINATE`; they never count as a pass.
- The current company model is flat. Company hierarchy and department-level permissions
  are not implemented.
- The service evaluates current directory records at a single `as_of` time; historical
  snapshots of worker, company, and contract state are not implemented.
- Rule-applicability discovery, a user interface, and a Rete-based engine are outside the
  current scope. CEL or Expr would not replace the SQL compilation needed by
  eligible-contract queries.
