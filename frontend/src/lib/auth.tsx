import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { request, setToken, getToken, type Me } from './api'

interface AuthState {
  me: Me | null
  loading: boolean
  error: string | null
  signIn: (email: string, password: string) => Promise<void>
  signOut: () => void
  can: (permission: string) => boolean
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null)
  const [loading, setLoading] = useState(Boolean(getToken()))
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!getToken()) return
    request<Me>('/auth/me')
      .then(setMe)
      .catch(() => setToken(null))
      .finally(() => setLoading(false))
  }, [])

  const signIn = useCallback(async (email: string, password: string) => {
    setError(null)
    setLoading(true)
    try {
      const token = await request<{ access_token: string }>('/auth/token', { body: { email, password } })
      setToken(token.access_token)
      setMe(await request<Me>('/auth/me'))
    } catch (caught) {
      setToken(null)
      setError(caught instanceof Error ? caught.message : 'Sign in failed')
      throw caught
    } finally {
      setLoading(false)
    }
  }, [])

  const signOut = useCallback(() => {
    setToken(null)
    setMe(null)
  }, [])

  const can = useCallback((permission: string) => Boolean(me?.permissions.includes(permission)), [me])

  const value = useMemo(() => ({ me, loading, error, signIn, signOut, can }), [me, loading, error, signIn, signOut, can])
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside AuthProvider')
  return context
}
