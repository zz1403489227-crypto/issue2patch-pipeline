// node --test harness/pi/edit_shim.test.mjs
import assert from 'node:assert/strict';
import test from 'node:test';
import {createEditToolDefinition} from '@earendil-works/pi-coding-agent';
import {repairEditArgs} from './edit_shim.mjs';

// Exact arguments from run s0, pytest-6202, implement node (3 failed edit calls in a row).
const FILE = '        parts.reverse()\n        s = ".".join(parts)\n        return s.replace(".[", "[")\n';
const S0_ARGS = {
  path: '/testbed/src/_pytest/python.py',
  edits: '[{"oldText": "        parts.reverse()\\n        s = \\".\\".join(parts)\\n        return s.replace(\\".\\[\\", \\"[\\")", ' +
    '"newText": "        parts.reverse()\\n        s = \\".\\".join(parts)\\n        return s"}]',
};

test("Pi's own shim leaves the s0 string unparsed (the failure we are fixing)", () => {
  const out = createEditToolDefinition('/testbed').prepareArguments(structuredClone(S0_ARGS));
  assert.equal(typeof out.edits, 'string');
});

test('s0 case: invalid escape \\[ is read as [ because only that occurs in the file', () => {
  const {args, repair} = repairEditArgs(S0_ARGS, FILE);
  assert.deepEqual(repair, {mode: 'drop'});
  assert.equal(args.edits.length, 1);
  assert.ok(FILE.includes(args.edits[0].oldText));
  assert.ok(args.edits[0].newText.endsWith('return s'));
});

test('regex-like escape \\d is kept as a literal backslash when that is what the file has', () => {
  const file = 'pat = re.compile("\\d+")\n';
  const {args, repair} = repairEditArgs(
    {path: 'a.py', edits: '[{"oldText": "re.compile(\\"\\d+\\")", "newText": "re.compile(\\"\\d*\\")"}]'}, file);
  assert.deepEqual(repair, {mode: 'literal'});
  assert.equal(args.edits[0].oldText, 're.compile("\\d+")');
  assert.equal(args.edits[0].newText, 're.compile("\\d*")');
});

test('valid escapes, including an escaped backslash, are untouched', () => {
  const file = 'a = "x\\\\y"\n';
  const {args} = repairEditArgs({path: 'a.py', edits: '[{"oldText": "\\"x\\\\\\\\y\\"\\n", "newText": "\\"z\\"\\n", "bad": "\\q"}]'}, file);
  assert.equal(args.edits[0].oldText, '"x\\\\y"\n');
});

test('no reading matches the file: arguments are returned unchanged and the failure is reported', () => {
  const {args, repair} = repairEditArgs(S0_ARGS, 'something else entirely\n');
  assert.deepEqual(repair, {failed: true});
  assert.equal(args, S0_ARGS);
});

test('already-structured arguments are not touched', () => {
  const a = {path: 'a.py', edits: [{oldText: 'x', newText: 'y'}]};
  assert.deepEqual(repairEditArgs(a, 'x'), {args: a, repair: null});
});

import {hoistPath} from './edit_shim.mjs';

test('path given inside every edit is moved to the top level', () => {
  const {args, hoisted} = hoistPath({edits: [{path: 'a.py', oldText: 'x', newText: 'y'},
                                             {path: 'a.py', oldText: 'p', newText: 'q'}]});
  assert.ok(hoisted);
  assert.deepEqual(args, {path: 'a.py', edits: [{oldText: 'x', newText: 'y'}, {oldText: 'p', newText: 'q'}]});
});

test('edits as a JSON string with the path inside are parsed and hoisted', () => {
  const {args, hoisted} = hoistPath({edits: '[{"path": "b.py", "oldText": "1", "newText": "2"}]'});
  assert.ok(hoisted);
  assert.equal(args.path, 'b.py');
});

test('different paths, or an existing top-level path, are left alone', () => {
  assert.equal(hoistPath({edits: [{path: 'a.py', oldText: 'x', newText: 'y'},
                                  {path: 'b.py', oldText: 'p', newText: 'q'}]}).hoisted, false);
  const a = {path: 'c.py', edits: [{path: 'd.py', oldText: 'x', newText: 'y'}]};
  assert.equal(hoistPath(a).args, a);
});

import {recoverPathFromString} from './edit_shim.mjs';

test('path is recovered from a string with invalid escapes, edits stay for the content-checked repair', () => {
  const s = '[{"path": "c.py", "oldText": "a\\[b", "newText": "a[b"}]';
  assert.equal(recoverPathFromString({edits: s}), 'c.py');
  const {args, repair} = repairEditArgs({path: 'c.py', edits: s}, 'x = "a[b"\n');
  assert.deepEqual(repair, {mode: 'drop'});
  assert.equal(args.edits[0].oldText, 'a[b');
});
