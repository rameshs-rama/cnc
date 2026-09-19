import { useCallback, useEffect, useMemo, useState } from 'react'
import { request, type ConfidenceMap, type FeatureView, type GeometryVersion, type Job, type Project, type Surface } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { Badge, DataTable, Empty, ErrorNote, GateCard, KeyValue, Loading, Panel, StatusBadge } from '@/components/ui'
import { JobWatcher } from '@/components/JobWatcher'
import { Viewer, type FeatureMarker } from '@/three/Viewer'
import { dual, shortHash, when } from '@/lib/format'

export function EngineeringReview({ project, onChanged }: { project: Project; onChanged: () => void }) {
  const { can } = useAuth()
  const [versions, setVersions] = useState<GeometryVersion[]>([])
  const [active, setActive] = useState<string | null>(null)
  const [map, setMap] = useState<ConfidenceMap | null>(null)
  const [surface, setSurface] = useState<Surface | null>(null)
  const [selectedFeature, setSelectedFeature] = useState<string | null>(null)
  const [filter, setFilter] = useState<string>('all')
  const [jobId, setJobId] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)

  const loadVersions = useCallback(async () => {
    try {
      const list = await request<GeometryVersion[]>(`/projects/${project.id}/geometry`)
      setVersions(list)
      setActive((current) => current ?? list[0]?.id ?? null)
    } catch (caught) {
      setError(caught)
    }
  }, [project.id])

  useEffect(() => {
    void loadVersions()
  }, [loadVersions])

  useEffect(() => {
    if (!active) return
    setMap(null)
    setSurface(null)
    void Promise.all([
      request<ConfidenceMap>(`/geometry/${active}/confidence-map`),
      request<Surface>(`/geometry/${active}/surface?downsample=2`),
    ])
      .then(([confidence, geometrySurface]) => {
        setMap(confidence)
        setSurface(geometrySurface)
      })
      .catch(setError)
  }, [active])

  const reconstruct = async () => {
    setError(null)
    try {
      const job = await request<Job>('/geometry-jobs', {
        body: { project_id: project.id, artifact_ids: [], known_dimensions: {} },
      })
      setJobId(job.id)
    } catch (caught) {
      setError(caught)
    }
  }

  const approve = async () => {
    if (!map) return
    const note = window.prompt('Approval note for the audit trail:') ?? undefined
    try {
      await request(`/geometry/${map.geometry_version_id}:approve`, { body: { note } })
      await loadVersions()
      setMap(await request<ConfidenceMap>(`/geometry/${map.geometry_version_id}/confidence-map`))
      onChanged()
    } catch (caught) {
      setError(caught)
    }
  }

  const features = useMemo(() => {
    if (!map) return []
    if (filter === 'all') return map.features
    return map.features.filter((feature) => feature.status === filter)
  }, [map, filter])

  const markers: FeatureMarker[] = useMemo(() => {
    if (!map) return []
    return map.features.flatMap((feature) => {
      const position = featurePosition(feature, map)
      if (!position) return []
      return [
        {
          key: feature.stable_key,
          label: feature.label,
          position,
          status: feature.status,
          radius: markerRadius(feature),
          selected: feature.stable_key === selectedFeature,
        },
      ]
    })
  }, [map, selectedFeature])

  const inspected = map?.features.find((feature) => feature.stable_key === selectedFeature) ?? null

  return (
    <div className="workspace">
      {error ? <ErrorNote error={error} /> : null}

      <Panel
        title="Reconstruction studio"
        subtitle="Geometry revisions are immutable. Editing creates the next revision and marks everything built on the old one stale."
        actions={
          can('geometry:edit') ? (
            <button type="button" onClick={() => void reconstruct()} disabled={Boolean(jobId)}>
              Run reconstruction
            </button>
          ) : null
        }
      >
        {jobId ? (
          <JobWatcher
            jobId={jobId}
            onDone={() => {
              setJobId(null)
              void loadVersions()
              onChanged()
            }}
            onFailed={() => setJobId(null)}
          />
        ) : null}
        <DataTable
          rows={versions}
          rowKey={(v) => v.id}
          selectedId={active ?? undefined}
          onRowClick={(v) => setActive(v.id)}
          empty={<Empty title="No geometry yet" hint="Run reconstruction once evidence has been uploaded." />}
          columns={[
            { header: 'Rev', cell: (v) => <strong>{v.revision}</strong>, width: '4rem' },
            {
              header: 'Status',
              cell: (v) => <Badge color={v.status === 'Approved' ? '#3fb950' : '#58a6ff'}>{v.status}</Badge>,
              width: '9rem',
            },
            {
              header: 'Scale',
              cell: (v) =>
                v.scale_established ? (
                  <span title={v.scale_source ?? ''}>
                    established ± {v.scale_uncertainty_mm?.toFixed(2) ?? '?'} mm
                  </span>
                ) : (
                  <Badge color="#f85149">not established</Badge>
                ),
              width: '16rem',
            },
            {
              header: 'Provisional',
              cell: (v) => (v.provisional ? <Badge color="#d29922">provisional</Badge> : 'derived from CAD'),
              width: '11rem',
            },
            { header: 'Hash', cell: (v) => <code>{shortHash(v.content_hash, 10)}</code>, width: '9rem' },
            { header: 'Created', cell: (v) => <span className="muted">{when(v.created_at)}</span>, width: '12rem' },
          ]}
        />
      </Panel>

      {!map && active ? <Loading what="the confidence map" /> : null}

      {map ? (
        <>
          <div className="split-2">
            <Panel
              title="Confidence map"
              subtitle="Markers carry the verification status of the feature. Click one to inspect its evidence."
              actions={
                <select value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter by status">
                  <option value="all">All statuses</option>
                  <option value="Confirmed">Confirmed</option>
                  <option value="Measured">Measured</option>
                  <option value="Inferred">Inferred</option>
                  <option value="Verification Required">Verification Required</option>
                  <option value="Unknown">Unknown</option>
                </select>
              }
            >
              <Viewer surface={surface} markers={markers} onSelect={setSelectedFeature} height={420} />
              <div className="status-counts">
                {Object.entries(map.status_counts)
                  .filter(([, count]) => count > 0)
                  .map(([status, count]) => (
                    <span key={status}>
                      <StatusBadge status={status} /> {count}
                    </span>
                  ))}
              </div>
            </Panel>

            <Panel title="Feature tree" subtitle={`${features.length} of ${map.features.length} shown`}>
              <DataTable
                rows={features}
                rowKey={(f) => f.stable_key}
                selectedId={selectedFeature ?? undefined}
                onRowClick={(f) => setSelectedFeature(f.stable_key)}
                empty={<Empty title="No features at this filter" />}
                columns={[
                  { header: 'Feature', cell: (f) => <strong>{f.label || f.stable_key}</strong> },
                  { header: 'Type', cell: (f) => f.type, width: '9rem' },
                  {
                    header: 'Support',
                    cell: (f) => (
                      <Badge
                        color={
                          f.support === 'Supported' ? '#3fb950' : f.support === 'Partially supported' ? '#d29922' : '#f85149'
                        }
                        title={f.support_reason ?? undefined}
                      >
                        {f.support}
                      </Badge>
                    ),
                    width: '13rem',
                  },
                  { header: 'Status', cell: (f) => <StatusBadge status={f.status} />, width: '11rem' },
                  { header: 'Conf.', cell: (f) => f.confidence.toFixed(2), width: '5rem', align: 'right' },
                ]}
              />
            </Panel>
          </div>

          <div className="split-2">
            <Panel
              title="Evidence inspector"
              subtitle={inspected ? `${inspected.label || inspected.stable_key}` : 'Select a feature to inspect its evidence'}
            >
              {inspected ? (
                <>
                  <KeyValue
                    rows={[
                      ['type', inspected.type],
                      ['criticality', inspected.criticality],
                      ['status', <StatusBadge key="s" status={inspected.status} />],
                      ['confidence', inspected.confidence.toFixed(3)],
                      ['access direction', inspected.access.join(', ')],
                      ['thread', inspected.thread_spec ?? 'not established'],
                      ['surface finish', inspected.surface_finish_ra ? `Ra ${inspected.surface_finish_ra}` : '—'],
                      ...(inspected.support_reason ? [['support note', inspected.support_reason] as [string, string]] : []),
                    ]}
                  />
                  <h3>Parameters</h3>
                  <pre>{JSON.stringify(inspected.parameters, null, 2)}</pre>
                  <h3>Backing observations</h3>
                  <DataTable
                    rows={inspected.observations ?? []}
                    rowKey={(o) => o.id}
                    empty={<Empty title="No observation is attached to this feature" />}
                    columns={[
                      { header: 'Attribute', cell: (o) => o.attribute },
                      {
                        header: 'Value',
                        cell: (o) => (o.value !== null ? dual(o.value) : (o.value_text ?? '—')),
                        width: '14rem',
                      },
                      { header: 'Method', cell: (o) => <span className="muted">{o.method}</span> },
                      {
                        header: 'Uncertainty',
                        cell: (o) => (o.uncertainty !== null ? `± ${o.uncertainty.toFixed(3)} mm` : '—'),
                        width: '9rem',
                      },
                      { header: 'Authority', cell: (o) => `${o.authority_rank}. ${o.authority}`, width: '14rem' },
                      { header: 'Disposition', cell: (o) => o.disposition, width: '8rem' },
                    ]}
                  />
                </>
              ) : (
                <Empty title="Nothing selected" hint="Pick a feature in the tree or click a marker in the viewport." />
              )}
            </Panel>

            <Panel
              title="Geometry approval gate"
              subtitle="A critical unknown or an unresolved conflict blocks process planning."
              tone={map.gate.passed ? 'default' : 'danger'}
              actions={
                can('geometry:approve') && map.status !== 'Approved' ? (
                  <button type="button" onClick={() => void approve()} disabled={!map.gate.passed}>
                    Approve geometry
                  </button>
                ) : null
              }
            >
              <GateCard gate={map.gate} />
              <h3>Reconstruction quality</h3>
              <pre>{JSON.stringify(map.quality_report, null, 2)}</pre>
            </Panel>
          </div>
        </>
      ) : null}
    </div>
  )
}

function featurePosition(feature: FeatureView, map: ConfidenceMap): [number, number, number] | null {
  const parameters = feature.parameters ?? {}
  const zTop = Number(parameters.top_z ?? map.part_model?.z_top ?? 0)
  if (Array.isArray(parameters.center)) {
    const depth = Number(parameters.depth ?? 0)
    return [Number(parameters.center[0]), Number(parameters.center[1]), zTop - depth / 2]
  }
  if (Array.isArray(parameters.start) && Array.isArray(parameters.end)) {
    return [
      (Number(parameters.start[0]) + Number(parameters.end[0])) / 2,
      (Number(parameters.start[1]) + Number(parameters.end[1])) / 2,
      zTop - Number(parameters.depth ?? 0) / 2,
    ]
  }
  return null
}

function markerRadius(feature: FeatureView): number {
  const parameters = feature.parameters ?? {}
  if (parameters.diameter) return Math.max(2, Number(parameters.diameter) / 2)
  if (Array.isArray(parameters.size)) return Math.max(3, Math.min(Number(parameters.size[0]), Number(parameters.size[1])) / 6)
  if (parameters.width) return Math.max(2, Number(parameters.width) / 2)
  return 3
}
