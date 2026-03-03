import path from 'path';
import { configDefaults, defineConfig } from 'vitest/config';

export default defineConfig({
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    exclude: [...configDefaults.exclude, 'src/__tests__/e2e/**'],
  },
});
