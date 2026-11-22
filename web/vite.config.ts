import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the Vite server (port 5173) forwards API calls to FastAPI (port 8000),
// so the browser sees a single origin and no CORS setup is needed.
const API = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174, // 5173 is often taken by other local Vite projects
    strictPort: true,
    proxy: {
      "/chat": API,
      "/trails": API,
      "/scenarios": API,
      "/health": API,
      "/pipeline": API,
      "/geocode": API,
    },
  },
});
