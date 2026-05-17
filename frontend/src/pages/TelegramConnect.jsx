import { useState, useEffect } from 'react'
import { supabase } from '../lib/supabase'
import { MessageSquare, CheckCircle, Trash2, ExternalLink, Info } from 'lucide-react'

export default function TelegramConnect({ session }) {
  const [conn, setConn] = useState(null)
  const [telegramId, setTelegramId] = useState('')
  const [botToken, setBotToken] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  useEffect(() => { load() }, [])

  const load = async () => {
    const { data } = await supabase
      .from('telegram_connections')
      .select('*')
      .eq('user_id', session.user.id)
      .single()
    setConn(data)
    setLoading(false)
  }

  const save = async (e) => {
    e.preventDefault()
    setError(''); setSaving(true)
    try {
      const tid = parseInt(telegramId)
      if (isNaN(tid)) throw new Error('Telegram User ID must be a number')
      const payload = {
        user_id: session.user.id,
        telegram_user_id: tid,
        bot_token: botToken.trim(),
        is_active: true,
      }
      const { error } = await supabase.from('telegram_connections').upsert(payload, { onConflict: 'user_id' })
      if (error) throw error
      setSuccess('Telegram account connected! You can now chat with @Zypher0_bot')
      setTelegramId(''); setBotToken('')
      load()
    } catch (err) { setError(err.message) }
    finally { setSaving(false) }
  }

  const remove = async () => {
    if (!confirm('Disconnect Telegram?')) return
    await supabase.from('telegram_connections').delete().eq('user_id', session.user.id)
    setConn(null); setSuccess('Telegram disconnected.')
  }

  if (loading) return <div className="flex justify-center py-20 text-gray-500 text-sm">Loading...</div>

  return (
    <div className="max-w-2xl mx-auto px-4 py-8">
      <div className="flex items-center gap-3 mb-8">
        <MessageSquare size={20} className="text-cyber-green" />
        <h1 className="text-xl font-bold neon-text tracking-widest">TELEGRAM CONNECTION</h1>
      </div>

      {/* Current connection */}
      {conn && (
        <div className="cyber-card p-5 mb-6 border-green-700/40">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <CheckCircle size={18} className="text-cyber-green" />
              <div>
                <div className="text-sm font-medium text-cyber-green">Connected</div>
                <div className="text-xs text-gray-400 mt-0.5">ID: {conn.telegram_user_id}</div>
              </div>
            </div>
            <button onClick={remove} className="cyber-btn-red cyber-btn px-3 py-1.5 rounded text-xs flex items-center gap-1.5">
              <Trash2 size={12} /> Disconnect
            </button>
          </div>
          <div className="mt-4 pt-4 border-t border-green-900/20">
            <div className="flex items-center gap-2 text-xs text-green-400">
              <span className="status-online" />
              You can now chat with <strong>@Zypher0_bot</strong> on Telegram
            </div>
            <a href="https://t.me/Zypher0_bot" target="_blank" rel="noreferrer"
              className="inline-flex items-center gap-1.5 mt-2 text-xs text-cyber-cyan hover:underline">
              Open @Zypher0_bot <ExternalLink size={11} />
            </a>
          </div>
        </div>
      )}

      {/* Instructions */}
      <div className="cyber-card p-5 mb-6 bg-blue-900/5 border-blue-800/20">
        <div className="flex items-start gap-2">
          <Info size={14} className="text-blue-400 flex-shrink-0 mt-0.5" />
          <div className="text-xs text-gray-400 space-y-2">
            <p className="font-medium text-gray-300">How to get your Telegram User ID:</p>
            <ol className="list-decimal list-inside space-y-1 ml-1">
              <li>Open Telegram and message <strong className="text-gray-300">@userinfobot</strong></li>
              <li>It will reply with your User ID (a number like 123456789)</li>
              <li>Enter that number below</li>
            </ol>
            <p className="mt-3 font-medium text-gray-300">Bot Token (optional — use system bot):</p>
            <p>Leave blank to use the default Zypher bot (<strong className="text-gray-300">@Zypher0_bot</strong>), or enter your own BotFather token.</p>
          </div>
        </div>
      </div>

      {/* Form */}
      {!conn && (
        <div className="cyber-card p-6">
          <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider mb-5">Connect Your Account</h2>
          <form onSubmit={save} className="space-y-4">
            <div>
              <label className="block text-xs text-gray-400 mb-1.5 uppercase tracking-wider">Telegram User ID *</label>
              <input
                value={telegramId} onChange={e => setTelegramId(e.target.value)} required
                placeholder="e.g. 6317345496"
                className="cyber-input w-full px-4 py-3 rounded text-sm"
              />
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1.5 uppercase tracking-wider">Bot Token (optional)</label>
              <input
                value={botToken} onChange={e => setBotToken(e.target.value)}
                placeholder="Leave blank for default @Zypher0_bot"
                className="cyber-input w-full px-4 py-3 rounded text-sm"
              />
            </div>
            {error && <div className="text-red-400 text-xs bg-red-900/20 border border-red-800/40 rounded px-3 py-2">{error}</div>}
            {success && <div className="text-green-400 text-xs bg-green-900/20 border border-green-800/40 rounded px-3 py-2">{success}</div>}
            <button type="submit" disabled={saving} className="cyber-btn w-full py-3 rounded text-sm uppercase tracking-widest">
              {saving ? '[ Connecting... ]' : '[ Connect Telegram ]'}
            </button>
          </form>
        </div>
      )}
    </div>
  )
}
