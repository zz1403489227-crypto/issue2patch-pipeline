// Replay every edit call that failed Pi's schema validation in recorded runs through the current argument
// shims, and count how many would now be well-formed. Offline, deterministic (closed-loop case 3).
// usage: node replay_edit_args.mjs <runs-dir> <run-id> [<run-id> ...]
import fs from 'node:fs';
import path from 'node:path';
import {createEditToolDefinition} from '@earendil-works/pi-coding-agent';
import {hoistPath, parseEdits, recoverPathFromString} from './edit_shim.mjs';

const [runsDir, ...runs] = process.argv.slice(2);
const piPrepare = createEditToolDefinition('/testbed').prepareArguments;
const wellFormed = a => a && typeof a.path === 'string' && Array.isArray(a.edits) && a.edits.length > 0 &&
  a.edits.every(e => e && typeof e.oldText === 'string' && typeof e.newText === 'string');

function* traces(dir) {
  for (const entry of fs.readdirSync(dir, {withFileTypes: true})) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) yield* traces(p);
    else if (entry.name === 'events.jsonl' && p.includes(`${path.sep}agent${path.sep}`)) yield p;
  }
}

let total = 0, fixed = 0;
const byShape = {};
for (const run of runs) {
  for (const file of traces(path.join(runsDir, run, 'tasks'))) {
    const starts = {};
    for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
      if (!line.includes('tool_execution')) continue;
      let e; try { e = JSON.parse(line); } catch { continue; }
      if (e.type !== 'pi_event') continue;
      const ev = e.event;
      if (ev.type === 'tool_execution_start') starts[ev.toolCallId] = ev.args;
      if (ev.type !== 'tool_execution_end' || ev.toolName !== 'edit' || !ev.isError) continue;
      const text = (ev.result?.content || []).map(c => c.text || '').join(' ');
      if (!text.startsWith('Validation failed')) continue;
      total++;
      const raw = structuredClone(starts[ev.toolCallId] ?? {});
      const shape = Object.keys(raw).sort().join(',') + (typeof raw.edits === 'string' ? ' (edits:string)' : '');
      let after = hoistPath(piPrepare(raw)).args;
      const p = recoverPathFromString(after);
      // with a recovered path, the runtime repair parses the string and checks oldText against the file;
      // offline there is no file, so count it as recoverable when some escape reading parses
      if (p) after = {...after, path: p, edits: parseEdits(after.edits, 'literal') ?? parseEdits(after.edits, 'drop')};
      const ok = wellFormed(after);
      fixed += ok;
      byShape[shape] ??= {count: 0, now_well_formed: 0};
      byShape[shape].count++;
      byShape[shape].now_well_formed += ok;
    }
  }
}
console.log(JSON.stringify({invalid_edit_calls: total, now_well_formed: fixed, by_shape: byShape}, null, 1));
