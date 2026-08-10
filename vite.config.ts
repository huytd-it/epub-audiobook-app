import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { VitePWA } from "vite-plugin-pwa";

export default defineConfig({
  root: "frontend",
  base: "/",
  build: { outDir: "../app/spa_dist", emptyOutDir: true },
  server: { port: 5173, proxy: { "/api": "http://127.0.0.1:8000", "/books": "http://127.0.0.1:8000", "/queue": "http://127.0.0.1:8000", "/video": "http://127.0.0.1:8000", "/music": "http://127.0.0.1:8000", "/photos": "http://127.0.0.1:8000", "/voices": "http://127.0.0.1:8000" } },
  plugins: [react(), VitePWA({
    registerType: "autoUpdate",
    includeAssets: ["studio-mark.svg"],
    manifest: {
      name: "Xưởng Sách Nói", short_name: "Xưởng Sách", lang: "vi",
      description: "Biên tập EPUB, dựng sách nói và xuất bản video.",
      theme_color: "#181711", background_color: "#181711", display: "standalone", start_url: "/",
      icons: [{ src: "/studio-mark.svg", sizes: "any", type: "image/svg+xml", purpose: "any maskable" }]
    },
    workbox: {
      navigateFallback: "/index.html",
      runtimeCaching: [{ urlPattern: /^https?:.*\/(api|books|queue|video|music|photos|voices)\//, handler: "NetworkOnly" }]
    }
  })]
});
