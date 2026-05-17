import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { supabase } from '../lib/supabase'
import { Terminal, MessageSquare, Key, Activity, Zap, Clock, Shield, AlertCircle } from 'lucide-react'

export default function Dashboard({ session }) {
  const [telegramConn, setTelegramConn] = useState(null)
  const [keyCount, setKeyCount] = useState(0)
  const [msgCount, setMsgCount] = useState(0)
  const [activeSession, setActiveSession] = useState(null)
  const [loading, setLoading] = useState(true)
  const nav = useNavigate()

  useEffect(() => {
    loadData()
    const iv = setInterval(loadData, 30000)
    return () => clearInterval(iv)
  }, [])

  const loadData = async () => {
    const uid = session.user.id
    const [tg, keys, msgs, sess] = await Promise.all([
      supabase.from('telegram_connections').select('*').eq('user_id', uid).single(),
      supabase.from('cerebras_keys').select('id', { count: 'exact' }).eq('user_id', uid).eq('is_active', true),
      supabase.from('chat_messages').select('id', { count: 'exact' }).eq('user_id', uid),
      supabase.from('active_sessions').select('*').eq('is_active', true).order('started_at', { ascending: false }).limit(1),
    ])
    setTelegramConn(tg.data)
    setKeyCount(keys.count || 0)
    setMsgCount(msgs.count || 0)
    setActiveSession(sess.data?.[0] || null)
    setLoading(false)
  }

  const fmtTime = (iso) => {
    if (!iso) return '—'
    return new Date(iso).toLocaleString()
  }

  const cards = [
    {
      label: 'Agent Status',
      value: activeSession ? '🟢 ONLINE' : '🔴 OFFLINE',
      sub: activeSession ? `Session ${activeSession.github_run_id?.slice(0,8)}` : 'No active session',
      icon: Activity,
      color: activeSession ? 'text-cyber-green' : 'text-red-400',
      action: null,
    },
    {
      label: 'Telegram',
      value: telegramConn ? '✓ CONNECTED' : '✗ NOT LINKED',
      sub: telegramConn ? `ID: ${telegramConn.telegram_user_id}` : 'Connect to chat with Zypher',
      icon: MessageSquare,
      color: telegramConn ? 'text-cyber-green' : 'text-yellow-400',
      action: !telegramConn ? () => nav('/telegram') : null,
      actionLabel: 'Connect Now',
    },
    {
      label: 'Cerebras Keys',
      value: String(keyCount),
      sub: keyCount === 0 ? 'No keys — add some' : `${keyCount} active key${keyCount !== 1 ? 's' : ''} rotating`,
      icon: Key,
      color: 'text-cyber-cyan',
      action: () => nav('/keys'),
      actionLabel: 'Manage Keys',
    },
    {
      label: 'Messages',
      value: String(msgCount),
      sub: 'Total interactions logged',
      icon: Terminal,
      color: 'text-purple-400',
      action: null,
    },
  ]

  return (
    <div className="max-w-6xl mx-auto px-4 py-8">
      {/* Header */}
      <div className="mb-8">
        <div className="flex items-center gap-3 mb-2">
          <Zap size={22} className="text-cyber-green" />
          <h1 className="text-2xl font-bold neon-text tracking-widest">ZYPHER CONTROL PANEL</h1>
        </div>
        <p className="text-gray-500 text-xs">OpenClaw AI Platform — Kali Linux Instance</p>
      </div>

      {/* Warning if no telegram */}
      {!loading && !telegramConn && (
        <div className="mb-6 flex items-start gap-3 bg-yellow-900/10 border border-yellow-700/30 rounded p-4">
          <AlertCircle size={16} className="text-yellow-400 flex-shrink-0 mt-0.5" />
          <div>
            <p className="text-yellow-300 text-sm font-medium">Telegram not connected</p>
            <p className="text-yellow-600 text-xs mt-1">You must connect your Telegram account to communicate with Zypher.</p>
            <button onClick={() => nav('/telegram')} className="cyber-btn px-3 py-1.5 rounded text-xs mt-2">Connect Telegram →</button>
          </div>
        </div>
      )}

      {/* Stat cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        {cards.map(({ label, value, sub, icon: Icon, color, action, actionLabel }) => (
          <div key={label} className="cyber-card p-5">
            <div className="flex items-start justify-between mb-3">
              <Icon size={16} className={color} />
              <span className="text-xs text-gray-600 uppercase tracking-wider">{label}</span>
            </div>
            <div className={`text-xl font-bold mb-1 font-mono ${color}`}>{value}</div>
            <div className="text-xs text-gray-500">{sub}</div>
            {action && (
              <button onClick={action} className="mt-3 text-xs text-cyber-green hover:underline">{actionLabel} →</button>
            )}
          </div>
        ))}
      </div>

      {/* Session info */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="cyber-card p-6">
          <div className="flex items-center gap-2 mb-4">
            <Clock size={14} className="text-cyber-green" />
            <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">Active Session</h3>
          </div>
          {activeSession ? (
            <div className="space-y-2 font-mono text-xs">
              <div className="flex justify-between">
                <span className="text-gray-500">Run ID</span>
                <span className="text-gray-300">{activeSession.github_run_id?.slice(0,12)}...</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Instance #</span>
                <span className="text-cyber-green">{activeSession.instance_number}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Started</span>
                <span className="text-gray-300">{fmtTime(activeSession.started_at)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Expires</span>
                <span className="text-yellow-400">{fmtTime(activeSession.expires_at)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-gray-500">Platform</span>
                <span className="text-cyber-cyan">Kali Linux / GitHub Actions</span>
              </div>
            </div>
          ) : (
            <div className="text-center py-6 text-gray-600 text-sm">
              <div className="text-2xl mb-2">💤</div>
              No active OpenClaw session
            </div>
          )}
        </div>

        <div className="cyber-card p-6">
          <div className="flex items-center gap-2 mb-4">
            <Shield size={14} className="text-cyber-green" />
            <h3 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">How to Use Zypher</h3>
          </div>
          <div className="space-y-3 text-xs text-gray-400">
            <div className="flex gap-2">
              <span className="text-cyber-green flex-shrink-0">01</span>
              <span>Connect your Telegram account in the Telegram tab</span>
            </div>
            <div className="flex gap-2">
              <span className="text-cyber-green flex-shrink-0">02</span>
              <span>Add your Cerebras API keys for AI inference</span>
            </div>
            <div className="flex gap-2">
              <span className="text-cyber-green flex-shrink-0">03</span>
              <span>Open Telegram and message <strong className="text-gray-300">@Zypher0_bot</strong></span>
            </div>
            <div className="flex gap-2">
              <span className="text-cyber-green flex-shrink-0">04</span>
              <span>Use <code className="bg-green-900/20 px-1">!command</code> to run Kali tools directly</span>
            </div>
            <div className="flex gap-2">
              <span className="text-cyber-green flex-shrink-0">05</span>
              <span>Ask Zypher to search, hack, research, or analyze anything</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
