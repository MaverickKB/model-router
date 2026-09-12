import { defineConfig } from "vite";

export default defineConfig({
  server: {
    host: "127.0.0.1",
    port: 4173,
    proxy: { "/api": "http://127.0.0.1:8690", "/v1": "http://127.0.0.1:8690" },
  },
  preview: { host: "127.0.0.1", port: 4173 },
});
