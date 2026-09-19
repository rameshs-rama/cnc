import { useCallback, useEffect, useState } from 'react'
import { request, type Job, type Plan, type Simulation, type Surface } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { Badge, DataTable, Empty, ErrorNote, GateCard, KeyValue, Panel, SeverityBadge } from '@/components/ui'
import { JobWatcher } from '@/components/JobWatcher'
import { Viewer } from '@/three/Viewer'
import { mm, seconds, shortHash, when } from '@/lib/format'

export function DigitalTwin({
  plan,
  onSimulation,
  onChanged,
}: {
  plan: Plan | null
  onSimulation: (simulation: Simulation | null) => void
  onChanged: () => void
}) {
  const { can } = useAuth()
  const [runs, setRuns] = useState<Simulation[]>([])
  const [active, setActive] = useState<Simulation | null>(null)
  const [stock, setStock] = useState<any>(null)
  const [surface, setSurface] = useState<Surface | null>(null)
  const [gate, setGate] = useState<any>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [voxel, setVoxel] = useState(1.0)

  const load = useCallback(async () => {
    if (!plan) return
    try {
      const [runList, geometrySurface] = await Promise.all([
        request<Simulation[]>(`/plans/${plan.id}/simulations`),
        request<Surface>(`/geometry/${plan.geometry_version_id}/surface?downsample=2`),
      ])
      setRuns(runList)
      setSurface(geometrySurface)
      const latest = runList[0] ?? null
      setActive(latest)
      onSimulation(latest)
    } catch (caught) {
      setError(caught)
    }
  }, [plan, onSimulation])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (!active) {
      setStock(null)
      setGate(null)
      return
    }
    void Promise.all([
      request<any>(`/simulations/${active.id}/stock`),
      request<any>(`/simulations/${active.id}/gate`),
    ])
      .then(([stockResult, gateResult]) => {
        setStock(stockResult.remaining_stock)
        setGate(gateResult)
      })
      .catch(setError)
  }, [active])

  const run = async () => {
    if (!plan) return
    setError(null)
    try {
      const job = await request<Job>('/simulations', {
        body: { plan_id: plan.id, voxel_pitch: voxel, tolerance_mm: 0.05 },
      })
      setJobId(job.id)
    } catch (caught) {
      setError(caught)
    }
  }

  const disposition = async (code: string, severity: string) => {
    if (!active) return
    const decision = severity === 'S1' ? 'acknowledged' : 'resolved'
    const reason = window.prompt(
      severity === 'S1'
        ? 'An S1 stop cannot be resolved away. Record an acknowledgement; the cause must still be corrected:'
        : 'Record the reason for this disposition:',
    )
    if (!reason) return
    try {
      await request(`/simulations/${active.id}/dispositions`, { body: { event_code: code, decision, reason } })
      const refreshed = await request<Simulation>(`/simulations/${active.id}`)
      setActive(refreshed)
      setGate(await request<any>(`/simulations/${active.id}/gate`))
      onChanged()
    } catch (caught) {
      setError(caught)
    }
  }

  if (!plan) return <Empty title="Select a plan" hint="Choose a candidate in the process planner first." />

  const comparison = active?.stock_comparison ?? {}

  return (
    <div className="workspace">
      {error ? <ErrorNote error={error} /> : null}

      <Panel
        title="Digital twin"
        subtitle="Stock removal, machine kinematics and collision against the fixture, the clamps and the remaining stock."
        actions={
          can('simulation:run') ? (
            <>
              <label className="inline">
                Voxel pitch
                <select value={voxel} onChange={(e) => setVoxel(Number(e.target.value))}>
                  <option value={2}>2.0 mm (fast)</option>
                  <option value={1}>1.0 mm (default)</option>
                  <option value={0.5}>0.5 mm (fine)</option>
                </select>
              </label>
              <button type="button" onClick={() => void run()} disabled={Boolean(jobId)}>
                Run simulation
              </button>
            </>
          ) : null
        }
      >
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
          rows={runs}
          rowKey={(r) => r.id}
          selectedId={active?.id}
          onRowClick={(r) => {
            setActive(r)
            onSimulation(r)
          }}
          empty={<Empty title="Not simulated yet" hint="Nothing may be postprocessed until a simulation passes." />}
          columns={[
            {
              header: 'Result',
              cell: (r) => (
                <>
                  <Badge color={r.passed ? '#3fb950' : '#f85149'}>{r.passed ? 'passed' : 'failed'}</Badge>
                  {r.stale ? <Badge color="#db6d28">stale</Badge> : null}
                </>
              ),
              width: '11rem',
            },
            { header: 'Cycle time', cell: (r) => seconds(r.cycle_time_seconds), width: '10rem' },
            {
              header: 'Events',
              cell: (r) =>
                Object.entries(r.event_counts ?? {})
                  .map(([severity, count]) => `${severity}×${count}`)
                  .join('  ') || 'none',
              width: '12rem',
            },
            { header: 'Voxel', cell: (r) => `${r.voxel_size_mm} mm`, width: '6rem' },
            { header: 'Identity', cell: (r) => <code>{shortHash(r.identity_hash, 10)}</code>, width: '9rem' },
            { header: 'Run', cell: (r) => <span className="muted">{when(r.created_at)}</span>, width: '12rem' },
          ]}
        />
      </Panel>

      {active ? (
        <>
          <div className="split-2">
            <Panel title="Stock comparison" subtitle="Finished surface against the machined stock.">
              <Viewer surface={surface} stock={stock} height={420} />
              <KeyValue
                rows={[
                  ['tolerance', mm(comparison.tolerance_mm, 3)],
                  ['overcut cells', comparison.overcut_cells ?? 0],
                  ['max overcut', mm(comparison.overcut_max_mm, 3)],
                  ['remaining cells', comparison.remaining_cells ?? 0],
                  ['max remaining', mm(comparison.remaining_max_mm, 3)],
                  ['volume left', comparison.volume ? `${Math.round(comparison.volume.remaining_volume_mm3)} mm³` : '—'],
                  ['volume overcut', comparison.volume ? `${Math.round(comparison.volume.overcut_volume_mm3)} mm³` : '—'],
                ]}
              />
            </Panel>

            <Panel title="Cycle time" subtitle="Decomposed by cutting, rapid, dwell, tool change, spindle and indexing.">
              <KeyValue
                rows={Object.entries(active.time_breakdown ?? {})
                  .filter(([key]) => key.endsWith('_s'))
                  .map(([key, value]) => [key.replace(/_s$/, ''), seconds(Number(value))])}
              />
              <h3>By operation</h3>
              <DataTable
                rows={(active.time_breakdown?.per_operation ?? []) as any[]}
                rowKey={(o) => o.operation_id}
                empty={<Empty title="No per-operation breakdown" />}
                columns={[
                  { header: 'Operation', cell: (o) => o.label },
                  { header: 'Tool', cell: (o) => o.tool, width: '9rem' },
                  { header: 'Removed', cell: (o) => `${Math.round(o.removed_volume_mm3)} mm³`, width: '9rem', align: 'right' },
                  { header: 'Time', cell: (o) => seconds(o.time?.total_s), width: '8rem', align: 'right' },
                ]}
              />
            </Panel>
          </div>

          <Panel
            title="Verification findings"
            subtitle="Each event names the responsible path segment, the entities involved and the time it occurs."
            tone={active.passed ? 'default' : 'danger'}
          >
            {active.events.length ? (
              <div className="event-list">
                {active.events.map((event, index) => (
                  <article key={`${event.code}-${index}`} className={`finding finding-${event.severity.toLowerCase()}`}>
                    <header>
                      <SeverityBadge severity={event.severity} />
                      <code>{event.code}</code>
                      <span className="muted">at {event.time_s.toFixed(2)} s</span>
                      {event.occurrences > 1 ? <Badge>{event.occurrences} occurrences</Badge> : null}
                      {event.setup ? <span className="muted">{event.setup}</span> : null}
                    </header>
                    <p className="finding-message">{event.message}</p>
                    <dl>
                      <dt>Consequence</dt>
                      <dd>{event.consequence}</dd>
                      <dt>Recommended resolution</dt>
                      <dd>{event.recommendation}</dd>
                      {event.entities.length ? (
                        <>
                          <dt>Entities</dt>
                          <dd>{event.entities.join(', ')}</dd>
                        </>
                      ) : null}
                      {event.position ? (
                        <>
                          <dt>Position</dt>
                          <dd>
                            X{event.position[0]} Y{event.position[1]} Z{event.position[2]}
                          </dd>
                        </>
                      ) : null}
                    </dl>
                    {can('simulation:disposition') ? (
                      <button type="button" onClick={() => void disposition(event.code, event.severity)}>
                        {event.severity === 'S1' ? 'Acknowledge (cannot be waived)' : 'Disposition'}
                      </button>
                    ) : null}
                  </article>
                ))}
              </div>
            ) : (
              <Empty title="No findings" hint="No collision, overcut or axis violation was detected." />
            )}
            {gate ? <GateCard gate={gate} /> : null}
          </Panel>
        </>
      ) : null}
    </div>
  )
}
