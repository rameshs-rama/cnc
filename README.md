# AI CNC Manufacturing Intelligence Platform

An engineering copilot for CNC milling. It turns incomplete component
information into a safe, reviewable manufacturing plan, preserves the evidence
behind every engineering value, exposes uncertainty, compares feasible machines,
simulates the chosen plan, estimates time and cost, and generates a
machine-specific NC program **only after a qualified engineer signs for it**.

It is not a machine controller. Photo-derived geometry is provisional,
deterministic services perform every engineering calculation, and a human
remains accountable for dimensions, workholding, tooling, postprocessing and
release.

[![CI](https://github.com/rameshs-rama/cnc/actions/workflows/ci.yml/badge.svg)](https://github.com/rameshs-rama/cnc/actions/workflows/ci.yml)

---

## Run it

```bash
git clone https://github.com/rameshs-rama/cnc.git
cd cnc
make setup
make api      # http://localhost:8000/docs
make web      # http://localhost:5173
```

Sign in as `manufacturing@example.com` with `Pilot2026!`. The demo tenant seeds
a pilot factory — two qualified machines, ten tool assemblies, two fixtures,
three materials and two FANUC posts — plus a benchmark bracket with real CAD, a
drawing, a DXF and first-piece measurements in `samples/`.

To watch the whole controlled workflow run in one go:

```bash
make walkthrough
```

That script performs intake, reconstruction, engineering review, planning, CAM,
simulation, costing, postprocessing, release and production feedback, and prints
what each gate did. It is the Definition of Done from the specification,
executable.

For the pilot stack (PostgreSQL, API, compute worker, web):

```bash
cp .env.example .env     # then set the two secrets it names
make up
```

---

## What it actually does

### Evidence before inference

Every manufacturing attribute stores its value, unit, source artifact,
extraction method, confidence, uncertainty, verification status, criticality and
disposition. Nothing in the system can assert a dimension without one of these
records behind it.

Sources are ranked. Verified CAD and PMI outrank drawings, which outrank
measurements, scans, calibrated photographs, ordinary photographs and finally AI
inference. When two sources disagree beyond their stated uncertainty, the
platform raises a conflict, names the authoritative source — and then waits for
a human. It never picks silently.

The parsers are real. A STEP file yields its declared units, its bounding box
from the point cloud, and its Z-axis cylindrical surfaces as hole candidates. A
PDF drawing yields toleranced callouts (`⌀12.02 +0.02 -0.00`), thread
designations, surface finish and the general tolerance class. A DXF yields the
outline profile and the hole pattern. A measurement CSV normalises units
explicitly and rejects a row it cannot read rather than rounding it into
something usable.

### Deterministic engineering

```
Vc, fz from the validated material rule
  -> n = 1000·Vc / (π·D),  clamped to machine and tool limits
  -> chip thinning correction below half-diameter engagement
  -> vf = n · z · fz,      clamped to the machine feed limit
  -> kc = kc1.1 · h^-mc    (Kienzle)
  -> P  = MRR · kc / 60e6 / η
  -> depth backs off deterministically until P fits the spindle curve
```

Every returned value carries the rule that produced it and every clamp that was
applied, so the CAM studio can answer "why this speed" from stored data rather
than a narrative.

The same discipline runs through the rest: trapezoidal velocity profiles for
cycle time, a voxel stock model that carries material across a part flip, a tool
modelled as stacked cylinders so **holder** crashes are detectable, and a
postprocessor that is declarative data rather than executable code.

### Gates that mean something

| Gate | Blocks | Example refusal |
| --- | --- | --- |
| Geometry approval | Process planning | "No traceable dimension established the scale of this geometry" |
| Plan approval | Toolpath generation | "No workholding concept is assigned to the plan" |
| Simulation pass | Postprocessing | "flute of T03-EM10 intersects strap clamp by 14.17 mm at 8.567 s" |
| Post validation | NC release | "Post FANUC-VMC02 is certified for VMC-02 but the plan targets VMC-01" |
| NC release | Download as released | "The approver also signed an upstream gate on this project" |

An S1 stop cannot be waived. That is an invariant of the finding object itself,
not a check in one code path — a forged or mistaken waiver on an S1 still
blocks, and there is a test that asserts it.

### A digital thread that invalidates itself

Change a pocket depth after simulation, and the plan, its toolpaths, its
simulation, its cost estimate and its NC candidate all become stale in the same
transaction. The release gate then refuses all of them. The engineer sees
exactly what a small change cost, rather than discovering it at the machine.

### Release as a real signature

Signing requires the release role, a verified TOTP second factor, a fully signed
checklist, and a gate that passes **at signing time** — the manifest hash is
recomputed and compared, so a candidate prepared an hour ago cannot be signed if
anything changed since. The result is a zip containing the manifest, the
signature, the NC programs, the IR they were built from and their validation
reports. Anything not in a released state downloads watermarked
`UNCONTROLLED COPY — NOT FOR PRODUCTION`.

---

## Repository layout

```
backend/
  app/
    api/v1/       HTTP routes: auth, projects, geometry, factory, planning,
                  verification, production, platform
    core/         enums, RBAC, security, hashing, storage, events, audit,
                  staleness graph, errors
    engines/      deterministic engineering — geom2d, partmodel, cutting,
                  toolpath, stock, kinematics, collision, simulate, planner,
                  optimize, cost, ir, post, ncvalidate
    parsers/      STEP, STL, DXF, PDF drawing, measurement CSV
    services/     evidence, geometry, planning, verification, postprocess,
                  release, economics, learning, policy, workflow
    models/       the canonical manufacturing model
    jobs/         durable queue and compute worker
    seed/         pilot factory and demo tenant
  tests/          79 tests: engines, parsers, NC validation, acceptance, API
frontend/src/     React workspaces and the three.js viewer
scripts/          end-to-end walkthrough
samples/          benchmark CAD, drawings and measurements
docs/             architecture, safety model, API, deployment, PRD traceability
```

## Testing

```bash
make test         # 79 tests
make lint         # ruff + tsc
make walkthrough  # end-to-end controlled workflow
```

The engine tests assert against analytic values a manufacturing engineer could
check by hand — a 50 × 30 rectangle offset inward by 5 has an area of exactly
800 mm², a 100 × 60 × 20 block is 120 000 mm³, `n = 1000·Vc/(π·D)` holds
exactly, a trapezoidal move of 100 mm at 100 mm/s with 1000 mm/s² takes 1.1 s.
The acceptance tests are the specification's own scenarios, each asserting the
refusal rather than only the happy path.

## Documentation

| Document | Contents |
| --- | --- |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System shape, digital thread, versioning, stock model, trust boundaries |
| [docs/SAFETY.md](docs/SAFETY.md) | Authority hierarchy, severity model, gate matrix, what the system refuses to do |
| [docs/API.md](docs/API.md) | Route map, error codes, event topics, worked example |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Local, pilot and production profiles; the pre-production checklist |
| [docs/PRD-TRACEABILITY.md](docs/PRD-TRACEABILITY.md) | Every requirement mapped to where it lives, and what is deferred |

## Scope

Supported in this build:

* Prismatic and 2.5D components; moderate 3D surfaces via height-field finishing
* 3-axis and indexed 3+2 vertical milling
* Aluminium, mild steel and a stainless grade with engineer-entered rules
* Faces, pockets, slots, holes, counterbores, steps, chamfers; threads and
  tight-tolerance bores require confirmation
* FANUC on a certified machine and post pair

Not in this build, and refused rather than approximated:

* Simultaneous 5-axis, turning and mill-turn
* Micron-level reconstruction from ordinary photographs
* Automatic tolerance or material invention
* Universal posts without machine-specific validation
* Unattended machine start or autonomous correction

The two capabilities left as integrations — a photogrammetry pipeline and a
commercial CAD/CAM kernel — are the two the specification itself identifies as
build-or-buy decisions. Both sit behind honest interfaces: the geometry gate
refuses unscaled photo-derived geometry instead of inventing dimensions, and
features outside the supported set are classified as manual planning instead of
being approximated. Substituting a real pipeline or kernel behind those
interfaces does not change the safety model or the workflow.

## Licence

Apache 2.0. See [LICENSE](LICENSE).
