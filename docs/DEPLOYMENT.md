# Deployment

Two profiles run the same signed images: a managed cloud pilot and a private
cluster. The only differences are the database endpoint, the object store and
the network policy.

## Local development

```bash
make setup      # virtualenv, backend and frontend dependencies
make api        # API on :8000, docs at /docs
make web        # web application on :5173
make test       # backend test suite
make walkthrough  # the full controlled workflow against a scratch database
```

The default database is SQLite under `var/`. It is for development only.

## Pilot stack

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(48))"   # do this twice
# put the two values into MIP_JWT_SECRET and MIP_RELEASE_SIGNING_SECRET
make up
```

That starts PostgreSQL, the API, a compute worker and the web application.
`docker compose` refuses to start if either secret is unset — a deployment must
not silently inherit a development key that can forge a release signature.

| Service | Purpose | Scaling |
| --- | --- | --- |
| `db` | Transactional metadata, audit, events | Vertical; managed Postgres in cloud |
| `api` | HTTP contract, gates, workflow | Horizontal behind a load balancer |
| `worker` | Reconstruction, CAM, simulation, postprocessing | Horizontal; this is where CPU goes |
| `web` | Static bundle plus reverse proxy | Horizontal or a CDN |

The API keeps one in-process worker so a single-container deployment still
functions. In the compose profile the dedicated worker service carries the load;
set `MIP_WORKER_THREADS` to the core count you want to give it.

## Configuration

Every setting is environment driven. See `.env.example` for the full list.

| Variable | Purpose | Production note |
| --- | --- | --- |
| `MIP_DATABASE_URL` | Metadata store | Must be PostgreSQL |
| `MIP_OBJECT_STORE` | Artifact and package root | A durable, backed-up volume or an S3-compatible mount |
| `MIP_JWT_SECRET` | Session signing | At least 32 bytes, unique per environment |
| `MIP_RELEASE_SIGNING_SECRET` | Release package signature | Rotate separately from the JWT secret; a leak here forges release signatures |
| `MIP_MAX_UPLOAD_BYTES` | Untrusted input guard | Tune to the largest legitimate CAD file |
| `MIP_WORKER_THREADS` | Compute concurrency | Match the CPU allocation of the worker container |
| `MIP_SEED_DEMO` | Demo tenant on first boot | Set to `false` in production |
| `MIP_CORS_ORIGINS` | Allowed browser origins | Exact origins, never `*` |

## Before a production pilot

1. Set `MIP_SEED_DEMO=false` and remove the demo tenant.
2. Generate fresh secrets. The defaults in `config.py` are labelled insecure and
   are for tests only.
3. Put the API behind TLS. The container listens on plain HTTP and expects a
   terminating proxy; it honours `X-Forwarded-*` via `--proxy-headers`.
4. Enrol a second factor for every user who will hold release authority. The
   release gate refuses to sign without one.
5. Register the real machines, tools, fixtures and materials, and mark machine
   geometry qualified only once it has been checked against the manufacturer
   data. An unqualified machine produces a warning on every plan.
6. Certify a postprocessor against each exact machine and controller version,
   with the hashes of the approved test programs.
7. Set the tenant NC policy: prohibited codes, the confidence threshold for
   release-critical attributes, the conflict tolerance and whether self-approval
   is permitted. Production tenants should leave self-approval off.

## Backup and retention

| What | Where | Why it matters |
| --- | --- | --- |
| Database | `db-data` volume or managed snapshots | Approvals, audit and the version graph |
| Object store | `object-store` volume | Source artifacts, IR, NC programs, release packages |

Both must be backed up together: a release manifest names hashes that only mean
something if the corresponding objects still exist. Restore drills should verify
that a released package still validates against its stored hash.

## Health and observability

* `GET /health` — liveness and readiness, used by the container health checks.
* `GET /v1/jobs` — queue depth and per-job stage, percent and diagnostics.
* `GET /v1/events` — the durable event store, replayable by sequence.
* `GET /v1/events/stream` — live server-sent events for dashboards.
* Every response carries `X-Trace-Id`, and every job records the trace that
  requested it, so a user report maps to a specific job and its logs.

## Upgrades

Schema creation is idempotent at startup. For a pilot this is sufficient. Before
the first production tenant, introduce a migration tool so that a schema change
can be reviewed and rolled back rather than applied implicitly — that is a
prerequisite of the operational readiness review in PRD E12, not an optional
refinement.
