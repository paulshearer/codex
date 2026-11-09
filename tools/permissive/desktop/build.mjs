import { build } from 'esbuild';
import { mkdirSync, copyFileSync } from 'node:fs';
import path from 'node:path';
import './create-icon.mjs';

const root = import.meta.dirname;
const output = path.join(root, 'dist');
mkdirSync(output, { recursive: true });
await build({ entryPoints: [path.join(root, 'entry.jsx')], bundle: true, outfile: path.join(output, 'renderer.js'),
  minify: true, sourcemap: false, target: 'es2022', define: { 'process.env.NODE_ENV': '"production"' } });
for (const file of ['index.html', 'style.css']) copyFileSync(path.join(root, file), path.join(output, file));
