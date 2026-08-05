const fs = require('fs');
const path = 'C:/Users/Redmi/Downloads/Почта новый веб по Акселератору.md';
let raw = fs.readFileSync(path, 'utf8');
let lines = raw.split(/\r?\n/)
  .filter(l => !/^\[image\d+\]:/.test(l))
  .map(l => l.replace(/!\[\]\[image\d+\]/g, '').replace(/\s+$/,''));

const unesc = s => s.replace(/\\(.)/g, '$1');
const strip = s => s.replace(/\*\*/g, '').replace(/^\s+|\s+$/g, '');

const headerRe = /(Письмо|ПИСЬМО)\s*([0-9]+(?:\.[0-9]+)?)/i;

let segments = [];
let cur = null;
for (let l of lines) {
  const t = strip(l);
  if (headerRe.test(t) && t.length < 90) {
    if (cur) segments.push(cur);
    cur = { header: unesc(t), raw: [] };
  } else if (cur) {
    cur.raw.push(l);
  }
}
if (cur) segments.push(cur);

const letters = segments.map(seg => {
  let subject = null, fromName = null;
  const body = [];
  const ctas = [];
  for (let l of seg.raw) {
    let u = unesc(l).trim();            // keeps **/* emphasis
    const t = strip(u).trim();          // plain, for detection
    if (!t) continue;
    if (/^Тема:/i.test(t)) { subject = u.replace(/^\**Тема:\s*/i, '').replace(/\*/g,'').trim(); continue; }
    if (/^от кого:/i.test(t)) { fromName = t.replace(/^от кого:\s*/i, '').trim(); continue; }
    if (/^\|/.test(u)) {
      u = u.replace(/^\|/, '').replace(/\|$/, '').trim();
      if (/^:?-{2,}:?$/.test(strip(u))) continue;
    }
    const m = strip(u).match(/^\[(.+?)\]$/);
    if (m) { ctas.push(m[1].trim()); continue; }
    body.push(u);
  }
  return { header: seg.header, subject, fromName, ctas, body };
});

fs.writeFileSync('C:/Users/Redmi/Desktop/Claude/gc_automation/letters_parsed.json', JSON.stringify(letters, null, 2), 'utf8');
letters.forEach((L, i) => {
  console.log(`#${i} | ${L.header} | subj=${L.subject ? ('"' + L.subject.slice(0, 55) + '"') : '—'} | body=${L.body.length} | cta=[${L.ctas.join(' / ')}]${L.fromName ? (' | from=' + L.fromName) : ''}`);
});
console.log('TOTAL', letters.length);
