// The markdown the app shows: assistant replies and the Now page. A small
// renderer for what the vault and the model actually write, not CommonMark.
// Everything is escaped before it becomes HTML; links get an href only for
// web and mail schemes, and a vault path is shown as its text, since the app
// has no page to open it in.
export const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));

const SLOT = '\u0000';
const SPANS = /`([^`]+)`|!?\[([^\]]+)\]\((?:<([^>]*)>|([^()\s]*(?:\([^()\s]*\)[^()\s]*)*))\)|\[reminder:([^\]\s]+)\]|https?:\/\/[^\s<>"]+/g;
const EMPHASIS = [
  [/\*\*(?=\S)(.+?)(?<=\S)\*\*/g, '<strong>$1</strong>'],
  [/(?<![\p{L}\p{N}_])__(?=\S)(.+?)(?<=\S)__(?![\p{L}\p{N}_])/gu, '<strong>$1</strong>'],
  [/~~(?=\S)(.+?)(?<=\S)~~/g, '<del>$1</del>'],
  [/(?<![\p{L}\p{N}*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\p{L}\p{N}*])/gu, '<em>$1</em>'],
  [/(?<![\p{L}\p{N}_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\p{L}\p{N}_])/gu, '<em>$1</em>'],
];
const URL_TRAILER = '.,;:!?)]*_~\'';

const webLink = (href, html) => `<a href="${escape(href)}" target="_blank" rel="noopener noreferrer">${html}</a>`;
const count = (text, char) => text.split(char).length - 1;

// A bare URL ends before trailing punctuation, and before a closing
// parenthesis it did not open: "(see https://example.com/a)".
function splitUrl(url) {
  let end = url.length;
  while (end > 0 && URL_TRAILER.includes(url[end - 1])) {
    if (url[end - 1] === ')' && count(url.slice(0, end), '(') >= count(url.slice(0, end), ')')) break;
    end--;
  }
  return [url.slice(0, end), url.slice(end)];
}

// One set-aside span as HTML, plus any text it matched but does not own.
function span(autolink, match, code, label, angled, target, reminder) {
  if (code !== undefined) return [`<code>${escape(code)}</code>`, ''];
  if (reminder !== undefined) return [`<span class="md-reminder" title="Reminder ${escape(reminder)}">reminder</span>`, ''];
  if (label !== undefined) {
    // A link's own text is never autolinked: an anchor inside an anchor is invalid.
    const href = (angled ?? target).trim(), text = inline(label, false);
    if (/^(?:https?:|mailto:)/i.test(href)) return [webLink(href, text), ''];
    return [`<span class="md-page" title="${escape(href)}">${text}</span>`, ''];
  }
  if (!autolink) return [escape(match), ''];
  const [url, rest] = splitUrl(match);
  return [webLink(url, escape(url)), rest];
}

// Code, links and markers are set aside before escaping and emphasis, so a
// URL's underscores stay in the URL and bold can still wrap a link.
function inline(text, autolink = true) {
  const slots = [];
  const held = String(text ?? '').replaceAll(SLOT, '').replace(SPANS, (...args) => {
    const [html, rest] = span(autolink, ...args.slice(0, 6));
    slots.push(html);
    return SLOT + (slots.length - 1) + SLOT + rest;
  });
  let html = escape(held);
  for (const [pattern, replacement] of EMPHASIS) html = html.replace(pattern, replacement);
  // A narrow screen would otherwise break an ISO date at its hyphens.
  html = html.replace(/(?<![\d-])\d{4}-\d{2}-\d{2}(?![\d-])/g, '<span class="md-date">$&</span>');
  return html.replace(/\u0000(\d+)\u0000/g, (_, i) => slots[i]);
}

const indentOf = line => line.match(/^[ \t]*/)[0].replace(/\t/g, '    ').length;

export function markdown(text) {
  const out = [];
  // Open lists, innermost last: their indent and tag. Each one's last <li> is still open.
  const lists = [];
  let fence = false, code = [], table = false, quote = false;
  const closeLists = (indent = -1) => {
    while (lists.length && lists.at(-1).indent > indent) out.push(`</li></${lists.pop().tag}>`);
  };
  const closeBlocks = () => {
    closeLists();
    if (table) { out.push('</tbody></table>'); table = false; }
    if (quote) { out.push('</blockquote>'); quote = false; }
  };
  const item = (indent, number, body) => {
    const tag = number ? 'ol' : 'ul';
    closeLists(indent);
    if (lists.at(-1)?.indent === indent && lists.at(-1).tag !== tag) closeLists(indent - 1);
    if (lists.at(-1)?.indent === indent) out.push('</li>');
    else {
      lists.push({indent, tag});
      out.push(number && Number(number) !== 1 ? `<ol start="${Number(number)}">` : `<${tag}>`);
    }
    const task = body.match(/^\[([ xX])\]\s+(.*)/);
    if (!task) { out.push(`<li>${inline(body)}`); return; }
    const done = task[1] !== ' ';
    out.push(`<li class="md-task${done ? ' md-done' : ''}"><span class="md-box" role="img" aria-label="${done ? 'Done' : 'Open'} task"></span>${inline(task[2])}`);
  };
  for (const line of String(text ?? '').split('\n')) {
    if (/^\s*```/.test(line)) {
      closeBlocks();
      if (fence) { out.push(`<pre>${escape(code.join('\n'))}</pre>`); code = []; }
      fence = !fence; continue;
    }
    if (fence) { code.push(line); continue; }
    // A blank line between items keeps the list going; whatever follows decides.
    if (!line.trim()) continue;
    const heading = line.match(/^(#{1,6})\s+(.+)/);
    const bullet = line.match(/^\s*(?:[-*+]|(\d+)[.)])\s+(.+)/);
    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) { closeBlocks(); out.push('<hr>'); }
    else if (heading) { closeBlocks(); out.push(`<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`); }
    else if (bullet) {
      if (table || quote) closeBlocks();
      item(indentOf(line), bullet[1], bullet[2]);
    } else if (lists.length && indentOf(line) > lists.at(-1).indent) out.push(`<br>${inline(line.trim())}`);
    else if (/^\s*>/.test(line)) {
      if (!quote) { closeBlocks(); out.push('<blockquote>'); quote = true; }
      out.push(`<p>${inline(line.replace(/^\s*>\s?/, ''))}</p>`);
    } else if (/^\s*\|/.test(line)) {
      if (/^\s*\|[\s:|-]+$/.test(line)) continue;
      if (!table) { closeBlocks(); out.push('<table><tbody>'); table = true; }
      out.push('<tr>' + line.trim().replace(/^\||\|$/g, '').split('|').map(cell => `<td>${inline(cell.trim())}</td>`).join('') + '</tr>');
    } else { closeBlocks(); out.push(`<p>${inline(line)}</p>`); }
  }
  closeBlocks(); if (fence) out.push(`<pre>${escape(code.join('\n'))}</pre>`);
  return `<div class="markdown">${out.join('')}</div>`;
}
