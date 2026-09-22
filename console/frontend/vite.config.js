import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5177,
    strictPort: false,
    proxy: {
      '/api': 'http://127.0.0.1:4730',
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 600,
  },
});
