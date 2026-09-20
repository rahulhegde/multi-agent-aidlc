import { defineConfig } from "vite";

export default defineConfig({
  envDir: "..",
  server: {
    host: "127.0.0.1",
    port: 5173,
  },
});
