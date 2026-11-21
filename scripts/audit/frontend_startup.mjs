#!/usr/bin/env node
// What the manager page parses before its first paint: the static import graph
// of every <script type="module"> in static/index.html, plus its classic
// scripts, by size. import() edges are listed but not followed — they are what
// a tab loads on first open.
//   node scripts/audit/frontend_startup.mjs [--top N] [--json | --importers]
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const STATIC = path.join(REPO, 'static');
const args = process.argv.slice(2);
const top = Number(args[args.indexOf('--top') + 1]) || 40;

const html = fs.readFileSync(path.join(STATIC, 'index.html'), 'utf8');
const tags = [...html.matchAll(/<script\b([^>]*)\bsrc="\/static\/([^"]+)"[^>]*>/g)]
  .map(m => ({ module: /type="module"/.test(m[1]), file: path.join(STATIC, m[2]) }));

const STATIC_IMPORT = /^\s*import\s+(?:[^'"]*?\bfrom\s+)?['"]([^'"]+)['"]/gm;
const DYNAMIC_IMPORT = /\bimport\(\s*['"]([^'"]+)['"]\s*\)/g;

const graph = new Map();   // file -> { size, via, lazy: [] }
const importers = new Map();   // file -> Set(files importing it statically)
function walk(file, via) {
  if (via && via !== 'index.html') {
    if (!importers.has(file)) importers.set(file, new Set());
    importers.get(file).add(via);
  }
  if (graph.has(file)) return;
  let src;
  try { src = fs.readFileSync(file, 'utf8'); } catch { return; }
  const node = { size: Buffer.byteLength(src), via, lazy: [] };
  graph.set(file, node);
  for (const m of src.matchAll(DYNAMIC_IMPORT)) node.lazy.push(path.join(path.dirname(file), m[1]));
  for (const m of src.matchAll(STATIC_IMPORT)) walk(path.join(path.dirname(file), m[1]), file);
}
const classic = [];
for (const t of tags) {
  if (t.module) walk(t.file, 'index.html');
  else { try { classic.push([t.file, fs.statSync(t.file).size]); } catch {} }
}

const rel = f => path.relative(STATIC, f);
const kb = n => Math.round(n / 1024);
const modules = [...graph].map(([f, v]) => ({ file: rel(f), kb: kb(v.size), via: v.via === 'index.html' ? v.via : rel(v.via) }))
  .sort((a, b) => b.kb - a.kb);
const lazy = [...new Set([...graph.values()].flatMap(v => v.lazy))].filter(f => !graph.has(f)).map(rel).sort();
const total = { modules: modules.length, module_kb: modules.reduce((s, m) => s + m.kb, 0),
  classic: classic.length, classic_kb: kb(classic.reduce((s, c) => s + c[1], 0)) };

if (args.includes('--importers')) {
  // Every static importer of each eager module: what must change before it can load lazily.
  for (const m of modules) {
    const by = [...(importers.get(path.join(STATIC, m.file)) || [])].map(rel).sort();
    console.log(`${String(m.kb).padStart(5)} KB  ${m.file}  <- ${by.join(', ') || 'index.html'}`);
  }
} else if (args.includes('--json')) {
  console.log(JSON.stringify({ total, modules, lazy, classic: classic.map(([f, s]) => ({ file: rel(f), kb: kb(s) })) }, null, 1));
} else {
  console.log(`eager modules: ${total.modules}, ${total.module_kb} KB; classic scripts: ${total.classic}, ${total.classic_kb} KB`);
  for (const m of modules.slice(0, top)) console.log(`${String(m.kb).padStart(5)} KB  ${m.file}  <- ${m.via}`);
  console.log(`\nloaded on demand (import()), not counted: ${lazy.join(', ') || 'none'}`);
}
