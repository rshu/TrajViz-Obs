import { defineConfig } from 'vite'
import solid from 'vite-plugin-solid'
import { isAbsolute } from 'node:path'

// OpenTUI's runtime JSX transform excludes node_modules. Ship compiled JSX
// while preserving the host's SDK, Solid and terminal-renderer instances.
export default defineConfig({
  plugins: [solid({ solid: { generate: 'universal', moduleName: '@opentui/solid' } })],
  build: {
    target: 'esnext',
    emptyOutDir: false,
    lib: { entry: 'src/tui.tsx', formats: ['es'], fileName: () => 'tui.js' },
    rollupOptions: {
      external: id => !id.startsWith('.') && !isAbsolute(id),
      output: { inlineDynamicImports: true },
    },
  },
})
