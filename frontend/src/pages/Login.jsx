import { useState } from 'react'
import { supabase } from '../lib/supabase'
import { Zap, Eye, EyeOff, Terminal } from 'lucide-react'

export default function Login() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [showPw, setShowPw] = useState(false)
  const [mode, setMode] = useState('login')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const submit = async (e) => {
    e.preventDefault()
    setError(''); setSuccess(''); setLoading(true)
    try {
      if (mode === 'login') {
        const { error } = await supabase.auth.signInWithPassword({ email, password })
        if (error) throw error
      } else {
        const { data, error } = await supabase.auth.signUp({ email, password })
        if (error) throw error
        if (data?.user) {
          await supabase.from('profiles').upsert({ id: data.user.id, email })
          setSuccess('Account created! Check your email to confirm.')
          setMode('login')
        }
      }
    } catch (err) {
      setError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center p-4 relative overflow-hidden">
      {/* Grid background */}
      <div className="absolute inset-0" style={{
        backgroundImage: 'linear-gradient(rgba(0,255,136,0.03) 1px, transparent 1px), linear-gradient(90deg, rgba(0,255,136,0.03) 1px, transparent 1px)',
        backgroundSize: '40px 40px'
      }} />

      <div className="relative w-full max-w-md">
        {/* Logo */}
        <div className="text-center mb-8">
          <div className="inline-flex items-center justify-center w-16 h-16 rounded-full bg-green-900/20 border border-green-700/40 mb-4 scanline">
            <Zap size={28} className="text-cyber-green" />
          </div>
          <h1 className="text-4xl font-bold neon-text tracking-widest">ZYPHER</h1>
          <p className="text-gray-500 text-xs mt-2 tracking-widest uppercase">OpenClaw AI Platform</p>
          <div className="flex items-center justify-center gap-2 mt-3">
            <span className="status-online" />
            <span className="text-xs text-green-400">Kali Linux Instance Active</span>
          </div>
        </div>

        {/* Card */}
        <div className="cyber-card p-8 scanline">
          <div className="flex mb-6 border-b border-green-900/30">
            {['login','register'].map(m => (
              <button key={m} onClick={() => { setMode(m); setError(''); setSuccess('') }}
                className={`flex-1 pb-3 text-xs uppercase tracking-widest font-medium transition-colors ${
                  mode === m ? 'text-cyber-green border-b-2 border-cyber-green -mb-px' : 'text-gray-500 hover:text-gray-300'
                }`}>
                {m === 'login' ? 'Access System' : 'Register'}
              </button>
            ))}
          </div>

          <form onSubmit={submit} className="space-y-4">
            <div>
              <label className="block text-xs text-gray-400 mb-1.5 uppercase tracking-wider">Email</label>
              <input
                type="email" value={email} onChange={e => setEmail(e.target.value)} required
                placeholder="operator@zypher.io"
                className="cyber-input w-full px-4 py-3 rounded text-sm"
              />
            </div>
            <div className="relative">
              <label className="block text-xs text-gray-400 mb-1.5 uppercase tracking-wider">Password</label>
              <input
                type={showPw ? 'text' : 'password'} value={password} onChange={e => setPassword(e.target.value)} required
                placeholder="••••••••••••"
                className="cyber-input w-full px-4 py-3 pr-10 rounded text-sm"
              />
              <button type="button" onClick={() => setShowPw(!showPw)}
                className="absolute right-3 top-[34px] text-gray-500 hover:text-gray-300">
                {showPw ? <EyeOff size={15} /> : <Eye size={15} />}
              </button>
            </div>

            {error && <div className="text-red-400 text-xs bg-red-900/20 border border-red-800/40 rounded px-3 py-2">{error}</div>}
            {success && <div className="text-green-400 text-xs bg-green-900/20 border border-green-800/40 rounded px-3 py-2">{success}</div>}

            <button type="submit" disabled={loading}
              className="cyber-btn w-full py-3 rounded text-sm uppercase tracking-widest mt-2">
              {loading ? '[ Processing... ]' : mode === 'login' ? '[ Access Zypher ]' : '[ Create Account ]'}
            </button>
          </form>

          <div className="mt-6 pt-4 border-t border-green-900/20 flex items-start gap-2">
            <Terminal size={12} className="text-gray-600 mt-0.5 flex-shrink-0" />
            <p className="text-xs text-gray-600">
              Access restricted to authorized operators. Telegram connection required to interact with the AI agent.
            </p>
          </div>
        </div>

        <div className="text-center mt-4 text-xs text-gray-700">
          Powered by OpenClaw · Kali Linux · Local Qwen GGUF
        </div>
      </div>
    </div>
  )
}
