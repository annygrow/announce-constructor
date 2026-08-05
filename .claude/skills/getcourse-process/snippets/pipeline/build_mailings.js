// Собирает HTML писем по дизайну шаблона из распарсенных данных.
const fs = require('fs');
const DIR = 'C:/Users/Redmi/Desktop/Claude/gc_automation/';
const L = JSON.parse(fs.readFileSync(DIR + 'letters_parsed.json', 'utf8'));

// ── оверрайды ──
L[1].subject = (L[1].subject + ' подтверждено 🔥').replace(/\s+/g, ' ').trim();
if (/подтверждено/i.test(L[1].body[0])) L[1].body.shift();
L[2].subject = 'Информация про твои бонусы 🎁';

const PICK = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12, 13, 14, 15, 16, 17, 20];

const LOGO = 'https://cp.unisender.com/en/v5/user-files?userId=4608943&resource=himg&disposition=inline&name=6wu1nit81r3cwaddup9z5dscaxpknfw9cpi6xjw1grnh6yiif831jzhg4jyqekerup5jxfx8rnionb7ihjzgpcct55bbag1wpo7ipsp9wfxtfxmw6e1sd6m4bu3t85fu9fjrihs39o7u5iba651rwxsen5w';
const TG_ICON = 'https://emcdn.ru/278815/240813_4213_blmG18u.png';
const FONT = 'Helvetica, Arial, sans-serif';
const BLUE = '#1445ea', YELLOW = '#e1fb52', GRAY = '#f5f5f5', DARK = '#2c343a';

const esc = s => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
function mdInline(s) {
  s = esc(s);
  s = s.replace(/\*\*\*(.+?)\*\*\*/g, '<strong><em>$1</em></strong>');
  s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/\*(.+?)\*/g, '<em>$1</em>');
  return s;
}
function withInlineCta(html) {
  return html.replace(/\[(.+?)\]/g, (_, t) =>
    `<br><br><a href="#" style="display:inline-block;background:${YELLOW};color:#000;font-family:${FONT};font-size:16px;font-weight:bold;text-decoration:none;border-radius:5px;padding:11px 22px;">${esc(t)}</a>`);
}
function bodyHtml(lines) {
  return lines.map(line => {
    const isList = /^(\d+\.\s)/.test(line) || /^(✔️|✅|📌|📍|🔻|—|-)/.test(line);
    const mb = isList ? '6px' : '14px';
    return `<div style="margin:0 0 ${mb} 0;">${withInlineCta(mdInline(line))}</div>`;
  }).join('\n');
}
function card(bg, inner) {
  return `<table cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:620px;margin:0 auto 14px auto;"><tr><td style="background:${bg};border-radius:20px;padding:22px;font-family:${FONT};font-size:18px;line-height:24px;color:#000;">${inner}</td></tr></table>`;
}
function ctaCard(text) {
  return `<table cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:620px;margin:0 auto 14px auto;"><tr><td align="center" style="background:${BLUE};border-radius:20px;padding:24px 20px;"><a href="#" style="display:inline-block;background:${YELLOW};color:#000;font-family:${FONT};font-size:16px;font-weight:bold;text-decoration:none;border-radius:5px;padding:12px 26px;">${esc(text)}</a></td></tr></table>`;
}
function banner() {
  return `<table cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:620px;margin:0 auto 14px auto;"><tr><td align="center" style="background:${GRAY};border:2px dashed #c9c9c9;border-radius:20px;padding:34px 20px;font-family:${FONT};font-size:14px;color:#999;">[ баннер — вставить картинку ]</td></tr></table>`;
}
function footer() {
  return `<table cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:620px;margin:0 auto;"><tr><td style="background:${DARK};border-radius:20px;padding:20px 24px;"><table width="100%"><tr><td align="left" style="font-family:${FONT};font-size:16px;line-height:24px;color:#fff;font-weight:bold;">8-999-333-69-78<br><a href="https://zerocoder.ru/" style="color:#fff;text-decoration:underline;">https://zerocoder.ru/</a></td><td align="right"><a href="https://t.me/zerocoder_study_bot"><img src="${TG_ICON}" width="30" alt="" style="display:block;"></a></td></tr></table></td></tr></table>`;
}
function render(letter) {
  const parts = [];
  parts.push(`<table cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:620px;margin:0 auto 14px auto;"><tr><td align="left" style="padding:6px 4px;"><img src="${LOGO}" width="158" alt="Zerocoder" style="display:block;max-width:158px;"></td></tr></table>`);
  parts.push(banner());
  parts.push(card(GRAY, bodyHtml(letter.body)));
  for (const c of letter.ctas) parts.push(ctaCard(c));
  parts.push(footer());
  return `<table cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#fff;"><tr><td align="center" style="padding:16px 12px;"><table cellpadding="0" cellspacing="0" border="0" width="100%" style="max-width:660px;">${parts.join('')}</table></td></tr></table>`;
}
function nameFor(Lr) {
  const m = Lr.header.match(/(?:Письмо|ПИСЬМО)\s*([\d.]+)/i);
  const num = m ? m[1] : '?';
  let v = '';
  if (/НЕ\s*подключ/i.test(Lr.header)) v = ' (без бота)';
  else if (/подключ/i.test(Lr.header)) v = ' (с ботом)';
  else if (/платн/i.test(Lr.header)) v = ' (платные)';
  else if (/диагностик/i.test(Lr.header)) v = ' (диагностика)';
  return `Акселератор — Письмо ${num}${v}`;
}
const built = PICK.map(i => ({ idx: i, name: nameFor(L[i]), subject: L[i].subject, html: render(L[i]) }));
fs.writeFileSync(DIR + 'letters_built.json', JSON.stringify(built, null, 2), 'utf8');
fs.writeFileSync(DIR + 'sample_email.html', '<!doctype html><meta charset=utf-8>' + built[0].html, 'utf8');
built.forEach(b => console.log(`${b.name}  |  ${b.subject}  |  ${b.html.length}b`));
console.log('TOTAL', built.length);
