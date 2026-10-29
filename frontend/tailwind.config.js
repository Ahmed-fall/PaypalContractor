/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["'IBM Plex Sans'", "ui-sans-serif", "system-ui", "sans-serif"],
        cond: ["'IBM Plex Sans Condensed'", "'IBM Plex Sans'", "sans-serif"],
      },
      colors: {
        // An auditor's working file: petrol ink on cool paper, red for money owed,
        // and a highlighter used only where a clause or an amount is being called out.
        ink: { DEFAULT: "#16302F", soft: "#3E5654", muted: "#6B7F7C" },
        paper: { DEFAULT: "#F2F4F3", card: "#FFFFFF", line: "#D9DEDC", rule: "#E8ECEA" },
        brand: { DEFAULT: "#16302F", dark: "#0E2221", soft: "#E3ECEA" },
        good: { DEFAULT: "#2E6B4F", soft: "#E4F0EA" },
        warn: { DEFAULT: "#8A5A00", soft: "#FBF1DA" },
        bad: { DEFAULT: "#B3261E", soft: "#FBE9E7" },
        hl: { DEFAULT: "#F3DF6B", soft: "#FBF3C4" },
      },
      borderRadius: { DEFAULT: "4px", md: "5px", lg: "6px", xl: "8px", "2xl": "10px" },
      boxShadow: { card: "none", pop: "0 12px 32px rgba(22,48,47,.18)" },
    },
  },
  plugins: [],
};
