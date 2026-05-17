import { useState, useEffect } from 'react'
import { supabase } from '../lib/supabase'
import { Key, Plus, Trash2, RotateCcw, Cpu, TrendingUp } from 'lucide-react'

export default function CerebrasKeys({ session }) {
  const [keys, setKeys] = useState([])
  const [newKey, setNewKey] = useState('')
  const [newLabel, setNewLabel] = useState('')
  const [loading, setLoading] = useState(true)
  const [adding, setAdding] = useState(false)
  const [error, setError] = useState('')
  const [rotIdx, setRotIdx] = useState(0)

  useEffect(() => { load() }, [])

  const load = async () => {
    const { data } = await supabase
      .from('cerebras_keys')
      .select('*')
      .eq('user_id', session.user.id)
      .order('created_at', { ascending: true })
    setKeys(data || [])
    const { data: rs } = await supabase
      .from('key_rotation_state')
      .select('current_key_index')
      .eq('user_id', session.user.id)
      .single()
    setRotIdx(rs?.current_key_index || 0)
    setLoading(false)
  }

  const add = async (e) => {
    e.preventDefault()
    setError(''); setAdding(true)
    try {
      const key = newKey.trim()
      if (!key.startsWith('csk-') && !key.startsWith('cs-')) {
        if (!confirm('This key format looks unusual. Add anyway?')) { setAdding(false); return }
      }
      const { error } = await supabase.from('cerebras_keys').insert({
        user_id: session.user.id,
        api_key: key,
        label: newLabel.trim() || `Key ${keys.length + 1}`,
        is_active: true,
      })
      if (error) throw error
      setNewKey(''); setNewLabel('')
      load()
    } catch (err) { setError(err.message) }
    finally { setAdding(false) }
  }

  const toggle = async (id, current) => {
    await supabase.from('cerebras_keys').update({ is_active: !current }).eq('id', id)
    load()
  }

  const remove = async (id) => {
    if (!confirm('Delete this key?')) return
    await supabase.from('cerebras_keys').delete().eq('id', id)
    load()
  }

  const fmtKey = (k) => k.slice(0, 10) + '••••••••' + k.slice(-4)
  const fmtNum = (n) => n >= 1000 ? (n/1000).toFixed(1)+'k' : String(n)

  const totalReqs = keys.reduce((s, k) => s + (k.requests_count || 0), 0)
  const totalTok = keys.reduce((s, k) => s + (k.tokens_used || 0), 0)
  const activeKeys = keys.filter(k => k.is_active)
  const currentKey = activeKeys[rotIdx % Math.max(activeKeys.length, 1)]

  if (loading) return <div className="flex justify-center py-20 text-gray-500 text-sm">Loading...</div>

  return (
    <div className="max-w-3xl mx-auto px-4 py-8">
      <div className="flex items-center gap-3 mb-8">
        <Key size={20} className="text-cyber-green" />
        <h1 className="text-xl font-bold neon-text tracking-widest">CEREBRAS API KEYS</h1>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-3 gap-3 mb-6">
        {[
          { label: 'Total Keys', value: String(keys.length), icon: Key, color: 'text-cyber-green' },
          { label: 'Total Requests', value: fmtNum(totalReqs), icon: TrendingUp, color: 'text-cyber-cyan' },
          { label: 'Tokens Used', value: fmtNum(totalTok), icon: Cpu, color: 'text-purple-400' },
        ].map(({ label, value, icon: Icon, color }) => (
          <div key={label} className="cyber-card p-4 text-center">
            <Icon size={14} className={`${color} mx-auto mb-2`} />
            <div className={`text-lg font-bold ${color}`}>{value}</div>
            <div className="text-xs text-gray-500">{label}</div>
          </div>
        ))}
      </div>

      {/* Rotation info */}
      {activeKeys.length > 0 && (
        <div className="cyber-card p-4 mb-6 bg-green-900/5 border-green-800/20">
          <div className="flex items-center gap-2 mb-2">
            <RotateCcw size={13} className="text-cyber-green" />
            <span className="text-xs font-semibold text-gray-300 uppercase tracking-wider">Round-Robin Rotation</span>
          </div>
          <div className="text-xs text-gray-400">
            Currently active: <span className="text-cyber-green font-mono">{currentKey?.label || '—'}</span>
            {' '}· {activeKeys.length} key{activeKeys.length !== 1 ? 's' : ''} in rotation
          </div>
        </div>
      )}

      {/* Add key form */}
      <div className="cyber-card p-6 mb-6">
        <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider mb-4 flex items-center gap-2">
          <Plus size={14} className="text-cyber-green" /> Add Cerebras Key
        </h2>
        <form onSubmit={add} className="space-y-3">
          <div className="flex gap-3">
            <div className="flex-1">
              <input
                value={newLabel} onChange={e => setNewLabel(e.target.value)}
                placeholder="Label (e.g. Primary Key)"
                className="cyber-input w-full px-3 py-2.5 rounded text-sm"
              />
            </div>
            <div className="flex-[2]">
              <input
                value={newKey} onChange={e => setNewKey(e.target.value)} required
                placeholder="csk-xxxxxxxxxxxxxxxxxxxx"
                className="cyber-input w-full px-3 py-2.5 rounded text-sm font-mono"
              />
            </div>
          </div>
          {error && <div className="text-red-400 text-xs bg-red-900/20 border border-red-800/40 rounded px-3 py-2">{error}</div>}
          <button type="submit" disabled={adding} className="cyber-btn px-6 py-2.5 rounded text-sm uppercase tracking-widest">
            {adding ? '[ Adding... ]' : '[ Add Key ]'}
          </button>
        </form>
      </div>

      {/* Keys list */}
      <div className="cyber-card">
        <div className="p-4 border-b border-green-900/20">
          <h2 className="text-sm font-semibold text-gray-300 uppercase tracking-wider">Your Keys ({keys.length})</h2>
        </div>
        {keys.length === 0 ? (
          <div className="text-center py-12 text-gray-600 text-sm">
            <Key size={24} className="mx-auto mb-3 opacity-30" />
            No keys yet. Add your Cerebras API keys above.
          </div>
        ) : (
          <div className="divide-y divide-green-900/10">
            {keys.map((k, i) => {
              const isCurrentRot = activeKeys.length > 0 && currentKey?.id === k.id
              return (
                <div key={k.id} className={`p-4 flex items-center gap-4 ${isCurrentRot ? 'bg-green-900/5' : ''}`}>
                  <div className="flex-shrink-0">
                    <div className={`w-2 h-2 rounded-full ${k.is_active ? 'bg-cyber-green shadow-[0_0_6px_#00ff88]' : 'bg-gray-600'}`} />
                  </div>
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-gray-200">{k.label}</span>
                      {isCurrentRot && <span className="text-xs bg-green-900/30 text-cyber-green px-1.5 py-0.5 rounded border border-green-800/40">ACTIVE</span>}
                    </div>
                    <div className="text-xs font-mono text-gray-500 mt-0.5">{fmtKey(k.api_key)}</div>
                    <div className="flex gap-4 mt-1 text-xs text-gray-600">
                      <span>{fmtNum(k.requests_count || 0)} reqs</span>
                      <span>{fmtNum(k.tokens_used || 0)} tokens</span>
                      {k.last_used_at && <span>Last: {new Date(k.last_used_at).toLocaleDateString()}</span>}
                    </div>
                  </div>
                  <div className="flex items-center gap-2 flex-shrink-0">
                    <button onClick={() => toggle(k.id, k.is_active)}
                      className={`px-2.5 py-1 rounded text-xs border transition-colors ${k.is_active ? 'border-green-700/40 text-cyber-green hover:bg-green-900/20' : 'border-gray-700 text-gray-500 hover:text-gray-300'}`}>
                      {k.is_active ? 'ON' : 'OFF'}
                    </button>
                    <button onClick={() => remove(k.id)}
                      className="p-1.5 text-gray-600 hover:text-red-400 transition-colors">
                      <Trash2 size={13} />
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </div>

      <div className="mt-4 text-xs text-gray-600 text-center">
        Keys are stored encrypted · Auto-rotated round-robin across all requests · Most powerful model auto-detected
      </div>
    </div>
  )
}
