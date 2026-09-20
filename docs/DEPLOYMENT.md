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

## Evaluation deployment on Render

`render.yaml` at the repository root is a blueprint for a temporary, shareable
deployment. Use the Deploy to Render button in the README, or point Render at the
repository and let it read the blueprint.

It defines two services:

| Service | What it is | Needed? |
| --- | --- | --- |
| `cnc-platform-api` | The FastAPI service, with `/docs` and `/health` | Yes — this alone is a working evaluation URL |
| `cnc-platform-web` | The React workspaces as a static site | Optional |

Both signing secrets use Render's `generateValue`, so no secret is typed in or
committed, and the demo tenant is seeded on first boot.

The API is self-contained. The static site needs the API's URL at **build** time,
because Vite inlines `VITE_API_BASE` into the bundle — so deploy the API first,
paste its URL into `VITE_API_BASE` on the web service, then set `MIP_CORS_ORIGINS`
on the API to the web service's URL and redeploy both. Both variables are marked
`sync: false` for exactly this reason.

**What this profile is not.** Render's free instance type has no persistent disk,
so SQLite and the object store sit on ephemeral storage and reset on every restart
or wake from idle. A release manifest names hashes whose objects would no longer
exist. That is fine for evaluating the workflow and wrong for anything else — the
blueprint carries the managed-Postgres and disk configuration to switch to, and
both require a paid instance type.

Before a real tenant touches a Render deployment, work through the checklist
below, starting with `MIP_SEED_DEMO=false`.

### Driving the deployment from an agent

`.mcp.json` at the repository root registers Render's own MCP server, so a
Claude Code session working in this repository can create the services, watch
the deploys and read the logs without anyone opening the dashboard. It is a
convenience, not a requirement — every step below has a dashboard equivalent,
and the blueprint is the source of truth either way.

It authenticates with a Render API key taken from the environment, so nothing
secret enters the repository:

```bash
export RENDER_API_KEY=rnd_...   # Render dashboard → Account Settings → API Keys
claude                          # the key is read from the environment, not from .env
```

`.env` is the wrong place for it. Claude Code expands `${RENDER_API_KEY}` from
its own process environment when it starts the server; the application's `.env`
is read by pydantic-settings inside the container and never reaches the agent.

The ordering constraint above is what the tools are for — each handoff needs a
URL that only exists after the previous step finished:

| Step | Tool | Why it is a separate step |
| --- | --- | --- |
| Pick the workspace | `list_workspaces`, then pass `workspaceId` on every later call | An account with more than one workspace is otherwise ambiguous |
| Create the API | `create_web_service` | Or let the blueprint create it; the blueprint keeps the generated secrets |
| Watch the first deploy | `list_deploys`, `get_deploy` | The image installs numpy and builds a wheel, so the first build is slow |
| Read the boot log | `list_logs` | Confirms `demo tenant … seeded` and the worker start |
| Hand the API URL to the site | `update_environment_variables` on `VITE_API_BASE` | Vite inlines it at **build** time, so it must precede the site's build |
| Hand the site URL back | `update_environment_variables` on `MIP_CORS_ORIGINS` | The browser is blocked until the API names the site as an allowed origin |
| Apply both | `trigger_deploy` | Environment changes do not rebuild a static site on their own |

Two cautions. The key is workspace-wide: these tools create, modify and delete
real infrastructure, and `query_render_postgres` reads tenant data, so use a key
scoped to the workspace you actually want an agent acting in. And the server
talks to `api.render.com` — a sandbox whose egress policy blocks `render.com`
cannot use it at all, which is a network failure rather than a credential one.

The hosted endpoint is `https://mcp.render.com/mcp`. If Render moves it, correct
the `url` in `.mcp.json`; the server can also be run locally over stdio with the
binary from `render-oss/render-mcp-server`, reading the same `RENDER_API_KEY`.

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
