import path from "path";
import react from "@vitejs/plugin-react";
import { defineConfig, ProxyOptions } from "vite";
import { VitePWA } from "vite-plugin-pwa";

const BACKEND = process.env.BACKEND_URL || "http://127.0.0.1:8000";

/** Tiền tố đường dẫn do FastAPI phục vụ. */
const API_PREFIXES = [
  "/api",
  "/books",
  "/queue",
  "/video",
  "/music",
  "/photos",
  "/voices",
  "/media",
  "/gameplay",
  "/text-studio",
  "/drive",
  "/youtube",
  "/logs",
  "/effects",
  "/database-io",
  "/flows",
  "/local-bridge",
  "/production-settings",
  "/health",
  "/bootstrap",
  "/pick-files",
  "/pick-folder",
];

/**
 * Nhiều route của SPA trùng tên với route API (/books, /video, /logs...).
 * Nếu proxy tất cả sang FastAPI thì khi mở thẳng http://localhost:5173/books/1
 * trình duyệt sẽ nhận index.html của bản build trong app/spa_dist — tức là code cũ,
 * không có HMR. Vì vậy chỉ điều hướng trang (document request) mới được giữ lại cho Vite,
 * còn fetch/XHR/audio vẫn đi tới backend.
 */
const SPA_ROUTES = [
  /^\/$/,
  /^\/books$/,
  /^\/books\/upload$/,
  /^\/books\/\d+(\/.*)?$/,
  /^\/(upload|queue|video|music|photos|voices|media|gameplay|tools|youtube|drive|database-io|flows|logs|effects|production-defaults)$/,
];

const isSpaNavigation = (req: { url?: string; method?: string; headers: Record<string, any> }) => {
  if (req.method && req.method !== "GET") return false;
  const dest = req.headers["sec-fetch-dest"];
  const accept = String(req.headers.accept || "");
  const isDocument = dest ? dest === "document" : accept.includes("text/html");
  if (!isDocument) return false;
  // Link tải file (`<a download>`) cũng là document request nên phải loại trừ theo đường dẫn.
  const pathname = (req.url || "/").split("?")[0];
  return SPA_ROUTES.some((pattern) => pattern.test(pathname));
};

const proxy: Record<string, ProxyOptions> = Object.fromEntries(
  API_PREFIXES.map((prefix) => [
    prefix,
    {
      target: BACKEND,
      changeOrigin: true,
      bypass: (req) => (isSpaNavigation(req as any) ? "/index.html" : undefined),
    } satisfies ProxyOptions,
  ])
);

export default defineConfig({
  root: "frontend",
  base: "/",
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./frontend/src"),
    },
  },
  build: { outDir: "../app/spa_dist", emptyOutDir: true },
  server: { port: 5173, proxy },
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      includeAssets: ["studio-mark.svg"],
      // Service worker chỉ dành cho bản build; bật ở dev sẽ cache asset cũ và phá HMR.
      devOptions: { enabled: false },
      manifest: {
        name: "Xưởng Sách Nói",
        short_name: "Xưởng Sách",
        lang: "vi",
        description: "Biên tập EPUB, dựng sách nói và xuất bản video.",
        theme_color: "#F5F6F3",
        background_color: "#F5F6F3",
        display: "standalone",
        start_url: "/",
        icons: [{ src: "/studio-mark.svg", sizes: "any", type: "image/svg+xml", purpose: "any maskable" }],
      },
      workbox: {
        cleanupOutdatedCaches: true,
        navigateFallback: "/index.html",
        // Tải file (audio/video/zip) và tài liệu API không được trả về index.html.
        navigateFallbackDenylist: [
          /\/(audio|video|download|export|export-batch)(\/|$)/,
          /^\/docs$/,
          /^\/redoc$/,
          /^\/openapi\.json$/,
        ],
        runtimeCaching: [
          {
            urlPattern:
              /^https?:.*\/(api|books|queue|video|music|photos|voices|media|gameplay|text-studio|drive|youtube|logs|effects|database-io|flows|local-bridge)\//,
            handler: "NetworkOnly",
          },
        ],
      },
    }),
  ],
});
