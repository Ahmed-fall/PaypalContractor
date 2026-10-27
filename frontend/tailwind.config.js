/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Inter", "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ["'JetBrains Mono'", "ui-monospace", "SFMono-Regular", "monospace"],
      },
      colors: {
        ink: { DEFAULT: "#0f172a", soft: "#334155", muted: "#64748b" },
        paper: { DEFAULT: "#f6f7f9", card: "#ffffff", line: "#e2e8f0" },
        brand: { DEFAULT: "#0b5cff", dark: "#0848c8", soft: "#e8efff" },
        good: { DEFAULT: "#0f7a4f", soft: "#e6f4ee" },
        warn: { DEFAULT: "#a15c00", soft: "#fdf3e1" },
        bad: { DEFAULT: "#b42318", soft: "#fdecea" },
      },
      boxShadow: { card: "0 1px 2px rgba(15,23,42,.06), 0 1px 1px rgba(15,23,42,.04)" },
    },
  },
  plugins: [],
};
