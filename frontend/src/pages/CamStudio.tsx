import { useCallback, useEffect, useState } from 'react'
import { request, type Job, type Plan, type Surface, type ToolpathView } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { Badge, DataTable, Empty, ErrorNote, KeyValue, Panel, StaleBadge } from '@/components/ui'
import { JobWatcher } from '@/components/JobWatcher'
import { Viewer } from '@/three/Viewer'
import { mm } from '@/lib/format'

export function CamStudio({ plan, onChanged }: { plan: Plan | null; onChanged: () => void }) {
  const { can } = useAuth()
  const [paths, setPaths] = useState<ToolpathView[]>([])
  const [surface, setSurface] = useState<Surface | null>(null)
  const [visible, setVisible] = useState<Record<string, boolean>>({})
  const [selected, setSelected] = useState<ToolpathView | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)

  const load = useCallback(async () => {
    if (!plan) return
    try {
      const [pathList, geometrySurface] = await Promise.all([
        request<ToolpathView[]>(`/plans/${plan.id}/toolpaths?include_moves=true`),
        request<Surface>(`/geometry/${plan.geometry_version_id}/surface?downsample=2`),
      ])
      setPaths(pathList)
      setSurface(geometrySurface)
      setVisible(Object.fromEntries(pathList.map((p) => [p.id, true])))
      setSelected((current) => pathList.find((p) => p.id === current?.id) ?? pathList[0] ?? null)
    } catch (caught) {
      setError(caught)
    }
  }, [plan])

  useEffect(() => {
    void load()
  }, [load])

  const generate = async () => {
    if (!plan) return
    setError(null)
    try {
      setJobId((await request<Job>(`/plans/${plan.id}/toolpaths:generate`, { method: 'POST' })).id)
    } catch (caught) {
      setError(caught)
    }
  }

  if (!plan) return <Empty title="Select a plan" hint="Choose a candidate in the process planner first." />

  const totals = paths.reduce(
    (acc, path) => ({
      moves: acc.moves + path.move_count,
      cutting: acc.cutting + path.cutting_length_mm,
      rapid: acc.rapid + path.rapid_length_mm,
    }),
    { moves: 0, cutting: 0, rapid: 0 },
  )

  return (
    <div className="workspace">
      {error ? <ErrorNote error={error} /> : null}

      <Panel
        title="CAM studio"
        subtitle="Cutting parameters are derived from the material rules, the tool and the machine limits, and each carries the rule that produced it."
        actions={
          can('toolpath:edit') ? (
            <button type="button" onClick={() => void generate()} disabled={Boolean(jobId)}>
              Generate toolpaths
            </button>
          ) : null
        }
      >
        {plan.stale ? <ErrorNote error={{ code: 'plan_stale', message: plan.stale_reason ?? 'This plan is stale' }} /> : null}
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
        <KeyValue
          rows={[
            ['operations', paths.length],
            ['moves', totals.moves.toLocaleString()],
            ['cutting length', mm(totals.cutting, 0)],
            ['non-cutting length', mm(totals.rapid, 0)],
          ]}
        />
      </Panel>

      <div className="split-2">
        <Panel title="Toolpath viewport" subtitle="Feed moves in blue, plunges in amber, rapids in red.">
          <Viewer
            surface={surface}
            toolpaths={paths.map((path) => ({
              id: path.id,
              label: path.label ?? path.operation_type ?? '',
              moves: path.moves ?? [],
              visible: visible[path.id] ?? true,
            }))}
            height={460}
          />
        </Panel>

        <Panel title="Operation tree">
          <DataTable
            rows={paths}
            rowKey={(p) => p.id}
            selectedId={selected?.id}
            onRowClick={setSelected}
            empty={<Empty title="No toolpaths yet" hint="Generate them once the plan gate passes." />}
            columns={[
              {
                header: 'Show',
                cell: (p) => (
                  <input
                    type="checkbox"
                    checked={visible[p.id] ?? true}
                    onChange={(e) => setVisible((v) => ({ ...v, [p.id]: e.target.checked }))}
                    onClick={(e) => e.stopPropagation()}
                    aria-label={`Show ${p.label}`}
                  />
                ),
                width: '4rem',
              },
              { header: 'Seq', cell: (p) => `${p.setup_sequence}.${p.sequence}`, width: '4rem' },
              { header: 'Operation', cell: (p) => p.label ?? p.operation_type, width: '' },
              { header: 'Tool', cell: (p) => p.tool?.code ?? '—', width: '9rem' },
              {
                header: 'S / F',
                cell: (p) =>
                  p.parameters?.rpm ? (
                    <span>
                      {Math.round(p.parameters.rpm)} / {Math.round(p.parameters.feed_mm_min)}
                    </span>
                  ) : (
                    '—'
                  ),
                width: '9rem',
                align: 'right',
              },
              { header: '', cell: (p) => (p.stale ? <StaleBadge /> : null), width: '4rem' },
            ]}
          />
        </Panel>
      </div>

      {selected ? (
        <Panel
          title={`Parameters — ${selected.label ?? selected.operation_type}`}
          subtitle="Why this speed, this feed and this depth of cut."
        >
          <div className="split-2">
            <div>
              <KeyValue
                rows={[
                  ['generator', `${selected.generator} ${selected.generator_version}`],
                  ['tool', selected.tool?.code ?? '—'],
                  ['spindle speed', selected.parameters?.rpm ? `${Math.round(selected.parameters.rpm)} rpm` : '—'],
                  ['surface speed', selected.parameters?.vc_m_min ? `${selected.parameters.vc_m_min} m/min` : '—'],
                  ['feed', selected.parameters?.feed_mm_min ? `${Math.round(selected.parameters.feed_mm_min)} mm/min` : '—'],
                  ['feed per tooth', selected.parameters?.fz_mm ? `${selected.parameters.fz_mm} mm` : '—'],
                  ['axial depth', mm(selected.parameters?.ap_mm)],
                  ['radial engagement', mm(selected.parameters?.ae_mm)],
                  ['removal rate', selected.parameters?.mrr_mm3_min ? `${Math.round(selected.parameters.mrr_mm3_min)} mm³/min` : '—'],
                  ['spindle power', selected.parameters?.power_kw ? `${selected.parameters.power_kw} kW` : '—'],
                  ['coolant', selected.parameters?.coolant ?? '—'],
                  ['cutting length', mm(selected.cutting_length_mm, 0)],
                  ['moves', selected.move_count.toLocaleString()],
                ]}
              />
            </div>
            <div>
              <h3>Derivation</h3>
              {selected.parameter_rationale?.rule_source ? (
                <>
                  <p>
                    <Badge color={selected.parameter_rationale.rule_validated ? '#3fb950' : '#d29922'}>
                      {selected.parameter_rationale.rule_validated ? 'validated rule' : 'rule not validated'}
                    </Badge>{' '}
                    {selected.parameter_rationale.rule_source}
                  </p>
                  <p className="muted">{selected.parameter_rationale.model}</p>
                </>
              ) : null}
              {selected.parameter_rationale?.notes?.length ? (
                <ul>
                  {selected.parameter_rationale.notes.map((note: string) => (
                    <li key={note}>{note}</li>
                  ))}
                </ul>
              ) : null}
              {selected.parameter_rationale?.clamps?.length ? (
                <>
                  <h3>Clamps applied</h3>
                  <ul className="warnings">
                    {selected.parameter_rationale.clamps.map((clamp: string) => (
                      <li key={clamp}>{clamp}</li>
                    ))}
                  </ul>
                </>
              ) : null}
              <details>
                <summary>Full rationale record</summary>
                <pre>{JSON.stringify(selected.parameter_rationale, null, 2)}</pre>
              </details>
            </div>
          </div>
        </Panel>
      ) : null}
    </div>
  )
}
