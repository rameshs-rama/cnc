import { useCallback, useEffect, useState } from 'react'
import { request, type MasterRecord } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { Badge, DataTable, Empty, ErrorNote, Panel } from '@/components/ui'
import { money } from '@/lib/format'

const COLLECTIONS = ['machines', 'tools', 'fixtures', 'materials', 'posts'] as const
type Collection = (typeof COLLECTIONS)[number]

export function FactoryTwin() {
  const { can } = useAuth()
  const [collection, setCollection] = useState<Collection>('machines')
  const [records, setRecords] = useState<MasterRecord[]>([])
  const [selected, setSelected] = useState<MasterRecord | null>(null)
  const [substitutes, setSubstitutes] = useState<any | null>(null)
  const [error, setError] = useState<unknown>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      const list = await request<MasterRecord[]>(`/factory/${collection}`)
      setRecords(list)
      setSelected(null)
      setSubstitutes(null)
    } catch (caught) {
      setError(caught)
    }
  }, [collection])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (collection !== 'tools' || !selected) return
    void request<any>(`/factory/tools/${selected.id}/availability`).then(setSubstitutes).catch(() => setSubstitutes(null))
  }, [collection, selected])

  const certify = async (post: MasterRecord) => {
    const machines = await request<MasterRecord[]>('/factory/machines')
    const machine = machines.find((m) => m.code === post.machine_code)
    if (!machine) {
      setError({ code: 'machine_missing', message: `No machine version matches ${String(post.machine_code)}` })
      return
    }
    const note = window.prompt(`Certification note for ${String(post.code)} against ${String(machine.code)}:`)
    if (!note) return
    const hashes = window.prompt('Comma-separated SHA-256 hashes of the approved test programs:')
    if (!hashes) return
    try {
      await request(`/factory/posts/${post.id}:certify`, {
        body: {
          machine_version_id: machine.id,
          note,
          test_program_hashes: hashes.split(',').map((h) => h.trim()).filter(Boolean),
        },
      })
      await load()
    } catch (caught) {
      setError(caught)
    }
  }

  const revoke = async (post: MasterRecord) => {
    const reason = window.prompt('Reason for revoking this post. Future releases will be blocked:')
    if (!reason) return
    try {
      await request(`/factory/posts/${post.id}:revoke`, { body: { reason } })
      await load()
    } catch (caught) {
      setError(caught)
    }
  }

  return (
    <div className="workspace">
      <Panel
        title="Factory digital twin"
        subtitle="Machines, tools, workholding, materials and postprocessors are versioned master data. A plan keeps the exact version it used."
        actions={
          <div className="tabs">
            {COLLECTIONS.map((name) => (
              <button
                key={name}
                type="button"
                className={collection === name ? 'active' : ''}
                onClick={() => setCollection(name)}
              >
                {name}
              </button>
            ))}
          </div>
        }
      >
        {error ? <ErrorNote error={error} onRetry={load} /> : null}
        <DataTable
          rows={records}
          rowKey={(r) => String(r.id)}
          selectedId={selected ? String(selected.id) : undefined}
          onRowClick={setSelected}
          empty={<Empty title={`No ${collection} registered`} />}
          columns={columnsFor(collection, { certify, revoke, can })}
        />
      </Panel>

      {selected ? (
        <Panel title={String(selected.code)} subtitle={String(selected.name ?? selected.description ?? '')}>
          {substitutes ? (
            <>
              <h3>Substitution penalty</h3>
              <p className="muted">
                If {String(selected.code)} is unavailable, these are the alternatives and the time they cost.
              </p>
              <DataTable
                rows={substitutes.substitutes ?? []}
                rowKey={(s: any) => s.tool_id}
                empty={<Empty title="No comparable substitute in the crib" />}
                columns={[
                  { header: 'Tool', cell: (s: any) => s.code },
                  { header: 'Diameter', cell: (s: any) => `${s.diameter_mm} mm`, width: '8rem', align: 'right' },
                  {
                    header: 'Time penalty',
                    cell: (s: any) => `${s.estimated_time_penalty_percent} %`,
                    width: '9rem',
                    align: 'right',
                  },
                  { header: 'Note', cell: (s: any) => <span className="muted">{s.note}</span> },
                ]}
              />
            </>
          ) : null}
          <details open>
            <summary>Record</summary>
            <pre>{JSON.stringify(selected, null, 2)}</pre>
          </details>
        </Panel>
      ) : null}
    </div>
  )
}

function columnsFor(
  collection: Collection,
  actions: { certify: (post: MasterRecord) => void; revoke: (post: MasterRecord) => void; can: (p: string) => boolean },
) {
  const base = [
    { header: 'Code', cell: (r: MasterRecord) => <strong>{String(r.code)}</strong>, width: '13rem' },
    { header: 'Rev', cell: (r: MasterRecord) => String(r.revision ?? '—'), width: '4rem' },
  ]

  if (collection === 'machines') {
    return [
      ...base,
      { header: 'Model', cell: (r: MasterRecord) => String(r.model ?? '') },
      { header: 'Controller', cell: (r: MasterRecord) => `${String(r.controller)} ${String(r.controller_version ?? '')}`, width: '13rem' },
      { header: 'Kinematics', cell: (r: MasterRecord) => String(r.kinematics), width: '8rem' },
      { header: 'Rate', cell: (r: MasterRecord) => `${money(Number(r.hourly_rate))}/h`, width: '8rem', align: 'right' as const },
      {
        header: 'Qualified',
        cell: (r: MasterRecord) => (
          <Badge color={r.geometry_qualified ? '#3fb950' : '#d29922'}>
            {r.geometry_qualified ? 'geometry qualified' : 'not qualified'}
          </Badge>
        ),
        width: '13rem',
      },
    ]
  }
  if (collection === 'tools') {
    return [
      ...base,
      { header: 'Description', cell: (r: MasterRecord) => String(r.description ?? '') },
      {
        header: 'Cutter',
        cell: (r: MasterRecord) => {
          const cutter = r.cutter as Record<string, any>
          return `${cutter?.type} ⌀${cutter?.diameter} × ${cutter?.flutes}fl`
        },
        width: '14rem',
      },
      { header: 'Gauge', cell: (r: MasterRecord) => `${String(r.gauge_length_mm)} mm`, width: '7rem', align: 'right' as const },
      {
        header: 'Available',
        cell: (r: MasterRecord) => (
          <Badge color={r.available ? '#3fb950' : '#f85149'}>{r.available ? String(r.location) : 'unavailable'}</Badge>
        ),
        width: '10rem',
      },
      {
        header: 'Collision model',
        cell: (r: MasterRecord) =>
          r.has_collision_model ? <Badge color="#3fb950">yes</Badge> : <Badge color="#f85149">missing</Badge>,
        width: '10rem',
      },
    ]
  }
  if (collection === 'fixtures') {
    return [
      ...base,
      { header: 'Name', cell: (r: MasterRecord) => String(r.name ?? '') },
      { header: 'Type', cell: (r: MasterRecord) => String(r.fixture_type), width: '8rem' },
      { header: 'Jaw opening', cell: (r: MasterRecord) => `${String(r.jaw_opening_mm)} mm`, width: '9rem', align: 'right' as const },
      { header: 'Grip depth', cell: (r: MasterRecord) => `${String(r.clamp_height_mm)} mm`, width: '9rem', align: 'right' as const },
      {
        header: 'Verified',
        cell: (r: MasterRecord) => (
          <Badge color={r.verified ? '#3fb950' : '#d29922'}>{r.verified ? 'verified' : 'unverified model'}</Badge>
        ),
        width: '13rem',
      },
    ]
  }
  if (collection === 'materials') {
    return [
      { header: 'Code', cell: (r: MasterRecord) => <strong>{String(r.code)}</strong>, width: '13rem' },
      { header: 'Name', cell: (r: MasterRecord) => String(r.name ?? '') },
      { header: 'Group', cell: (r: MasterRecord) => String(r.group), width: '9rem' },
      { header: 'Density', cell: (r: MasterRecord) => `${String(r.density_g_cm3)} g/cm³`, width: '9rem', align: 'right' as const },
      { header: 'Price', cell: (r: MasterRecord) => `${money(Number(r.price_per_kg))}/kg`, width: '8rem', align: 'right' as const },
      {
        header: 'Rules',
        cell: (r: MasterRecord) => (
          <Badge color={r.rules_validated ? '#3fb950' : '#d29922'}>
            {r.rules_validated ? 'validated' : 'entered, not validated'}
          </Badge>
        ),
        width: '15rem',
      },
    ]
  }
  return [
    ...base,
    { header: 'Machine', cell: (r: MasterRecord) => String(r.machine_code), width: '9rem' },
    { header: 'Controller', cell: (r: MasterRecord) => `${String(r.controller)} ${String(r.controller_version ?? '')}`, width: '13rem' },
    {
      header: 'Certification',
      cell: (r: MasterRecord) => (
        <Badge color={r.revoked ? '#f85149' : r.certified && r.enabled ? '#3fb950' : '#d29922'}>
          {r.revoked ? 'revoked' : r.certified ? (r.enabled ? 'certified and enabled' : 'certified, disabled') : 'not certified'}
        </Badge>
      ),
      width: '16rem',
    },
    {
      header: '',
      cell: (r: MasterRecord) =>
        actions.can('post:certify') ? (
          <span className="row-actions">
            {!r.certified && !r.revoked ? (
              <button type="button" onClick={(e) => { e.stopPropagation(); actions.certify(r) }}>
                Certify
              </button>
            ) : null}
            {r.certified && !r.revoked ? (
              <button type="button" onClick={(e) => { e.stopPropagation(); actions.revoke(r) }}>
                Revoke
              </button>
            ) : null}
          </span>
        ) : null,
      width: '11rem',
    },
  ]
}
