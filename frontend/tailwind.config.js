export default {
  content: ["./index.html", "./src/**/*.{js,jsx}"],
  theme: {
    extend: {
      colors: {
        cyber: {
          900: '#0a0a0f',
          800: '#0d0d1a',
          700: '#111124',
          600: '#1a1a2e',
          500: '#16213e',
          400: '#0f3460',
          green: '#00ff88',
          cyan: '#00ffff',
          purple: '#9b59ff',
          red: '#ff3366',
        }
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'Fira Code', 'Consolas', 'monospace'],
      },
      animation: {
        'pulse-slow': 'pulse 3s cubic-bezier(0.4,0,0.6,1) infinite',
        'scan': 'scan 2s linear infinite',
        'glow': 'glow 2s ease-in-out infinite alternate',
      },
      keyframes: {
        scan: { '0%': { top: '0%' }, '100%': { top: '100%' } },
        glow: {
          '0%': { boxShadow: '0 0 5px #00ff88, 0 0 10px #00ff88' },
          '100%': { boxShadow: '0 0 20px #00ff88, 0 0 40px #00ff88, 0 0 80px #00ff88' },
        },
      }
    },
  },
  plugins: [],
}
