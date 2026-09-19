import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { request, type Project } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { Badge, DataTable, Empty, ErrorNote, Loading, Panel } from '@/components/ui'
import { when } from '@/lib/format'

const STATE_TONE: Record<string, string> = {
  Released: '#3fb950',
  'In production': '#3fb950',
  Quarantined: '#f85149',
  Cancelled: '#8b949e',
  Archived: '#8b949e',
}

export function Portfolio() {
  const navigate = useNavigate()
  const { can } = useAuth()
  const [projects, setProjects] = useState<Project[] | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [search, setSearch] = useState('')
  const [creating, setCreating] = useState(false)

  const load = useCallback(async () => {
    setError(null)
    try {
      const query = search ? `?search=${encodeURIComponent(search)}` : ''
      const page = await request<{ items: Project[]; total: number }>(`/projects${query}`)
      setProjects(page.items)
    } catch (caught) {
      setError(caught)
    }
  }, [search])

  useEffect(() => {
    void load()
  }, [load])

  const create = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    try {
      const project = await request<Project>('/projects', {
        body: {
          part_number: String(form.get('part_number')),
          name: String(form.get('name')),
          revision: String(form.get('revision') || 'A'),
          quantity: Number(form.get('quantity') || 1),
          target_material_code: String(form.get('material') || '') || null,
          intended_use: String(form.get('intended_use') || '') || null,
        },
      })
      setCreating(false)
      navigate(`/projects/${project.id}`)
    } catch (caught) {
      setError(caught)
    }
  }

  return (
    <div className="workspace">
      <Panel
        title="Portfolio"
        subtitle="Every part project in this tenant, with its position in the controlled workflow."
        actions={
          <>
            <input
              placeholder="Search part number or name"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              aria-label="Search projects"
            />
            {can('project:create') ? (
              <button type="button" onClick={() => setCreating((v) => !v)}>
                {creating ? 'Cancel' : 'New project'}
              </button>
            ) : null}
          </>
        }
      >
        {creating ? (
          <form className="inline-form" onSubmit={create}>
            <label>
              Part number
              <input name="part_number" required placeholder="BRK-1042" />
            </label>
            <label>
              Revision
              <input name="revision" defaultValue="A" />
            </label>
            <label>
              Name
              <input name="name" required placeholder="Mounting bracket" />
            </label>
            <label>
              Quantity
              <input name="quantity" type="number" min={1} defaultValue={1} />
            </label>
            <label>
              Target material
              <input name="material" placeholder="AL6082-T6" />
            </label>
            <label className="wide">
              Intended use
              <input name="intended_use" placeholder="Structural bracket for a conveyor drive" />
            </label>
            <button type="submit">Create</button>
          </form>
        ) : null}

        {error ? <ErrorNote error={error} onRetry={load} /> : null}
        {!projects && !error ? <Loading what="projects" /> : null}
        {projects ? (
          <DataTable
            rows={projects}
            rowKey={(project) => project.id}
            onRowClick={(project) => navigate(`/projects/${project.id}`)}
            empty={
              <Empty
                title="No projects yet"
                hint={can('project:create') ? 'Create one to begin collecting evidence.' : 'Ask a project engineer to create one.'}
              />
            }
            columns={[
              { header: 'Part', cell: (p) => <strong>{p.part_number}</strong>, width: '12rem' },
              { header: 'Rev', cell: (p) => p.revision, width: '4rem' },
              { header: 'Name', cell: (p) => p.name },
              {
                header: 'State',
                cell: (p) => <Badge color={STATE_TONE[p.state] ?? '#58a6ff'}>{p.state}</Badge>,
                width: '12rem',
              },
              { header: 'Qty', cell: (p) => p.quantity, width: '5rem', align: 'right' },
              { header: 'Material', cell: (p) => p.target_material_code ?? '—', width: '9rem' },
              { header: 'Updated', cell: (p) => <span className="muted">{when(p.updated_at)}</span>, width: '12rem' },
            ]}
          />
        ) : null}
      </Panel>
    </div>
  )
}
