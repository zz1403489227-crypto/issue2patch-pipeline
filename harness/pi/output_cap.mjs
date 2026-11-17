// Context management for tool output (decision log 2026-11-16).
//
// Pi's bash tool already truncates at 2000 lines / 50 KB and saves the full output to a temp file.
// Measured on stage-0 traces (687 bash/read results): median 502 chars, p90 2.7K, p99 20K, max 51K;
// the 12 results over 10K chars carried 38% of all tool-output characters. So for bash we cap what
// the model sees at CAP chars: the head and the tail stay (errors and test summaries are usually at
// the end), the full text goes to a file in the sandbox, and the model is told where it is.
import fs from 'node:fs';

export const CAP = 12000;
export const HEAD = 2000;
export const TAIL = 8000;
export const DIR = '/tmp/i2p/tool-output';

export function capText(text, id, {cap = CAP, head = HEAD, tail = TAIL, dir = DIR} = {}) {
  if (typeof text !== 'string' || text.length <= cap) return {text, capped: false};
  fs.mkdirSync(dir, {recursive: true});
  const file = `${dir}/${String(id).replace(/[^\w.-]/g, '_')}.txt`;
  fs.writeFileSync(file, text);
  const omitted = text.length - head - tail;
  const note = `\n\n[... ${omitted} characters omitted. Full output (${text.length} chars) saved to ${file}; ` +
    `inspect it with grep, sed -n or tail instead of printing it again ...]\n\n`;
  return {text: text.slice(0, head) + note + text.slice(-tail), capped: true, file, original: text.length};
}

// Apply capText to every text part of a tool result ({content: [...], details}).
export function capResult(result, id, opts) {
  if (!result || !Array.isArray(result.content)) return {result, capped: []};
  const capped = [];
  const content = result.content.map((part, i) => {
    if (part?.type !== 'text') return part;
    const r = capText(part.text, `${id}-${i}`, opts);
    if (r.capped) capped.push({file: r.file, original: r.original});
    return r.capped ? {...part, text: r.text} : part;
  });
  return {result: {...result, content}, capped};
}
