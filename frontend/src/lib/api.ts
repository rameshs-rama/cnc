/**
 * Typed API client.
 *
 * Errors carry the platform's machine-readable code and detail, so a workspace
 * can render the specific recovery action rather than a generic failure.
 */

const BASE_STORAGE_KEY = 'mip.apiBase'

/**
 * Where the API lives, resolved once per page load:
 *
 *   1. `?api=https://…` on the address bar. It is persisted and then removed
 *      from the URL, so one link can point a static deployment (GitHub Pages,
 *      a Render static site) at any API without a rebuild. A bare `?api=`
 *      clears the persisted choice.
 *   2. The choice persisted by (1) or by the sign-in page.
 *   3. `VITE_API_BASE`, inlined at build time.
 *   4. Nothing: same origin, behind the reverse proxy of the Compose stack.
 *
 * Only http(s) URLs are accepted. The value ends up in fetch URLs and in an
 * href, so anything else — a javascript: URL arriving through a crafted link —
 * is dropped rather than followed.
 */
function resolveApiBase(): string {
  const params = new URLSearchParams(window.location.search)
  const fromUrl = params.get('api')
  let override = ''
  if (fromUrl !== null) {
    override = normaliseApiBase(fromUrl)
    persistApiBase(override)
    params.delete('api')
    const query = params.toString()
    window.history.replaceState(
      null,
      '',
      `${window.location.pathname}${query ? `?${query}` : ''}${window.location.hash}`,
    )
  }
  return override || readApiBase() || normaliseApiBase(import.meta.env.VITE_API_BASE ?? '')
}

function normaliseApiBase(raw: string): string {
  const value = raw.trim()
  if (!value) return ''
  try {
    const url = new URL(value)
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return ''
    return `${url.origin}${url.pathname}`.replace(/\/$/, '')
  } catch {
    return ''
  }
}

function readApiBase(): string {
  try {
    return normaliseApiBase(localStorage.getItem(BASE_STORAGE_KEY) ?? '')
  } catch {
    return ''
  }
}

function persistApiBase(value: string) {
  try {
    if (value) localStorage.setItem(BASE_STORAGE_KEY, value)
    else localStorage.removeItem(BASE_STORAGE_KEY)
  } catch {
    /* storage unavailable: the choice lasts for this page load only */
  }
}

const BASE = resolveApiBase()

/** The API base in use: an absolute URL, or '' for the same origin. */
export function apiBase(): string {
  return BASE
}

/** True when the base came from a link or the sign-in page rather than the build. */
export function apiBaseIsOverride(): boolean {
  return readApiBase() !== ''
}

/**
 * Change the API base. The client resolves it once per page load, so this
 * persists the choice and reloads; nothing half-configured survives.
 */
export function setApiBase(next: string) {
  persistApiBase(normaliseApiBase(next))
  window.location.reload()
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

/** A fetch that never reached the API: wrong base, CORS, offline, or asleep. */
function unreachable(cause: unknown): ApiError {
  const where = BASE || 'the same origin'
  return new ApiError(
    0,
    'unreachable',
    `Cannot reach the API at ${where}. Check the API endpoint on the sign-in page.`,
    { cause: String(cause) },
  )
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly detail?: unknown,
    readonly traceId?: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }

  /** Gate findings, when the failure was a blocked safety gate. */
  get findings(): Finding[] {
    const detail = this.detail as { findings?: Finding[] } | undefined
    return detail?.findings ?? []
  }
}

let token: string | null = localStorage.getItem('mip.token')

export function setToken(next: string | null) {
  token = next
  if (next) localStorage.setItem('mip.token', next)
  else localStorage.removeItem('mip.token')
}

export function getToken() {
  return token
}

type Options = { method?: string; body?: unknown; form?: FormData; signal?: AbortSignal }

export async function request<T>(path: string, options: Options = {}): Promise<T> {
  const headers: Record<string, string> = {}
  if (token) headers.Authorization = `Bearer ${token}`
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'

  let response: Response
  try {
    response = await fetch(`${BASE}/v1${path}`, {
      method: options.method ?? (options.body || options.form ? 'POST' : 'GET'),
      headers,
      body: options.form ?? (options.body !== undefined ? JSON.stringify(options.body) : undefined),
      signal: options.signal,
    })
  } catch (caught) {
    if (isAbort(caught)) throw caught
    throw unreachable(caught)
  }

  if (response.status === 204) return undefined as T

  const text = await response.text()
  const payload = text ? safeParse(text) : null

  if (!response.ok) {
    const body = (payload ?? {}) as { code?: string; message?: string; detail?: unknown; trace_id?: string }
    throw new ApiError(
      response.status,
      body.code ?? 'error',
      body.message ?? `${response.status} ${response.statusText}`,
      body.detail,
      body.trace_id ?? response.headers.get('X-Trace-Id') ?? undefined,
    )
  }
  return payload as T
}

function safeParse(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

export async function download(path: string): Promise<{ blob: Blob; filename: string; controlled: boolean }> {
  let response: Response
  try {
    response = await fetch(`${BASE}/v1${path}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
  } catch (caught) {
    throw unreachable(caught)
  }
  if (!response.ok) throw new ApiError(response.status, 'download_failed', 'Download failed')
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const match = /filename="?([^"]+)"?/.exec(disposition)
  return {
    blob: await response.blob(),
    filename: match?.[1] ?? 'download',
    controlled: response.headers.get('X-Controlled') === 'true',
  }
}

/** Subscribe to the server-sent event stream for live job and gate updates. */
export function subscribe(onEvent: (frame: EventFrame) => void, projectId?: string): () => void {
  const query = new URLSearchParams()
  if (projectId) query.set('project_id', projectId)
  // EventSource cannot set headers, so the token travels as a query parameter
  // on this read-only stream; the server scopes every frame to the tenant.
  if (token) query.set('access_token', token)

  let closed = false
  let controller: AbortController | null = null

  const run = async () => {
    while (!closed) {
      controller = new AbortController()
      try {
        const response = await fetch(`${BASE}/v1/events/stream?${query}`, {
          headers: token ? { Authorization: `Bearer ${token}` } : {},
          signal: controller.signal,
        })
        if (!response.body) throw new Error('no stream')
        const reader = response.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        while (!closed) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += decoder.decode(value, { stream: true })
          const chunks = buffer.split('\n\n')
          buffer = chunks.pop() ?? ''
          for (const chunk of chunks) {
            const data = chunk.split('\n').find((line) => line.startsWith('data:'))
            if (!data) continue
            try {
              onEvent(JSON.parse(data.slice(5).trim()) as EventFrame)
            } catch {
              /* keep-alive or malformed frame */
            }
          }
        }
      } catch {
        if (closed) return
      }
      // Reconnect with a short backoff rather than hammering the API.
      await new Promise((resolve) => setTimeout(resolve, 3000))
    }
  }
  void run()

  return () => {
    closed = true
    controller?.abort()
  }
}

// --------------------------------------------------------------------- types
export type Severity = 'S1' | 'S2' | 'S3' | 'S4'
export type VerificationStatus = 'Confirmed' | 'Measured' | 'Inferred' | 'Verification Required' | 'Unknown'

export interface Finding {
  code: string
  severity: Severity
  message: string
  consequence: string
  recommendation: string
  object_ref: string | null
  detail: Record<string, unknown>
  waived_by: string | null
  waivable: boolean
  blocking: boolean
}

export interface GateResult {
  gate: string
  passed: boolean
  blocks: string
  max_severity: Severity | null
  target_kind: string
  target_id: string | null
  findings: Finding[]
}

export interface EventFrame {
  id: string
  topic: string
  project_id: string | null
  sequence: number
  payload: Record<string, unknown>
}

export interface Project {
  id: string
  part_number: string
  revision: string
  name: string
  state: string
  quantity: number
  unit_system: string
  intended_use: string | null
  target_material_code: string | null
  due_date: string | null
  provenance_declaration: Record<string, unknown>
  version: number
  created_at: string
  updated_at: string
}

export interface Artifact {
  id: string
  filename: string
  kind: string
  media_type: string
  byte_size: number
  content_hash: string
  authority: string
  authority_overridden: boolean
  authority_override_reason: string | null
  status: string
  parser: string | null
  parse_warnings: string[]
  extracted: Record<string, any>
  capture_metrics: Record<string, unknown>
  created_at: string
}

export interface Observation {
  id: string
  attribute: string
  feature_key: string | null
  value: number | null
  value_text: string | null
  unit: string
  original_representation: string | null
  authority: string
  authority_rank: number
  method: string
  confidence: number
  uncertainty: number | null
  status: VerificationStatus
  criticality: string
  disposition: string
  disposition_reason: string | null
  source_artifact_id: string | null
  source_region: Record<string, unknown>
  superseded_by_id: string | null
  version: number
}

export interface Conflict {
  id: string
  attribute: string
  feature_key: string | null
  observation_ids: string[]
  authoritative_observation_id: string | null
  delta: number | null
  severity: Severity
  summary: string
  resolved: boolean
}

export interface FeatureView {
  stable_key: string
  label: string
  type: string
  parameters: Record<string, any>
  access: number[]
  support: string
  support_reason: string | null
  criticality: string
  status: VerificationStatus
  confidence: number
  tolerance: Record<string, unknown>
  thread_spec: string | null
  surface_finish_ra: number | null
  observations?: Observation[]
  carried_from_previous_revision?: boolean
}

export interface ConfidenceMap {
  geometry_version_id: string
  revision: number
  status: string
  provisional: boolean
  scale: { established: boolean; source: string | null; uncertainty_mm: number | null }
  quality_report: Record<string, any>
  part_model: Record<string, any>
  status_counts: Record<string, number>
  features: FeatureView[]
  part_observations: Observation[]
  gate: GateResult
}

export interface Surface {
  geometry_version_id: string
  revision: number
  units: string
  grid: { x0: number; y0: number; nx: number; ny: number; pitch: number; downsample: number }
  bounds: { min: number[]; max: number[] }
  z_top: number
  z_bottom: number
  height: number[][]
  outline: number[][]
  stock: { min: number[]; max: number[] }
}

export interface GeometryVersion {
  id: string
  project_id: string
  revision: number
  parent_id: string | null
  units: string
  scale_established: boolean
  scale_source: string | null
  scale_uncertainty_mm: number | null
  provisional: boolean
  status: string
  content_hash: string
  quality_report: Record<string, any>
  part_model: Record<string, any>
  created_at: string
  version: number
}

export interface Plan {
  id: string
  project_id: string
  label: string
  geometry_version_id: string
  machine_version_id: string
  fixture_version_id: string | null
  material_id: string | null
  objective: Record<string, unknown>
  candidate_rank: number
  objective_score: number
  score_breakdown: Record<string, any>
  stock: Record<string, any>
  decision_record: Record<string, any>
  status: string
  stale: boolean
  stale_reason: string | null
  content_hash: string
  version: number
  created_at: string
}

export interface Operation {
  id: string
  sequence: number
  operation_type: string
  feature_keys: string[]
  tool_assembly_id: string
  parameters: Record<string, any>
  parameter_rationale: Record<string, any>
  coolant: string
  suppressed: boolean
}

export interface Setup {
  id: string
  sequence: number
  name: string
  work_offset: string
  orientation_deg: number[]
  index_position: Record<string, unknown>
  clearance_plane_mm: number
  setup_minutes: number
  datum_scheme: Record<string, any>
  operations: Operation[]
}

export interface Feasibility {
  machine_version_id: string
  machine_code: string | null
  machine_name: string | null
  controller: string | null
  kinematics: string | null
  hourly_rate: number | null
  feasible: boolean
  cause_code: string | null
  binding_constraint: string | null
  detail: Record<string, any>
  plan_id: string | null
}

export interface ToolpathView {
  id: string
  operation_id: string
  setup_id: string | null
  setup_name: string | null
  setup_sequence: number | null
  sequence: number | null
  operation_type: string | null
  label: string | null
  feature_keys: string[]
  tool: { id: string; code: string; cutter: Record<string, any> } | null
  parameters: Record<string, any>
  parameter_rationale: Record<string, any>
  generator: string
  generator_version: string
  move_count: number
  cutting_length_mm: number
  rapid_length_mm: number
  content_hash: string
  stale: boolean
  moves?: Move[]
}

export interface Move {
  t: 'rapid' | 'linear' | 'plunge' | 'arc' | 'drill' | 'dwell'
  x?: number
  y?: number
  z?: number
  f?: number
  cycle?: Record<string, any>
}

export interface SimulationEvent {
  severity: Severity
  code: string
  message: string
  consequence: string
  recommendation: string
  time_s: number
  setup: string | null
  operation_id: string | null
  move_index: number | null
  entities: string[]
  position: number[] | null
  detail: Record<string, any>
  occurrences: number
}

export interface Simulation {
  id: string
  plan_id: string
  engine_version: string
  identity_hash: string
  input_hashes: Record<string, unknown>
  voxel_size_mm: number
  passed: boolean
  events: SimulationEvent[]
  event_counts: Record<string, number>
  max_severity: Severity | null
  cycle_time_seconds: number
  time_breakdown: Record<string, any>
  stock_comparison: Record<string, any>
  programmed_envelope: Record<string, any>
  stale: boolean
  dispositions: Array<Record<string, unknown>>
  created_at: string
}

export interface CostEstimate {
  id: string
  plan_id: string
  currency: string
  quantity: number
  rates: Record<string, any>
  assumptions: Record<string, any>
  breakdown: Record<string, number>
  unit_cost: number
  unit_price: number
  quantity_breaks: Array<{ quantity: number; unit_cost: number; unit_price: number; batch_total: number }>
  sensitivity: Record<string, any>
  stale: boolean
  created_at: string
}

export interface NCProgram {
  id: string
  plan_id: string
  program_number: string
  line_count: number
  program_hash: string
  ir_hash: string
  validations: Finding[]
  validation_passed: boolean
  max_severity: Severity | null
  programmed_envelope: Record<string, any>
  status: string
  stale: boolean
  created_at: string
}

export interface Release {
  id: string
  project_id: string
  plan_id: string
  revision: number
  nc_program_ids: string[]
  manifest: Record<string, any>
  package_hash: string
  package_signature: string | null
  gate_results: GateResult[]
  status: string
  superseded_by_id: string | null
  released_at: string | null
  version: number
  created_at: string
}

export interface Job {
  id: string
  kind: string
  project_id: string | null
  status: string
  stage: string
  percent: number
  payload: Record<string, unknown>
  result: Record<string, any>
  error: string | null
  attempts: number
  trace_id: string
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface Me {
  id: string
  email: string
  full_name: string
  roles: string[]
  permissions: string[]
  mfa_enabled: boolean
  tenant: {
    id: string
    name: string
    unit_system: string
    currency: string
    allow_self_approval: boolean
    critical_confidence_threshold: number
    conflict_tolerance_mm: number
  }
}

export interface MasterRecord {
  id: string
  code: string
  revision?: number
  name?: string
  status?: string
  [key: string]: unknown
}
