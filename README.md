# FirstWork Compliance Service

A tenant-scoped compliance service built with Django REST Framework, PostgreSQL, Redis,
and a pure-Python rule engine. The MVP includes immutable rule/check versions, explainable
evaluation, SQL-backed eligible-contract queries, and API-key authentication.

<img width="1248" height="732" alt="Screenshot 2026-11-13 at 7 49 21 AM" src="https://github.com/user-attachments/assets/521bd849-3793-4125-8a41-99f604432530" />


## Start the local stack

```sh
make init
make up-async
```

The API is available at <http://localhost:8000>, liveness at `/healthz`, and dependency
readiness at `/readyz`. Interactive OpenAPI docs are at `/api/docs/` and the schema is at
`/api/schema/`. PostgreSQL is exposed on host port 5433; Redis uses 6379. The Temporal
development UI is at <http://localhost:8233>.

The first database startup initializes the `migrator` and `app_user` roles. The API and
async processes connect as `app_user`; migrations and tests use `migrator`. Both role
passwords in `docker/postgres/init.sql` are for local development only. Initialization
scripts only run when the Postgres data volume is first created.

## Commands

| Command | Effect |
| --- | --- |
| `make init` | Copy `.env.example` to `.env` without replacing an existing file |
| `make up` | Build and start PostgreSQL, Redis, Temporal, migrations, and API |
| `make up-async` | Also start the Temporal worker |
| `make benchmark` | Run the 100k-rule fixture, Temporal bulk jobs, and latency benchmark |
| `make down` | Stop services, including the async profile |
| `make logs` | Follow API logs |
| `make migrate` | Apply Django migrations as `migrator` |
| `make shell` | Open a Django shell in the API container |
| `make psql` | Connect to PostgreSQL as `migrator` (table owner; bypasses RLS) |
| `make test` | Run pytest as `migrator`; tenancy tests connect separately as `app_user` |
| `make lint` | Run Ruff and mypy |
| `make reset` | **Destructive:** stop services and delete database/workflow volumes |

`make reset` is the only provided command that removes persistent volumes. Use it only
when intentionally discarding local data.

## Tenant setup and API keys

Workers, companies, contracts, rules, checks, versions, and evaluation results are tenant
owned. API keys have the form `<tenant-uuid>.<secret>`; the database stores a hash of the
complete key and the secret is shown only once. The authenticated key determines the
tenant. Do not send tenant IDs in request bodies, query parameters, or headers.

For a local demo tenant, seed the directory and then issue a key:

```sh
docker compose exec api python manage.py seed_demo
docker compose exec api python manage.py create_api_key <tenant-uuid> --name local
```

Use the printed key as a bearer token:

```sh
curl -H "Authorization: Bearer <tenant-uuid>.<secret>" \
  http://localhost:8000/api/v1/workers
```

The demo seed command creates a new set of directory records each time it runs; use it
once per demo tenant.

## API overview

All endpoints are under `/api/v1` and require a bearer API key.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST`, `GET` | `/workers` | Create a worker or list workers |
| `GET` | `/workers/{id}` | Read a worker |
| `POST`, `GET` | `/companies` | Create a company or list companies |
| `GET` | `/companies/{id}` | Read a company |
| `POST`, `GET` | `/contracts` | Create a contract or list contracts |
| `GET` | `/contracts/{id}` | Read a contract |
| `POST` | `/rules` | Create a rule and its version 1 |
| `GET`, `PUT` | `/rules/{id}` | Read a rule or create a new immutable version |
| `GET` | `/rules/{id}/versions` | List rule versions |
| `GET` | `/rules/{id}/dependents` | List immutable rule versions that reference this rule |
| `POST` | `/rules/{id}/archive` | Archive a rule |
| `POST`, `GET` | `/compliance-checks` | Create a check or list checks |
| `GET`, `PUT` | `/compliance-checks/{id}` | Read a check or create a new pinned version |
| `POST` | `/compliance-checks/{id}/evaluate` | Evaluate a contract with explainable results |
| `POST` | `/compliance-checks/{id}/evaluate-bulk` | Start an asynchronous evaluation of tenant contracts |
| `GET` | `/evaluation-jobs/{id}` | Read bulk job status and progress |
| `GET` | `/companies/{id}/eligible-contracts` | Query contracts passing a rule or check |

Collection reads use signed keyset cursors (`limit` defaults to 100 and is capped at 1000).
The cursor is bound to the tenant and resource; it cannot be reused for a different list
or eligibility query. Errors use `application/problem+json`.

The assignment's `User` entity is represented by the `Worker` model. Property entity
prefixes are case-insensitive, and `user.` is accepted as an alias for `worker.`; inputs
such as `User.age` are canonicalized to `worker.age` before validation and storage. API
responses, traces, and validation paths use the canonical property name.

Create a rule:

```json
{
  "name": "Minimum working age",
  "definition": {
    "op": "OR",
    "children": [
      {
        "op": "AND",
        "children": [
          {"property": "contract.country", "operator": "==", "value": "DE"},
          {"property": "worker.age", "operator": ">=", "value": 18}
        ]
      },
      {
        "op": "AND",
        "children": [
          {"property": "contract.country", "operator": "==", "value": "FR"},
          {"property": "worker.age", "operator": ">=", "value": 16}
        ]
      }
    ]
  }
}
```

Rule and check updates require `If-Match` with the current ETag. Each rule version is
immutable. A check version pins the exact immutable rule versions selected when it is
created, so later rule edits do not change existing check behavior. Evaluations support
`Idempotency-Key` and return a condition-level trace and the `as_of` instant used.
The evaluation response keeps the three-state `outcome` and adds `compliant`, which is
true only for COMPLIANT. A false value can mean either NON_COMPLIANT or INDETERMINATE.

### Rule references and staged rollout

A group child may refer to a reusable rule by ID and optional version:

```json
{
  "name": "German adult worker",
  "definition": {
    "op": "AND",
    "children": [
      {"rule_ref": {"rule_id": 42}},
      {"property": "User.nationality", "operator": "==", "value": "DE"}
    ]
  }
}
```

When omitted, the target's current version is resolved at creation and that exact version
is stored. Newly written definitions use schema version 2; existing schema-version-1 rows
are not backfilled and remain readable. Expansion happens before interpretation and SQL
compilation, and the 20-level/200-node limits apply to the expanded expression. Updates to
a referenced rule never change a parent; create a new parent version to move its pin.
Already-pinned versions continue to evaluate after the target is archived. The dependents
endpoint shows direct parent versions and the referenced version; condition traces include
a `via` chain such as `["42@1"]`.

Reference creation is disabled by default (`RULE_REFERENCES_ENABLED=false`). For rollout,
deploy the reader/evaluator version with creation disabled across the service, then set the
flag to `true` to enable creating references. Before rolling back after enabling creation,
disable it and verify that no stored definition contains a reference; older code cannot
read those definitions.

The engine supports `==`, `!=`, `>`, `<`, `>=`, and `<=` where the property type permits
them; nested AND/OR trees are validated with bounded depth and node counts. Missing
context values evaluate as UNKNOWN, and check outcomes are COMPLIANT,
NON_COMPLIANT, or INDETERMINATE. Age is derived from date of birth using completed years;
February 29 birthdays are observed on February 28 in non-leap years.

### Bulk evaluation jobs

Start a job to evaluate the authenticated tenant's contracts using the check version
current at submission time:

```sh
curl -X POST -H "Authorization: Bearer <api-key>" \
  http://localhost:8000/api/v1/compliance-checks/12/evaluate-bulk
```

The API returns `202 Accepted` with a job ID and a `Location` header. Poll
`GET /api/v1/evaluation-jobs/{id}` for `status`, `completed_contracts`,
`total_contracts`, and `progress_percent`. The job pins the check version and captures
the maximum contract ID at submission; contracts created afterwards are not included.
Results are stored per `(job, contract)` and batch cursor updates commit atomically with
those result upserts, so Temporal activity retries do not duplicate results or progress.
Job records and results are protected by tenant RLS and same-tenant foreign keys.

Run the Temporal worker with the async profile (`make up-async`). Batches use keyset
pagination on contract ID and evaluate against the job's fixed `as_of` timestamp.

The pytest suite tests the API and database activities without requiring the long-running
worker. `tests/integration/test_temporal_workflow.py` also runs the actual workflow against
Temporal's SDK test server, verifies retry after a transient activity failure, and checks
that processing continues through completion. The SDK test server is downloaded and
cached automatically on first run; the test does not use the Docker Compose Temporal
development service.

## 100k-rule and scale behavior

Run the end-to-end benchmark with:

```sh
make benchmark
```

This starts the API and Temporal worker, creates a uniquely named tenant with 100,000
rules, 1,000 workers, 20 companies and 2,000 contracts, then runs two compliance
checks (20 pinned rules each) as concurrent Temporal bulk jobs. The benchmark creates
adult workers so the additional-contract scenario has sufficient eligible candidates.
It creates one job per check; each job evaluates all 2,000 contracts against its own pinned rules,
producing 4,000 contract-check results in total. The 100,000 rules provide the scale
fixture, but each check evaluates only its 20 pinned rules. The sample rules cover worker,
company and contract properties. The benchmark waits for both jobs and checks that every
contract has a persisted result before reporting fixture counts, per-job worker duration
and aggregate throughput. After both jobs finish, it selects 50 distinct workers who
are older than 18 and have an active contract, copies one active contract per worker,
and runs a third Temporal job using the first check. The new job evaluates all 2,050
contracts, including each of the 50 new ones; the original jobs retain their 2,000
contract snapshots. The JSON report includes the new contract IDs and the third job
under `additional_contract_scenario`, separate from the initial throughput, followed
by the existing latency and query-plan report.

Age is in completed calendar years (February 29 birthdays are observed on February 28
in non-leap years). A contract is active when its start is at or before selection time
and its end is after it. New contracts copy the selected source's company, country and
dates. If fewer than 50 distinct eligible workers exist, the scenario fails without
creating any additional contracts. This scenario uses the newly generated tenant, not
an arbitrary existing tenant; each benchmark invocation creates a separate fixture.
The benchmark leaves its tenant, jobs and results in the database; it never deletes
existing data. The default timeout is 30 minutes per phase;
a timeout does not cancel already-started workflows.

To run a single check and bulk job instead, invoke the command directly:

```sh
make up-async
docker compose exec -T api python manage.py run_temporal_benchmark --checks 1
```

For a smaller smoke test or a custom timeout, start the async services with `make
up-async` and invoke the command directly, for example:

```sh
docker compose exec -T api python manage.py run_temporal_benchmark \
  --rules 20 --contracts 50 --checks 2 --iterations 1 --timeout 120
```

The worker count is capped at 1,000 and the company count at 20, so small contract
counts produce smaller fixtures. `--rules-per-check`, `--poll-interval` and the other
counts are also configurable. Fewer than 50 contracts require the explicit
`--skip-additional-contract-scenario` option; the scenario is then omitted from the
benchmark report (its value is `null`). These smaller fixtures are not 100k-scale
measurements.

The benchmark fixture generator defaults to 100 tenants × 1,000 rules (100,000 rules
total) and 100 tenants × 10,000 contracts (one million contracts total):

```sh
docker compose exec api python manage.py generate_benchmark_data
```

Rule versions remain tenant-owned and indexed; API reads and evaluation load only the
requested rules or the versions pinned by the selected check. A check does not load all
100,000 rules. The pure-Python engine evaluates a validated definition tree, while
eligible-contract queries compile registry-approved properties and parameterized values
to SQL and use the tenant/company/id index plus keyset pagination. SQL identifiers are
never accepted from user input.

Measure the first generated tenant's 20-rule evaluation and eligibility query, including
PostgreSQL `EXPLAIN ANALYZE` output:

```sh
docker compose exec api python manage.py run_compliance_benchmark \
  --tenant benchmark-0000 --iterations 50
```

To exercise the expanded-tree path, first enable rule-reference creation for a reader-ready
deployment and create a composite rule, then pass its ID:

```sh
docker compose exec api python manage.py run_compliance_benchmark \
  --tenant benchmark-0000 --composite-rule-id <rule-id> --iterations 50
```

The composite rule is included in the 20-rule evaluation and used for eligibility SQL.
The report includes the expanded-rule cache hit rate as well as the measured p95 values
and query plan.

The command reports measured p95 values for the local fixture and query plan; results
depend on hardware, database state, and fixture size. The assignment's targets (20-rule
evaluation p95 under 100 ms and eligible-contract first-page p95 under 500 ms over one
million contracts) are not claimed until measured on that scale. Use a fresh database or
a unique `--prefix` when generating the fixture; the generator is intended for one run per
prefix.

## Tenancy and data integrity

Tenant identity is derived from an API key. `TenantContextMiddleware` validates the key,
sets the tenant ID transaction-locally, and rejects unauthenticated `/api/v1/` requests.
Every tenant-owned table uses PostgreSQL row-level security with `FORCE ROW LEVEL
SECURITY`; the runtime `app_user` is not a table owner and does not have `BYPASSRLS`.
Composite foreign keys prevent contracts and pinned rule versions from crossing tenants.
Immutable rule/check version rows and their pin records are protected by database
triggers as well as application behavior.

## Configuration and testing

`.env.example` documents the local environment. `make init` creates `.env`, which must not
be committed. Settings fail fast when required values are missing. `DATABASE_URL` is the
runtime connection; the migrate service overrides it with the `migrator` URL.
`APP_DATABASE_URL` is used by the RLS test suite to open a separate runtime-role
connection.

Run the integration/unit/parity/tenancy suites and static checks:

```sh
make test
make lint
```

`make test` includes the Temporal SDK workflow test and therefore needs network access the
first time the SDK's test server binary is downloaded. Subsequent runs use its local cache.

The interpreter/SQL parity tests use Hypothesis-generated nested nullable string rules.
The RLS suite uses the actual runtime database role and also checks raw SQL isolation and
cross-tenant foreign keys.

## Development boundaries

- The rule engine in `domain/engine` is independent of Django persistence.
- New references are persisted with a tenant-protected dependency relation; no existing
  rule data is migrated or backfilled.
- Cache failures fall back to the database; evaluation results are idempotent per tenant.
- Bulk evaluation runs through the Temporal worker. Webhook delivery, shared system
  rules, and production deployment remain deferred.
