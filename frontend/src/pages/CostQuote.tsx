import { useCallback, useEffect, useState } from 'react'
import { request, type CostEstimate, type Plan, type Simulation } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { DataTable, Empty, ErrorNote, KeyValue, Panel } from '@/components/ui'
import { money, percent, seconds } from '@/lib/format'

export function CostQuote({ plan, simulation }: { plan: Plan | null; simulation: Simulation | null }) {
  const { can } = useAuth()
  const [estimates, setEstimates] = useState<CostEstimate[]>([])
  const [quantity, setQuantity] = useState(25)
  const [overrides, setOverrides] = useState<Record<string, number>>({})
  const [error, setError] = useState<unknown>(null)

  const load = useCallback(async () => {
    if (!plan) return
    try {
      setEstimates(await request<CostEstimate[]>(`/plans/${plan.id}/cost-estimates`))
    } catch (caught) {
      setError(caught)
    }
  }, [plan])

  useEffect(() => {
    void load()
  }, [load])

  const estimate = async () => {
    if (!plan || !simulation) return
    setError(null)
    try {
      await request<CostEstimate>('/cost-estimates', {
        body: { plan_id: plan.id, simulation_id: simulation.id, quantity, rate_overrides: overrides },
      })
      await load()
    } catch (caught) {
      setError(caught)
    }
  }

  if (!plan) return <Empty title="Select a plan" />
  const latest = estimates[0] ?? null

  return (
    <div className="workspace">
      {error ? <ErrorNote error={error} /> : null}

      <Panel
        title="Cost and quote"
        subtitle="Every figure decomposes to a rate, a quantity and a measured time. Nothing is a lump sum."
        actions={
          can('cost:edit') ? (
            <>
              <label className="inline">
                Quantity
                <input
                  type="number"
                  min={1}
                  value={quantity}
                  onChange={(e) => setQuantity(Math.max(1, Number(e.target.value)))}
                />
              </label>
              <button type="button" onClick={() => void estimate()} disabled={!simulation}>
                Estimate
              </button>
            </>
          ) : null
        }
      >
        {!simulation ? (
          <Empty title="No simulation" hint="Cost is derived from a measured cycle time, so a simulation must run first." />
        ) : null}

        {can('cost:edit') ? (
          <div className="rate-editor">
            {[
              ['machine_hourly', 'Machine rate /h'],
              ['setup_hourly', 'Setup rate /h'],
              ['labour_hourly', 'Labour rate /h'],
              ['material_price_per_kg', 'Material /kg'],
              ['scrap_percent', 'Scrap %'],
              ['overhead_percent', 'Overhead %'],
              ['margin_percent', 'Margin %'],
              ['programming_hours', 'Programming hours'],
            ].map(([key, label]) => (
              <label key={key} className="inline">
                {label}
                <input
                  type="number"
                  step="0.1"
                  placeholder={String(latest?.rates?.[key] ?? '')}
                  value={overrides[key] ?? ''}
                  onChange={(e) =>
                    setOverrides((current) => {
                      const next = { ...current }
                      if (e.target.value === '') delete next[key]
                      else next[key] = Number(e.target.value)
                      return next
                    })
                  }
                />
              </label>
            ))}
          </div>
        ) : null}
      </Panel>

      {latest ? (
        <>
          <div className="split-2">
            <Panel title="Cost tree" subtitle={`Quantity ${latest.quantity}, ${latest.currency}`}>
              <DataTable
                rows={Object.entries(latest.breakdown).map(([key, value]) => ({ key, value }))}
                rowKey={(row) => row.key}
                columns={[
                  { header: 'Element', cell: (row) => row.key.replace(/_/g, ' ') },
                  { header: 'Amount', cell: (row) => money(row.value, latest.currency), width: '10rem', align: 'right' },
                ]}
              />
            </Panel>

            <Panel title="Assumptions" subtitle="What the figures rest on.">
              <KeyValue
                rows={[
                  ['cycle time', seconds(latest.assumptions.cycle_time_seconds)],
                  ['setup', seconds(Number(latest.assumptions.setup_minutes) * 60)],
                  ['setup amortisation', String(latest.assumptions.setup_amortisation)],
                  ['stock mass', `${latest.assumptions.stock_mass_kg} kg`],
                  ['material note', String(latest.assumptions.material_note)],
                  ['tool life basis', String(latest.assumptions.tool_life_basis)],
                  ['cutting minutes', String(latest.assumptions.cutting_minutes)],
                  ['distinct tools', String(latest.assumptions.distinct_tools)],
                ]}
              />
            </Panel>
          </div>

          <div className="split-2">
            <Panel title="Quantity breaks">
              <DataTable
                rows={latest.quantity_breaks}
                rowKey={(row) => String(row.quantity)}
                columns={[
                  { header: 'Quantity', cell: (row) => row.quantity, width: '7rem', align: 'right' },
                  { header: 'Unit cost', cell: (row) => money(row.unit_cost, latest.currency), align: 'right' },
                  { header: 'Unit price', cell: (row) => money(row.unit_price, latest.currency), align: 'right' },
                  { header: 'Batch total', cell: (row) => money(row.batch_total, latest.currency), align: 'right' },
                ]}
              />
            </Panel>

            <Panel title="Sensitivity" subtitle={String(latest.sensitivity.basis ?? '')}>
              {['cycle_time', 'scrap_rate', 'machine_rate'].map((dimension) => (
                <div key={dimension}>
                  <h3>{dimension.replace(/_/g, ' ')}</h3>
                  <DataTable
                    rows={(latest.sensitivity[dimension] ?? []) as any[]}
                    rowKey={(row) => row.scenario}
                    columns={[
                      { header: 'Scenario', cell: (row) => row.scenario },
                      {
                        header: 'Unit price',
                        cell: (row) => (
                          <span>
                            {money(row.unit_price, latest.currency)}{' '}
                            <span className="muted">
                              {percent((row.unit_price - latest.unit_price) / latest.unit_price)}
                            </span>
                          </span>
                        ),
                        align: 'right',
                        width: '14rem',
                      },
                    ]}
                  />
                </div>
              ))}
            </Panel>
          </div>
        </>
      ) : (
        <Empty title="No estimate yet" />
      )}
    </div>
  )
}
