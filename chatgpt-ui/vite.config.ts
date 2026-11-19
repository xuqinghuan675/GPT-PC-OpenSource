import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    host: "127.0.0.1",
    port: 1420,
    strictPort: true,
    watch: {
      ignored: [
        "**/.cargo/**",
        "**/.rustup/**",
        "**/src-tauri/target/**",
        "**/.edge-test/**",
        "**/.edge-output.html",
        "**/.edge-error.log",
        "**/.dsh-upstream/**",
      ],
    },
  },
  build: {
    target: "es2022",
    sourcemap: false,
  },
});
