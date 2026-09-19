import { useState, type FormEvent } from 'react'
import { useAuth } from '@/lib/auth'

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
      </form>
    </div>
  )
}
