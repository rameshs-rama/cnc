import { useCallback, useEffect, useState } from 'react'
import { request, type MasterRecord, type Project, type Release } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { Badge, DataTable, Empty, ErrorNote, KeyValue, Panel } from '@/components/ui'
import { seconds, when } from '@/lib/format'

interface VarianceRow {
  run_id: string
  release_revision: number | null
  program: string | null
  predicted_cycle_s: number | null
  actual_cycle_s: number | null
  variance_percent: number | null
  result: string
  alarms: number
  scrap: number
  telemetry_suspect: boolean
}

export function Analytics({ project }: { project: Project }) {
  const { can } = useAuth()
  const [report, setReport] = useState<{ summary: Record<string, any>; runs: VarianceRow[] } | null>(null)
  const [proposals, setProposals] = useState<MasterRecord[]>([])
  const [releases, setReleases] = useState<Release[]>([])
  const [error, setError] = useState<unknown>(null)

  const load = useCallback(async () => {
    try {
      const [variance, proposalList, releaseList] = await Promise.all([
        request<{ summary: Record<string, any>; runs: VarianceRow[] }>(`/projects/${project.id}/variance`),
        request<MasterRecord[]>('/rule-proposals'),
        request<Release[]>(`/projects/${project.id}/releases`),
      ])
      setReport(variance)
      setProposals(proposalList)
      setReleases(releaseList)
    } catch (caught) {
      setError(caught)
    }
  }, [project.id])

  useEffect(() => {
    void load()
  }, [load])

  const recordRun = async () => {
    const released = releases.find((r) => r.status === 'Released')
    if (!released) {
      setError({ code: 'no_release', message: 'Production can only be recorded against a released program' })
      return
    }
    const cycle = window.prompt('Actual cycle time in seconds:')
    if (!cycle) return
    try {
      await request('/machine-runs', {
        body: {
          release_id: released.id,
          nc_program_id: released.nc_program_ids[0],
          actual_cycle_seconds: Number(cycle),
          result: 'Good',
          pieces: 1,
          operator: 'shop floor entry',
        },
      })
      await load()
    } catch (caught) {
      setError(caught)
    }
  }

  const propose = async () => {
    try {
      await request(`/projects/${project.id}/rule-proposals`, { method: 'POST' })
      await load()
    } catch (caught) {
      setError(caught)
    }
  }

  const review = async (proposalId: string, approve: boolean) => {
    const note = window.prompt(approve ? 'Note for promoting this rule:' : 'Note for rejecting this proposal:')
    if (!note) return
    try {
      await request(`/rule-proposals/${proposalId}/review`, { body: { approve, note } })
      await load()
    } catch (caught) {
      setError(caught)
    }
  }

  const summary = report?.summary ?? {}

  return (
    <div className="workspace">
      {error ? <ErrorNote error={error} /> : null}

      <Panel
        title="Production analytics"
        subtitle="Predicted against actual. Variance is reported; no production rule changes without an approved proposal."
        actions={
          can('run:record') ? (
            <>
              <button type="button" onClick={() => void recordRun()}>
                Record a run
              </button>
              <button type="button" onClick={() => void propose()}>
                Derive proposals
              </button>
            </>
          ) : null
        }
      >
        <KeyValue
          rows={[
            ['runs recorded', summary.run_count ?? 0],
            ['comparable sample', summary.sample_size ?? 0],
            ['median variance', summary.median_variance_percent !== null && summary.median_variance_percent !== undefined ? `${summary.median_variance_percent} %` : '—'],
            [
              'calibration',
              summary.sample_size ? (
                <Badge key="c" color={summary.within_calibration_band ? '#3fb950' : '#d29922'}>
                  {summary.within_calibration_band ? 'within band' : 'outside band'} (±{summary.calibration_band_percent} %)
                </Badge>
              ) : (
                '—'
              ),
            ],
            ['inspections', `${summary.inspection_count ?? 0} recorded, ${summary.inspection_failures ?? 0} out of tolerance`],
          ]}
        />
        <p className="muted">{summary.note}</p>

        <DataTable
          rows={report?.runs ?? []}
          rowKey={(row) => row.run_id}
          empty={<Empty title="No production records" hint="Record a run against a released program." />}
          columns={[
            { header: 'Release', cell: (row) => row.release_revision ?? '—', width: '6rem' },
            { header: 'Program', cell: (row) => row.program ?? '—', width: '8rem' },
            { header: 'Predicted', cell: (row) => seconds(row.predicted_cycle_s), width: '9rem' },
            { header: 'Actual', cell: (row) => seconds(row.actual_cycle_s), width: '9rem' },
            {
              header: 'Variance',
              cell: (row) =>
                row.variance_percent === null ? (
                  '—'
                ) : (
                  <Badge color={Math.abs(row.variance_percent) <= 15 ? '#3fb950' : '#d29922'}>
                    {row.variance_percent > 0 ? '+' : ''}
                    {row.variance_percent} %
                  </Badge>
                ),
              width: '8rem',
            },
            { header: 'Result', cell: (row) => row.result, width: '7rem' },
            { header: 'Alarms', cell: (row) => row.alarms, width: '6rem', align: 'right' },
            {
              header: '',
              cell: (row) => (row.telemetry_suspect ? <Badge color="#f85149">suspect telemetry</Badge> : null),
              width: '11rem',
            },
          ]}
        />
      </Panel>

      <Panel
        title="Rule proposals"
        subtitle="A learned suggestion stays a proposal until a manufacturing engineer promotes it."
      >
        <DataTable
          rows={proposals}
          rowKey={(p) => String(p.id)}
          empty={<Empty title="No proposals" hint="Proposals appear once enough comparable runs exist." />}
          columns={[
            { header: 'Scope', cell: (p) => String(p.scope), width: '14rem' },
            { header: 'Target', cell: (p) => <code>{String(p.target_ref)}</code> },
            {
              header: 'Change',
              cell: (p) => (
                <span>
                  {JSON.stringify(p.current_value)} → <strong>{JSON.stringify(p.proposed_value)}</strong>
                </span>
              ),
            },
            { header: 'Sample', cell: (p) => String(p.sample_size), width: '6rem', align: 'right' },
            {
              header: 'State',
              cell: (p) => (
                <Badge color={p.status === 'Approved' ? '#3fb950' : p.status === 'Rejected' ? '#8b949e' : '#d29922'}>
                  {String(p.status)} / {String(p.validation_state)}
                </Badge>
              ),
              width: '13rem',
            },
            {
              header: '',
              cell: (p) =>
                can('rule:promote') && p.status === 'Proposed' ? (
                  <span className="row-actions">
                    <button type="button" onClick={() => void review(String(p.id), true)}>
                      Promote
                    </button>
                    <button type="button" onClick={() => void review(String(p.id), false)}>
                      Reject
                    </button>
                  </span>
                ) : (
                  <span className="muted">{p.review_note ? String(p.review_note) : ''}</span>
                ),
              width: '14rem',
            },
          ]}
        />
      </Panel>

      <Panel title="Release history" subtitle="A released package is never edited; a change supersedes it.">
        <DataTable
          rows={releases}
          rowKey={(r) => r.id}
          empty={<Empty title="Nothing released yet" />}
          columns={[
            { header: 'Rev', cell: (r) => r.revision, width: '5rem' },
            { header: 'Status', cell: (r) => r.status, width: '11rem' },
            { header: 'Released', cell: (r) => when(r.released_at), width: '13rem' },
            { header: 'Superseded by', cell: (r) => r.superseded_by_id ?? '—' },
          ]}
        />
      </Panel>
    </div>
  )
}
