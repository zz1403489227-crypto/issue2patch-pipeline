// Runs one agent node with Pi 1.0.0 inside the task sandbox.
// Input: one JSON object on stdin. Output: JSON events on stdout, one per line.
import fs from 'node:fs';
import net from 'node:net';
import nodePath from 'node:path';
import {
  createAgentSession, createBashToolDefinition, createEditToolDefinition, createExtensionRuntime, ModelRuntime,
  SessionManager, SettingsManager,
} from '@earendil-works/pi-coding-agent';
import {Type} from 'typebox';
import {hoistPath, recoverPathFromString, repairEditArgs} from './edit_shim.mjs';
import {capResult, capText} from './output_cap.mjs';

const emit = (type, data = {}) => process.stdout.write(JSON.stringify({t: Date.now(), type, ...data}) + '\n');
const cfg = JSON.parse(fs.readFileSync(0, 'utf8'));

// The sandbox has no network. Pi speaks HTTP to a loopback port that is piped
// to the gateway's Unix socket mounted at cfg.gateway_socket.
const forwarder = net.createServer(client => {
  const upstream = net.connect(cfg.gateway_socket);
  client.pipe(upstream);
  upstream.pipe(client);
  const close = () => { client.destroy(); upstream.destroy(); };
  client.on('error', close);
  upstream.on('error', close);
});
await new Promise(resolve => forwarder.listen(0, '127.0.0.1', resolve));
const baseUrl = `http://127.0.0.1:${forwarder.address().port}/v1`;

function fieldSchema(f) {
  const opts = {description: f.description};
  if (f.type === 'string') return Type.String(opts);
  if (f.type === 'enum') return Type.Union(f.values.map(v => Type.Literal(v)), opts);
  if (f.type === 'string_array') return Type.Array(Type.String(), opts);
  throw new Error(`unknown submit field type ${f.type}`);
}

let submitted = null;
function submitTool(spec) {
  const props = {};
  for (const [name, f] of Object.entries(spec.fields)) {
    props[name] = f.optional ? Type.Optional(fieldSchema(f)) : fieldSchema(f);
  }
  return {
    name: 'submit',
    label: 'Submit',
    description: spec.description,
    parameters: Type.Object(props),
    async execute(_toolCallId, params) {
      submitted = params;
      emit('submit', {params});
      return {content: [{type: 'text', text: 'Submitted. This step is complete; stop now.'}], details: {}};
    },
  };
}

// The local vLLM endpoint is declared the documented way: a models.json "compatible endpoint"
// (docs/models.md). Only compat flags we verified against this server are set (see cfg.compat).
// --- edit tool: Pi's edit plus one extra argument shim (see edit_shim.mjs) -------------------------
function editTool(cwd) {
  const tool = createEditToolDefinition(cwd);
  const piPrepare = tool.prepareArguments;
  tool.prepareArguments = raw => {
    let args = piPrepare ? piPrepare(raw) : raw;
    const h = hoistPath(args);
    if (h.hoisted) {
      emit('arg_repair', {tool: 'edit', kind: 'path_hoisted', path: h.args.path});
      args = h.args;
    } else {
      const p = recoverPathFromString(args);
      if (p) {
        emit('arg_repair', {tool: 'edit', kind: 'path_recovered_from_string', path: p});
        args = {...args, path: p};
      }
    }
    if (!args || typeof args.edits !== 'string' || typeof args.path !== 'string') return args;
    let content = null;
    try { content = fs.readFileSync(nodePath.resolve(cwd, args.path), 'utf8'); } catch { /* keep null */ }
    const {args: out, repair} = repairEditArgs(args, content);
    if (repair) emit(repair.failed ? 'arg_repair_failed' : 'arg_repair', {tool: 'edit', path: args.path, ...repair});
    return out;
  };
  return tool;
}

// --- closed-loop case 3: a flat edit tool for Qwen3.6 ---------------------------------------------
// Pi's edit takes `edits: [{oldText, newText}, ...]`, a nested array. With Qwen3.6's XML tool-call format
// the model often breaks that nesting: in the baseline 99 of 1,139 edit calls (8.7%) failed schema
// validation, and about two thirds of those were the model mixing JSON into the XML parameters
// (`newText` holding `..., "oldText": ..., "path>/testbed/...`). Same tool, same name, flat parameters:
// one replacement per call. Execution is Pi's own edit (with the shims above).
function flatEditTool(cwd) {
  const inner = editTool(cwd);
  return {
    ...inner,
    description: 'Edit a file by replacing exactly one occurrence of `oldText` with `newText`. `oldText` must ' +
      'match the file exactly, including whitespace and indentation, and must be unique in the file. For ' +
      'several separate changes, call the tool once per change.',
    promptSnippet: 'edit(path, oldText, newText): replace one exact, unique occurrence of oldText in a file',
    promptGuidelines: ['Use edit for precise changes; keep oldText small but unique.',
      'Use one edit call per change; do not combine several changes into one call.'],
    parameters: Type.Object({
      path: Type.String({description: 'Path of the file to edit'}),
      oldText: Type.String({description: 'Exact text to replace; must occur exactly once in the file'}),
      newText: Type.String({description: 'Text to put in its place'}),
    }),
    prepareArguments: raw => {
      // a model that still sends the nested form gets it translated when it is unambiguous
      const a = inner.prepareArguments ? inner.prepareArguments(raw) : raw;
      if (a && typeof a.path === 'string' && Array.isArray(a.edits) && a.edits.length === 1 &&
          typeof a.oldText !== 'string') {
        return {path: a.path, oldText: a.edits[0].oldText, newText: a.edits[0].newText};
      }
      return a;
    },
    async execute(id, params, ...rest) {
      return inner.execute(id, {path: params.path, edits: [{oldText: params.oldText, newText: params.newText}]},
        ...rest);
    },
  };
}

// --- bash tool: Pi's bash with a tighter cap on what the model sees (see output_cap.mjs) ---------
function bashTool(cwd) {
  const tool = createBashToolDefinition(cwd);
  const piExecute = tool.execute;
  tool.execute = async (id, params, ...rest) => {
    try {
      const {result, capped} = capResult(await piExecute(id, params, ...rest), id);
      if (capped.length) emit('output_capped', {tool: 'bash', id, capped});
      return result;
    } catch (e) {  // Pi throws for timeouts/aborts, with the output in the message
      const r = capText(String(e?.message ?? e), `${id}-error`);
      if (r.capped) emit('output_capped', {tool: 'bash', id, capped: [{file: r.file, original: r.original}]});
      throw new Error(r.text);
    }
  };
  return tool;
}

fs.mkdirSync(cfg.agent_dir, {recursive: true});
const modelsPath = `${cfg.agent_dir}/models.json`;
fs.writeFileSync(modelsPath, JSON.stringify({
  providers: {
    lab: {
      baseUrl, api: 'openai-completions', apiKey: 'local-no-secret',
      models: [{
        id: cfg.model, name: cfg.model, reasoning: cfg.thinking !== 'off', input: ['text'],
        contextWindow: cfg.context_window, maxTokens: cfg.max_tokens,
        cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}, compat: cfg.compat,
      }],
    },
  },
}));
const runtime = await ModelRuntime.create({authPath: `${cfg.agent_dir}/auth.json`, modelsPath});
const model = runtime.getModel('lab', cfg.model);
if (!model) throw new Error(`model lab/${cfg.model} not found in ${modelsPath}`);

const loader = {
  getExtensions: () => ({extensions: [], errors: [], runtime: createExtensionRuntime()}),
  getSkills: () => ({skills: [], diagnostics: []}),
  getPrompts: () => ({prompts: [], diagnostics: []}),
  getThemes: () => ({themes: [], diagnostics: []}),
  getAgentsFiles: () => ({agentsFiles: []}),
  getSystemPrompt: () => cfg.system_prompt,
  getSystemPromptSource: () => undefined,
  getAppendSystemPrompt: () => [],
  getAppendSystemPromptSources: () => [],
  extendResources: () => {},
  reload: async () => {},
};

const customTools = cfg.submit ? [submitTool(cfg.submit)] : [];
if (cfg.tools.includes('edit')) {  // same name: replaces the built-in
  customTools.push(cfg.edit_tool === 'flat' ? flatEditTool(cfg.cwd) : editTool(cfg.cwd));
}
if (cfg.tools.includes('bash')) customTools.push(bashTool(cfg.cwd));
const toolNames = [...new Set([...cfg.tools, ...customTools.map(t => t.name)])];
const manager = SessionManager.inMemory(cfg.cwd);
const {session, extensionsResult} = await createAgentSession({
  cwd: cfg.cwd, agentDir: cfg.agent_dir, modelRuntime: runtime,
  model, thinkingLevel: cfg.thinking,
  resourceLoader: loader, sessionManager: manager, tools: toolNames, customTools,
  // Pi defaults (docs/settings.md) except: a provider timeout of our own, no telemetry, and no
  // cache warming (a local server has nothing to warm). Retry policy proper belongs to the
  // orchestrator (stage 1); Pi's own transient-failure retry stays at its default.
  settingsManager: SettingsManager.inMemory({
    retry: {provider: {timeoutMs: cfg.request_timeout_s * 1000}},
    enableAnalytics: false, enableInstallTelemetry: false, cacheWarming: 'off',
  }),
});
emit('configuration', {node: cfg.node, tools: session.getActiveToolNames(), extension_errors: extensionsResult.errors,
  system_prompt: session.systemPrompt});

let requests = 0, toolCalls = 0, toolErrors = 0, stopReason = null, finalized = false;
const FINAL_TURNS = 3;
const usage = {input: 0, output: 0, cacheRead: 0, totalTokens: 0};
session.subscribe(event => {
  // streaming deltas are noise; the complete message/tool result arrives in the *_end events
  if (['message_update', 'message_start', 'tool_execution_update'].includes(event.type)) return;
  if (event.type === 'message_end' && event.message?.role === 'assistant') {
    requests++;
    const u = event.message.usage || {};
    usage.input += u.input || 0; usage.output += u.output || 0;
    usage.cacheRead += u.cacheRead || 0; usage.totalTokens += u.totalTokens || 0;
    const left = cfg.max_requests - requests;
    const callsSubmit = (event.message.content || []).some(c => c?.type === 'toolCall' && c?.name === 'submit');
    // Closed-loop case 1 (decision log 2026-11-16): in the baseline the "only N turns left" steer was
    // ignored 408 times; 87 of 96 exhausted reproduce runs had written a test and lost it. So when
    // FINAL_TURNS remain, every tool except submit is switched off (setActiveToolsByName, docs/sdk.md:
    // takes effect on the next turn) and the model is told why.
    if (cfg.finalize && !submitted && !stopReason && cfg.submit && !finalized && left === FINAL_TURNS) {
      finalized = true;
      emit('finalize', {requests, left});
      session.setActiveToolsByName(['submit']);
      void session.steer(`This step is out of budget: every tool except \`submit\` is now disabled. Call ` +
        `\`submit\` now with your best result so far; anything not submitted is discarded.`);
    } else if (!cfg.finalize && !submitted && !stopReason && cfg.submit &&
               left === Math.min(5, Math.floor(cfg.max_requests / 4))) {
      // baseline behaviour, kept switchable for paired comparisons
      emit('budget_warning', {requests, left});
      void session.steer(`Only ${left} model turns remain for this step. Stop exploring, finish the ` +
        `essential work and call the submit tool now; anything not submitted is discarded.`);
    }
    // A submit issued in the very last turn still counts: let the tool run, it ends the session.
    if (requests >= cfg.max_requests && !submitted && !stopReason && !(cfg.finalize && callsSubmit)) {
      stopReason = 'budget_exhausted';
      void session.abort();
    }
  }
  if (event.type === 'tool_execution_end') {
    toolCalls++;
    if (event.isError) toolErrors++;
    if (event.toolName === 'submit' && submitted && !stopReason) {
      stopReason = 'submitted';
      void session.abort();
    }
  }
  if (event.type === 'agent_end') { emit('pi_event', {event: {type: 'agent_end'}}); return; }
  emit('pi_event', {event});
});

const timer = setTimeout(() => { if (!stopReason) { stopReason = 'timeout'; void session.abort(); } }, cfg.timeout_s * 1000);
let error = null;
try {
  await session.prompt(cfg.user_prompt);
  // Retry class 2 (model output format): the model ended its turn without the structured result.
  // Send the error back once, in the same session, instead of re-running the node.
  const lastMsg = session.messages.at(-1);
  if (cfg.submit && !submitted && !stopReason && lastMsg?.stopReason !== 'error' && requests < cfg.max_requests) {
    emit('submit_nudge', {requests});
    await session.prompt('You ended this step without calling the `submit` tool, so nothing was recorded. ' +
      'Call `submit` now with your result.');
  }
} catch (e) {
  error = String(e?.stack || e);
}
clearTimeout(timer);
const last = session.messages.at(-1);
let status = submitted ? 'submitted' : stopReason;
if (!status) status = (error || last?.stopReason === 'error') ? 'error' : 'finished_without_submit';
emit('native_session', {entries: manager.getEntries()});
// After submit/budget/timeout we abort on purpose; the abort error is not a failure.
if (stopReason) error = null;
emit('agent_finished', {
  status, error: error ?? (!stopReason && last?.stopReason === 'error' ? (last?.errorMessage || 'model error') : null),
  requests, tool_calls: toolCalls, tool_errors: toolErrors, usage,
  last_stop_reason: last?.stopReason, last_text: session.getLastAssistantText?.() ?? null,
});
session.dispose();
forwarder.close();
// stdout to a pipe is asynchronous: exit only after everything queued has been flushed,
// otherwise the last (and most important) events are lost.
process.stdout.write('', () => process.exit(0));
