# Architecture

## Shape of the system

A modular monolith carries the transactional workflow, permissions and audit.
Every engineering calculation runs behind a durable job queue, so reconstruction,
CAM and simulation can be scaled or isolated without restructuring the API
(PRD 7.2).

```
                      +------------------------------------------+
  Browser ----------> | Web application (React, WebGL viewer)     |
                      +---------------+--------------------------+
                                      | REST + server-sent events
                      +---------------v--------------------------+
                      | API and orchestration (FastAPI)          |
                      |  identity - RBAC - workflow - idempotency|
                      |  gate policy engine - events - jobs      |
                      +---+-------------------------+------------+
                          |                         |
          +---------------v----------+   +----------v----------------+
          | Services                 |   | Compute worker(s)         |
          | evidence - geometry      |   | reconstruction - planning |
          | planning - verification  |   | CAM - simulation - post   |
          | economics - release      |   +----------+----------------+
          | learning - policy        |              |
          +---------------+----------+              |
                          |                         |
          +---------------v-------------------------v----------------+
          | Deterministic engines                                    |
          | geom2d - partmodel - cutting - toolpath - stock          |
          | kinematics - collision - simulate - ir - post - validate |
          | planner - optimize - cost                                |
          +---------------+------------------------------------------+
                          |
          +---------------v------------------------------------------+
          | Data platform                                            |
          | PostgreSQL (metadata, audit, events)                     |
          | Object store (artifacts, IR, NC, release packages)       |
          +----------------------------------------------------------+
```

Nothing in `app/engines` consults a language model. Geometry, kinematics,
collision, cutting parameters, postprocessing and validation are computed from
explicit inputs, so the same inputs always produce the same result.

## Layer responsibilities

| Layer | Package | Responsibility |
| --- | --- | --- |
| Experience | `frontend/src` | Workflow, visualisation, controlled edits, explanations, approvals |
| API | `app/api` | Authorisation at object and action level, state transitions, idempotency |
| Services | `app/services` | Business rules, versioning, gates, audit and events |
| Engines | `app/engines` | Deterministic engineering calculation |
| Parsers | `app/parsers` | Bounded readers for untrusted source files |
| Jobs | `app/jobs` | Durable queue, worker loop, progress and cancellation |
| Data | `app/models` | The canonical manufacturing model of PRD 8.1 |

## The digital thread

Every object records what it was produced from, and the hash of that input:

```
SourceArtifact --> EvidenceObservation --> GeometryVersion --> ManufacturingPlan
                                                 |                     |
                                                 |                     +--> ToolpathVersion
                                                 |                     +--> SimulationRun
                                                 |                     +--> CostEstimate
                                                 |                     +--> NCProgram --> Release
                                                                                            |
                                                                          MachineRun <------+
                                                                          InspectionResult
                                                                          RuleProposal
```

`app/core/staleness.py` stores these as edges. When an upstream object gains a
new version, everything transitively downstream is marked stale and refused at
the release gate. A geometry edit therefore invalidates the plan, its toolpaths,
its simulation, its cost and its NC candidate in one transaction, so the engineer
sees exactly what a "small" change cost.

## Versioning

* `SourceArtifact` is immutable. A correction is a new artifact plus a
  `supersedes` relationship.
* A geometry edit creates revision N+1. N is preserved and still referenced by
  anything built from it.
* Machines, tools, fixtures, materials and postprocessors are versioned master
  data. A plan stores the exact version it used, so a later shop change cannot
  silently alter a historical plan.
* A released NC program is never edited. A change produces a new candidate and a
  new release revision; the prior release becomes `Superseded`, not deleted.

## Simulation identity

A simulation result is only meaningful for the exact inputs it ran against, so
its identity hash covers the engine version, the numeric tolerances, the voxel
pitch, and the hashes of the machine, fixture, geometry, plan, toolpaths and
tools. Change any of them and the previous result no longer applies.

## Stock modelling

The authoritative stock is a voxel occupancy grid in part coordinates, so
material carries correctly across a part flip or an indexed 3+2 setup. Within
one setup the tool axis is fixed, so the voxels are projected into a height
field, cutting updates the height field, and the result is written back when the
setup closes.

Conformance is checked two ways, because each catches what the other cannot:

* the **height field** comparison finds sub-millimetre surface deviation as seen
  from +Z, which is where tolerance lives;
* the **occupancy** comparison finds material left or removed from any
  direction, which is what a flipped or indexed setup needs.

## Trust boundaries

* Uploaded files are untrusted. Parsers are bounded in input size and iteration
  count, run inside a worker, and return a recorded failure rather than raising.
* Postprocessors are **declarative data**, not code. The runtime interprets
  format specifications and templates; a compromised post can change the shape
  of the output but cannot execute anything on the host.
* Tenant scoping is enforced on every query, on object storage paths, and in the
  route dependencies. An object in another tenant is reported as not found
  rather than forbidden, so the API does not confirm that an identifier exists.
* Released packages are content hashed and signed. Any modification invalidates
  the package hash and the contents become an uncontrolled copy.
* Cloud services never command machine motion. Machine connectivity, when added,
  goes through an edge gateway.

## Technology choices and why

| Component | Choice | Reasoning |
| --- | --- | --- |
| API | FastAPI, OpenAPI-first | Typed contracts and a published schema the web app is written against |
| Metadata | PostgreSQL (SQLite for development) | Transactional integrity for approvals and versioning |
| Large payloads | Content-addressed object store | No CAD, image or NC blobs in relational tables |
| Numerics | NumPy | Voxel and height-field work is array-shaped; the hot paths are vectorised |
| Geometry | Purpose-built 2.5D kernel | Honest about the MVP support matrix; a commercial kernel is a build-or-buy decision (PRD 15.3) |
| Viewer | three.js over the engine's own height field | The engineer sees exactly what the verification engine compared against |
| Jobs | Database-backed queue | Survives a restart; retry-safe because inputs are immutable and outputs content addressed |

## What is deliberately not here

The MVP support matrix (PRD Appendix A) is enforced in code, not in prose:

* Simultaneous 5-axis, turning and mill-turn are not modelled. A machine whose
  kinematics cannot reach a feature's access direction is rejected with a cause
  code.
* Photogrammetric reconstruction is a stub that is honest about being one: shape
  can be proposed from images, absolute size cannot, and geometry without a
  traceable dimension is refused by the geometry gate.
* Free-form surface machining is recognised and flagged for engineering review
  rather than planned automatically.
