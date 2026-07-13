import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Built output is served by the ZMM backend at /static/therapy/, so asset
// URLs must be relative (base './'), and the bundle lands straight in the
// app's static tree — run `pnpm build` here after editing src/.
export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir: '../../static/therapy',
    emptyOutDir: true,
    rollupOptions: {
      output: {
        // Stable names so rebuilds don't churn the committed static tree
        entryFileNames: 'assets/index-therapy.js',
        chunkFileNames: 'assets/[name]-therapy.js',
        assetFileNames: 'assets/[name][extname]',
      },
    },
  },
});
