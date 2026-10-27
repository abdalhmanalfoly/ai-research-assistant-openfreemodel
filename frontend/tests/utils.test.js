import test from 'node:test';
import assert from 'node:assert/strict';
import { validatePdf, validateQuestion, validateMetadata, doiUrl, safeHttpUrl } from '../js/utils/validation.js';
import { parseBlocks, parseInline } from '../js/utils/markdown.js';
import { formatAuthors, formatDateTime, formatBytes, statusMeta, describeWarning, truncate } from '../js/utils/format.js';
import { resolveTheme } from '../js/components/theme.js';

const pdf = (o = {}) => ({ name: 'a.pdf', type: 'application/pdf', size: 1000, ...o });
test('validatePdf', () => {
  assert.equal(validatePdf(pdf()).ok, true); assert.equal(validatePdf(pdf({ type: '' })).ok, true);
  assert.equal(validatePdf(null).ok, false); assert.equal(validatePdf(pdf({ name: 'a.txt' })).ok, false);
  assert.equal(validatePdf(pdf({ type: 'image/png' })).ok, false); assert.equal(validatePdf(pdf({ size: 0 })).ok, false);
  assert.equal(validatePdf(pdf({ size: 51 * 1024 * 1024 }), 50).ok, false); assert.equal(validatePdf(pdf({ size: 51 * 1024 * 1024 }), 60).ok, true);
  assert.equal(validatePdf(pdf({ name: 'A.PDF' })).ok, true);
});
test('validateQuestion trims, rejects empty and over-long', () => {
  assert.equal(validateQuestion('  hi ').value, 'hi'); assert.equal(validateQuestion('   ').ok, false);
  assert.equal(validateQuestion('x'.repeat(2001)).ok, false); assert.equal(validateQuestion('x'.repeat(2000)).ok, true);
});
test('validateMetadata + URL helpers (javascript: URLs are never accepted)', () => {
  assert.deepEqual(validateMetadata({ year: '2021', doi: '10.1234/abc', sourceUrl: 'https://x.org/p' }), {});
  assert.deepEqual(Object.keys(validateMetadata({ year: '20', doi: 'abc', sourceUrl: 'ftp://x' })).sort(), ['doi', 'sourceUrl', 'year']);
  assert.equal(safeHttpUrl('javascript:alert(1)'), null); assert.equal(safeHttpUrl('data:text/html,x'), null); assert.ok(safeHttpUrl('https://a.org'));
  assert.equal(doiUrl('10.1234/abc.1'), 'https://doi.org/10.1234/abc.1'); assert.equal(doiUrl('doi:10.1234/abc'), 'https://doi.org/10.1234/abc');
  assert.equal(doiUrl('javascript:alert(1)'), null); assert.equal(doiUrl(null), null);
});
test('markdown blocks', () => {
  const b = parseBlocks('# Title\n\nPara one\nstill one.\n\n- a\n- b\n\n1. x\n2. y\n\n```js\n<b>code</b>\n```\n');
  assert.deepEqual(b.map((x) => x.type), ['h', 'p', 'ul', 'ol', 'code']);
  assert.equal(b[1].text, 'Para one still one.'); assert.deepEqual(b[2].items, ['a', 'b']); assert.equal(b[4].text, '<b>code</b>');
});
test('markdown inline: bold/em/code/links; citations only for KNOWN ids', () => {
  const ids = new Set(['E1', 'E2']);
  const t = parseInline('A **b** *c* `d` [E1, E2] [E9] [site](https://x.org)', ids);
  assert.deepEqual(t.filter((x) => x.t !== 'text').map((x) => x.t), ['strong', 'em', 'code', 'cite', 'link']);
  assert.deepEqual(t.find((x) => x.t === 'cite').ids, ['E1', 'E2']);
  assert.ok(t.some((x) => x.t === 'text' && x.v.includes('[E9]')), 'unknown id stays plain text');
});
test('markdown is XSS-safe by construction: HTML/script/javascript: stay inert text', () => {
  const evil = '<img src=x onerror=alert(1)> <script>alert(1)</script> [click](javascript:alert(1)) [x](data:text/html,hi)';
  const tokens = parseInline(evil, new Set());
  assert.ok(tokens.every((x) => x.t === 'text'), 'no element-producing tokens for hostile input');
  assert.ok(!tokens.some((x) => x.t === 'link'));
  assert.equal(parseBlocks('<script>x</script>')[0].type, 'p');
});
test('format helpers', () => {
  assert.equal(formatAuthors(['A', 'B', 'C', 'D']), 'A, B, C et al.'); assert.equal(formatAuthors(null), '');
  assert.equal(formatDateTime(null), '—'); assert.equal(formatDateTime('garbage'), '—'); assert.notEqual(formatDateTime('2025-01-02T03:04:05Z'), '—');
  assert.equal(formatBytes(1536), '1.5 KB'); assert.equal(truncate('abcdef', 4), 'abc…');
  assert.deepEqual(statusMeta('paper', 'needs_ocr'), { label: 'Needs OCR', tone: 'warn' });
  assert.deepEqual(statusMeta('job', 'weird'), { label: 'weird', tone: 'neutral' });
  assert.match(describeWarning('no_valid_citations'), /unverified/); assert.equal(describeWarning('new_code'), 'new_code');
});
test('theme resolution', () => {
  assert.equal(resolveTheme('dark', false), 'dark'); assert.equal(resolveTheme('light', true), 'light');
  assert.equal(resolveTheme('system', true), 'dark'); assert.equal(resolveTheme('system', false), 'light'); assert.equal(resolveTheme('bogus', true), 'dark');
});
