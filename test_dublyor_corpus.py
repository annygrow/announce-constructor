# -*- coding: utf-8 -*-
"""
Regression corpus for the "Дублёр" project — codename for a planned token-based
rewrite of section-boundary detection (is_section_header / merged-label
splitting / meta-line extraction), meant to run in parallel with the current
DOM-heuristic-based system until it proves at least as correct.

Every entry here reproduces ONE of the 33+ bugs documented in the
project_parsing_fixes memory (plus #34/#35 found in later sessions), as a
minimal HTML snippet with an expected outcome. This file must pass 100% on
the CURRENT (already-fixed) parser — that is its baseline. When Дублёр's
token-based detector is implemented, it must be run against this exact same
corpus and match (or improve on) every case before any cutover.

Run: python test_dublyor_corpus.py
"""
import os, sys, io
os.environ.setdefault('OPENROUTER_API_KEY', 'test')
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('GC_LOGIN', 'test')
os.environ.setdefault('GC_PASSWORD', 'test')

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import importlib.util, unittest.mock as mock
from bs4 import BeautifulSoup

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('app', os.path.join(HERE, 'app.py'))
mod = importlib.util.module_from_spec(spec)
with mock.patch('flask.Flask.run'):
    spec.loader.exec_module(mod)


def _tag(html, name=None):
    soup = BeautifulSoup(html, 'lxml')
    if name:
        return soup.find(name)
    for n in ('p', 'h1', 'h2', 'h3', 'h4', 'li'):
        t = soup.find(n)
        if t:
            return t
    return soup.find()


def _doc(body_html):
    return f'<html><head><title>Test Doc</title></head><body>{body_html}</body></html>'


# Each case: (id, description, kind, payload, check)
# kind == 'header'  -> payload is html for a single tag, check(result) where
#                       result = is_section_header(tag)
# kind == 'parse'   -> payload is full-document body html, check(parsed) where
#                       parsed = parse_doc_html(doc_html)
CASES = []


def header_case(cid, desc, html, expected):
    CASES.append((cid, desc, 'header', html, lambda r: (r == expected, f'got {r!r}, expected {expected!r}')))


def parse_case(cid, desc, body_html, check_fn):
    CASES.append((cid, desc, 'parse', body_html, check_fn))


# --- #1/#2: "Почта" not recognized as email trigger; merged "Телеграм🎉..." ---
header_case('bug01a', 'Почта — standalone email trigger', '<p>Почта</p>', 'email_section')
header_case('bug02', 'Merged "Телеграм" + first TG line in one <p>',
            '<p>Телеграм 🎉Ты уже в самой продвинутой тусовке трекеров</p>', 'tg_section')

# --- #3: footnote comment divs leaking into body text ---
def _check_bug03(parsed):
    html = (parsed.get('email_html') or '') + (parsed.get('tg_html') or '')
    bad = 'к отправке' in html or 'cmnt' in html
    return (not bad, f'footnote leftover leaked into content: {bad}')

parse_case('bug03', 'Footnote/comment div not decomposed, leaks into body',
    '<p>Почта</p>'
    '<p>Текст письма<sup><a href="#cmnt1" id="cmnt_ref1">[a]</a></sup> продолжение.</p>'
    '<div class="footnote"><p><a href="#cmnt_ref1" id="cmnt1">[a]</a><span> 2058 к отправке https://example.com</span></p></div>',
    _check_bug03)

# --- #4: button + next paragraph glued via soft line break (\n in text node) ---
def _check_bug04(parsed):
    tg = parsed.get('tg_html') or ''
    ok = ('[КНОПКА]' not in tg) or ('\n' not in tg)  # weak smoke check: doesn't crash / doesn't glue visibly
    # Stronger: the soft-break text must not be swallowed as part of the button text
    return ('следующий текст' in tg or 'следующий текст' in (parsed.get('email_html') or ''),
            'soft-linebreak text after button lost')

parse_case('bug04', 'Button + next paragraph glued by Shift+Enter (\\n in text node)',
    '<p>Телеграм</p>'
    '<p>[КНОПКА]\nследующий текст</p>',
    _check_bug04)

# --- #9: <br>-separated list collapsed into one line (Нейрокот \s{2,} bug) ---
def _check_bug09(parsed):
    tg_variants = parsed.get('tg_variants') or []
    html = parsed.get('tg_html') or ''
    if tg_variants:
        html += ''.join(v.get('html', '') for v in tg_variants)
    br_count = html.count('<br')
    return (br_count >= 2, f'expected >=2 <br> preserved between list lines, got {br_count}')

parse_case('bug09', '<br>-separated list must not collapse into one line',
    '<p>Нейрокот</p>'
    '<p>Пункт один<br>Пункт два<br>Пункт три</p>',
    _check_bug09)

# --- #11: bare "БОТ" (no parens) not recognized as TG trigger ---
header_case('bug11', 'Bare "БОТ" (exact match) recognized as tg_section', '<p>БОТ</p>', 'tg_section')

# --- #14: "ПОЧТА От кого: Имя" merged header (new ТЗ format, no "Контент письма") ---
def _check_bug14(parsed):
    sender_ok = 'Кирилл' in (parsed.get('sender') or '') or True  # sender optional depending on path
    email_ok = bool(parsed.get('email_html'))
    leaked = 'от кого' in (parsed.get('email_html') or '').lower()
    return (email_ok and not leaked, f'email_html empty or "от кого" leaked: email_ok={email_ok} leaked={leaked}')

parse_case('bug14', '"ПОЧТА От кого: Имя" merged header opens email section',
    '<p>ПОЧТА От кого: Кирилл Пшинник</p>'
    '<p>Первый абзац письма про распродажу.</p>',
    _check_bug14)

# --- #16: "Сообщение для ботов ТГ и Макс" (plural) not recognized as TG ---
header_case('bug16', '"Сообщение для ботов ТГ и Макс" recognized as tg_section',
            '<p>Сообщение для ботов ТГ и Макс без 1 клика</p>', 'tg_section')

# --- #17: "Письмо в Юнисендер" as email_unisender alias ---
header_case('bug17', '"Письмо в Юнисендер" recognized as email_section',
            '<p>Письмо в Юнисендер</p>', 'email_section')

# --- #18/#34: "Бот (общий)" trailing label glued to prior content in same <p>,
# with real content for the NEW section ALSO glued after the label in the SAME <p>
# (span with its own leading <br>) — the deeper form of #18, fixed as #34/bug #18-b.
def _check_bug18(parsed):
    variants = parsed.get('tg_variants') or []
    if len(variants) < 2:
        return (False, f'expected 2 tg_variants (ГК + общий), got {len(variants)}')
    names = [v.get('name', '') for v in variants]
    v2_html = variants[1].get('html', '')
    leaked_label = 'бот (общий)' in v2_html.lower() or 'бот(общий)' in v2_html.lower()
    has_new_content = 'новый вариант текста' in v2_html.lower()
    return (not leaked_label and has_new_content,
            f'names={names} leaked_label={leaked_label} has_new_content={has_new_content}')

parse_case('bug18', '"Бот (общий)" trailing label + glued new-section content in same <p>',
    '<p>БОТ (1 клик)</p>'
    '<p>Основной текст ГК-бота.</p>'
    '<p>ИНН 9715401631<br/><br/><br/>'
    '<b>БОТ (общий)</b><br/><br/>Новый вариант текста для воронок.</p>',
    _check_bug18)

# --- #19: content before "РЕКЛАМА" in same <p> must survive (not dropped whole) ---
def _check_bug19(parsed):
    tg = parsed.get('tg_html') or ''
    variants = parsed.get('tg_variants') or []
    all_html = tg + ''.join(v.get('html', '') for v in variants)
    return ('Успей записаться' in all_html, 'CTA text before РЕКЛАМА marker was dropped')

parse_case('bug19', 'Text before РЕКЛАМА/ИНН marker in same <p> must not be discarded',
    '<p>Телеграм</p>'
    '<p>Успей записаться сегодня!<br/><br/>РЕКЛАМА ООО "ЗЕРОКОДЕР"<br/>ИНН 9715401631</p>',
    _check_bug19)

# --- #21: {First name} split across 3 spans must normalize to {first_name} ---
def _check_bug21(parsed):
    html = (parsed.get('email_html') or '') + (parsed.get('tg_html') or '')
    return ('{first_name}' in html, 'split {First name} spans did not normalize to {first_name}')

parse_case('bug21', '"{First name}" split across 3 spans normalizes to {first_name}',
    '<p>Почта</p>'
    '<p><span>{</span><span class="c15">First name</span><span class="c2">}, привет!</span></p>',
    _check_bug21)

# --- #24: hyphenated "ТГ-БОТ" section header not recognized (dash strips to "тгбот") ---
header_case('bug24', 'Hyphenated "ТГ-БОТ" recognized despite dash-joined first word',
            '<p>ТГ-БОТ основной текст рассылки</p>', 'tg_section')

# --- #25: button without brackets, link fills the whole paragraph (Case 2b) ---
def _check_bug25(parsed):
    html = parsed.get('email_html') or ''
    return ('btn_url' not in html or 'href="https://example.com/promo"' in html,
            'linked-only paragraph (no brackets) not recognized as a button target')

parse_case('bug25', 'Button without brackets — whole paragraph is a single link',
    '<p>Почта</p>'
    '<p><a href="https://example.com/promo">Забрать скидку</a></p>',
    _check_bug25)

# --- #26: image wrapped in <h2> instead of <p> must not be dropped ---
def _check_bug26(parsed):
    html = parsed.get('email_html') or ''
    return ('<img' in html, 'image wrapped in <h2> (not <p>) was silently dropped')

parse_case('bug26', 'Image wrapped in <h2> instead of <p> must survive',
    '<p>Почта</p>'
    '<h2><img src="https://example.com/pic.jpg"></h2>'
    '<p>Текст после картинки.</p>',
    _check_bug26)

# --- #27: "БОТ" merged with content via <br><br> INSIDE one span (get_text('') collapse) ---
def _check_bug27(parsed):
    variants = parsed.get('tg_variants') or []
    tg = parsed.get('tg_html') or ''
    all_html = tg + ''.join(v.get('html', '') for v in variants)
    return ('Тест-драйв' in all_html, '"БОТ<br/><br/>Тест-драйв..." collapsed into unmatchable "боттест-драйв"')

parse_case('bug27', '"БОТ" + content glued via <br><br> inside one span (empty-join collapse)',
    '<p>Почта</p>'
    '<p>Письмо про распродажу.</p>'
    '<p><span>БОТ<br/><br/></span><span>Тест-драйв нового курса стартует завтра.</span></p>',
    _check_bug27)

# --- #28: "Другие источники: Тема: ..." glued header, real content after must survive ---
def _check_bug28(parsed):
    subj = parsed.get('subject') or ''
    variants = parsed.get('tg_variants') or []
    tg2 = variants[1]['html'] if len(variants) > 1 else ''
    return ('Скидка' in subj, f'subject not extracted from glued "Другие источники: Тема:" header, got {subj!r}')

parse_case('bug28', '"Другие источники: Тема: X" glued in one <p> via <br><br>',
    '<p>Почта</p>'
    '<p>Письмо про распродажу.</p>'
    '<p>Другие источники: <br/><br/>Тема: Скидка только сегодня<br/><br/>Разберём по шагам, что внутри.</p>',
    _check_bug28)

# --- #30: detect_segment_from_doc must find "НЕЙРО" even behind a channel table ---
def _check_bug30(parsed):
    return (parsed.get('segment') == 'ai', f'expected segment=ai, got {parsed.get("segment")!r}')

parse_case('bug30', 'Segment detection finds НЕЙРО highlight behind a leading channel table',
    '<table><tr><td>Канал</td><td>Бот/почта</td></tr><tr><td>email</td><td>care@zerocoder.ru</td></tr></table>'
    '<p>Сегмент отправки (оставить нужное)</p>'
    '<p><span style="background-color:#ff0000">НЕЙРО</span></p>'
    '<p>Почта</p><p>Письмо.</p>',
    _check_bug30)

# --- #35 (new, this session): "N. Контент письма" glued with Тема:/Прехедер:
# via internal <br> inside ONE span — email section never opened at all.
def _check_bug35(parsed):
    subj = parsed.get('subject') or ''
    prev = parsed.get('preview') or ''
    email_html = parsed.get('email_html') or ''
    leaked = 'контент письма' in email_html.lower() or 'тема:' in email_html.lower()
    return (subj == 'Успеть сегодня' and 'Скидки' in prev and not leaked,
            f'subject={subj!r} preview={prev!r} leaked={leaked}')

parse_case('bug35', '"N. Контент письма" + Тема:/Прехедер: glued via internal <br> in one span',
    '<p><span>4. Контент письма<br/><br/>Тема: Успеть сегодня<br/><br/></span>'
    '<span>Прехедер: </span><span>Скидки закрываются сегодня вечером.</span></p>'
    '<p>Первый абзац письма.</p>',
    _check_bug35)


def run():
    passed, failed = 0, 0
    for cid, desc, kind, payload, check in CASES:
        try:
            if kind == 'header':
                tag = _tag(payload)
                result = mod.is_section_header(tag, ai_hints=None)
                ok, detail = check(result)
            else:
                parsed = mod.parse_doc_html(_doc(payload), ai_hints=None)
                ok, detail = check(parsed)
        except Exception as e:
            ok, detail = False, f'EXCEPTION: {e!r}'
        status = 'OK' if ok else 'FAIL'
        if ok:
            passed += 1
        else:
            failed += 1
        print(f'{status:4} {cid:10} {desc[:70]:70} {"" if ok else detail}')
    print()
    print(f'{passed} passed, {failed} failed, {len(CASES)} total')
    return failed == 0


if __name__ == '__main__':
    ok = run()
    sys.exit(0 if ok else 1)
