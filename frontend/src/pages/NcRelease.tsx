import { useCallback, useEffect, useState } from 'react'
import { download, request, type Job, type MasterRecord, type NCProgram, type Plan, type Project, type Release, type Simulation, apiBase, getToken } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { Badge, DataTable, Empty, ErrorNote, FindingCard, GateCard, KeyValue, Panel, Loading } from '@/components/ui'
import { JobWatcher } from '@/components/JobWatcher'
import { shortHash, when } from '@/lib/format'

export function NcRelease({
  project,
  plan,
  simulation,
  onChanged,
}: {
  project: Project
  plan: Plan | null
  simulation: Simulation | null
  onChanged: () => void
}) {
  const { can, me } = useAuth()
  const [posts, setPosts] = useState<MasterRecord[]>([])
  const [programs, setPrograms] = useState<NCProgram[]>([])
  const [releases, setReleases] = useState<Release[]>([])
  const [activeRelease, setActiveRelease] = useState<Release | null>(null)
  const [checklist, setChecklist] = useState<Array<{ key: string; label: string; auto_state: boolean; evidence: string | null }>>([])
  const [signed, setSigned] = useState<Record<string, boolean>>({})
  const [postId, setPostId] = useState('')
  const [programText, setProgramText] = useState<string | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [error, setError] = useState<unknown>(null)

  const load = useCallback(async () => {
    try {
      const [postList, releaseList] = await Promise.all([
        request<MasterRecord[]>('/factory/posts'),
        request<Release[]>(`/projects/${project.id}/releases`),
      ])
      setPosts(postList)
      setReleases(releaseList)
      setPostId((current) => current || String(postList.find((p) => p.certified && p.enabled)?.id ?? postList[0]?.id ?? ''))
      if (plan) setPrograms(await request<NCProgram[]>(`/plans/${plan.id}/nc-programs`))
      const latest = releaseList[0] ?? null
      setActiveRelease(latest)
    } catch (caught) {
      setError(caught)
    }
  }, [project.id, plan])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (!activeRelease) {
      setChecklist([])
      return
    }
    void request<{ items: typeof checklist }>(`/releases/${activeRelease.id}/checklist`)
      .then((response) => {
        setChecklist(response.items)
        setSigned(Object.fromEntries(response.items.map((item) => [item.key, false])))
      })
      .catch(setError)
  }, [activeRelease])

  const postprocess = async () => {
    if (!plan || !simulation) return
    setError(null)
    try {
      const job = await request<Job>('/nc-programs:postprocess', {
        body: { plan_id: plan.id, simulation_id: simulation.id, post_id: postId },
      })
      setJobId(job.id)
    } catch (caught) {
      setError(caught)
    }
  }

  const prepare = async () => {
    if (!plan || !simulation) return
    const valid = programs.filter((p) => p.validation_passed)
    if (!valid.length) {
      setError({ code: 'no_valid_program', message: 'No validated NC program is available to release' })
      return
    }
    try {
      const release = await request<Release>('/releases', {
        body: { plan_id: plan.id, simulation_id: simulation.id, nc_program_ids: valid.map((p) => p.id) },
      })
      await load()
      setActiveRelease(release)
    } catch (caught) {
      setError(caught)
    }
  }

  const sign = async () => {
    if (!activeRelease) return
    const code = window.prompt('Enter the current code from your second factor:')
    if (!code) return
    const statement = window.prompt('Approval statement for the audit trail:')
    if (!statement) return
    try {
      await request(`/releases/${activeRelease.id}:approve`, {
        body: { totp_code: code, statement, checklist: signed },
      })
      await load()
      onChanged()
    } catch (caught) {
      setError(caught)
    }
  }

  const fetchPackage = async () => {
    if (!activeRelease) return
    const { blob, filename, controlled } = await download(`/releases/${activeRelease.id}/package`)
    if (!controlled) {
      window.alert('This package is not released. It is watermarked as an uncontrolled copy and must not be run.')
    }
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = filename
    anchor.click()
    URL.revokeObjectURL(url)
  }

  const viewProgram = async (program: NCProgram) => {
    const response = await fetch(
      `${apiBase()}/v1/nc-programs/${program.id}/text`,
      { headers: { Authorization: `Bearer ${getToken()}` } },
    )
    setProgramText(await response.text())
  }

  if (!plan) return <Empty title="Select a plan" />
  const allSigned = checklist.length > 0 && checklist.every((item) => signed[item.key])

  return (
    <div className="workspace">
      {error ? <ErrorNote error={error} /> : null}

      <Panel
        title="Postprocessing"
        subtitle="Output is produced only through an enabled post certified for this exact machine and controller."
        actions={
          can('nc:postprocess') ? (
            <>
              <label className="inline">
                Postprocessor
                <select value={postId} onChange={(e) => setPostId(e.target.value)}>
                  {posts.map((post) => (
                    <option key={String(post.id)} value={String(post.id)}>
                      {String(post.code)} r{String(post.revision)} → {String(post.machine_code)}
                      {post.certified ? '' : ' (not certified)'}
                      {post.revoked ? ' (revoked)' : ''}
                    </option>
                  ))}
                </select>
              </label>
              <button type="button" onClick={() => void postprocess()} disabled={!simulation || Boolean(jobId)}>
                Generate NC
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
            }}
            onFailed={() => setJobId(null)}
          />
        ) : null}
        <DataTable
          rows={programs}
          rowKey={(p) => p.id}
          onRowClick={(p) => void viewProgram(p)}
          empty={<Empty title="No NC candidate yet" hint="A passing simulation and a certified post are required." />}
          columns={[
            { header: 'Program', cell: (p) => <strong>{p.program_number}</strong>, width: '8rem' },
            { header: 'Blocks', cell: (p) => p.line_count.toLocaleString(), width: '7rem', align: 'right' },
            {
              header: 'Validation',
              cell: (p) => (
                <Badge color={p.validation_passed ? '#3fb950' : '#f85149'}>
                  {p.validation_passed ? 'passed' : (p.max_severity ?? 'failed')}
                </Badge>
              ),
              width: '9rem',
            },
            { header: 'Program hash', cell: (p) => <code>{shortHash(p.program_hash, 10)}</code>, width: '10rem' },
            { header: 'IR hash', cell: (p) => <code>{shortHash(p.ir_hash, 10)}</code>, width: '10rem' },
            { header: 'Status', cell: (p) => p.status, width: '8rem' },
          ]}
        />
        {programs.some((p) => p.validations.length) ? (
          <>
            <h3>Validation findings</h3>
            {programs.flatMap((program) =>
              program.validations.map((finding, index) => (
                <FindingCard key={`${program.id}-${index}`} finding={finding as any} />
              )),
            )}
          </>
        ) : null}
        {programText ? (
          <details open>
            <summary>NC program text</summary>
            <pre className="nc-text">{programText}</pre>
          </details>
        ) : null}
      </Panel>

      <Panel
        title="NC release"
        subtitle="Only a current, simulated and certified package may be signed, by a user holding release authority."
        actions={
          <>
            {can('nc:postprocess') ? (
              <button type="button" onClick={() => void prepare()} disabled={!simulation}>
                Prepare release candidate
              </button>
            ) : null}
            {activeRelease ? (
              <button type="button" onClick={() => void fetchPackage()}>
                Download package
              </button>
            ) : null}
          </>
        }
      >
        <DataTable
          rows={releases}
          rowKey={(r) => r.id}
          selectedId={activeRelease?.id}
          onRowClick={setActiveRelease}
          empty={<Empty title="No release candidate yet" />}
          columns={[
            { header: 'Rev', cell: (r) => r.revision, width: '4rem' },
            {
              header: 'Status',
              cell: (r) => (
                <Badge
                  color={
                    r.status === 'Released' ? '#3fb950' : r.status === 'Superseded' ? '#8b949e' : r.status === 'Rejected' ? '#f85149' : '#58a6ff'
                  }
                >
                  {r.status}
                </Badge>
              ),
              width: '10rem',
            },
            { header: 'Package hash', cell: (r) => <code>{shortHash(r.package_hash, 12)}</code>, width: '12rem' },
            { header: 'Released', cell: (r) => <span className="muted">{when(r.released_at)}</span>, width: '12rem' },
          ]}
        />

        {activeRelease ? (
          <div className="release-detail">
            {activeRelease.gate_results?.length ? <GateCard gate={activeRelease.gate_results[activeRelease.gate_results.length - 1]} /> : null}

            <h3>Release checklist</h3>
            {checklist.length ? (
              <ul className="checklist">
                {checklist.map((item) => (
                  <li key={item.key}>
                    <label>
                      <input
                        type="checkbox"
                        checked={signed[item.key] ?? false}
                        disabled={activeRelease.status === 'Released'}
                        onChange={(e) => setSigned((current) => ({ ...current, [item.key]: e.target.checked }))}
                      />
                      {item.label}
                    </label>
                    <span className="muted">
                      {item.auto_state ? 'system verified' : 'requires the approver to confirm'}
                      {item.evidence ? ` — ${item.evidence}` : ''}
                    </span>
                  </li>
                ))}
              </ul>
            ) : (
              <Loading what="the checklist" />
            )}

            {activeRelease.status !== 'Released' ? (
              can('nc:release') ? (
                <div className="sign-row">
                  <button type="button" onClick={() => void sign()} disabled={!allSigned}>
                    Sign release
                  </button>
                  {!me?.mfa_enabled ? (
                    <span className="error-text">Enrol a second factor before signing.</span>
                  ) : (
                    <span className="muted">Signing requires a current code from your second factor.</span>
                  )}
                </div>
              ) : (
                <p className="muted">
                  Your roles do not include release authority. Signing is a separate privilege from postprocessing.
                </p>
              )
            ) : (
              <KeyValue
                rows={[
                  ['package hash', <code key="h">{activeRelease.package_hash}</code>],
                  ['signature', <code key="s">{activeRelease.package_signature}</code>],
                  ['released at', when(activeRelease.released_at)],
                ]}
              />
            )}

            <details>
              <summary>Release manifest</summary>
              <pre>{JSON.stringify(activeRelease.manifest, null, 2)}</pre>
            </details>
          </div>
        ) : null}
      </Panel>
    </div>
  )
}
