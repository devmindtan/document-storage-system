/** @type {import('tailwindcss').Config} */
module.exports = {
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
      },
    },
  },
  plugins: [],
};
