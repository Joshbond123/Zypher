import { Link, useLocation, useNavigate } from 'react-router-dom'
import { supabase } from '../lib/supabase'
import { Terminal, MessageSquare, Key, LogOut, Zap } from 'lucide-react'

export default function NavBar({ session }) {
  const loc = useLocation()
  const nav = useNavigate()

  const logout = async () => {
    await supabase.auth.signOut()
    nav('/')
  }

  const links = [
    { to: '/dashboard', label: 'Dashboard', icon: Terminal },
    { to: '/telegram', label: 'Telegram', icon: MessageSquare },
    { to: '/keys', label: 'API Keys', icon: Key },
  ]

  return (
    <nav className="border-b border-green-900/40 bg-cyber-800/80 backdrop-blur-md sticky top-0 z-50">
      <div className="max-w-6xl mx-auto px-4 flex items-center justify-between h-14">
        <div className="flex items-center gap-6">
          <Link to="/dashboard" className="flex items-center gap-2">
            <Zap size={18} className="text-cyber-green" />
            <span className="neon-text font-bold text-lg tracking-widest">ZYPHER</span>
          </Link>
          <div className="hidden sm:flex items-center gap-1">
            {links.map(({ to, label, icon: Icon }) => (
              <Link
                key={to}
                to={to}
                className={`flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium transition-all ${
                  loc.pathname === to
                    ? 'bg-green-900/30 text-cyber-green border border-green-700/40'
                    : 'text-gray-400 hover:text-gray-200'
                }`}
              >
                <Icon size={13} />
                {label}
              </Link>
            ))}
          </div>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-xs text-gray-500 hidden sm:block">{session.user.email}</span>
          <button onClick={logout} className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-red-400 transition-colors px-2 py-1.5">
            <LogOut size={13} />
            <span className="hidden sm:inline">Exit</span>
          </button>
        </div>
      </div>
    </nav>
  )
}
