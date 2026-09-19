import { useCallback, useEffect, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { request, subscribe, type EventFrame, type Plan, type Project, type Simulation } from '@/lib/api'
import { Badge, ErrorNote, Loading, Panel } from '@/components/ui'
import { ProjectIntake } from './ProjectIntake'
import { EngineeringReview } from './EngineeringReview'
import { ProcessPlanner } from './ProcessPlanner'
import { CamStudio } from './CamStudio'
import { DigitalTwin } from './DigitalTwin'
import { CostQuote } from './CostQuote'
import { NcRelease } from './NcRelease'
import { Analytics } from './Analytics'

const STAGES = [
  { key: 'intake', label: 'Intake' },
  { key: 'review', label: 'Engineering review' },
  { key: 'plan', label: 'Process planner' },
  { key: 'cam', label: 'CAM studio' },
  { key: 'twin', label: 'Digital twin' },
  { key: 'cost', label: 'Cost and quote' },
  { key: 'release', label: 'NC release' },
  { key: 'analytics', label: 'Production' },
] as const

type Stage = (typeof STAGES)[number]['key']

export function ProjectWorkspace() {
  const { projectId = '' } = useParams()
  const [project, setProject] = useState<Project | null>(null)
  const [stage, setStage] = useState<Stage>('intake')
  const [planId, setPlanId] = useState<string | null>(null)
  const [plan, setPlan] = useState<Plan | null>(null)
  const [simulation, setSimulation] = useState<Simulation | null>(null)
  const [events, setEvents] = useState<EventFrame[]>([])
  const [error, setError] = useState<unknown>(null)

  const reload = useCallback(async () => {
    try {
      setProject(await request<Project>(`/projects/${projectId}`))
    } catch (caught) {
      setError(caught)
    }
  }, [projectId])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    if (!planId) return
    void request<Plan>(`/plans/${planId}`).then(setPlan).catch(() => setPlan(null))
  }, [planId, stage])

  // Live workflow events: job progress, conflicts, gate and release changes.
  useEffect(() => {
    if (!projectId) return
    return subscribe((frame) => {
      setEvents((current) => [frame, ...current].slice(0, 40))
      if (frame.topic === 'project.state.changed' || frame.topic === 'release.status.changed') void reload()
    }, projectId)
  }, [projectId, reload])

  if (error) return <ErrorNote error={error} onRetry={reload} />
  if (!project) return <Loading what="the project" />

  return (
    <div className="project">
      <header className="project-head">
        <div>
          <Link to="/" className="back">
            ← Portfolio
          </Link>
          <h1>
            {project.part_number} rev {project.revision}
          </h1>
          <p className="muted">{project.name}</p>
        </div>
        <div className="project-state">
          <Badge color="#58a6ff">{project.state}</Badge>
          <span className="muted">quantity {project.quantity}</span>
          <span className="muted">{project.target_material_code ?? 'no material set'}</span>
        </div>
      </header>

      <nav className="stage-nav" aria-label="Workflow stages">
        {STAGES.map((entry) => (
          <button
            key={entry.key}
            type="button"
            className={stage === entry.key ? 'active' : ''}
            onClick={() => setStage(entry.key)}
          >
            {entry.label}
          </button>
        ))}
      </nav>

      {stage === 'intake' ? <ProjectIntake project={project} onChanged={reload} /> : null}
      {stage === 'review' ? <EngineeringReview project={project} onChanged={reload} /> : null}
      {stage === 'plan' ? (
        <ProcessPlanner project={project} selectedPlanId={planId} onSelectPlan={setPlanId} onChanged={reload} />
      ) : null}
      {stage === 'cam' ? <CamStudio plan={plan} onChanged={reload} /> : null}
      {stage === 'twin' ? <DigitalTwin plan={plan} onSimulation={setSimulation} onChanged={reload} /> : null}
      {stage === 'cost' ? <CostQuote plan={plan} simulation={simulation} /> : null}
      {stage === 'release' ? (
        <NcRelease project={project} plan={plan} simulation={simulation} onChanged={reload} />
      ) : null}
      {stage === 'analytics' ? <Analytics project={project} /> : null}

      {events.length ? (
        <Panel title="Live activity" subtitle="Domain events for this project, newest first.">
          <ul className="event-feed">
            {events.map((frame) => (
              <li key={`${frame.id}-${frame.sequence}`}>
                <code>{frame.topic}</code>
                <span className="muted">{summarise(frame)}</span>
              </li>
            ))}
          </ul>
        </Panel>
      ) : null}
    </div>
  )
}

function summarise(frame: EventFrame): string {
  const payload = frame.payload as Record<string, any>
  switch (frame.topic) {
    case 'job.progress':
      return `${payload.kind}: ${payload.stage} ${Math.round(Number(payload.percent ?? 0))}%`
    case 'simulation.completed':
      return `${payload.result} in ${Math.round(Number(payload.cycle_time_seconds ?? 0))} s`
    case 'engineering.conflict.detected':
      return `${payload.attribute} on ${payload.feature ?? 'the part'} (Δ ${payload.delta})`
    case 'plan.candidate.created':
      return `${payload.machine}: ${payload.operation_count} operations, ${payload.tool_count} tools`
    case 'release.status.changed':
      return `${payload.prior_state ?? '—'} → ${payload.new_state}`
    case 'geometry.version.created':
      return `revision ${payload.revision}${payload.scale_established ? '' : ' (scale not established)'}`
    default:
      return JSON.stringify(payload).slice(0, 120)
  }
}
