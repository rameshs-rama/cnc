import { useCallback, useEffect, useRef, useState } from 'react'
import { request, type Artifact, type Conflict, type Observation, type Project } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { Badge, DataTable, Empty, ErrorNote, KeyValue, Loading, Panel, SeverityBadge, StatusBadge } from '@/components/ui'
import { shortHash, when } from '@/lib/format'

const AUTHORITIES = [
  'Verified CAD and PMI',
  'Drawing',
  'Measurement',
  'Scan',
  'Calibrated photograph',
  'Ordinary photograph',
  'AI inference',
]

export function ProjectIntake({ project, onChanged }: { project: Project; onChanged: () => void }) {
  const { can } = useAuth()
  const [artifacts, setArtifacts] = useState<Artifact[] | null>(null)
  const [observations, setObservations] = useState<Observation[]>([])
  const [conflicts, setConflicts] = useState<Conflict[]>([])
  const [selected, setSelected] = useState<Artifact | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [busy, setBusy] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      const [artifactList, observationList, conflictList] = await Promise.all([
        request<Artifact[]>(`/projects/${project.id}/artifacts`),
        request<Observation[]>(`/projects/${project.id}/observations`),
        request<Conflict[]>(`/projects/${project.id}/conflicts`),
      ])
      setArtifacts(artifactList)
      setObservations(observationList)
      setConflicts(conflictList)
    } catch (caught) {
      setError(caught)
    }
  }, [project.id])

  useEffect(() => {
    void load()
  }, [load])

  const upload = async (files: FileList | null) => {
    if (!files?.length) return
    setBusy(true)
    setError(null)
    try {
      for (const file of Array.from(files)) {
        const form = new FormData()
        form.append('file', file)
        form.append('provenance', JSON.stringify({ declared_by: 'web upload', source: 'engineer upload' }))
        await request<Artifact>(`/projects/${project.id}/artifacts`, { form })
      }
      await load()
      onChanged()
    } catch (caught) {
      setError(caught)
    } finally {
      setBusy(false)
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const overrideAuthority = async (artifact: Artifact, authority: string) => {
    const reason = window.prompt(
      `Reclassifying ${artifact.filename} as "${authority}" changes how it wins conflicts.\nRecord the reason:`,
    )
    if (!reason) return
    try {
      await request(`/projects/${project.id}/artifacts/${artifact.id}/authority`, {
        method: 'PATCH',
        body: { authority, reason },
      })
      await load()
    } catch (caught) {
      setError(caught)
    }
  }

  const resolve = async (conflict: Conflict) => {
    const reason = window.prompt(`Resolve the conflict on ${conflict.attribute}.\nRecord the reason:`)
    if (!reason) return
    try {
      await request(`/projects/${project.id}/conflicts/${conflict.id}/resolve`, {
        body: { chosen_observation_id: conflict.authoritative_observation_id, reason },
      })
      await load()
    } catch (caught) {
      setError(caught)
    }
  }

  return (
    <div className="workspace split-3">
      <Panel
        title="Project metadata"
        subtitle="Provenance is declared here; it travels with every artifact and into the release package."
      >
        <KeyValue
          rows={[
            ['part number', <strong key="p">{project.part_number}</strong>],
            ['revision', project.revision],
            ['state', <Badge key="s">{project.state}</Badge>],
            ['quantity', project.quantity],
            ['unit system', project.unit_system],
            ['target material', project.target_material_code ?? 'Not set'],
            ['intended use', project.intended_use ?? '—'],
            ['due', when(project.due_date)],
          ]}
        />
        <h3>Provenance declaration</h3>
        {Object.keys(project.provenance_declaration ?? {}).length ? (
          <KeyValue rows={Object.entries(project.provenance_declaration).map(([k, v]) => [k, String(v)])} />
        ) : (
          <Empty title="No provenance declared" hint="Release requires a declared source and permitted use." />
        )}
      </Panel>

      <Panel
        title="Artifact inventory"
        subtitle="Source bytes are immutable. A correction is a new artifact, never an edit."
        actions={
          can('artifact:upload') ? (
            <>
              <input
                ref={fileRef}
                type="file"
                multiple
                onChange={(e) => void upload(e.target.files)}
                aria-label="Upload source artifacts"
              />
              {busy ? <span className="muted">uploading…</span> : null}
            </>
          ) : null
        }
      >
        {error ? <ErrorNote error={error} onRetry={load} /> : null}
        {!artifacts ? <Loading what="artifacts" /> : null}
        {artifacts ? (
          <DataTable
            rows={artifacts}
            rowKey={(a) => a.id}
            selectedId={selected?.id}
            onRowClick={setSelected}
            empty={<Empty title="No evidence yet" hint="Upload CAD, a drawing, measurements, images or video." />}
            columns={[
              { header: 'File', cell: (a) => a.filename },
              { header: 'Kind', cell: (a) => <Badge>{a.kind}</Badge>, width: '9rem' },
              {
                header: 'Authority',
                cell: (a) => (
                  <span>
                    {a.authority}
                    {a.authority_overridden ? <Badge color="#d29922">overridden</Badge> : null}
                  </span>
                ),
                width: '14rem',
              },
              {
                header: 'Status',
                cell: (a) => (
                  <Badge color={a.status === 'Processed' ? '#3fb950' : a.status === 'Failed' ? '#f85149' : '#d29922'}>
                    {a.status}
                  </Badge>
                ),
                width: '8rem',
              },
              { header: 'SHA-256', cell: (a) => <code>{shortHash(a.content_hash)}</code>, width: '10rem' },
            ]}
          />
        ) : null}

        {selected ? (
          <div className="detail">
            <h3>{selected.filename}</h3>
            <KeyValue
              rows={[
                ['parser', selected.parser ?? '—'],
                ['bytes', selected.byte_size.toLocaleString()],
                ['content hash', <code key="h">{selected.content_hash}</code>],
                ['uploaded', when(selected.created_at)],
              ]}
            />
            {selected.parse_warnings.length ? (
              <ul className="warnings">
                {selected.parse_warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            ) : null}
            {can('artifact:classify') ? (
              <label className="inline">
                Reclassify authority
                <select
                  value={selected.authority}
                  onChange={(e) => void overrideAuthority(selected, e.target.value)}
                >
                  {AUTHORITIES.map((authority) => (
                    <option key={authority}>{authority}</option>
                  ))}
                </select>
              </label>
            ) : null}
            <details>
              <summary>Extraction</summary>
              <pre>{JSON.stringify(selected.extracted, null, 2)}</pre>
            </details>
          </div>
        ) : null}
      </Panel>

      <Panel
        title="Evidence status"
        subtitle="Conflicts stay visible until an engineer dispositions them; nothing is chosen silently."
        tone={conflicts.length ? 'warning' : 'default'}
      >
        <h3>Open conflicts</h3>
        {conflicts.length ? (
          <div className="conflict-list">
            {conflicts.map((conflict) => (
              <article key={conflict.id} className="conflict">
                <header>
                  <SeverityBadge severity={conflict.severity} />
                  <strong>{conflict.attribute}</strong>
                  {conflict.feature_key ? <code>{conflict.feature_key}</code> : null}
                  {conflict.delta !== null ? <span className="muted">Δ {conflict.delta.toFixed(3)} mm</span> : null}
                </header>
                <p>{conflict.summary}</p>
                {can('geometry:edit') ? (
                  <button type="button" onClick={() => void resolve(conflict)}>
                    Accept the authoritative source
                  </button>
                ) : null}
              </article>
            ))}
          </div>
        ) : (
          <Empty title="No open conflicts" />
        )}

        <h3>Observations ({observations.length})</h3>
        <DataTable
          rows={observations.slice(0, 60)}
          rowKey={(o) => o.id}
          empty={<Empty title="No observations yet" hint="Parsing an artifact produces candidates." />}
          columns={[
            { header: 'Attribute', cell: (o) => o.attribute },
            { header: 'Feature', cell: (o) => o.feature_key ?? <span className="muted">part</span>, width: '7rem' },
            {
              header: 'Value',
              cell: (o) => (o.value !== null ? `${o.value.toFixed(3)} ${o.unit}` : (o.value_text ?? '—')),
              width: '9rem',
              align: 'right',
            },
            {
              header: 'Authority',
              cell: (o) => (
                <span title={`Rank ${o.authority_rank}; lower wins a conflict`}>
                  {o.authority_rank}. {o.authority}
                </span>
              ),
              width: '15rem',
            },
            { header: 'Status', cell: (o) => <StatusBadge status={o.status} />, width: '11rem' },
            { header: 'Confidence', cell: (o) => o.confidence.toFixed(2), width: '6rem', align: 'right' },
          ]}
        />
      </Panel>
    </div>
  )
}
