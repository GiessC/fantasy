import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, Vite serves the UI and proxies /api to the FastAPI backend, so the
// frontend never needs to know the backend's port. In production the backend
// serves the built assets itself and no proxy is involved.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.FANTASY_AI_API ?? "http://127.0.0.1:8765",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
