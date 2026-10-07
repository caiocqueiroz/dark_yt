// LLM bridge: exposes the local Claude Code / Codex CLIs (logged in with the
// user's subscriptions) as a small authenticated HTTP API for n8n.
//
// POST /v1/complete  Authorization: Bearer <LLM_BRIDGE_TOKEN>
//   { prompt, system?, schema?, provider?: "auto"|"claude"|"codex", model? }
// -> { ok: true, provider, model, text, json, duration_ms }
// -> { ok: false, error_code, error_message, attempts }
//
// "auto" tries Claude first and falls back to Codex.

import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { readFileSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { timingSafeEqual } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');

function loadDotenv(path) {
  const env = {};
  try {
    for (const line of readFileSync(path, 'utf8').split('\n')) {
      const m = line.match(/^([A-Z0-9_]+)=(.*)$/);
      if (m) env[m[1]] = m[2].trim();
    }
  } catch {}
  return env;
}

const dotenv = loadDotenv(join(ROOT, '.env'));
const cfg = (k, d) => process.env[k] ?? dotenv[k] ?? d;

const TOKEN = cfg('LLM_BRIDGE_TOKEN');
const HOST = cfg('LLM_BRIDGE_HOST', '127.0.0.1');
const PORT = Number(cfg('LLM_BRIDGE_PORT', '8787'));
const CLAUDE_BIN = cfg('CLAUDE_BIN', join(homedir(), '.local/bin/claude'));
const CODEX_BIN = cfg('CODEX_BIN', join(homedir(), '.local/bin/codex'));
const CLAUDE_MODEL = cfg('LLM_BRIDGE_CLAUDE_MODEL', 'sonnet');
const CODEX_MODEL = cfg('LLM_BRIDGE_CODEX_MODEL', '');
const TIMEOUT_MS = Number(cfg('LLM_BRIDGE_TIMEOUT_MS', '240000'));
const MAX_CONCURRENCY = Number(cfg('LLM_BRIDGE_CONCURRENCY', '2'));
const MAX_BODY = 2 * 1024 * 1024;

if (!TOKEN || TOKEN.length < 32) {
  console.error('LLM_BRIDGE_TOKEN missing or too short in .env');
  process.exit(1);
}

const log = (obj) => console.log(JSON.stringify({ ts: new Date().toISOString(), ...obj }));

// --- concurrency limit ------------------------------------------------------

let running = 0;
const waiters = [];
async function withSlot(fn) {
  if (running >= MAX_CONCURRENCY) await new Promise((r) => waiters.push(r));
  running++;
  try {
    return await fn();
  } finally {
    running--;
    waiters.shift()?.();
  }
}

// --- helpers ----------------------------------------------------------------

class BridgeError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

function run(bin, args, { input, cwd }) {
  return new Promise((resolve, reject) => {
    const env = { ...process.env };
    delete env.ANTHROPIC_API_KEY; // force subscription auth
    delete env.OPENAI_API_KEY;
    const child = spawn(bin, args, { cwd, env, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '';
    let stderr = '';
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      reject(new BridgeError('LLM_TIMEOUT', `${bin} exceeded ${TIMEOUT_MS}ms`));
    }, TIMEOUT_MS);
    child.stdout.on('data', (d) => (stdout += d));
    child.stderr.on('data', (d) => (stderr += d));
    child.on('error', (e) => {
      clearTimeout(timer);
      reject(new BridgeError('LLM_SPAWN_FAILED', e.message));
    });
    child.on('close', (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr });
    });
    child.stdin.end(input);
  });
}

function extractJson(text) {
  if (typeof text !== 'string') return null;
  const stripped = text.trim().replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '');
  try {
    return JSON.parse(stripped);
  } catch {}
  const start = stripped.search(/[{[]/);
  const end = Math.max(stripped.lastIndexOf('}'), stripped.lastIndexOf(']'));
  if (start >= 0 && end > start) {
    try {
      return JSON.parse(stripped.slice(start, end + 1));
    } catch {}
  }
  return null;
}

const RATE_LIMIT_RE = /usage limit|rate limit|limit reached|quota|too many requests|429/i;

// --- providers --------------------------------------------------------------

async function callClaude({ prompt, system, schema, model }, cwd) {
  const args = [
    '-p',
    '--output-format', 'json',
    '--model', model || CLAUDE_MODEL,
    '--tools', '',
    '--no-session-persistence',
    '--setting-sources', '',
    '--strict-mcp-config',
    '--system-prompt', system || 'Responda exatamente o que foi pedido.',
  ];
  if (schema) args.push('--json-schema', JSON.stringify(schema));

  const { code, stdout, stderr } = await run(CLAUDE_BIN, args, { input: prompt, cwd });
  let out;
  try {
    out = JSON.parse(stdout);
  } catch {
    const msg = (stderr || stdout).slice(0, 500);
    throw new BridgeError(RATE_LIMIT_RE.test(msg) ? 'LLM_RATE_LIMITED' : 'LLM_INVALID_OUTPUT', `claude exit ${code}: ${msg}`);
  }
  if (out.is_error || code !== 0) {
    const msg = String(out.result ?? stderr).slice(0, 500);
    throw new BridgeError(RATE_LIMIT_RE.test(msg) ? 'LLM_RATE_LIMITED' : 'LLM_FAILED', `claude: ${msg}`);
  }
  const usedModel = Object.keys(out.modelUsage ?? {})[0] ?? model ?? CLAUDE_MODEL;
  return { model: usedModel, text: out.result, json: out.structured_output ?? extractJson(out.result) };
}

async function callCodex({ prompt, system, schema, model }, cwd) {
  const outFile = join(cwd, 'out.txt');
  const args = [
    'exec',
    '--skip-git-repo-check',
    '--ephemeral',
    '--ignore-user-config',
    '--ignore-rules',
    '--sandbox', 'read-only',
    '-C', cwd,
    '-o', outFile,
  ];
  if (model || CODEX_MODEL) args.push('-m', model || CODEX_MODEL);
  if (schema) {
    const schemaFile = join(cwd, 'schema.json');
    await writeFile(schemaFile, JSON.stringify(schema));
    args.push('--output-schema', schemaFile);
  }
  args.push('-');

  const input = system ? `${system}\n\n---\n\n${prompt}` : prompt;
  const { code, stderr } = await run(CODEX_BIN, args, { input, cwd });
  const text = await readFile(outFile, 'utf8').catch(() => '');
  if (code !== 0 || !text) {
    const msg = stderr.slice(-500);
    throw new BridgeError(RATE_LIMIT_RE.test(msg) ? 'LLM_RATE_LIMITED' : 'LLM_FAILED', `codex exit ${code}: ${msg}`);
  }
  const usedModel = stderr.match(/^model:\s*(\S+)/m)?.[1] ?? model ?? CODEX_MODEL ?? 'codex-default';
  return { model: usedModel, text, json: extractJson(text) };
}

const PROVIDERS = { claude: callClaude, codex: callCodex };

async function complete(req) {
  const order = req.provider === 'claude' ? ['claude'] : req.provider === 'codex' ? ['codex'] : ['claude', 'codex'];
  const attempts = [];
  for (const name of order) {
    const cwd = await mkdtemp(join(tmpdir(), 'llm-bridge-'));
    const started = Date.now();
    try {
      const res = await withSlot(() => PROVIDERS[name](req, cwd));
      if (req.schema && res.json == null) {
        throw new BridgeError('LLM_INVALID_JSON', `${name} returned non-JSON output`);
      }
      return { ok: true, provider: name, ...res, duration_ms: Date.now() - started, attempts };
    } catch (e) {
      attempts.push({ provider: name, error_code: e.code ?? 'LLM_FAILED', error_message: e.message, duration_ms: Date.now() - started });
    } finally {
      rm(cwd, { recursive: true, force: true });
    }
  }
  const last = attempts.at(-1);
  return { ok: false, error_code: last.error_code, error_message: last.error_message, attempts };
}

// --- http -------------------------------------------------------------------

function authorized(req) {
  const given = Buffer.from((req.headers.authorization ?? '').replace(/^Bearer\s+/i, ''));
  const expected = Buffer.from(TOKEN);
  return given.length === expected.length && timingSafeEqual(given, expected);
}

function send(res, status, body) {
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(body));
}

async function readBody(req) {
  let size = 0;
  const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY) throw new BridgeError('BODY_TOO_LARGE', 'request body over 2MB');
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

createServer(async (req, res) => {
  if (req.method === 'GET' && req.url === '/healthz') return send(res, 200, { status: 'ok', running, queued: waiters.length });
  if (req.method !== 'POST' || req.url !== '/v1/complete') return send(res, 404, { ok: false, error_code: 'NOT_FOUND' });
  if (!authorized(req)) return send(res, 401, { ok: false, error_code: 'UNAUTHORIZED' });

  let body;
  try {
    body = await readBody(req);
  } catch (e) {
    return send(res, 400, { ok: false, error_code: e.code ?? 'INVALID_BODY', error_message: e.message });
  }
  if (typeof body?.prompt !== 'string' || !body.prompt.trim()) {
    return send(res, 400, { ok: false, error_code: 'INVALID_BODY', error_message: 'prompt is required' });
  }

  const started = Date.now();
  const result = await complete(body);
  // Metadata only: prompts and outputs are never logged.
  log({
    event: 'complete',
    ok: result.ok,
    provider: result.provider,
    model: result.model,
    error_code: result.error_code,
    fallbacks: result.attempts?.length ?? 0,
    duration_ms: Date.now() - started,
  });
  send(res, result.ok ? 200 : 502, result);
}).listen(PORT, HOST, () => log({ event: 'listening', host: HOST, port: PORT, claude_model: CLAUDE_MODEL }));
