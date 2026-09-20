import { BrowserRouter, Link, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { AuthProvider, useAuth } from '@/lib/auth'
import { apiBase } from '@/lib/api'
import { Login } from '@/pages/Login'
import { Portfolio } from '@/pages/Portfolio'
import { ProjectWorkspace } from '@/pages/ProjectWorkspace'
import { FactoryTwin } from '@/pages/FactoryTwin'
import { Loading } from '@/components/ui'

function Shell() {
  const { me, signOut } = useAuth()
  const location = useLocation()

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <strong>CNC Intelligence</strong>
          <span className="muted">engineering copilot</span>
        </div>
        <nav>
          <Link className={location.pathname === '/' ? 'active' : ''} to="/">
            Portfolio
          </Link>
          <Link className={location.pathname.startsWith('/factory') ? 'active' : ''} to="/factory">
            Factory twin
          </Link>
          <a href={`${apiBase()}/docs`} target="_blank" rel="noreferrer">
            API contract
          </a>
        </nav>
        <div className="who">
          <strong>{me?.full_name}</strong>
          <span className="muted">{me?.tenant.name}</span>
          <div className="roles">
            {me?.roles.map((role) => (
              <span key={role} className="role">
                {role.replace(/_/g, ' ')}
              </span>
            ))}
          </div>
          <button type="button" onClick={signOut}>
            Sign out
          </button>
        </div>
        <p className="disclaimer">
          Photo-derived geometry is provisional. A qualified engineer remains accountable for dimensions,
          workholding, tooling, postprocessing and release.
        </p>
      </aside>

      <main className="main">
        <Routes>
          <Route path="/" element={<Portfolio />} />
          <Route path="/projects/:projectId" element={<ProjectWorkspace />} />
          <Route path="/factory" element={<FactoryTwin />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  )
}

function Gate() {
  const { me, loading } = useAuth()
  if (loading) return <Loading what="your session" />
  if (!me) return <Login />
  return <Shell />
}

export default function App() {
  return (
    <AuthProvider>
      {/* Vite's build base becomes the router base, so the same bundle serves
          from a domain root or from a sub-path such as GitHub Pages' /cnc/. */}
      <BrowserRouter basename={import.meta.env.BASE_URL.replace(/\/$/, '')}>
        <Gate />
      </BrowserRouter>
    </AuthProvider>
  )
}
