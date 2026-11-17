// Extra argument shim for Pi's edit tool (decision log 2026-11-16).
//
// Pi's own prepareArguments already accepts `edits` sent as a JSON string. Qwen3.6 sometimes writes
// that string with escapes JSON does not allow (e.g. `\[` copied from code), so JSON.parse fails and
// the call dies in schema validation with an error the model does not recover from. We retry the parse
// with two readings of each invalid escape `\X`: literal backslash + X, or just X. A reading is used
// only if every oldText occurs in the file (the literal reading is tried first), so a wrong guess
// cannot edit anything; otherwise the arguments are returned unchanged and Pi's validation error goes
// back to the model as before.

export function fixEscapes(s, mode) {
  return s.replace(/\\(u[0-9a-fA-F]{4}|[\s\S])/g, (m, c) =>
    (c.length > 1 || '"\\/bfnrt'.includes(c)) ? m : (mode === 'literal' ? '\\\\' + c : c));
}

export function parseEdits(text, mode) {
  try {
    const v = JSON.parse(fixEscapes(text, mode));
    const list = Array.isArray(v) ? v : [v];
    if (!list.every(e => e && typeof e.oldText === 'string' && typeof e.newText === 'string')) return null;
    return list.map(({path: _p, ...rest}) => rest);  // a per-edit path (see hoistPath) is not part of the schema
  } catch {
    return null;
  }
}

// args: arguments after Pi's own prepareArguments; content: current file text or null.
// Returns {args, repair} where repair is null (nothing done), or {mode} / {failed: true}.
export function repairEditArgs(args, content) {
  if (!args || typeof args.edits !== 'string' || typeof args.path !== 'string' || content == null) {
    return {args, repair: null};
  }
  for (const mode of ['literal', 'drop']) {
    const edits = parseEdits(args.edits, mode);
    if (edits && edits.every(e => content.includes(e.oldText))) {
      return {args: {...args, edits}, repair: {mode}};
    }
  }
  return {args, repair: {failed: true}};
}

// Closed-loop case 3 (decision log 2026-11-16). In the baseline, 24 of the 99 edit calls that failed
// schema validation had put `path` inside every edit ({edits: [{path, oldText, newText}]}) instead of at
// the top level, and 12 more did the same with `edits` sent as a JSON string. When the top-level path is
// missing and every edit names the same path, move it up. A plain JSON.parse is used for string edits
// here (no escape guessing), so nothing is reinterpreted; Pi still validates the result and the edit
// tool still requires each oldText to match the file exactly once.
export function hoistPath(args) {
  if (!args || typeof args !== 'object' || typeof args.path === 'string') return {args, hoisted: false};
  let edits = args.edits;
  if (typeof edits === 'string') {
    try { edits = JSON.parse(edits); } catch { return {args, hoisted: false}; }
  }
  if (edits && !Array.isArray(edits) && typeof edits === 'object') edits = [edits];
  if (!Array.isArray(edits) || edits.length === 0) return {args, hoisted: false};
  const paths = new Set(edits.map(e => (e && typeof e.path === 'string') ? e.path : null));
  if (paths.size !== 1 || paths.has(null)) return {args, hoisted: false};
  const [path] = paths;
  return {args: {...args, path, edits: edits.map(({path: _p, ...rest}) => rest)}, hoisted: true};
}

// Same case, with `edits` sent as a string that plain JSON.parse rejects (invalid escapes): recover only
// the path here; repairEditArgs then picks the escape reading whose oldText actually occurs in that file.
export function recoverPathFromString(args) {
  if (!args || typeof args.path === 'string' || typeof args.edits !== 'string') return null;
  for (const mode of ['literal', 'drop']) {
    let v;
    try { v = JSON.parse(fixEscapes(args.edits, mode)); } catch { continue; }
    const list = Array.isArray(v) ? v : [v];
    const paths = new Set(list.map(e => (e && typeof e.path === 'string') ? e.path : null));
    if (paths.size === 1 && !paths.has(null)) return [...paths][0];
  }
  return null;
}
