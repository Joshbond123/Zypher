import { useState, useEffect } from 'react'
import { Routes, Route, Navigate } from 'react-router-dom'
import { supabase } from './lib/supabase'
import Login from './pages/Login'
import Dashboard from './pages/Dashboard'
import TelegramConnect from './pages/TelegramConnect'
import NavBar from './components/NavBar'

export default function App() {
  const [session, setSession] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      setSession(data.session)
      setLoading(false)
    })
    const { data: { subscription } } = supabase.auth.onAuthStateChange((_e, s) => setSession(s))
    return () => subscription.unsubscribe()
  }, [])

  if (loading) return (
    <div className="min-h-screen flex items-center justify-center bg-cyber-900">
      <div className="text-center">
        <div className="text-4xl neon-text font-bold mb-4 animate-pulse">ZYPHER</div>
        <div className="text-gray-500 text-sm">Initializing OpenClaw...</div>
      </div>
    </div>
  )

  return (
    <div className="min-h-screen bg-cyber-900">
      {session && <NavBar session={session} />}
      <Routes>
        <Route path="/" element={!session ? <Login /> : <Navigate to="/dashboard" />} />
        <Route path="/dashboard" element={session ? <Dashboard session={session} /> : <Navigate to="/" />} />
        <Route path="/telegram" element={session ? <TelegramConnect session={session} /> : <Navigate to="/" />} />
        <Route path="*" element={<Navigate to="/" />} />
      </Routes>
    </div>
  )
}
