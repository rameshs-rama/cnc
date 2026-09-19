# PRD traceability

Where each requirement of the product specification is implemented, and what is
deliberately left for a later phase. This is a map, not a claim of completeness:
the "Status" column is honest about partial work.

Legend — **Done**: implemented and tested. **Partial**: implemented within a
stated limit. **Deferred**: not in this build, with the reason.

## 4.1 Project intake and evidence

| ID | Requirement | Status | Where |
| --- | --- | --- | --- |
| FR-INT-001 | Tenant-scoped project with part number, revision, quantity, units, use, material, due date | Done | `models/project.py`, `api/v1/projects.py` |
| FR-INT-002 | Accept JPG, PNG, MP4, STEP, STL, OBJ, DXF, PDF, CSV; quarantine unsupported | Done | `parsers/detect.py`, `parsers/registry.py` |
| FR-INT-003 | Hash every artifact, preserve bytes, record uploader, timestamp, type, provenance | Done | `services/evidence.py::ingest_artifact` |
| FR-INT-004 | Classify authority; engineer may override with a reason | Done | `services/evidence.py::override_authority` |
| FR-INT-005 | Extract drawing text and dimensions as candidates, never verified values | Done | `parsers/drawing.py` |
| FR-INT-006 | Detect conflicting values and create resolution tasks | Done | `services/evidence.py::detect_for` |

## 4.2 Guided capture and reconstruction

| ID | Requirement | Status | Where |
| --- | --- | --- | --- |
| FR-CAP-001 | Guided capture with calibration, blur, glare, overlap and coverage checks | Partial | Session, target, region coverage and completeness are modelled (`models/project.py::CaptureSession`); image quality analysis is deferred — it needs a vision pipeline, not a stub that would produce numbers nobody should trust |
| FR-CAP-002 | Live completeness by face or region; request missing views | Done | `api/v1/projects.py::upload_artifact`, `services/geometry.py::_coverage_report` |
| FR-CAP-003 | Extract video frames and deduplicate near-identical images | Deferred | Requires the same vision pipeline |
| FR-REC-001 | Calibrated cameras, point cloud, mesh and quality report | Partial | The quality report, coverage, scale source and uncertainty are produced and enforced; photogrammetry itself is an integration decision (PRD 15.3) and is not reimplemented here |
| FR-REC-002 | Fit primitives with residual error | Partial | Analytic extraction from STEP cylinders and DXF circles with confidence and residual; robust fitting from a point cloud belongs with the photogrammetry integration |
| FR-REC-003 | Editable provisional geometry without overwriting the reconstruction | Done | `services/geometry.py::edit_features` |
| FR-REC-004 | Refuse scaled geometry without a known dimension or calibration reference | Done | `services/policy.py::evaluate_geometry` (`scale_not_established`, S1) |
| FR-REC-005 | Store uncertainty and lineage at face, feature and dimension level | Done | `models/project.py::EvidenceObservation` |

## 4.3 Engineering review and feature recognition

| ID | Requirement | Status | Where |
| --- | --- | --- | --- |
| FR-ENG-001 | Confidence colours and status filters | Done | `frontend/src/pages/EngineeringReview.tsx` |
| FR-ENG-002 | Edit dimensions, tolerances, material, finish and thread | Done | `api/v1/geometry.py::edit_features` |
| FR-ENG-003 | Show source, method, confidence, residual and verifier per value | Done | `services/geometry.py::confidence_map` |
| FR-ENG-004 | Block approval on a critical unknown unless waived with rationale | Done | `services/policy.py::evaluate_geometry` |
| FR-FTR-001 | Recognise faces, holes, counterbores, pockets, slots, steps, bosses, chamfers, fillets, threads | Done | `core/enums.py::FeatureType`, `engines/partmodel.py` |
| FR-FTR-002 | Stable feature ids across revisions | Done | `services/geometry.py::stable_key` |
| FR-FTR-003 | Classify supported, partially supported or manual | Done | `engines/partmodel.py::Feature.support` |

## 4.4 Factory twin and tool crib

| ID | Requirement | Status | Where |
| --- | --- | --- | --- |
| FR-MCH-001 | Machine model, controller, travels, kinematics, spindle curve, rapids, accelerations, magazine, coolant, rate | Done | `models/factory.py::MachineVersion` |
| FR-MCH-002 | Version configuration; historical plans do not adopt later changes | Done | Versioned master data plus `core/staleness.py` |
| FR-MCH-003 | Validated machine geometry and post certification | Done | `MachineVersion.geometry_qualified`, `services/postprocess.py::certify_post` |
| FR-TOL-001 | Assembled tool with gauge length and collision geometry | Done | `models/factory.py::ToolAssemblyVersion`, `engines/collision.py::build_segments` |
| FR-TOL-002 | Availability, location, maximum rpm, remaining life | Done | `ToolAssemblyVersion` |
| FR-TOL-003 | Recommend available tools; quantify substitution penalty | Done | `engines/planner.py::select_tool`, `api/v1/factory.py::tool_availability` |
| FR-FIX-001 | Fixture, vise, jaws, clamps, stock and clearance geometry | Done | `models/factory.py::FixtureVersion` |

## 4.5 Planning, CAM, optimisation and simulation

| ID | Requirement | Status | Where |
| --- | --- | --- | --- |
| FR-PLN-001 | Candidate stock, orientation, setup and operation plans | Done | `engines/planner.py::plan_candidate` |
| FR-PLN-002 | Compare feasible machines and explain infeasibility | Done | `engines/planner.py::assess_machine` |
| FR-PLN-003 | Minimum cycle time, cost, tool changes and weighted objectives | Done | `engines/optimize.py::PRESETS` |
| FR-CAM-001 | Facing, contouring, pocketing, adaptive, rest, drilling, boring, chamfering, 3D finishing, indexed 3+2 | Done | `engines/toolpath.py` |
| FR-CAM-002 | Derive rpm and feed from deterministic rules, machine limits and engagement | Done | `engines/cutting.py` |
| FR-CAM-003 | Maintain remaining stock and use it for subsequent paths | Done | `engines/stock.py`, `services/planning.py::generate_toolpaths` |
| FR-OPT-001 | Optimise order, tool reuse, linking, stepdown, stepover and orientation within hard constraints | Done | `engines/optimize.py`, `api/v1/planning.py::optimise` |
| FR-SIM-001 | Stock removal and full machine kinematics including holder, fixture, clamp and rapid collisions | Done | `engines/simulate.py` |
| FR-SIM-002 | Detect overcut, undercut, gouge, reach and axis-limit failures with the responsible segment | Done | `engines/simulate.py`, `engines/stock.py::compare_to_target` |
| FR-SIM-003 | Cycle time by cutting, rapid, dwell, tool change, spindle and indexing | Done | `engines/kinematics.py::TimeBreakdown` |

## 4.6 Cost, postprocessing, release and feedback

| ID | Requirement | Status | Where |
| --- | --- | --- | --- |
| FR-CST-001 | Material, setup, machining, tooling, inspection, labour, scrap, overhead, margin | Done | `engines/cost.py` |
| FR-CST-002 | Assumptions, currency, quantity breaks and sensitivity | Done | `engines/cost.py::estimate` |
| FR-PST-001 | Controller-neutral IR before postprocessing | Done | `engines/ir.py` |
| FR-PST-002 | FANUC output only through an enabled, versioned, machine-specific validated post | Done | `engines/post.py`, `services/policy.py::evaluate_post` |
| FR-PST-003 | Syntax, travel, tool, work-offset and prohibited-code checks | Done | `engines/ncvalidate.py` |
| FR-REL-001 | Gates plus signature, role, timestamp and approval statement | Done | `services/release.py::approve` |
| FR-REL-002 | Immutable package with hashes for every input | Done | `services/release.py::build_manifest` |
| FR-FBK-001 | Record setup time, cycle time, tool changes, alarms, scrap and inspection | Done | `services/learning.py::record_run` |
| FR-FBK-002 | Compare predicted and actual without changing production rules | Done | `services/learning.py::variance_report` |
| FR-FBK-003 | Controlled review before learned recommendations become defaults | Done | `services/learning.py::review_proposal` |

## 5 Confidence and release policy

| Section | Status | Where |
| --- | --- | --- |
| 5.1 Confidence object | Done | `models/project.py::EvidenceObservation` — every field of the table is a column |
| 5.2 Gate matrix | Done | `core/enums.py::Gate`, `GATE_BLOCKS`, `services/policy.py` |
| 5.3 Severity model | Done | `core/enums.py::Severity`; S1 non-waivability is an invariant of `Finding.blocking` |

## 6 Screens

| Workspace | Status | Where |
| --- | --- | --- |
| Portfolio | Done | `pages/Portfolio.tsx` |
| Project intake | Done | `pages/ProjectIntake.tsx` |
| Guided capture | Partial | Session API and coverage accounting exist; the camera client is deferred with FR-CAP-001 |
| Reconstruction studio | Done | `pages/EngineeringReview.tsx` (version timeline, quality report) |
| Engineering review | Done | `pages/EngineeringReview.tsx` |
| Process planner | Done | `pages/ProcessPlanner.tsx` |
| Machine selector | Done | `pages/ProcessPlanner.tsx` (machine comparison with cause codes) |
| Tool manager | Done | `pages/FactoryTwin.tsx` |
| CAM studio | Done | `pages/CamStudio.tsx` |
| Optimization studio | Partial | The search API is complete and exposed; a dedicated weight-slider screen is not built |
| Digital twin | Done | `pages/DigitalTwin.tsx` |
| Cost and quote | Done | `pages/CostQuote.tsx` |
| NC release | Done | `pages/NcRelease.tsx` |
| Production analytics | Done | `pages/Analytics.tsx` |

## 8, 9, 10 Model, API and algorithms

| Section | Status | Where |
| --- | --- | --- |
| 8.1 Canonical model | Done | Every entity in the table exists in `app/models` |
| 8.2 Versioning rules | Done | `core/staleness.py`, `services/geometry.py`, `services/release.py` |
| 9.1 API principles | Done | `api/deps.py` (idempotency, expected version, object-level authorisation) |
| 9.2 Event topics | Done | `core/events.py::Topic` — all eight published topics |
| 10.1 Reconstruction | Partial | Metrics, lineage and degraded-condition reporting are enforced; the photogrammetry pipeline is an integration |
| 10.2 Feature and planning intelligence | Done | Rule graph first; ranking cannot introduce an infeasible candidate |
| 10.3 Optimisation objective | Done | `engines/optimize.py` — all eight terms, normalised and weighted |
| 10.4 Explainability contract | Done | Decision records on plans and operations; `parameter_rationale` carries rule, alternatives, clamps and model |

## 11, 12 Non-functional and security

| Area | Status | Notes |
| --- | --- | --- |
| Reproducibility | Done | Seeded search, content-addressed outputs, simulation identity hash |
| Audit | Done | Append-only `AuditRecord` and `DomainEvent` |
| Security | Partial | Tenant isolation, RBAC, MFA for release, signed packages, sandboxed declarative posts and bounded parsers are implemented. Penetration testing, SAST and DAST are process items for the readiness review |
| Accessibility | Partial | Keyboard focus, semantic tables, and severity carried in text as well as colour. A full WCAG 2.2 AA audit is a process item |
| Observability | Partial | Trace ids, job stages, event store and health endpoint. Distributed tracing and dashboards are an operations task |
| Data lifecycle | Deferred | Retention, legal hold and export need a policy decision before implementation |

## 13.2 Acceptance scenarios

Every scenario in the table is a test in `backend/tests/test_acceptance.py`:

| Scenario | Test |
| --- | --- |
| Calibrated image reconstruction | `test_photo_only_geometry_cannot_be_approved_without_scale`, `test_scaled_reconstruction_records_its_scale_source` |
| Evidence conflict | `test_drawing_outranks_a_photo_estimate_and_the_conflict_stays_visible` |
| Unknown thread | `test_unresolved_thread_blocks_geometry_and_prevents_tapping` |
| Machine infeasibility | `test_machine_without_travel_is_rejected_with_the_binding_constraint` |
| Stock-aware rest machining | `test_rest_machining_uses_remaining_stock_and_does_not_air_cut` |
| Collision stop | `test_a_clamp_in_the_path_produces_an_unwaivable_stop` |
| Stale artifact | `test_editing_geometry_makes_everything_downstream_stale` |
| Post mismatch | `test_post_certified_for_another_machine_is_refused` |
| Release integrity | `test_release_requires_role_second_factor_and_a_signed_checklist`, `test_editing_after_release_creates_a_new_revision_and_supersedes_the_old` |
| Actual feedback | `test_actuals_link_to_the_exact_program_and_change_nothing_automatically` |

## 14 Epics

| Epic | Status |
| --- | --- |
| E1 Platform and governance | Done |
| E2 Evidence intake | Done |
| E3 Guided capture | Partial — API and coverage model; camera client deferred |
| E4 Reconstruction | Partial — geometry model, versioning and quality gating; photogrammetry integration deferred |
| E5 Engineering review | Done |
| E6 Factory twin | Done |
| E7 Planning and CAM | Done |
| E8 Simulation | Done |
| E9 Optimisation and costing | Done |
| E10 Post and release | Done |
| E11 Production feedback | Done |
| E12 Enterprise readiness | Partial — containers, CI, health, backup volumes and configuration profiles; the readiness review itself is a process |

## The honest summary

This build implements the platform, the safety model, the deterministic
engineering engines and the controlled workflow end to end, for the part family
the MVP support matrix describes: prismatic and 2.5D components, 3-axis and
indexed 3+2 vertical milling, FANUC on a certified machine and post pair.

The two capabilities it does not implement are the two the PRD itself calls out
as build-or-buy decisions in section 15.3: the photogrammetry pipeline and a
commercial CAD or CAM kernel. Both are represented by honest interfaces — the
geometry gate refuses unscaled photo-derived geometry rather than inventing
dimensions, and features outside the supported set are classified as manual
planning rather than approximated. Substituting a real pipeline or kernel behind
those interfaces does not change the safety model or the workflow.
