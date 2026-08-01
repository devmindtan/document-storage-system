/** @type {import('tailwindcss').Config} */
module.exports = {
  darkMode: "class",
  content: ["./templates/**/*.html"],
  theme: {
    extend: {
      fontFamily: { sans: ['"Plus Jakarta Sans"', "sans-serif"] },
      colors: {
        brand: {
          50: "#f0f5fa",
          100: "#e1ebf4",
          500: "#3b82f6",
          800: "#1b4d78",
          900: "#0a2540",
          accent: "#c9852a",
        },
        // Alias tên ngữ nghĩa cho các màu status đã dùng rải rác khắp
        // template (emerald/amber/rose/sky) — cùng giá trị hex mặc định
        // của Tailwind, chỉ đặt tên lại để dùng chung 1 chuẩn.
        success: {
          50: "#ecfdf5",
          100: "#d1fae5",
          500: "#10b981",
          600: "#059669",
          700: "#047857",
        },
        warning: {
          50: "#fffbeb",
          100: "#fef3c7",
          500: "#f59e0b",
          600: "#d97706",
          700: "#b45309",
        },
        danger: {
          50: "#fff1f2",
          100: "#ffe4e6",
          500: "#f43f5e",
          600: "#e11d48",
          700: "#be123c",
        },
        info: {
          50: "#f0f9ff",
          100: "#e0f2fe",
          500: "#0ea5e9",
          600: "#0284c7",
          700: "#0369a1",
        },
      },
      fontSize: {
        "page-title": ["32px", { lineHeight: "1.25" }],
        "section-title": ["24px", { lineHeight: "1.3" }],
        "card-title": ["18px", { lineHeight: "1.4" }],
        caption: ["12px", { lineHeight: "1.4" }],
      },
    },
  },
  plugins: [],
};
