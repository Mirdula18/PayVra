import path from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      // Dev-only: forward API calls to the FastAPI backend on :8000.
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
