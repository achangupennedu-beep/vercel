/** @type {import('next').NextConfig} */
const nextConfig = {
  typescript: {
    ignoreBuildErrors: true,
  },
  images: {
    unoptimized: true,
  },

  // dukascopy-node is a heavy Node.js library that uses optional dev deps
  // (cli-highlight, prettier) at runtime. Mark it — and its dependency chain —
  // as server-side external so Next.js uses native require() instead of
  // bundling them, avoiding "module not found" build errors.
  serverExternalPackages: [
    'dukascopy-node',
    'fastest-validator',
    'cli-highlight',
    'prettier',
  ],

  // Next.js 16 uses Turbopack by default. An empty turbopack config silences
  // the "webpack config present but no turbopack config" startup error.
  // The webpack externals are already handled by serverExternalPackages above.
  turbopack: {},
}

export default nextConfig
