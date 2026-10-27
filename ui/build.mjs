import { build } from 'esbuild';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const result = await build({
  entryPoints: [path.join(here, 'src/panel.ts')],
  bundle: true,
  write: false,
  minify: true,
  format: 'esm',
  target: ['es2022'],
  legalComments: 'inline',
  metafile: true,
});
const css = await readFile(path.join(here, 'src/panel.css'), 'utf8');
const js = result.outputFiles[0].text.replace(/<\/script/gi, '<\\/script');
const bundledPackages = new Set();
for (const input of Object.keys(result.metafile.inputs)) {
  const match = input.replaceAll('\\', '/').match(/node_modules\/((?:@[^/]+\/)?[^/]+)/);
  if (match) bundledPackages.add(match[1]);
}
const notices = [];
for (const name of [...bundledPackages].sort()) {
  const folder = path.join(here, 'node_modules', name);
  const pkg = JSON.parse(await readFile(path.join(folder, 'package.json'), 'utf8'));
  let license = '';
  for (const filename of ['LICENSE', 'LICENSE.md', 'LICENSE.txt', 'license']) {
    try { license = await readFile(path.join(folder, filename), 'utf8'); break; } catch {}
  }
  if (!license) throw new Error(`A license notice is required for bundled dependency ${name}`);
  notices.push(`${name} ${pkg.version}\n${license.trim()}`);
}
const licenseNotice = notices.join('\n\n' + '='.repeat(72) + '\n\n').replaceAll('-->', '-- >');
const html = `<!doctype html>
<!-- Third-party licenses for the bundled browser code:\n${licenseNotice}\n-->
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="color-scheme" content="dark"><title>Aster</title><style>${css}</style></head>
<body><div id="aster-root"><div class="boot"><span class="boot-mark">✦</span><h1>Aster</h1><p>Connecting to your workspace…</p></div></div><noscript>Aster needs JavaScript to display your workspace. You can still use Aster tools in chat.</noscript><script type="module">${js}</script></body></html>`;
const output = path.resolve(here, '../coordination/ui/aster-panel.html');
await mkdir(path.dirname(output), { recursive: true });
await writeFile(output, html);
await writeFile(path.join(here, 'THIRD_PARTY_NOTICES.txt'), licenseNotice + '\n');
console.log(`Built coordination/ui/aster-panel.html (${Buffer.byteLength(html).toLocaleString()} bytes; all assets inline)`);
