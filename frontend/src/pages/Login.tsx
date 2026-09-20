import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useAuth } from '@/lib/auth'
import { apiBase, apiBaseIsOverride, setApiBase } from '@/lib/api'

const DEMO_ACCOUNTS = [
  ['engineer@example.com', 'Project and reverse engineering'],
  ['manufacturing@example.com', 'Manufacturing engineering and CAM'],
  ['cam@example.com', 'CAM programming'],
  ['estimator@example.com', 'Estimating'],
  ['quality@example.com', 'Quality and production records'],
  ['approver@example.com', 'NC release authority'],
  ['admin@example.com', 'Administration and post certification'],
]

export function Login() {
  const { signIn, error, loading } = useAuth()
  const [email, setEmail] = useState('manufacturing@example.com')
  const [password, setPassword] = useState('Pilot2026!')

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    try {
      await signIn(email, password)
    } catch {
      /* the error is surfaced from the auth context */
    }
  }

  return (
    <div className="login">
      <form onSubmit={submit} className="login-card">
        <h1>CNC Manufacturing Intelligence</h1>
        <p className="muted">
          An engineering copilot. Deterministic services perform every engineering calculation and a qualified
          engineer remains accountable for release.
        </p>

        <label>
          Email
          <input value={email} onChange={(e) => setEmail(e.target.value)} type="email" autoComplete="username" required />
        </label>
        <label>
          Password
          <input
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            type="password"
            autoComplete="current-password"
            required
          />
        </label>

        {error ? <p className="error-text">{error}</p> : null}
        <button type="submit" disabled={loading}>
          {loading ? 'Signing in…' : 'Sign in'}
        </button>

        <details className="login-accounts">
          <summary>Pilot accounts (password Pilot2026!)</summary>
          <ul>
            {DEMO_ACCOUNTS.map(([address, role]) => (
              <li key={address}>
                <button type="button" onClick={() => setEmail(address)}>
                  {address}
                </button>
                <span className="muted">{role}</span>
              </li>
            ))}
          </ul>
          <p className="muted">
            Each persona holds only its own permissions, so a CAM programmer cannot sign a release and an
            estimator cannot create a project.
          </p>
        </details>

        <ApiEndpoint />
      </form>
    </div>
  )
}

/**
 * Which API this page talks to, with a live reachability probe.
 *
 * A static deployment of the app (GitHub Pages, a Render static site) is only
 * useful once it can reach an API, and a wrong endpoint otherwise surfaces as
 * the browser's bare "Failed to fetch". This shows the resolved base, whether
 * /health answers from here, and lets the endpoint be changed without a rebuild.
 */
function ApiEndpoint() {
  const base = apiBase()
  const [draft, setDraft] = useState(base)
  const [state, setState] = useState<'checking' | 'ok' | 'unreachable'>('checking')
  const [version, setVersion] = useState<string | null>(null)
  const panel = useRef<HTMLDetailsElement>(null)

  useEffect(() => {
    const controller = new AbortController()
    fetch(`${base}/health`, { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(String(response.status))
        const body = (await response.json()) as { version?: string }
        setVersion(body.version ?? null)
        setState('ok')
      })
      .catch(() => {
        if (!controller.signal.aborted) setState('unreachable')
      })
    return () => controller.abort()
  }, [base])

  // Open the panel once when the probe fails; leave it alone afterwards so it
  // never snaps shut under someone typing a new endpoint.
  useEffect(() => {
    if (state === 'unreachable' && panel.current) panel.current.open = true
  }, [state])

  const apply = (event: FormEvent) => {
    event.preventDefault()
    setApiBase(draft)
  }

  return (
    <details className="login-endpoint" ref={panel}>
      <summary>
        API endpoint: <code>{base || 'same origin'}</code>{' '}
        {state === 'checking' ? <span className="muted">checking…</span> : null}
        {state === 'ok' ? <span className="ok-text">reachable{version ? `, v${version}` : ''}</span> : null}
        {state === 'unreachable' ? <span className="error-text">not reachable</span> : null}
      </summary>
      <div className="login-endpoint-form">
        <label>
          API base URL
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="https://cnc-platform-api.onrender.com"
            inputMode="url"
            autoComplete="off"
          />
        </label>
        <div className="login-endpoint-actions">
          <button type="button" onClick={apply}>
            Use this API
          </button>
          {apiBaseIsOverride() ? (
            <button type="button" onClick={() => setApiBase('')}>
              Reset to default
            </button>
          ) : null}
        </div>
        <p className="muted">
          Any deployment of this app can use any API you run. Append <code>?api=https://…</code> to the address
          to set it from a link. A free instance that has gone to sleep takes up to a minute to answer its first
          request. If the API is up but still shows as not reachable, its <code>MIP_CORS_ORIGINS</code> must
          include this site&apos;s origin, <code>{window.location.origin}</code>.
        </p>
      </div>
    </details>
  )
}
