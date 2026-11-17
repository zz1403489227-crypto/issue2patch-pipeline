// node --test harness/pi/output_cap.test.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import test from 'node:test';
import {capResult, capText} from './output_cap.mjs';

const dir = fs.mkdtempSync(`${os.tmpdir()}/i2p-cap-`);

test('short output passes through untouched', () => {
  assert.deepEqual(capText('ok', 'a', {dir}), {text: 'ok', capped: false});
});

test('long output keeps head and tail, saves the full text, and says where', () => {
  const text = 'H'.repeat(5000) + 'M'.repeat(20000) + 'T'.repeat(9000);
  const r = capText(text, 'call/1', {dir});
  assert.ok(r.capped);
  assert.ok(r.text.startsWith('H'.repeat(2000)));
  assert.ok(r.text.endsWith('T'.repeat(8000)));
  assert.ok(r.text.length < 12000);
  assert.equal(fs.readFileSync(r.file, 'utf8'), text);
  assert.match(r.text, /saved to .*call_1-?\w*\.txt/);
});

test('capResult only touches text parts and reports what it capped', () => {
  const res = {content: [{type: 'text', text: 'x'.repeat(30000)}, {type: 'image', data: '...'}], details: {a: 1}};
  const {result, capped} = capResult(res, 'id9', {dir});
  assert.equal(capped.length, 1);
  assert.equal(result.content[1].type, 'image');
  assert.deepEqual(result.details, {a: 1});
  assert.ok(result.content[0].text.length < 12000);
});
