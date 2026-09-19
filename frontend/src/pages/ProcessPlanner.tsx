import { useCallback, useEffect, useState } from 'react'
import {
  request,
  type Feasibility,
  type GeometryVersion,
  type Job,
  type MasterRecord,
  type Plan,
  type Project,
  type Setup,
} from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { Badge, DataTable, Empty, ErrorNote, GateCard, KeyValue, Loading, Panel, StaleBadge } from '@/components/ui'
import { JobWatcher } from '@/components/JobWatcher'
import { money, seconds, shortHash } from '@/lib/format'

const OBJECTIVES = ['balanced', 'min_cycle_time', 'min_cost', 'min_tool_changes', 'max_quality']

export function ProcessPlanner({
  project,
  selectedPlanId,
  onSelectPlan,
  onChanged,
}: {
  project: Project
  selectedPlanId: string | null
  onSelectPlan: (planId: string) => void
  onChanged: () => void
}) {
  const { can } = useAuth()
  const [plans, setPlans] = useState<Plan[]>([])
  const [setups, setSetups] = useState<Setup[]>([])
  const [feasibility, setFeasibility] = useState<Feasibility[]>([])
  const [geometry, setGeometry] = useState<GeometryVersion[]>([])
  const [materials, setMaterials] = useState<MasterRecord[]>([])
  const [fixtures, setFixtures] = useState<MasterRecord[]>([])
  const [machines, setMachines] = useState<MasterRecord[]>([])
  const [gate, setGate] = useState<any>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [objective, setObjective] = useState('balanced')
  const [materialId, setMaterialId] = useState('')
  const [fixtureId, setFixtureId] = useState('')

  const load = useCallback(async () => {
    try {
      const [planList, feasibilityList, geometryList, materialList, fixtureList, machineList] = await Promise.all([
        request<Plan[]>(`/projects/${project.id}/plans`),
        request<Feasibility[]>(`/projects/${project.id}/machine-feasibility`),
        request<GeometryVersion[]>(`/projects/${project.id}/geometry`),
        request<MasterRecord[]>('/factory/materials'),
        request<MasterRecord[]>('/factory/fixtures'),
        request<MasterRecord[]>('/factory/machines'),
      ])
      setPlans(planList)
      setFeasibility(feasibilityList)
      setGeometry(geometryList)
      setMaterials(materialList)
      setFixtures(fixtureList)
      setMachines(machineList)
      setMaterialId((current) => current || String(materialList.find((m) => m.code === project.target_material_code)?.id ?? materialList[0]?.id ?? ''))
      setFixtureId((current) => current || String(fixtureList[0]?.id ?? ''))
      if (!selectedPlanId && planList[0]) onSelectPlan(planList[0].id)
    } catch (caught) {
      setError(caught)
    }
  }, [project.id, project.target_material_code, selectedPlanId, onSelectPlan])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (!selectedPlanId) return
    void Promise.all([
      request<Setup[]>(`/plans/${selectedPlanId}/setups`),
      request<any>(`/plans/${selectedPlanId}/gate`),
    ])
      .then(([setupList, gateResult]) => {
        setSetups(setupList)
        setGate(gateResult)
      })
      .catch(setError)
  }, [selectedPlanId, plans])

  const approvedGeometry = geometry.find((g) => g.status === 'Approved')

  const generate = async () => {
    if (!approvedGeometry) {
      setError({ code: 'geometry_not_approved', message: 'Approve a geometry revision before planning' })
      return
    }
    setError(null)
    try {
      const job = await request<Job>('/plans:generate', {
        body: {
          project_id: project.id,
          geometry_version_id: approvedGeometry.id,
          material_id: materialId,
          fixture_id: fixtureId || null,
          machine_ids: [],
          objective,
        },
      })
      setJobId(job.id)
    } catch (caught) {
      setError(caught)
    }
  }

  const plan = plans.find((p) => p.id === selectedPlanId) ?? null
  const unplanned: Array<Record<string, any>> = plan?.decision_record?.unplanned ?? []

  return (
    <div className="workspace">
      {error ? <ErrorNote error={error} /> : null}

      <Panel
        title="Process planner"
        subtitle="Candidates are produced by deterministic rules; a weight can rank them but never relax a constraint."
        actions={
          can('plan:create') ? (
            <>
              <label className="inline">
                Material
                <select value={materialId} onChange={(e) => setMaterialId(e.target.value)}>
                  {materials.map((m) => (
                    <option key={String(m.id)} value={String(m.id)}>
                      {String(m.code)}
                      {m.rules_validated === false ? ' (rules not validated)' : ''}
                    </option>
                  ))}
                </select>
              </label>
              <label className="inline">
                Workholding
                <select value={fixtureId} onChange={(e) => setFixtureId(e.target.value)}>
                  {fixtures.map((f) => (
                    <option key={String(f.id)} value={String(f.id)}>
                      {String(f.code)}
                      {f.verified === false ? ' (unverified)' : ''}
                    </option>
                  ))}
                </select>
              </label>
              <label className="inline">
                Objective
                <select value={objective} onChange={(e) => setObjective(e.target.value)}>
                  {OBJECTIVES.map((value) => (
                    <option key={value}>{value}</option>
                  ))}
                </select>
              </label>
              <button type="button" onClick={() => void generate()} disabled={Boolean(jobId) || !approvedGeometry}>
                Generate candidates
              </button>
            </>
          ) : null
        }
      >
        {!approvedGeometry ? (
          <Empty
            title="No approved geometry"
            hint="Process planning is blocked until a geometry revision passes the approval gate."
          />
        ) : null}
        {jobId ? (
          <JobWatcher
            jobId={jobId}
            onDone={() => {
              setJobId(null)
              void load()
              onChanged()
            }}
            onFailed={() => setJobId(null)}
          />
        ) : null}

        <DataTable
          rows={plans}
          rowKey={(p) => p.id}
          selectedId={selectedPlanId ?? undefined}
          onRowClick={(p) => onSelectPlan(p.id)}
          empty={<Empty title="No candidates yet" />}
          columns={[
            { header: 'Candidate', cell: (p) => <strong>{p.label}</strong> },
            { header: 'Rank', cell: (p) => p.candidate_rank, width: '5rem', align: 'right' },
            {
              header: 'Status',
              cell: (p) => (
                <>
                  <Badge color={p.status === 'Approved' ? '#3fb950' : '#58a6ff'}>{p.status}</Badge>
                  {p.stale ? <StaleBadge reason={p.stale_reason} /> : null}
                </>
              ),
              width: '13rem',
            },
            { header: 'Hash', cell: (p) => <code>{shortHash(p.content_hash, 10)}</code>, width: '9rem' },
          ]}
        />
      </Panel>

      <div className="split-2">
        <Panel
          title="Machine comparison"
          subtitle="Every registered machine, with the exact binding constraint where it cannot make the part."
        >
          <DataTable
            rows={feasibility}
            rowKey={(f) => f.machine_version_id}
            empty={<Empty title="No assessment yet" hint="Generate candidates to compare machines." />}
            columns={[
              { header: 'Machine', cell: (f) => <strong>{f.machine_code}</strong>, width: '8rem' },
              { header: 'Kinematics', cell: (f) => f.kinematics ?? '—', width: '7rem' },
              { header: 'Controller', cell: (f) => f.controller ?? '—', width: '7rem' },
              {
                header: 'Rate',
                cell: (f) => (f.hourly_rate !== null ? money(f.hourly_rate) + '/h' : '—'),
                width: '7rem',
                align: 'right',
              },
              {
                header: 'Feasible',
                cell: (f) => <Badge color={f.feasible ? '#3fb950' : '#f85149'}>{f.feasible ? 'yes' : 'no'}</Badge>,
                width: '6rem',
              },
              {
                header: 'Binding constraint',
                cell: (f) =>
                  f.feasible ? (
                    <span className="muted">
                      {f.detail?.surface_speed_shortfall
                        ? `runs at ${Math.round((f.detail.surface_speed_shortfall.fraction_of_rated_surface_speed ?? 0) * 100)}% of rated surface speed`
                        : (f.detail?.warning ?? '—')}
                    </span>
                  ) : (
                    <span>
                      <code>{f.cause_code}</code> {f.binding_constraint}
                    </span>
                  ),
              },
            ]}
          />
          <p className="muted">
            {machines.length} machine version{machines.length === 1 ? '' : 's'} registered in the factory twin.
          </p>
        </Panel>

        <Panel
          title="Plan approval gate"
          subtitle="Material, machine, workholding, datums and supported operations must all be complete."
          tone={gate && !gate.passed ? 'danger' : 'default'}
        >
          {gate ? <GateCard gate={gate} /> : <Loading what="the gate evaluation" />}
          {unplanned.length ? (
            <>
              <h3>Not planned automatically</h3>
              <ul className="warnings">
                {unplanned.map((entry, index) => (
                  <li key={index}>
                    <strong>{entry.feature}</strong> — {entry.reason}
                  </li>
                ))}
              </ul>
            </>
          ) : null}
        </Panel>
      </div>

      {plan ? (
        <Panel title="Setups and operations" subtitle={plan.label}>
          <KeyValue
            rows={[
              ['stock', `${plan.stock?.min?.map((v: number) => v.toFixed(1)).join(', ')} to ${plan.stock?.max?.map((v: number) => v.toFixed(1)).join(', ')} mm`],
              ['objective', JSON.stringify(plan.objective)],
              ['strategy', plan.decision_record?.strategy ?? '—'],
              ['operation order rule', plan.decision_record?.operation_order_rule ?? '—'],
              ['stock rule', plan.decision_record?.stock_rule ?? '—'],
            ]}
          />
          {setups.map((setup) => (
            <div key={setup.id} className="setup-card">
              <header>
                <h3>
                  {setup.sequence}. {setup.name}
                </h3>
                <Badge>{setup.work_offset}</Badge>
                <span className="muted">clearance {setup.clearance_plane_mm} mm</span>
                <span className="muted">setup {seconds(setup.setup_minutes * 60)}</span>
                {Object.keys(setup.index_position ?? {}).length ? (
                  <Badge color="#d29922">indexed {JSON.stringify(setup.index_position)}</Badge>
                ) : null}
              </header>
              <p className="muted">
                Datums — primary {setup.datum_scheme?.primary ?? '?'}; secondary {setup.datum_scheme?.secondary ?? '?'};
                tertiary {setup.datum_scheme?.tertiary ?? '?'}
              </p>
              <DataTable
                rows={setup.operations}
                rowKey={(o) => o.id}
                empty={<Empty title="No operations in this setup" />}
                columns={[
                  { header: '#', cell: (o) => o.sequence, width: '3rem' },
                  { header: 'Operation', cell: (o) => o.operation_type, width: '11rem' },
                  { header: 'Label', cell: (o) => o.parameters?.label ?? '—' },
                  { header: 'Features', cell: (o) => o.feature_keys.join(', ') || '—', width: '12rem' },
                  {
                    header: 'Why this tool',
                    cell: (o) => (
                      <span className="muted" title={JSON.stringify(o.parameter_rationale, null, 2)}>
                        {o.parameter_rationale?.selection_rule ?? o.parameter_rationale?.why ?? '—'}
                      </span>
                    ),
                  },
                ]}
              />
            </div>
          ))}
        </Panel>
      ) : null}
    </div>
  )
}
