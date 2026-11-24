import { copyFile } from "node:fs/promises";
import { build } from "esbuild";

await build({
  entryPoints: { app: "src/main.tsx" },
  bundle: true,
  minify: true,
  define: { "process.env.NODE_ENV": '"production"' },
  format: "esm",
  target: ["es2022"],
  outdir: "dist/assets",
});
await copyFile("index.html", "dist/index.html");
