# Safety model

The product is an engineering copilot, not a machine controller. This document
describes the mechanisms that keep it on the right side of that line, and where
each one lives in the code.

## The six non-negotiable rules

| Rule | Where it is enforced |
| --- | --- |
| Evidence before inference | `models/project.py::EvidenceObservation` — value, unit, source, authority, method, confidence, uncertainty, status, criticality and disposition are columns, not optional metadata |
| Authority hierarchy | `core/enums.py::AUTHORITY_RANKS` and `services/evidence.py::detect_for` |
| Deterministic engineering | `app/engines/*` — no model call anywhere in the package |
| Human release | `services/release.py::approve` — role, second factor, signed checklist and a re-evaluated gate |
| Traceability | `core/hashing.py`, `core/staleness.py`, `core/audit.py`, `models/verification.py::Release` |
| Fail safe | `services/policy.py` — a missing critical value produces a stop, never a default |

## Evidence authority

Lower rank wins a conflict:

| Rank | Authority |
| --- | --- |
| 1 | Verified CAD and PMI |
| 2 | Drawing |
| 3 | Measurement |
| 4 | Scan |
| 5 | Calibrated photograph |
| 6 | Ordinary photograph |
| 7 | AI inference |

A parser never assigns rank 1. A STEP file is classified as a scan until an
engineer promotes it with a recorded reason, because "this file is the released
model" is a claim about process, not about file format.

Two observations conflict when their uncertainty intervals do not overlap and
the gap exceeds the tenant tolerance. The authoritative observation is
identified, shown, and left for a human to disposition. The system never picks
silently.

## Severity model

| Severity | Meaning | Behaviour |
| --- | --- | --- |
| S1 Stop | Collision, overcut, axis violation, wrong units, uncertified post | Cannot be waived |
| S2 Engineer resolution | Manufacturing-critical uncertainty or constraint breach | Blocks, but an authorised engineer may waive with a recorded rationale |
| S3 Warning | Likely performance or quality issue | Must be acknowledged; does not block |
| S4 Advisory | Optimisation opportunity | Does not block |

The S1 invariant lives in the `Finding.blocking` property itself rather than
only in the waiver-application path, so a finding constructed or mutated
anywhere else still cannot be waived. `tests/test_acceptance.py` asserts this
directly.

## Gate matrix

| Gate | Must be true | Blocks |
| --- | --- | --- |
| Geometry approval | Units, scale, stock envelope, and every release-critical feature verified or formally waived | Process planning |
| Plan approval | Material, machine, workholding, setup datums and supported operations complete | Toolpath generation |
| Simulation pass | No collision, overcut, axis violation or unresolved severity-one warning | Postprocessing |
| Post validation | Exact machine model, controller and post version certified as a pair | NC release |
| NC release | Approver holds release authority, checklist signed, artifacts unchanged since simulation | Download as released |
| Production completion | Actual run linked, first-piece disposition recorded | Learning eligibility |

Gates are evaluated from persisted state, not from what the caller claims, and
the release gate is re-evaluated **at signing time**. A candidate prepared an
hour ago cannot be signed if anything changed since: the manifest hash is
recomputed and compared against the one the candidate was prepared with.

## What the system refuses to do

* Produce scaled manufacturing geometry from photographs with no traceable
  dimension or calibration reference.
* Select tapping for a thread whose pitch is not established.
* Plan a machine that cannot reach a feature's access direction, without naming
  the exact binding constraint.
* Postprocess with a post that is uncertified, disabled, revoked, or certified
  for a different machine or controller.
* Release a program produced from a different simulation than the one that
  passed.
* Accept a release signature without the release role, a verified second factor,
  and a fully signed checklist.
* Attach production telemetry to an approximately matching program.
* Change a production rule because a model suggested it.

Each of these has a test in `backend/tests/test_acceptance.py` that asserts the
refusal, not just the happy path.

## NC program safeguards

`app/engines/ncvalidate.py` re-reads the emitted program the way a controller
would, rather than trusting the generator that produced it — a bug in the post
is exactly the class of fault this is meant to catch. It checks:

* units, working plane, positioning mode and feed mode **as they stood at the
  first motion block**, because a safe start that only appears afterwards has
  protected nothing;
* a work offset commanded before motion, and allowed on that machine;
* every tool present in the magazine, with length compensation applied before
  the first cutting move;
* the spindle running and coolant commanded before cutting;
* the programmed envelope inside machine travel, and consistent with the
  envelope the simulation observed;
* prohibited codes, macro variables, macro flow control and subprogram calls — a
  released program must be fully determined by its own text;
* the release identity embedded in the header, and a reachable program end.

Incremental blocks and reference returns are excluded from the envelope and
travel checks, because under `G91` the axis words are distances, not
coordinates.

## Collision model

The cutter is only the first segment of the tool. Shank, extension and holder
are modelled as further stacked cylinders, which is what makes a holder crash —
the most common real collision — detectable rather than invisible. Each sampled
point along every move is tested against:

* the remaining stock, separately for the cutting and non-cutting segments;
* every fixture, jaw and clamp solid, with the fixture's declared clearance;
* the machine travel envelope.

A single crash produces thousands of samples, so events are collapsed by cause
and counted rather than repeated. The first occurrence keeps its exact time,
position, penetration depth and the named entities involved.

## Proof-out

The software does not replace the factory proof-out procedure. The release
checklist requires the approver to confirm, physically, that the workholding and
tool list match the model, and that first proof-out will run single block with
feed override and first-piece inspection.
