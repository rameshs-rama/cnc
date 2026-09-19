# API

The contract is OpenAPI-first: `/docs` serves the interactive documentation and
`/openapi.json` the schema. `make openapi` writes it to `docs/openapi.json`.

## Principles

* REST for transactional actions, an object-storage style upload session for
  large artifacts, and a server-sent event stream for job progress.
* Every mutation accepts an idempotency key and, where it matters, an expected
  version. A stale write is rejected with `version_conflict` rather than
  silently overwriting a concurrent engineering edit.
* Every response carries `X-Trace-Id`; tenant, version and lifecycle status
  appear on the objects themselves.
* Engineering calculations are asynchronous jobs. The endpoint returns `202`
  with a job, and the job exposes stage, percent and diagnostics.
* Authorisation is checked at object and action level, not only at route level.

## Errors

```json
{
  "code": "gate_blocked",
  "message": "Simulation pass did not pass; Postprocessing remains blocked",
  "detail": { "gate": "Simulation pass", "passed": false, "findings": [ ... ] },
  "trace_id": "9f2c1a44d0e7b310"
}
```

| Code | Status | Meaning |
| --- | --- | --- |
| `unauthorized` | 401 | Missing, expired or unverifiable token |
| `permission_denied` | 403 | Roles do not grant the action; `detail.missing` lists what is required |
| `not_found` | 404 | Absent, or in another tenant |
| `conflict` / `invalid_transition` | 409 | The state machine does not allow this move; `detail.allowed` lists what it does |
| `version_conflict` | 409 | The object changed since you loaded it |
| `gate_blocked` | 409 | A safety gate refused; `detail.findings` carries the reasons |
| `validation_failed` | 422 | The request is well formed but not acceptable |
| `unsupported_artifact` | 415 | No parser is registered for this file kind |

## Route map

| Method and route | Purpose |
| --- | --- |
| `POST /v1/auth/token` | Exchange credentials for an access token |
| `GET /v1/auth/me` | Identity, roles and effective permissions |
| `POST /v1/auth/mfa/enrol` | Enrol the second factor required to sign a release |
| `POST /v1/projects` | Create a project; returns id, version and `Draft` |
| `POST /v1/projects/{id}/transitions` | Move along the state machine |
| `POST /v1/projects/{id}/artifacts:initiate` | Start a resumable upload; returns limits and accepted kinds |
| `POST /v1/projects/{id}/artifacts` | Upload a source artifact |
| `PATCH /v1/projects/{id}/artifacts/{aid}/authority` | Reclassify evidence authority with a reason |
| `POST /v1/projects/{id}/capture-sessions` | Start guided capture; returns target and coverage requirements |
| `GET /v1/projects/{id}/observations` | Every value with its source, method, confidence and status |
| `POST /v1/projects/{id}/observations/{oid}/disposition` | Accept, edit, reject or waive a value |
| `GET /v1/projects/{id}/conflicts` | Open evidence conflicts |
| `POST /v1/projects/{id}/conflicts/{cid}/resolve` | Record a disposition |
| `POST /v1/geometry-jobs` | Request reconstruction against immutable artifact ids |
| `GET /v1/jobs/{id}` | Stage, percent and diagnostics; never partial geometry as approved |
| `GET /v1/geometry/{id}/confidence-map` | The engineering review payload |
| `GET /v1/geometry/{id}/surface` | Finished surface as a height field, for the viewer |
| `POST /v1/geometry/{id}/features` | Edit features into revision N+1 |
| `POST /v1/geometry/{id}:approve` | Approve for planning; runs the confidence policy |
| `POST /v1/plans:generate` | Generate route candidates for the allowed machines |
| `GET /v1/projects/{id}/machine-feasibility` | Machine comparison with cause codes |
| `POST /v1/plans/{id}/toolpaths:generate` | Generate paths, asynchronously and version locked |
| `POST /v1/optimizations` | Search candidates with bounds, locks, weights and a budget |
| `POST /v1/simulations` | Stock and machine verification; returns events, time and pass status |
| `POST /v1/simulations/{id}/dispositions` | Disposition a finding; S1 can be acknowledged, never resolved |
| `POST /v1/cost-estimates` | Cost decomposition, quantity breaks and sensitivity |
| `POST /v1/nc-programs:postprocess` | Generate an NC candidate through a certified post |
| `GET /v1/nc-programs/{id}/text` | Program text, watermarked unless released |
| `GET /v1/nc-programs/{id}/ir` | The controller-neutral IR it was built from |
| `POST /v1/releases` | Prepare a candidate and record the gate evaluation |
| `GET /v1/releases/{id}/checklist` | What the approver must sign |
| `POST /v1/releases/{id}:approve` | Sign; requires role, second factor and a complete checklist |
| `GET /v1/releases/{id}/package` | Controlled package, or an uncontrolled copy if not released |
| `POST /v1/machine-runs` | Record an actual run against the exact released program |
| `POST /v1/inspection-results` | Record measurements against a feature |
| `GET /v1/projects/{id}/variance` | Predicted against actual |
| `POST /v1/projects/{id}/rule-proposals` | Derive governed proposals from observed variance |
| `POST /v1/rule-proposals/{id}/review` | Promote or reject; the only way a rule changes |
| `POST /v1/factory/{collection}` | Create a master data version |
| `POST /v1/factory/posts/{id}:certify` | Certify a post for one machine and controller pair |
| `POST /v1/factory/posts/{id}:revoke` | Block future releases; history stays auditable |
| `GET /v1/events` / `GET /v1/events/stream` | Event store and live stream |
| `GET /v1/projects/{id}/audit` | Append-only audit trail |

## Event topics

| Topic | Produced when | Minimum payload |
| --- | --- | --- |
| `artifact.processed` | Parsing or extraction completes | artifact id, parser, status, warnings |
| `geometry.version.created` | New geometry is stored | project, geometry version, source hashes |
| `engineering.conflict.detected` | Evidence disagrees beyond policy | feature, observations, severity |
| `plan.candidate.created` | Planner produces a candidate | plan, machine, setup count |
| `simulation.completed` | Verification ends | simulation id, input hashes, result, event counts |
| `release.status.changed` | Approval, release or supersession occurs | release, prior and new state, actor |
| `machine.run.completed` | Production record closes | run, released NC id, actual time, result |
| `learning.rule.proposed` | Feedback suggests a rule | evidence set, expected impact, validation state |

`job.progress` and `project.state.changed` are also published for the user
interface; they are operational rather than part of the published contract.

## Worked example

```bash
BASE=http://localhost:8000/v1
TOKEN=$(curl -s -X POST $BASE/auth/token -H 'Content-Type: application/json' \
  -d '{"email":"manufacturing@example.com","password":"Pilot2026!"}' | jq -r .access_token)
AUTH="Authorization: Bearer $TOKEN"

PROJECT=$(curl -s -H "$AUTH" $BASE/projects | jq -r '.items[0].id')

curl -s -X POST "$BASE/projects/$PROJECT/artifacts" -H "$AUTH" \
  -F "file=@samples/cad/BRK-1042.step" | jq '{kind, authority, content_hash}'

JOB=$(curl -s -X POST $BASE/geometry-jobs -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"project_id\":\"$PROJECT\"}" | jq -r .id)
curl -s -H "$AUTH" $BASE/jobs/$JOB | jq '{status, stage, percent, result}'

GEOMETRY=$(curl -s -H "$AUTH" $BASE/projects/$PROJECT/geometry | jq -r '.[0].id')
curl -s -H "$AUTH" $BASE/geometry/$GEOMETRY/gate | jq '{passed, blocks, findings: [.findings[].code]}'
```

The gate call is the interesting one: it reports what is blocking and why,
before anyone tries to approve.
