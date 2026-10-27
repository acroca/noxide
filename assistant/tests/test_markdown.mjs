import assert from 'node:assert/strict';
import test from 'node:test';
import { markdown } from '../src/assistant/pwa/markdown.js';

// The rendered blocks, with dates unwrapped: one test below covers that wrapper.
const body = text => markdown(text).replace(/^<div class="markdown">|<\/div>$/g, '')
  .replace(/<span class="md-date">([^<]*)<\/span>/g, '$1');

test('upcoming day groups nest under their bold day line', () => {
  const html = body([
    '## Upcoming',
    '',
    '- **Tomorrow — Friday**',
    '  - 17:30 — Hockey (2026-10-23)',
    '- **Saturday**',
    '  - 09:00 — Match (2026-10-24)',
    '  - Fair (2026-10-24)',
    '- 2026-11-07 — Away game',
  ].join('\n'));
  assert.equal(html, '<h2>Upcoming</h2><ul>'
    + '<li><strong>Tomorrow — Friday</strong><ul><li>17:30 — Hockey (2026-10-23)</li></ul></li>'
    + '<li><strong>Saturday</strong><ul><li>09:00 — Match (2026-10-24)</li><li>Fair (2026-10-24)</li></ul></li>'
    + '<li>2026-11-07 — Away game</li></ul>');
});

test('tasks show a box, and a page link groups them as plain text', () => {
  const html = body('[Familia](areas/familia.md)\n- [ ] Call the school (due 2026-10-30)\n- [x] Book the trip (done 2026-10-20)');
  assert.equal(html, '<p><span class="md-page" title="areas/familia.md">Familia</span></p><ul>'
    + '<li class="md-task"><span class="md-box" role="img" aria-label="Open task"></span>Call the school (due 2026-10-30)</li>'
    + '<li class="md-task md-done"><span class="md-box" role="img" aria-label="Done task"></span>Book the trip (done 2026-10-20)</li></ul>');
});

test('web links open outside the app; other schemes never become an href', () => {
  assert.equal(body('[Docs](https://example.com/a_b_c)'),
    '<p><a href="https://example.com/a_b_c" target="_blank" rel="noopener noreferrer">Docs</a></p>');
  assert.equal(body('[x](javascript:alert(1))'), '<p><span class="md-page" title="javascript:alert(1)">x</span></p>');
  // The link's own text is not linked again.
  assert.equal(body('[https://example.com](https://example.com)'),
    '<p><a href="https://example.com" target="_blank" rel="noopener noreferrer">https://example.com</a></p>');
});

test('bare URLs are linked without trailing punctuation or an unopened parenthesis', () => {
  assert.equal(body('Thread (https://www.reddit.com/r/x/s/tNh6w) (someday).'),
    '<p>Thread (<a href="https://www.reddit.com/r/x/s/tNh6w" target="_blank" rel="noopener noreferrer">https://www.reddit.com/r/x/s/tNh6w</a>) (someday).</p>');
  assert.equal(body('See https://en.wikipedia.org/wiki/Foo_(bar).'),
    '<p>See <a href="https://en.wikipedia.org/wiki/Foo_(bar)" target="_blank" rel="noopener noreferrer">https://en.wikipedia.org/wiki/Foo_(bar)</a>.</p>');
  assert.equal(body('a https://x.test/?a=1&b="2"'),
    '<p>a <a href="https://x.test/?a=1&amp;b=" target="_blank" rel="noopener noreferrer">https://x.test/?a=1&amp;b=</a>&quot;2&quot;</p>');
});

test('emphasis, strikethrough and code, with snake_case left alone', () => {
  assert.equal(body('_Updated: 2026-10-27 (nightly)._'), '<p><em>Updated: 2026-10-27 (nightly).</em></p>');
  assert.equal(body('- ~~12:00 — Lunch~~ moved, **see** `a_b` and file_name_here'),
    '<ul><li><del>12:00 — Lunch</del> moved, <strong>see</strong> <code>a_b</code> and file_name_here</li></ul>');
  assert.equal(body('**[Familia](areas/familia.md)** and 2 * 3 * 4'),
    '<p><strong><span class="md-page" title="areas/familia.md">Familia</span></strong> and 2 * 3 * 4</p>');
});

test('reminder markers become a quiet label', () => {
  assert.equal(body('- 09:30 — Pills [reminder:05c31b23]'),
    '<ul><li>09:30 — Pills <span class="md-reminder" title="Reminder 05c31b23">reminder</span></li></ul>');
});

test('everything is escaped', () => {
  assert.equal(body('# <b>Now</b>\n- <img src=x onerror=alert(1)>\n`<i>`'),
    '<h1>&lt;b&gt;Now&lt;/b&gt;</h1><ul><li>&lt;img src=x onerror=alert(1)&gt;</li></ul><p><code>&lt;i&gt;</code></p>');
  assert.equal(body('[a"b](https://x.test/"onmouseover="1)'),
    '<p><a href="https://x.test/&quot;onmouseover=&quot;1" target="_blank" rel="noopener noreferrer">a&quot;b</a></p>');
  assert.equal(body('a \u00000\u0000 b'), '<p>a 0 b</p>');
});

test('numbered lists keep their numbering across a blank line and a paragraph', () => {
  assert.equal(body('1. One\n\n2. Two\n\nNote\n\n3. Three'),
    '<ol><li>One</li><li>Two</li></ol><p>Note</p><ol start="3"><li>Three</li></ol>');
  assert.equal(body('- a\n1. b'), '<ul><li>a</li></ul><ol><li>b</li></ol>');
});

test('list continuation lines, rules, quotes, tables and fences', () => {
  assert.equal(body('- item\n  more of it\n---\n> quoted\n> again\n| a | b |\n|---|---|\n| 1 | 2 |\n```\n**raw**\n```'),
    '<ul><li>item<br>more of it</li></ul><hr><blockquote><p>quoted</p><p>again</p></blockquote>'
    + '<table><tbody><tr><td>a</td><td>b</td></tr><tr><td>1</td><td>2</td></tr></tbody></table><pre>**raw**</pre>');
});

test('dates stay on one line, outside URLs and code', () => {
  const html = markdown('- 2026-10-30 → 2026-11-02 `2026-01-01` https://x.test/2026-01-01 12026-01-019');
  assert.equal(html, '<div class="markdown"><ul><li><span class="md-date">2026-10-30</span> → <span class="md-date">2026-11-02</span> '
    + '<code>2026-01-01</code> <a href="https://x.test/2026-01-01" target="_blank" rel="noopener noreferrer">https://x.test/2026-01-01</a> 12026-01-019</li></ul></div>');
});
