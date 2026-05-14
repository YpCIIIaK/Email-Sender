/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./templates/**/*.html"],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['JetBrains Mono', 'monospace'],
      },
      colors: {
        brand: {
          50:  '#eef4ff',
          100: '#dbe6ff',
          200: '#bdd1ff',
          300: '#90b0ff',
          400: '#6285ff',
          500: '#3b5cff',
          600: '#2a3ef5',
          700: '#222fd1',
          800: '#1f2ba6',
          900: '#1e2a82',
        }
      },
      boxShadow: {
        'glow': '0 0 0 1px rgba(59,92,255,0.35), 0 8px 24px -8px rgba(59,92,255,0.35)',
      }
    },
  },
  plugins: [],
}