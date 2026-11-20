import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// In development the UI is served by Vite and API calls are proxied to the
// backend, so the browser only ever talks to one origin.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  test: { environment: "node" },
});
