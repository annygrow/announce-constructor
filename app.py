import re
import os
import json
import logging
import requests
from datetime import timedelta
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from bs4 import BeautifulSoup, NavigableString, Tag
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, unquote, quote
from dotenv import load_dotenv
from openai import OpenAI
from config import CHANNELS, LOGO_URL, SITE_URL, PHONE, LEGAL_NOTICE

load_dotenv()

logging.basicConfig(
    filename='debug.log',
    level=logging.DEBUG,
    format='%(asctime)s %(levelname)s: %(message)s',
    encoding='utf-8',
)

app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
app.secret_key = os.environ.get('SECRET_KEY', 'change-me-please')
app.permanent_session_lifetime = timedelta(days=7)

APP_USERNAME = os.environ.get('APP_USERNAME', '')
APP_PASSWORD = os.environ.get('APP_PASSWORD', '')

@app.before_request
def require_login():
    if request.path.startswith('/static/') or request.path in ('/login', '/favicon.ico'):
        return None
    if not session.get('logged_in'):
        return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        if username == APP_USERNAME and password == APP_PASSWORD:
            session.permanent = True
            session['logged_in'] = True
            return redirect(url_for('index'))
        error = 'Неверный логин или пароль'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.after_request
def no_cache_static(response):
    if request.path.startswith('/static/'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

# ---------------------------------------------------------------------------
# AI parsing (OpenRouter)
# ---------------------------------------------------------------------------

_AI_SYSTEM_PROMPT = """Ты — помощник, который разбирает тексты рассылок на секции по каналам. Тебе дают сырой текст из Google Docs (экспорт в plain text). Твоя задача — определить границы секций и вернуть структурированный JSON.

=== СТРУКТУРА ДОКУМЕНТА ===

Документ состоит из секций, разделённых заголовками. Заголовки — это короткие строки (до 120 символов), которые называют канал или тип контента. Они НЕ являются частью контента письма.

=== СЕКЦИИ И ИХ ЗАГОЛОВКИ ===

**ТЕМА ПИСЬМА** (поле "subject"):
Заголовок секции содержит одно из: "тема письма", "тема:", "темы:", "subject:"
Значение — текст после двоеточия на той же строке, или следующая строка. Без самой метки.
ВАЖНО: если тема письма вынесена отдельной строкой без двоеточия (как первый заголовок документа), определи её по контексту — короткая фраза до начала контента писем.

**ПРЕХЕДЕР / ПРЕВЬЮ** (поле "preview"):
Заголовок содержит: "превью:", "прехедер:", "preview:", "preheader:"
Значение — ТОЛЬКО текст после двоеточия на той же строке. Без самой метки.
ВАЖНО: прехедер — это короткая фраза (обычно до 150 символов). Это НЕ абзацы письма. Если после метки идёт только одна короткая строка — бери только её, не захватывай следующий абзац.

**СЕКЦИИ, КОТОРЫЕ НУЖНО ПРОПУСКАТЬ (не включать в контент)**:
- строки-заголовки секций (сами метки, не контент)
- строки "ссылки:", "список ссылок", "ссылки"
- строки "кампания:", "каналы:", "сегмент:", "исключаем", "включаем", "from:", "от кого:"
- строки с адресами "@zerocoder", "care@", "getcourse", "unisender", "zerocoder.ru"
- строки "РЕКЛАМА ООО", "ИНН 9715401631" (юридический дисклеймер)
- строки-сноски вида "[a]", "[b]", "[1]" (аннотации к ссылкам)

**EMAIL — ВАРИАНТ ДЛЯ ГК** (поле "email_gc"):

‼️ КЛЮЧЕВОЕ ПРАВИЛО: Если заголовок секции содержит "1 клик", "1click", "в один клик" — эта секция ВСЕГДА идёт в email_gc, НЕЗАВИСИМО от порядка в документе.

Примеры заголовков → email_gc: "Почта (1 клик)", "Email (1 клик)", "В 1 клик", "Контент письма (1 клик)"
Также в email_gc идут секции с заголовками: "контент письма", "текст письма", "почта:", "e-mail:", "письмо:", "для почты", "email:"

Эта секция содержит переменные {first_name}, {offer_url_...} и т.п. — сохранять нетронутыми.

‼️ ВАЖНО: наличие текста "1 клик" или "в 1 клик" ВНУТРИ КНОПКИ или ссылки [ЗАРЕГИСТРИРОВАТЬСЯ В 1 КЛИК] — это КНОПКА, не заголовок секции. Такой текст не определяет принадлежность СЕКЦИИ к email_gc.

**EMAIL — ВАРИАНТ ДЛЯ UNISENDER** (поле "email_unisender"):
Секция с заголовком "другие источники", "другие каналы", "другой источник", "другие боты", "другой текст".
Это письмо для Unisender — без GC-переменных {first_name} и т.п.
Если такой секции нет — вернуть null.

‼️ ВАЖНО: "Другие источники" это ОТДЕЛЬНАЯ секция — не путать с кнопками или ссылками внутри email_gc.

**TG — ОСНОВНОЙ** (поле "tg_main"):
Секции с заголовком "телеграм", "telegram", "тг", "tg", "max", "телеграм/max", "тг/max" и т.п.
Это основной текст для ТГ бот ГК и Max. Если такой секции нет — вернуть null (не копировать email_gc).

**TG — ВОРОНКИ** (поле "tg_voronki"):

‼️ КЛЮЧЕВОЕ ПРАВИЛО: tg_voronki — это TG-текст для воронок/ботов. Его источник:
- отдельная секция "тг воронки", "для воронки", "телеграм воронки"
- ИЛИ TG-часть внутри секции "Другие источники" (если там есть короткий TG-текст отдельно от email)

‼️ НЕЛЬЗЯ помещать в tg_voronki:
- email-контент (длинные параграфы с {first_name}, кнопками [BUTTON], HTML-структурой)
- содержимое email_gc
- дубликат tg_main

Если отдельного TG-текста для воронок нет — вернуть null.

**НЕЙРОКОТ** (поле "neurocat"):
Только если есть явная секция с заголовком "нейрокот". Иначе — null.

=== ТИПИЧНАЯ СТРУКТУРА ДОКУМЕНТА ===

Вариант A (с 1 кликом):
1. Тема: ...
2. Превью: ...
3. [Почта (1 клик)] → email_gc (с {first_name}, для GetCourse)
4. [Другие источники] → email_unisender (без {first_name}, для Unisender)
   Если внутри "Другие источники" есть короткий TG-блок — это tg_voronki
5. [ТГ / Telegram / Max] → tg_main

Вариант B (без разделения):
1. Тема: ...
2. [Почта / Email] → email_gc
3. [ТГ / Telegram] → tg_main

=== ПРАВИЛА ОБРАБОТКИ КОНТЕНТА ===

1. Сохраняй переменные {first_name}, {offer_url_...}, {firstName} и любые {переменная} НЕТРОНУТЫМИ.
2. Сохраняй эмодзи в тексте и в кнопках.
3. Сохраняй форматирование (<b>, <i>) если есть в исходнике.
4. Сохраняй ссылки в тексте.
5. Сохраняй маркеры списков и структуру абзацев.
6. НЕ добавляй "РЕКЛАМА ООО ЗЕРОКОДЕР" и "ИНН 9715401631".
7. НЕ включай заголовки секций в контент.
8. Кнопки [ТЕКСТ КНОПКИ] — сохранять как есть, они обрабатываются отдельно.

=== ФОРМАТ ОТВЕТА ===

Возвращай ТОЛЬКО валидный JSON, без markdown-обёртки (без ```json, без пояснений).
Если секция не найдена — верни null для этого поля.

{
  "subject": "текст темы письма без метки",
  "preview": "текст прехедера без метки",
  "email_gc": "полный текст email-секции для ГК (HTML или plain text)",
  "email_unisender": "текст второго email-варианта или null",
  "tg_main": "текст основного TG-варианта или null",
  "tg_voronki": "TG-текст для воронок (короткий, без email-контента) или null",
  "neurocat": "текст для Нейрокота или null"
}"""


def parse_with_ai(raw_html):
    api_key = os.getenv('OPENROUTER_API_KEY')
    base_url = os.getenv('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1')
    model = os.getenv('OPENROUTER_MODEL', 'google/gemini-2.5-flash-lite')

    if not api_key:
        raise ValueError('OPENROUTER_API_KEY не задан в .env')

    client = OpenAI(api_key=api_key, base_url=base_url)

    soup = BeautifulSoup(raw_html, 'lxml')
    plain_text = soup.get_text(separator='\n', strip=True)

    if len(plain_text) > 30000:
        plain_text = plain_text[:30000]

    response = client.chat.completions.create(
        model=model,
        messages=[
            {'role': 'system', 'content': _AI_SYSTEM_PROMPT},
            {'role': 'user', 'content': f'Текст документа:\n\n{plain_text}'},
        ],
        temperature=0.1,
        max_tokens=8000,
    )

    raw_answer = response.choices[0].message.content.strip()
    raw_answer = re.sub(r'^```(?:json)?\s*', '', raw_answer)
    raw_answer = re.sub(r'\s*```$', '', raw_answer)

    parsed = json.loads(raw_answer)
    return parsed


_EMPTY_P_RE = re.compile(
    r'(<p[^>]*>(?:\s|&nbsp;|\xa0|<br\s*/?>)*</p>\s*)+$',
    re.IGNORECASE
)

def strip_trailing_empty_paragraphs(html):
    """Remove trailing empty/<br>-only <p> tags and last-paragraph bottom margin."""
    html = _EMPTY_P_RE.sub('', html).rstrip()
    # Remove bottom margin on the last <p> to avoid extra whitespace at block bottom
    last_pos = html.rfind('margin:0 0 10px 0')
    if last_pos != -1:
        html = html[:last_pos] + 'margin:0 0 0 0' + html[last_pos + len('margin:0 0 10px 0'):]
    return html


# ---------------------------------------------------------------------------
# Email HTML template pieces
# ---------------------------------------------------------------------------

EMAIL_WRAPPER_START = '''<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<title>{subject}</title>
<style type="text/css">
body {{ margin:0; padding:0; background-color:#F6F6F6; }}
img {{ border:0; outline:none; text-decoration:none; -ms-interpolation-mode:bicubic; }}
a {{ text-decoration:none; }}
@media only screen and (max-width:600px) {{
  .es-content-body {{ width:100% !important; }}
  .es-footer-body {{ width:100% !important; }}
  .es-left, .es-right {{ float:none !important; width:100% !important; }}
  .esdev-mso-td {{ display:block !important; width:100% !important; }}
  .esdev-mso-table {{ width:100% !important; }}
  .es-col-2, .es-col-3 {{ display:block !important; width:100% !important; padding:5px 0 !important; }}
  .es-col-img {{ max-width:200px !important; width:auto !important; display:block; margin:0 auto; }}
}}
</style>
</head>
<body style="margin:0;padding:0;background-color:#F6F6F6">
<table class="es-wrapper" cellspacing="0" cellpadding="0" width="100%" role="none" style="border-collapse:collapse;border-spacing:0;padding:0;margin:0;width:100%;height:100%;background-color:#F6F6F6">
<tr><td valign="top" style="padding:0;margin:0">'''

EMAIL_HEADER = '''
<table cellpadding="0" cellspacing="0" align="center" role="none" style="border-collapse:collapse;border-spacing:0;width:600px;background-color:#f6f6f6">
<tr><td align="left" bgcolor="#f6f6f6" style="padding:0 20px;margin:0;background-color:#f6f6f6">
<table cellpadding="0" cellspacing="0" width="100%" role="none" style="border-collapse:collapse;border-spacing:0">
<tr><td align="center" style="padding:10px;margin:0;font-size:0px">
<a href="https://zerocoder.ru/" target="_blank">
<img src="{logo_url}" alt="ZeroCoder" width="220" style="display:block;border:0;max-width:220px">
</a>
</td></tr>
</table></td></tr>
</table>
'''

EMAIL_FOOTER = '''
<table class="es-footer-body" cellspacing="0" cellpadding="0" align="center" role="none" style="border-collapse:collapse;border-spacing:0;background-color:#333333;width:600px">
<tr><td align="left" style="padding:20px 20px 10px;margin:0">
<table cellpadding="0" cellspacing="0" class="es-left" align="left" role="none" style="border-collapse:collapse;border-spacing:0;float:left;width:270px">
<tr><td align="left" style="padding:0;margin:0;width:270px">
<table cellpadding="0" cellspacing="0" width="100%" role="presentation" style="border-collapse:collapse;border-spacing:0">
<tr><td align="left" style="padding:0;margin:0">
<p style="margin:0;font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;line-height:21px;color:#FFFFFF;font-size:14px">Остались вопросы? Мы готовы помочь! Просто ответьте на это письмо.</p>
</td></tr>
<tr><td align="left" style="padding:10px 5px 20px 0;margin:0">
<p style="margin:0;font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;line-height:21px;color:#FFFFFF;font-size:14px">8-999-333-69-78</p>
</td></tr>
</table></td></tr>
</table>
<table cellpadding="0" cellspacing="0" class="es-right" align="right" role="none" style="border-collapse:collapse;border-spacing:0;float:right;width:270px">
<tr><td align="left" style="padding:0;margin:0;width:270px">
<table cellpadding="0" cellspacing="0" width="100%" role="presentation" style="border-collapse:collapse;border-spacing:0">
<tr><td align="right" style="padding:0;margin:0">
<p style="margin:0;font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;line-height:21px;color:#FFFFFF;font-size:14px"><a href="https://zerocoder.ru/" style="color:#FFFFFF;text-decoration:underline">https://zerocoder.ru/</a></p>
</td></tr>
<tr><td align="right" style="padding:20px 0 0;margin:0;font-size:0">
<table cellpadding="0" cellspacing="0" role="presentation" style="border-collapse:collapse;border-spacing:0">
<tr>
<td align="center" valign="top" style="padding:0 10px 0 0;margin:0"><a target="_blank" href="https://university.zerocoder.ru/wa" style="color:#FFFFFF"><img src="https://ifcna.stripocdnplugin.email/content/assets/img/messenger-icons/logo-white/whatsapp-logo-white.png" alt="Whatsapp" width="32" height="32" style="display:block;border:0"></a></td>
<td align="center" valign="top" style="padding:0;margin:0"><a target="_blank" href="https://university.zerocoder.ru/wa" style="color:#FFFFFF"><img src="https://ifcna.stripocdnplugin.email/content/assets/img/messenger-icons/logo-white/telegram-logo-white.png" alt="Telegram" width="32" height="32" style="display:block;border:0"></a></td>
</tr>
</table></td></tr>
</table></td></tr>
</table>
</td></tr>
</table>
'''

EMAIL_WRAPPER_END = '''
</td></tr>
</table>
</body>
</html>'''

# ---------------------------------------------------------------------------
# Email block helpers
# ---------------------------------------------------------------------------

def block_white(paragraphs_html):
    return (
        '<tr><td align="left" bgcolor="#ffffff" style="padding:10px 20px;margin:0;background-color:#ffffff">\n'
        + paragraphs_html
        + '\n</td></tr>'
    )

def block_grey(paragraphs_html):
    return (
        '<tr><td style="padding:5px 10px 10px;margin:0;background-color:#ffffff">\n'
        '<table cellspacing="0" cellpadding="0" width="100%" style="border-collapse:separate;border-spacing:0;border:10px solid #f0f1f3;border-radius:20px" role="presentation">\n'
        '<tr><td align="left" bgcolor="#f0f1f3" style="padding:10px 15px;margin:0;font-family:roboto,\'helvetica neue\',helvetica,arial,sans-serif;font-size:18px;line-height:27px;color:#333333">\n'
        + paragraphs_html
        + '\n</td></tr>\n</table></td></tr>'
    )

def block_dotted(paragraphs_html):
    return (
        '<tr><td style="padding:10px;margin:0;background-color:#ffffff">\n'
        '<table cellspacing="0" cellpadding="0" width="100%" style="border-collapse:separate;border-spacing:0;border:3px dashed #1544ed;border-radius:13px" role="presentation">\n'
        '<tr><td align="left" style="padding:15px 10px;margin:0;font-family:roboto,\'helvetica neue\',helvetica,arial,sans-serif;font-size:18px;line-height:27px;color:#333333">\n'
        + paragraphs_html
        + '\n</td></tr>\n</table></td></tr>'
    )

def block_blue_cta(text_html, button_url, button_text):
    return (
        '<tr><td style="padding:5px 10px 10px;margin:0;background-color:#ffffff">\n'
        '<table cellspacing="0" cellpadding="0" width="100%" style="border-collapse:separate;border-spacing:0;border:10px solid #1445ea;border-radius:20px" role="presentation">\n'
        '<tr><td align="left" bgcolor="#1445ea" style="padding:15px 10px 5px;margin:0;font-family:roboto,\'helvetica neue\',helvetica,arial,sans-serif;font-size:16px;line-height:24px;color:#ffffff">\n'
        + text_html
        + '\n</td></tr>\n'
        '<tr><td align="center" bgcolor="#1445ea" style="padding:10px 0 15px;margin:0">\n'
        f'<a href="{button_url}" target="_blank" style="background:#E1FB52;color:#000000;padding:12px 50px;border-radius:30px;text-decoration:none;font-family:roboto,\'helvetica neue\',helvetica,arial,sans-serif;font-size:16px;display:inline-block;font-weight:600">{button_text}</a>\n'
        '</td></tr>\n</table></td></tr>'
    )

def block_image_center(image_url):
    return (
        '<tr><td align="center" bgcolor="#ffffff" style="padding:10px 20px;margin:0;background-color:#ffffff;font-size:0px">\n'
        f'<img src="{image_url}" alt="" width="560" style="display:block;border:0;max-width:100%;border-radius:10px">\n'
        '</td></tr>'
    )

def block_blue_text(paragraphs_html):
    """Blue block without a CTA button."""
    return (
        '<tr><td style="padding:5px 10px 10px;margin:0;background-color:#ffffff">\n'
        '<table cellspacing="0" cellpadding="0" width="100%" style="border-collapse:separate;border-spacing:0;border:10px solid #1445ea;border-radius:20px" role="presentation">\n'
        '<tr><td align="left" bgcolor="#1445ea" style="padding:15px 10px;margin:0;font-family:roboto,\'helvetica neue\',helvetica,arial,sans-serif;font-size:18px;line-height:27px;color:#ffffff">\n'
        + paragraphs_html
        + '\n</td></tr>\n</table></td></tr>'
    )

def block_button(btn_url, btn_text):
    """Standalone button on white background, centered."""
    btn_style = (
        "background:#E1FB52;color:#000000;padding:12px 50px;border-radius:30px;"
        "text-decoration:none;font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;"
        "font-size:16px;display:inline-block;font-weight:600"
    )
    return (
        '<tr><td align="center" bgcolor="#ffffff" style="padding:8px 20px 20px;background-color:#ffffff">\n'
        f'<a href="{btn_url}" target="_blank" style="{btn_style}">{btn_text}</a>\n'
        '</td></tr>'
    )

def block_spacer(height=20):
    """Vertical spacer between blocks."""
    return (
        f'<tr><td height="{height}" style="height:{height}px;font-size:1px;line-height:1px;'
        f'background-color:#ffffff">&nbsp;</td></tr>'
    )

def block_2col_img_text(image_url, text_html):
    """Two-column block: image left, text right."""
    col_style = "font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;font-size:16px;line-height:24px;color:#333333"
    return (
        '<tr><td align="left" bgcolor="#ffffff" style="padding:10px 20px;background-color:#ffffff">\n'
        '<table cellpadding="0" cellspacing="0" width="100%" role="none" style="border-collapse:collapse;border-spacing:0">\n'
        '<tr>\n'
        '<td class="es-col-2" align="left" valign="top" style="padding-right:12px;width:50%">\n'
        f'<img src="{image_url}" alt="" class="es-col-img" style="display:block;border:0;width:100%;max-width:260px;border-radius:8px">\n'
        '</td>\n'
        f'<td class="es-col-2" align="left" valign="top" style="padding-left:12px;width:50%;{col_style}">\n'
        + text_html
        + '\n</td>\n</tr>\n</table>\n</td></tr>'
    )

def block_2col_text_img(text_html, image_url):
    """Two-column block: text left, image right."""
    col_style = "font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;font-size:16px;line-height:24px;color:#333333"
    return (
        '<tr><td align="left" bgcolor="#ffffff" style="padding:10px 20px;background-color:#ffffff">\n'
        '<table cellpadding="0" cellspacing="0" width="100%" role="none" style="border-collapse:collapse;border-spacing:0">\n'
        '<tr>\n'
        f'<td class="es-col-2" align="left" valign="top" style="padding-right:12px;width:50%;{col_style}">\n'
        + text_html
        + '\n</td>\n'
        '<td class="es-col-2" align="left" valign="top" style="padding-left:12px;width:50%">\n'
        f'<img src="{image_url}" alt="" class="es-col-img" style="display:block;border:0;width:100%;max-width:260px;border-radius:8px">\n'
        '</td>\n</tr>\n</table>\n</td></tr>'
    )

def block_2col_text_text(left_html, right_html):
    """Two equal text columns."""
    col_style = "font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;font-size:16px;line-height:24px;color:#333333"
    return (
        '<tr><td align="left" bgcolor="#ffffff" style="padding:10px 20px;background-color:#ffffff">\n'
        '<table cellpadding="0" cellspacing="0" width="100%" role="none" style="border-collapse:collapse;border-spacing:0">\n'
        '<tr>\n'
        f'<td class="es-col-2" align="left" valign="top" style="padding-right:12px;width:50%;{col_style}">\n'
        + left_html
        + f'\n</td>\n<td class="es-col-2" align="left" valign="top" style="padding-left:12px;width:50%;{col_style}">\n'
        + right_html
        + '\n</td>\n</tr>\n</table>\n</td></tr>'
    )

def block_3col_text(col1_html, col2_html, col3_html):
    """Three equal text columns, each styled as a grey box."""
    inner_style = "padding:12px 14px;font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;font-size:14px;line-height:20px;color:#333333"
    box_style = "border-collapse:separate;border-spacing:0;width:100%;height:100%;background-color:#f5f5f5;border-radius:8px"

    def col_box(html, outer_pad):
        return (
            f'<td class="es-col-3" align="left" valign="top" style="{outer_pad};width:33%;height:100%">\n'
            f'<table cellpadding="0" cellspacing="0" role="presentation" width="100%" height="100%" style="{box_style}">\n'
            f'<tr><td valign="top" style="{inner_style}">\n{html}\n</td></tr></table>\n</td>'
        )

    return (
        '<tr><td align="left" bgcolor="#ffffff" style="padding:10px 20px;background-color:#ffffff">\n'
        '<table cellpadding="0" cellspacing="0" width="100%" role="none" style="border-collapse:collapse;border-spacing:0">\n'
        '<tr>\n'
        + col_box(col1_html, 'padding-right:6px')
        + '\n'
        + col_box(col2_html, 'padding:0 3px')
        + '\n'
        + col_box(col3_html, 'padding-left:6px')
        + '\n</tr>\n</table>\n</td></tr>'
    )

def make_p(text, bold=False, italic=False, font_size=18, color='#333333'):
    style = (
        f"margin:0;font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;"
        f"line-height:27px;color:{color};font-size:{font_size}px"
    )
    content = text
    if bold:
        content = f'<b>{content}</b>'
    if italic:
        content = f'<i>{content}</i>'
    return f'<p style="{style}">{content}</p>'

# ---------------------------------------------------------------------------
# URL / UTM helpers
# ---------------------------------------------------------------------------

def decode_google_redirect(url):
    """Unwrap https://www.google.com/url?q=ACTUAL_URL"""
    if 'google.com/url' in url:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        if 'q' in qs:
            return unquote(qs['q'][0])
    return url

def is_gc_variable(url):
    """Check if a string is a GC template variable like {offer_url_...} or {first_name}"""
    return bool(re.match(r'^\{[^}]+\}$', url.strip()))

def build_utm_url(url, channel_key, campaign, date, segment=''):
    """Inject UTM parameters into a URL. Preserves GC variables unchanged."""
    if not url:
        return url
    url = url.strip()
    if is_gc_variable(url):
        return url
    if not url.startswith('http'):
        return url

    url = decode_google_redirect(url)
    ch = CHANNELS.get(channel_key, {})

    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query, keep_blank_values=True)

        if ch.get('source'):
            params['utm_source'] = [ch['source']]
        if ch.get('medium'):
            params['utm_medium'] = [ch['medium']]
        if campaign:
            params['utm_campaign'] = [campaign]
        if ch.get('content'):
            _seg_channels = {'email', 'tg_gc'}
            suffix = ('-dev' if segment == 'dev' else '-ai' if segment == 'ai' else '') if channel_key in _seg_channels else ''
            params['utm_content'] = [ch['content'] + suffix]
        if date:
            params['utm_term'] = [date]

        new_query = urlencode({k: v[0] for k, v in params.items()})
        return urlunparse(parsed._replace(query=new_query))
    except Exception:
        return url

def inject_utm_in_html(html_text, channel_key, campaign, date, footnote_links=None, segment=''):
    """
    Replace all href="..." values in an HTML string with UTM-injected versions.
    Also replaces footnote anchors [a], [b] etc. with resolved URLs.
    """
    footnote_map = {}
    if footnote_links:
        for link in footnote_links:
            footnote_map[link.get('label', '')] = link.get('url', '')

    def replace_href(match):
        raw = match.group(1)
        if is_gc_variable(raw):
            return match.group(0)
        decoded = decode_google_redirect(raw)
        utmified = build_utm_url(decoded, channel_key, campaign, date, segment)
        return f'href="{utmified}"'

    result = re.sub(r'href="([^"]*)"', replace_href, html_text)
    return result

# ---------------------------------------------------------------------------
# Google Doc fetching & parsing
# ---------------------------------------------------------------------------

def extract_doc_id(url):
    m = re.search(r'docs\.google\.com/document/d/([a-zA-Z0-9_-]+)', url)
    if m:
        return m.group(1)
    return None

def _title_from_content_disposition(cd_header):
    """Extract document filename from Content-Disposition, strip extension."""
    if not cd_header:
        return ''
    m = re.search(r"filename\*=UTF-8''([^\s;]+)", cd_header, re.IGNORECASE)
    if m:
        name = unquote(m.group(1))
    else:
        m = re.search(r'filename=["\']?([^"\';\r\n]+)["\']?', cd_header, re.IGNORECASE)
        name = m.group(1).strip() if m else ''
    return re.sub(r'\.(html?|docx?)$', '', name, flags=re.IGNORECASE).strip()


def fetch_google_doc_html(doc_id):
    export_url = f'https://docs.google.com/document/d/{doc_id}/export?format=html'
    headers = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/120.0.0.0 Safari/537.36'
        )
    }
    resp = requests.get(export_url, headers=headers, allow_redirects=True, timeout=30)
    resp.raise_for_status()
    title = _title_from_content_disposition(resp.headers.get('Content-Disposition', ''))
    return resp.text, title

def get_text_content(tag):
    """Get plain text from a BS4 tag, collapsing whitespace."""
    return ' '.join(tag.get_text(' ', strip=True).split())

def is_section_header(tag):
    """
    Returns the section key if this tag is a section divider, else None.
    Matches any short <p>, <li>, or heading that contains section keywords —
    no bold-only requirement, because Google Docs section headers export as
    plain paragraphs or numbered list items.
    """
    tag_name = tag.name if tag.name else ''
    if tag_name not in ('h1', 'h2', 'h3', 'h4', 'p', 'li'):
        return None

    # Tags inside HTML tables are channel-config cells, never section headers
    if tag.find_parent('table'):
        return None

    text = get_text_content(tag).lower().strip()
    if not text:
        return None

    # Early check: merged paragraph where label is the very first word and content follows.
    # Example: "Телеграм🎉Ты уже в самой продвинутой тусовке..."
    # Must run BEFORE the length guard, since merged paragraphs are long.
    _first_word_raw = text.split()[0] if text.split() else ''
    _first_word_alpha = ''.join(ch for ch in _first_word_raw if ch.isalpha())
    if _first_word_alpha in {'телеграм', 'telegram'} and text != _first_word_alpha:
        return 'tg_section'

    # Section headers are short labels, not body sentences
    if len(text) > 120:
        return None

    # Skip rows that are clearly channel entries or service addresses
    skip_kw = ['care@', '@zerocoder', 'getcourse', 'unisender', 'bot (', 'бот (',
               'zerocoder_bot', 'zerocoder.ru', 'newcat.zerocoder']
    if any(s in text for s in skip_kw):
        return None

    email_kw = ['контент письма', 'текст письма', 'текст:', 'текст для почты',
                'почта:', 'почта (', '3.контент', '3. контент', 'e-mail:', 'письмо:',
                'для почты', 'email:',
                # Group separators: start a new email variant (and later TG variant within them)
                # "Другие источники" contains Unisender email + Voronki TG content
                'другие источники', 'другие каналы', 'другой источник', 'другие боты',
                'другой текст',
                # Abbreviated forms actually used in documents
                'для др источников', 'для других источников', 'др источники',
                'для др. источников', 'другой ист', 'другие ист']
    tg_kw = ['телеграм/max', 'телеграм/макс', 'telegram/max', 'telegram/макс', 'тг/max', 'тг/макс', 'тг+max',
             'телеграм бот', 'тг бот', 'telegram бот', 'текст для тг', 'для телеграм', 'для тг',
             'телеграм (', 'тг (', 'telegram (', 'тг:', 'тг+мax',
             'телеграм воронки', 'тг воронки', 'telegram воронки', 'для воронки',
             'бота тг', 'бота telegram', 'сообщение для тг', 'сообщение для бота']
    tg_only_kw = ['телеграм:', 'telegram:']
    subject_kw = ['тема письма', 'тема:', 'темы:', 'subject:']
    preview_kw = ['превью:', 'прехедер:', 'прехендер:', 'preview:', 'preheader:', 'preheader :', 'прехэдер:']
    meta_kw = ['кампания:', 'каналы ', 'каналы(', 'сегмент ', 'сегмент(',
               'исключаем', 'включаем', 'от кого:', 'from:']

    # Meta-content labels: skip entirely (neither section header nor content)
    meta_label_kw = ['от лица ', 'от лица:', 'в 1 клик', 'в 2 клик', 'в один клик',
                     'обычная рассылка', 'ссылки:', 'список ссылок']
    if any(text.startswith(kw) for kw in meta_label_kw):
        return 'skip'
    if text in ('ссылки',):
        return 'skip'

    # Skip meta-section headers (campaign, channels, segments, sender info)
    if any(text.startswith(kw) or text == kw.rstrip(':') for kw in meta_kw):
        return 'skip'

    # Standalone email section headers — exact match
    email_exact = {'почта', 'письмо', 'e-mail'}
    if text in email_exact:
        return 'email_section'

    # Standalone TG section headers — single-word only, exact match to avoid false positives
    tg_exact = {'телеграм', 'telegram', 'тг', 'tg', 'max', 'instagram', 'push', 'youtube',
                'инстаграм', 'ютуб', 'нейрокот', 'помощник'}
    if text in tg_exact:
        return 'tg_section'

    # Handle merged paragraph: label + first content line in one <p> (e.g. "Телеграм\n🎉Ты уже...")
    # Google Docs sometimes puts the section label and first line in the same paragraph via <br> tags.
    # No length limit here — the merged paragraph can be arbitrarily long.
    first_word = text.split()[0] if text.split() else ''
    # Also handle label glued to emoji without space: "Телеграм🎉..."
    first_word_alpha = ''.join(ch for ch in first_word if ch.isalpha()).lower()
    if first_word_alpha in {'телеграм', 'telegram'}:
        return 'tg_section'

    # Skip standalone single-word channel-type labels that appear in config tables
    if text in ('email', 'telegram/max', 'приложение'):
        return None

    for kw in subject_kw:
        if text.startswith(kw) or kw in text:
            return 'subject'
    for kw in preview_kw:
        if text.startswith(kw) or kw in text:
            return 'preview'
    for kw in email_kw:
        if kw in text:
            return 'email_section'
    for kw in tg_kw:
        if kw in text:
            return 'tg_section'
    for kw in tg_only_kw:
        if text.startswith(kw):
            return 'tg_section'

    return None

def extract_footnotes(soup):
    """
    Extract footnote links from a Google Docs HTML export.
    Returns:
      footnotes: list of {'label': 'a', 'url': '...', 'text': '...'}
      cmnt_url_map: dict {cmnt_id: url} for resolving inline comment references
    """
    footnotes = []
    cmnt_url_map = {}

    # Google Docs exports comment/annotation links as:
    # <div class="..."><p><a href="#cmnt_refN" id="cmntN">[letter]</a><span>url or {var}</span></p></div>
    # Body text references them via: <sup><a href="#cmntN" id="cmnt_refN">[letter]</a></sup>
    for a_tag in soup.find_all('a', id=re.compile(r'^cmnt\d')):
        cmnt_id = a_tag.get('id', '')  # e.g. "cmnt1", "cmnt4"
        label_text = a_tag.get_text(strip=True)  # e.g. "[a]", "[d]"
        label_m = re.match(r'^\[([a-zA-Z0-9]+)\]$', label_text)
        if not label_m:
            continue
        label = label_m.group(1)
        # URL/variable is in the immediately-following sibling element or span
        next_sib = a_tag.next_sibling
        url_text = ''
        if next_sib is not None:
            if hasattr(next_sib, 'get_text'):
                url_text = next_sib.get_text(strip=True)
            else:
                url_text = str(next_sib).strip()
        url_m = re.search(r'(https?://\S+|\{[a-zA-Z_][^}]*\})', url_text)
        if url_m:
            url = decode_google_redirect(url_m.group(1).rstrip('.,)'))
            footnotes.append({'label': label, 'url': url, 'text': label_text + ' ' + url_text})
            cmnt_url_map[cmnt_id] = url

    # Fallback: classic Google Docs footnotes in <div id="ftn..."> containers
    if not footnotes:
        for div in soup.find_all('div', id=re.compile(r'^ftn')):
            text = get_text_content(div)
            label_m = re.search(r'\[([a-zA-Z0-9]+)\]', text)
            label = label_m.group(1) if label_m else str(len(footnotes) + 1)
            for a in div.find_all('a', href=True):
                url = decode_google_redirect(a['href'])
                footnotes.append({'label': label, 'url': url, 'text': text.strip()})

    # Remove footnote/comment containers from the soup so they don't leak into content.
    # These are divs that contain <a id="cmntN"> or <a id="ftnN"> anchors.
    removed = set()
    for a_tag in soup.find_all('a', id=re.compile(r'^(cmnt|ftn)\d')):
        container = a_tag.find_parent('div')
        if container and id(container) not in removed:
            removed.add(id(container))
            container.decompose()

    return footnotes, cmnt_url_map


def resolve_comment_refs(html_str, cmnt_url_map):
    """
    Pre-process HTML: for each <sup><a href="#cmntN">[x]</a></sup> pattern,
    wrap the immediately-preceding sibling text/span with <a href="url">.
    Removes the <sup> marker after wrapping.
    """
    if not cmnt_url_map or not html_str:
        return html_str

    soup = BeautifulSoup(html_str, 'lxml')
    body = soup.find('body') or soup

    for sup in body.find_all('sup'):
        a_tag = sup.find('a', href=re.compile(r'^#cmnt'))
        if not a_tag:
            continue
        ref = a_tag.get('href', '').lstrip('#')  # e.g. "cmnt4"
        url = cmnt_url_map.get(ref)
        if not url:
            continue

        # Find the immediately preceding sibling node
        prev = sup.previous_sibling
        if prev is None:
            sup.decompose()
            continue

        # If prev contains only punctuation (e.g. "."), look one step further back
        # to find the actual linked word (e.g. "тут") and wrap that instead
        prev_text = ''
        if isinstance(prev, NavigableString):
            prev_text = str(prev).strip()
        elif hasattr(prev, 'name'):
            prev_text = prev.get_text().strip()

        if prev_text and re.match(r'^[.,!?;:\-–—]+$', prev_text):
            word_prev = prev.previous_sibling
            if word_prev is not None and hasattr(word_prev, 'name') and word_prev.name in ('span', 'b', 'strong', 'i', 'em'):
                word_prev.wrap(soup.new_tag('a', href=url))
                sup.decompose()
                continue

        if isinstance(prev, NavigableString):
            # Wrap the text node in <a>
            new_a = soup.new_tag('a', href=url)
            prev.replace_with(new_a)
            new_a.string = str(prev)
        elif hasattr(prev, 'name') and prev.name in ('span', 'b', 'strong', 'i', 'em'):
            prev.wrap(soup.new_tag('a', href=url))

        sup.decompose()

    # Return just the body's inner HTML
    body = soup.find('body')
    return ''.join(str(c) for c in body.children) if body else html_str

def extract_all_links(soup):
    """
    Collect all unique hrefs from the doc body (excluding footnote divs,
    navigation, and Google redirect wrappers).
    Returns list of unique resolved URLs.
    """
    links = []
    seen = set()
    for a in soup.find_all('a', href=True):
        raw = a['href']
        if not raw or raw.startswith('#'):
            continue
        resolved = decode_google_redirect(raw)
        if resolved not in seen:
            seen.add(resolved)
            links.append({'url': resolved, 'text': a.get_text(strip=True)})
    return links

def inline_gdoc_formatting(soup):
    """
    Google Docs HTML uses CSS class-based formatting (e.g. .c3 {font-weight:700}).
    Extract those rules and apply them as inline styles so downstream parsers can detect them.
    """
    style_tag = soup.find('style')
    if not style_tag:
        return

    css_text = style_tag.get_text()
    bold_cls, italic_cls, center_cls, right_cls = set(), set(), set(), set()

    for selector, props in re.findall(r'(\.[\w-]+)\s*\{([^}]+)\}', css_text):
        cls = selector[1:]
        p = props.lower().replace(' ', '')
        if 'font-weight:700' in p or 'font-weight:bold' in p:
            bold_cls.add(cls)
        if 'font-style:italic' in p:
            italic_cls.add(cls)
        if 'text-align:center' in p:
            center_cls.add(cls)
        if 'text-align:right' in p:
            right_cls.add(cls)

    if not (bold_cls or italic_cls or center_cls or right_cls):
        return

    for tag in soup.find_all(True):
        classes = tag.get('class', [])
        if isinstance(classes, str):
            classes = classes.split()
        if not classes:
            continue
        existing = tag.get('style', '')
        additions = []
        if any(c in bold_cls for c in classes) and 'font-weight' not in existing:
            additions.append('font-weight:700')
        if any(c in italic_cls for c in classes) and 'font-style' not in existing:
            additions.append('font-style:italic')
        if any(c in center_cls for c in classes) and 'text-align' not in existing:
            additions.append('text-align:center')
        elif any(c in right_cls for c in classes) and 'text-align' not in existing:
            additions.append('text-align:right')
        if additions:
            sep = ';' if existing and not existing.endswith(';') else ''
            tag['style'] = existing + sep + ';'.join(additions)


def fix_orphan_sups(body):
    """
    Google Docs sometimes exports <sup> comment markers OUTSIDE the preceding <p> tag:
      <p>ссылка</p><sup><a href="#cmntN">...</a></sup>
    walk_blocks only collects <p> tags, so such orphan <sup> elements are silently
    dropped and their references are never resolved.  Move them inside the preceding
    block tag so they are serialised together.
    """
    for sup in list(body.find_all('sup')):
        parent = sup.parent
        if not parent:
            continue
        # Only fix when sup is a direct child of a block container (div, body, section…)
        if parent.name in ('p', 'li', 'td', 'th', 'span', 'b', 'i', 'em', 'strong', 'a', 'u', 's'):
            continue  # already inline — leave as is
        # Walk backwards past whitespace text nodes to find the previous sibling
        prev = sup.previous_sibling
        while prev is not None and isinstance(prev, NavigableString) and not prev.strip():
            prev = prev.previous_sibling
        if prev is not None and hasattr(prev, 'name') and prev.name in ('p', 'li'):
            sup.extract()
            prev.append(sup)


def parse_doc_html(html_content):
    """
    Parse Google Docs exported HTML and return structured content dict:
    {
      'email_text': <BeautifulSoup section or None>,
      'tg_text':    <BeautifulSoup section or None>,
      'subject':    str,
      'preview':    str,
      'links':      [...],
      'footnotes':  [...],
      'raw_paragraphs': [all p tags],
    }
    But we return serialised HTML strings, not soup objects, for JSON serialisation.
    """
    soup = BeautifulSoup(html_content, 'lxml')
    inline_gdoc_formatting(soup)
    body = soup.find('body') or soup
    fix_orphan_sups(body)

    # Extract Google Docs document title (strip " - Google Docs / Документы" suffix)
    title_tag = soup.find('title')
    doc_title = title_tag.get_text(strip=True) if title_tag else ''
    doc_title = re.sub(r'\s*[-–]\s*Google\s+(?:Docs|Документы|Document[s]?)\s*$', '', doc_title, flags=re.IGNORECASE).strip()

    footnotes, cmnt_url_map = extract_footnotes(soup)
    all_links = extract_all_links(soup)

    sections = {
        'other': [],
    }
    email_subsections = []  # list of {'name': str, 'blocks': []}
    tg_subsections = []     # list of {'name': str, 'blocks': []}

    current_section = 'other'
    subject = ''
    preview = ''
    sender = ''

    def process_block(tag):
        nonlocal current_section, subject, preview, sender
        section_type = is_section_header(tag)

        if section_type == 'skip':
            # Capture sender name from "от кого: ..." meta line before discarding
            if not sender:
                raw_text = get_text_content(tag).strip()
                m = re.match(r'^от\s+кого\s*[:\s]\s*(.+)', raw_text, re.IGNORECASE)
                if m:
                    val = m.group(1).strip()
                    if 'зерокодер' not in val.lower():
                        val = val + ' из Зерокодера'
                    sender = val
            return

        if section_type == 'email_section':
            name = get_text_content(tag).strip()
            # Trim "От кого: ..." suffix from sub-variant names
            name = re.sub(r'\s+от кого.*', '', name, flags=re.IGNORECASE).strip()
            email_subsections.append({'name': name[:50], 'blocks': []})
            current_section = 'email_section'
            return
        if section_type == 'tg_section':
            full_text = get_text_content(tag).strip()
            tg_subsections.append({'name': full_text[:50], 'blocks': []})
            current_section = 'tg_section'
            # If merged paragraph (label + content in same <p>), preserve content without label.
            # "Merged" means the paragraph has more than just the section-label word.
            # We detect this by checking that the tag contains at least two distinct spans
            # (the label span and the content span), or the text is longer than the label alone.
            tg_label_words = {'телеграм', 'telegram', 'тг', 'tg', 'max'}
            full_lower = full_text.lower()
            # Strip leading alpha chars to get the label (handles "Телеграм🎉..." glued together)
            first_word_raw = full_text.split()[0] if full_text.split() else ''
            first_word_alpha = ''.join(ch for ch in first_word_raw if ch.isalpha()).lower()
            is_merged = first_word_alpha in tg_label_words and full_lower != first_word_alpha
            if is_merged:
                # Clone the tag, remove the first span that is the section label
                tag_copy = BeautifulSoup(str(tag), 'lxml').find(tag.name)
                if tag_copy:
                    for span in list(tag_copy.find_all('span')):
                        span_text_alpha = ''.join(
                            ch for ch in get_text_content(span).strip() if ch.isalpha()
                        ).lower()
                        if span_text_alpha == first_word_alpha:
                            span.decompose()
                            break
                    # Remove empty wrapper tags left after span removal (e.g. <b></b>)
                    for empty_wrapper in tag_copy.find_all(['b', 'strong', 'i', 'em', 'u', 's']):
                        if not empty_wrapper.get_text(strip=True) and not empty_wrapper.find():
                            empty_wrapper.decompose()
                    if get_text_content(tag_copy).strip():
                        tg_subsections[-1]['blocks'].append(tag_copy)
            return
        if section_type == 'subject':
            # Extract text after the keyword from the tag itself
            raw = get_text_content(tag).strip()
            extracted = re.sub(r'^[^:]+:\s*', '', raw, count=1).strip()
            if extracted and not subject:
                subject = extracted
            return  # consumed, don't add to any section
        if section_type == 'preview':
            raw = get_text_content(tag).strip()
            extracted = re.sub(r'^[^:]+:\s*', '', raw, count=1).strip()
            if extracted and not preview:
                preview = extracted
            return

        # Also detect inline subject/preview in the 'other' or 'email_section' when
        # the keyword and value are on the SAME line (e.g. "Тема: My Subject")
        if not subject or not preview:
            txt = get_text_content(tag).strip()
            txt_lower = txt.lower()
            for kw in ['тема письма:', 'тема:', 'темы:', 'subject:']:
                if txt_lower.startswith(kw) and not subject:
                    subject = re.sub(r'^[^:]+:\s*', '', txt, count=1).strip()
                    return
            for kw in ['превью:', 'прехедер:', 'preview:', 'preheader:']:
                if txt_lower.startswith(kw) and not preview:
                    preview = re.sub(r'^[^:]+:\s*', '', txt, count=1).strip()
                    return

        if current_section == 'tg_section' and tg_subsections:
            tg_subsections[-1]['blocks'].append(tag)
        elif current_section == 'email_section' and email_subsections:
            email_subsections[-1]['blocks'].append(tag)
        else:
            sections['other'].append(tag)

    def walk_blocks(parent):
        """Walk direct block children; for ul/ol peek inside for section-header li items."""
        for child in parent.children:
            if not hasattr(child, 'name') or child.name is None:
                continue
            name = child.name
            if name in ('h1', 'h2', 'h3', 'h4', 'p', 'table'):
                process_block(child)
            elif name in ('ul', 'ol'):
                # Peek at direct <li> children for section headers
                lis = child.find_all('li', recursive=False)
                if any(is_section_header(li) for li in lis):
                    for li in lis:
                        process_block(li)
                else:
                    process_block(child)
            elif name == 'div':
                div_id = child.get('id', '')
                # Skip Google Docs comment/annotation divs (id="cmnt1", "cmnt2", etc.)
                if re.match(r'^cmnt', div_id):
                    continue
                walk_blocks(child)

    walk_blocks(body)

    # Build HTML strings for each section
    def tags_to_html(tags):
        parts = []
        for t in tags:
            # Replace non-breaking spaces before checking emptiness —
            # Google Docs often exports empty paragraphs as <p><span>&nbsp;</span></p>
            txt = t.get_text(strip=True).replace('\xa0', '').strip()
            if txt or t.name in ('ul', 'ol', 'table'):
                parts.append(str(t))
        return '\n'.join(parts)

    # Build email variants
    email_variant_list = []
    for sub in email_subsections:
        html = tags_to_html(sub['blocks'])
        if html:
            email_variant_list.append({'name': sub['name'], 'html': html})

    # Reorder: variant with "1 клик" in name must always be at index 0 (email_gc),
    # regardless of order in the source document.
    if len(email_variant_list) > 1:
        one_klik_idx = next(
            (i for i, v in enumerate(email_variant_list)
             if 'клик' in v['name'].lower() or 'klik' in v['name'].lower()),
            None
        )
        if one_klik_idx is not None and one_klik_idx != 0:
            email_variant_list.insert(0, email_variant_list.pop(one_klik_idx))

    if not email_variant_list:
        email_html = ''
        email_variants = None
    elif len(email_variant_list) == 1:
        email_html = email_variant_list[0]['html']
        email_variants = None
    else:
        # Default to first variant (guaranteed to be "1 клик" / email_gc after reorder above)
        email_html = email_variant_list[0]['html']
        email_variants = email_variant_list

    # Build TG variants
    tg_variant_list = []
    for sub in tg_subsections:
        html = tags_to_html(sub['blocks'])
        if html:
            tg_variant_list.append({'name': sub['name'], 'html': html})

    # Reorder: variant with "1 клик" must be at index 0 (TG ГК = основной бот),
    # and general/"общее"/"макс"/"другие источники" variant at index 1 (Воронки/bots).
    if len(tg_variant_list) > 1:
        one_klik_idx = next(
            (i for i, v in enumerate(tg_variant_list)
             if 'клик' in v['name'].lower() or 'klik' in v['name'].lower()),
            None
        )
        if one_klik_idx is not None and one_klik_idx != 0:
            # Move the "1 клик" variant to position 0
            item = tg_variant_list.pop(one_klik_idx)
            tg_variant_list.insert(0, item)

    if not tg_variant_list:
        tg_html = ''
        tg_variants = None
    elif len(tg_variant_list) == 1:
        tg_html = tg_variant_list[0]['html']
        tg_variants = None
    else:
        # index=0 → TG ГК ("1 клик" основной бот), index=1 → Воронки/bots (другие источники)
        tg_html = tg_variant_list[0]['html']
        tg_variants = tg_variant_list

    # Fallback: if only one content block found, use it for both
    if not email_html and not tg_html:
        email_html = tags_to_html(sections['other'])
        tg_html = email_html
        email_variants = None
        tg_variants = None
    elif not email_html:
        email_html = tg_html
        email_variants = None

    # Resolve Google Docs comment references (<sup><a href="#cmntN">) to real URLs
    if cmnt_url_map:
        email_html = resolve_comment_refs(email_html, cmnt_url_map)
        tg_html = resolve_comment_refs(tg_html, cmnt_url_map)
        if tg_variants:
            for v in tg_variants:
                v['html'] = resolve_comment_refs(v['html'], cmnt_url_map)
        if email_variants:
            for v in email_variants:
                v['html'] = resolve_comment_refs(v['html'], cmnt_url_map)
    elif not tg_html:
        tg_html = email_html

    # Auto-detect segment from planning metadata (the non-email/non-TG part of the document)
    other_text = ' '.join(t.get_text(strip=True) for t in sections['other']).lower()
    if re.search(r'\bнейро\b', other_text):
        segment = 'ai'
    elif re.search(r'\b(техно|технари|тех[/ ]бизнес)\b', other_text):
        segment = 'dev'
    else:
        segment = ''

    # Auto-detect utm_campaign (тег активности) and date (дата отправки) from planning section.
    # Two doc formats:
    #   Format 1 (labeled):   list item = "Тег активности: slug"  /  "Дата отправки: dd.mm"
    #   Format 2 (positional): list items = ["campaign-slug", "dd.mm", "HH:MM"]
    from datetime import datetime as _dt
    def _norm_date(raw):
        raw = re.sub(r'\s*\.\s*', '.', raw.strip())
        dm = re.match(r'^(\d{1,2}\.\d{1,2})(?:\.(\d{2,4}))?', raw)
        if dm:
            y = dm.group(2) or f'{_dt.now().year % 100:02d}'
            if len(y) == 4:
                y = y[2:]
            return f'{dm.group(1)}.{y}'
        return raw

    doc_campaign = ''
    doc_date = ''

    # Gather all list items and paragraphs from sections['other']
    li_items = []  # (text, tag) from ul/ol
    p_items  = []  # text from p/h*
    for t in sections['other']:
        if t.name in ('ul', 'ol'):
            for li in t.find_all('li'):
                li_items.append(get_text_content(li).strip())
        elif t.name == 'li':
            li_items.append(get_text_content(t).strip())
        elif t.name in ('p', 'h1', 'h2', 'h3', 'h4'):
            p_items.append(get_text_content(t).strip())

    all_items = li_items + p_items

    # Pass 1: look for labeled lines (Format 1)
    for item in all_items:
        m = re.match(r'^тег\s+активности\s*[:\s]\s*(.+)', item, re.IGNORECASE)
        if m and not doc_campaign:
            doc_campaign = m.group(1).strip()
        m2 = re.match(r'^дата\s+отправки\s*[:\s]\s*(.+)', item, re.IGNORECASE)
        if m2 and not doc_date:
            doc_date = _norm_date(m2.group(1).strip())

    # Pass 2: if still missing, use positional heuristic (Format 2)
    if not doc_campaign or not doc_date:
        for item in li_items:
            if not item:
                continue
            if re.match(r'^\d{1,2}:\d{2}$', item):  # time like "08:00" — skip
                continue
            if re.match(r'^\d{1,2}\s*\.\s*\d{1,2}', item) and not doc_date:
                doc_date = _norm_date(item)
            elif not doc_campaign and re.match(r'^[a-zA-Zа-яА-ЯёЁ][a-zA-Zа-яА-ЯёЁ0-9\-_]*$', item):
                doc_campaign = item

    return {
        'email_html': email_html,
        'email_variants': email_variants,
        'tg_html': tg_html,
        'tg_variants': tg_variants,
        'subject': subject,
        'preview': preview,
        'links': all_links,
        'footnotes': footnotes,
        'segment': segment,
        'doc_campaign': doc_campaign,
        'doc_date': doc_date,
        'doc_title': doc_title,
        'sender': sender,
    }

# ---------------------------------------------------------------------------
# Email HTML generation
# ---------------------------------------------------------------------------

GC_VAR_RE = re.compile(r'\{[^}]+\}')

def elem_inner_html_for_email(tag, _in_bold=False, link_color='#1445ea'):
    """
    Convert a BS4 tag's contents to email-safe HTML:
    - keep <b>, <strong>, <i>, <em>, <a href>
    - decode Google redirect URLs
    - preserve GC variables
    """
    parts = []
    for child in tag.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag):
            name = child.name
            if name in ('b', 'strong'):
                inner = elem_inner_html_for_email(child, _in_bold=True, link_color=link_color)
                parts.append(f'<b>{inner}</b>')
            elif name in ('i', 'em'):
                inner = elem_inner_html_for_email(child, _in_bold=_in_bold, link_color=link_color)
                parts.append(f'<i>{inner}</i>')
            elif name == 'a' and child.get('href'):
                inner = elem_inner_html_for_email(child, _in_bold=_in_bold, link_color=link_color)
                href = decode_google_redirect(child['href'])
                parts.append(f'<a href="{href}" target="_blank" style="color:{link_color};text-decoration:underline">{inner}</a>')
            elif name == 'sup':
                pass
            elif name == 'span':
                style = child.get('style', '')
                is_bold = ('font-weight:700' in style or 'font-weight: 700' in style
                           or 'font-weight:bold' in style or 'font-weight: bold' in style)
                is_italic = 'font-style:italic' in style or 'font-style: italic' in style
                inner = elem_inner_html_for_email(child, _in_bold=_in_bold or is_bold, link_color=link_color)
                if is_bold and not _in_bold:
                    inner = f'<b>{inner}</b>'
                if is_italic:
                    inner = f'<i>{inner}</i>'
                parts.append(inner)
            elif name == 'br':
                br_style = child.get('style', '').replace(' ', '')
                if 'display:none' not in br_style:
                    parts.append('<br>')
            else:
                inner = elem_inner_html_for_email(child, _in_bold=_in_bold, link_color=link_color)
                parts.append(inner)
    # Strip trailing <br> tags from parts — Google Docs artifacts at element ends
    while parts and parts[-1] == '<br>':
        parts.pop()
    return ''.join(parts)

# Matches {first_name} GC variable (plain text, not inside HTML tags)
_FIRST_NAME_VAR = r'\{first_name\}'

def _strip_first_name(text):
    """Remove {first_name} GC variable with smart punctuation/capitalization cleanup.

    Handles patterns like:
      'Привет, {first_name}!'  → 'Привет!'
      '{first_name}, привет!'  → 'Привет!'
      '{first_name}! Текст'    → 'Текст'
      'Привет {first_name}!'   → 'Привет!'
    """
    # Google Docs uses \xa0 (non-breaking space) as separator — normalize first
    # so regex \s patterns can match around the variable.
    text = text.replace('\xa0', ' ')

    def _cap(s):
        s = s.strip()
        return s[0].upper() + s[1:] if s else s

    # {first_name} at start + optional separator → remove and capitalize what follows
    text, n = re.subn(r'^\s*' + _FIRST_NAME_VAR + r'\s*[,!?.;:\-–—]?\s*', '', text)
    if n:
        text = _cap(text)

    # ", {first_name}" in the middle → remove comma + variable, preserve following punctuation
    # e.g. "Привет, {first_name}. Я Павел" → "Привет. Я Павел"
    text = re.sub(r'\s*,\s*' + _FIRST_NAME_VAR, '', text)
    # " {first_name}," — space before variable, optional separator after
    text = re.sub(r'\s+' + _FIRST_NAME_VAR + r'\s*[,!?.;:\-–—]?', ' ', text)

    # Any remaining bare occurrence
    text = re.sub(_FIRST_NAME_VAR, '', text)

    # Clean up leftover leading punctuation or extra spaces
    text = re.sub(r'^[,\s]+', '', text)
    text = re.sub(r'\s{2,}', ' ', text)

    return text.strip()


def tag_to_email_p(tag, channel_key='email', campaign='', date='', font_size=18, color='#333333', link_color='#1445ea', segment=''):
    """Convert a single <p> or heading tag to an email <p> with styles."""
    # Fast pre-check: if the tag has no visible text (even with spans/nbsp), skip it
    tag_plain = tag.get_text().replace('\xa0', '').replace(' ', '').strip()
    if not tag_plain:
        return None
    # Skip standalone footnote references like [a], [b], [d], [1] — Google Docs link annotations
    if _FOOTNOTE_REF_RE.match(tag_plain):
        return None
    inner = elem_inner_html_for_email(tag, link_color=link_color)
    inner = inner.strip()
    # Strip leading/trailing <br> tags — Google Docs artifacts at paragraph boundaries
    inner = re.sub(r"^(\s*<br\s*/?>\s*)+", "", inner)
    inner = re.sub(r"(\s*<br\s*/?>\s*)+$", "", inner)
    inner = inner.strip()
    # Treat &nbsp;-only and <br>-only paragraphs (Google Docs empty lines) as empty
    if not inner or inner.replace('\xa0', '').replace('&nbsp;', '').replace('<br>', '').replace('<br/>', '').strip() == '':
        return None

    # Normalize GC variables that Google Docs may split across formatting spans:
    # e.g., {<b>first_name</b>} → {first_name}
    inner = re.sub(r'\{(?:<[^>]+>)*([\w]+)(?:<[^>]+>)*\}', r'{\1}', inner)

    # Strip/rename {first_name} GC variable for channels that need it
    if CHANNELS.get(channel_key, {}).get('strip_gc_vars'):
        inner = _strip_first_name(inner)
        # Clean up inline HTML tag left with only punctuation after {first_name} removal:
        # e.g. <b>{first_name}, </b>текст → after strip → <b>, </b>текст → текст
        inner = re.sub(
            r'^(\s*<(?:b|i|em|strong|span|u|s)[^>]*>[,!?.;:\s]*</(?:b|i|em|strong|span|u|s)>)+\s*',
            '', inner
        )
        if inner and inner[0].isalpha() and not inner[0].isupper():
            inner = inner[0].upper() + inner[1:]
    if CHANNELS.get(channel_key, {}).get('rename_first_name'):
        inner = inner.replace('{first_name}', '{firstName}')
    if not inner:
        return None

    # Inject UTM into hrefs
    inner = inject_utm_in_html(inner, channel_key, campaign, date, segment=segment)

    # Preserve text alignment from the original tag
    tag_style = tag.get('style', '')
    align_prefix = ''
    if 'text-align:center' in tag_style or 'text-align: center' in tag_style:
        align_prefix = 'text-align:center;'
    elif 'text-align:right' in tag_style or 'text-align: right' in tag_style:
        align_prefix = 'text-align:right;'

    # Detect if this is a heading (larger font)
    if tag.name in ('h1', 'h2', 'h3'):
        fs = 22 if tag.name == 'h1' else 20
        style = (
            f"{align_prefix}margin:0 0 8px 0;font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;"
            f"line-height:32px;color:{color};font-size:{fs}px;font-weight:bold"
        )
    else:
        style = (
            f"{align_prefix}margin:0 0 10px 0;font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;"
            f"line-height:27px;color:{color};font-size:{font_size}px"
        )
    return f'<p style="{style}">{inner}</p>'

# Matches paragraph whose entire text is [BUTTON TEXT] or EMOJI [BUTTON TEXT].
# Group 1 = optional emoji/non-word prefix, Group 2 = text inside brackets.
_BTN_BRACKET_RE = re.compile(r'^\s*([^\w\[\]]*)\[([^\]]+)\]\s*$', re.DOTALL)

# Matches standalone footnote reference paragraphs like [a], [b], [d], [1], [2]
# These are Google Docs link annotations, not email content
_FOOTNOTE_REF_RE = re.compile(r'^\[[a-z0-9]{1,3}\]$', re.IGNORECASE)

# Trailing footnote refs like [a], [b], [1] appended by Google Docs to button text
_TRAILING_FOOTNOTE_RE = re.compile(r'(\[[a-z0-9]{1,3}\])+\s*$', re.IGNORECASE)

# Inline footnote refs that appear between button text and body text (after <br/>):
# e.g. "[КНОПКА][b]Дополнительный текст" — [b] is a footnote marker, not button content
# Pattern: ']' then immediately '[single-letter/digit]' then more substantial text follows
_INLINE_FOOTNOTE_RE = re.compile(r'(\])\s*(?:\[[a-z0-9]{1,3}\])+\s*(?=\S)', re.IGNORECASE)

def _strip_trailing_footnotes(text):
    """Remove trailing Google Docs footnote markers like [a][b] from text."""
    return _TRAILING_FOOTNOTE_RE.sub('', text).strip()

def _strip_button_footnotes(text):
    """Remove inline Google Docs footnote markers that appear immediately after a closing
    bracket and before additional body text, e.g. '[КНОПКА][b]Текст' → '[КНОПКА]Текст'.
    This handles the Google Docs pattern where a <sup> footnote ref is placed between the
    button span and a descriptive text span (both separated by <br/> in the original).
    Also strips trailing footnotes."""
    # Remove inline footnote refs after ']' before body text
    text = _INLINE_FOOTNOTE_RE.sub(r'\1 ', text)
    # Remove trailing footnote refs
    text = _TRAILING_FOOTNOTE_RE.sub('', text).strip()
    return text

_CHECKMARKS = ['✅', '❌', '☑', '☒']
_FEATURE_EMOJI = ['💎', '🎁', '🎯', '📌', '🔑', '⭐', '🏆', '💡', '🚀', '📅', '🗓', '📢']

def render_block_from_tags(tags, channel_key, campaign, date, segment='', images=None, user_img_idx=None):
    """
    Given a list of BS4 tags from one logical block, build an email table row.
    Detects [BUTTON TEXT] CTAs, checkmark lists, feature emoji, etc.
    Returns (html_string, meta_dict) or (None, None).
    """
    tags = [t for t in tags if not _is_reklama(t)]
    if not tags:
        return None, None

    combined_text = ' '.join(t.get_text(strip=True) for t in tags)
    first_text = tags[0].get_text(strip=True) if tags else ''
    preview_text = combined_text[:80]

    # Build an ordered items list: {'kind': 'tag', 'tag': tag} or {'kind': 'btn', 'text': str, 'url': str}
    items = []

    for tag in tags:
        tag_text = tag.get_text(strip=True)
        # Strip trailing AND inline Google Docs footnote markers before button detection.
        # _strip_button_footnotes also handles the pattern "[КНОПКА][b]Дополнительный текст"
        # where [b] is an inline <sup> footnote ref placed between the button and body text.
        tag_text_clean = _strip_button_footnotes(tag_text)

        # Case -1: paragraph containing only an image (no text)
        if not tag_text_clean and tag.name == 'p':
            img_tag = tag.find('img')
            # Accept both http URLs and base64-encoded images from Google Docs
            img_src = img_tag.get('src', '') if img_tag else ''
            if img_src and (img_src.startswith('http') or img_src.startswith('data:image')):
                items.append({'kind': 'img', 'src': img_src})
                continue

        # Case 0: "Кнопка: TEXT" — handles one or multiple buttons in same tag (via <br>)
        if re.match(r'^кнопка:\s*', tag_text_clean, re.IGNORECASE):
            labels = [s.strip() for s in re.split(r'(?i)\s*кнопка:\s*', tag_text_clean) if s.strip()]
            links = tag.find_all('a', href=True)
            for i, lbl in enumerate(labels):
                a = links[i] if i < len(links) else None
                items.append({'kind': 'btn', 'text': lbl, 'url': a.get('href', '#') if a else '#'})
            continue

        # Case 1: the whole tag is [BUTTON TEXT] or EMOJI [BUTTON TEXT]
        m = _BTN_BRACKET_RE.match(tag_text_clean)
        if m:
            prefix = m.group(1).strip()
            btn_inner = m.group(2).strip()
            btn_label = f'{prefix} {btn_inner}'.strip() if prefix else btn_inner
            a = tag.find('a', href=True)
            items.append({'kind': 'btn', 'text': btn_label, 'url': a.get('href', '#') if a else '#'})
            continue

        # Case 1.5: Google Docs pattern — [BUTTON] span + <br/> + <sup>[footnote]</sup> + <br/> + text span.
        # get_text() collapses these into "[BUTTON][footnote]text", which breaks Case 1.
        # Detect by checking if the FIRST span in the tag is a button, and the tag contains a <br>.
        if tag.find('br') and tag.name == 'p':
            first_span = tag.find('span')
            if first_span:
                first_span_text = _strip_trailing_footnotes(first_span.get_text(strip=True))
                m_fs = _BTN_BRACKET_RE.match(first_span_text)
                if m_fs:
                    fs_prefix = m_fs.group(1).strip()
                    fs_inner = m_fs.group(2).strip()
                    btn_label = f'{fs_prefix} {fs_inner}'.strip() if fs_prefix else fs_inner
                    a = tag.find('a', href=True)
                    items.append({'kind': 'btn', 'text': btn_label, 'url': a.get('href', '#') if a else '#'})
                    # Collect remaining text (after the button span) as a separate tag item
                    tag_copy = BeautifulSoup(str(tag), 'lxml').find(tag.name)
                    if tag_copy:
                        # Remove the first span (button) and any <br> and <sup> siblings
                        for child in list(tag_copy.children):
                            if not hasattr(child, 'name'):
                                continue
                            if child.name in ('br', 'sup'):
                                child.decompose()
                            elif child.name == 'span':
                                span_txt = _strip_trailing_footnotes(child.get_text(strip=True))
                                if _BTN_BRACKET_RE.match(span_txt):
                                    child.decompose()
                                    break  # only remove the button span
                        remaining = tag_copy.get_text(strip=True).replace('\xa0', '').strip()
                        if remaining:
                            items.append({'kind': 'tag', 'tag': tag_copy})
                    continue

        # Case 2: an <a> inside the tag wraps [BUTTON TEXT] (possibly with emoji before <a>)
        found_btn_anchor = False
        for a in tag.find_all('a', href=True):
            m2 = _BTN_BRACKET_RE.match(_strip_trailing_footnotes(a.get_text(strip=True)))
            if m2:
                a_prefix = m2.group(1).strip()
                btn_inner = m2.group(2).strip()
                btn_label = f'{a_prefix} {btn_inner}'.strip() if a_prefix else btn_inner
                # Also pick up any emoji-only text sibling that precedes the <a> in the tag
                if not a_prefix:
                    full_m = _BTN_BRACKET_RE.match(tag_text_clean)
                    if full_m and full_m.group(1).strip():
                        btn_label = f'{full_m.group(1).strip()} {btn_label}'.strip()
                btn_href = a.get('href', '#')
                tag_copy = BeautifulSoup(str(tag), 'lxml').find(tag.name)
                if tag_copy:
                    for ba in tag_copy.find_all('a', href=True):
                        if _BTN_BRACKET_RE.match(_strip_trailing_footnotes(ba.get_text(strip=True))):
                            ba.decompose()
                    remaining = _strip_trailing_footnotes(tag_copy.get_text(strip=True))
                    # Remove the emoji prefix that's now part of the button label
                    if btn_label and remaining and remaining.startswith(m2.group(1).strip()):
                        remaining = remaining[len(m2.group(1).strip()):].strip()
                    if remaining:
                        items.append({'kind': 'tag', 'tag': tag_copy})
                items.append({'kind': 'btn', 'text': btn_label, 'url': btn_href})
                found_btn_anchor = True
                break
        if not found_btn_anchor:
            items.append({'kind': 'tag', 'tag': tag})

    has_btn = any(item['kind'] == 'btn' for item in items)
    has_checkmarks = any(c in combined_text for c in _CHECKMARKS)
    starts_with_feature = any(first_text.startswith(e) for e in _FEATURE_EMOJI)

    if has_btn:
        # CTA block: render items in document order, button at its actual position
        FONT_BASE = "font-family:roboto,'helvetica neue',helvetica,arial,sans-serif"
        BTN_A_STYLE = (
            "background:#E1FB52;color:#000000;padding:12px 50px;border-radius:30px;"
            "text-decoration:none;font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;"
            "font-size:16px;display:inline-block;font-weight:600"
        )

        inner_rows = []
        for item in items:
            if item['kind'] == 'tag':
                tag = item['tag']
                if tag.name in ('ul', 'ol'):
                    li_parts = []
                    for li in tag.find_all('li'):
                        inner = elem_inner_html_for_email(li, link_color='#e1fb52')
                        inner = inject_utm_in_html(inner, channel_key, campaign, date, segment=segment)
                        li_s = (f"margin:0 0 6px 0;padding-left:20px;{FONT_BASE};"
                                "line-height:27px;color:#ffffff;font-size:18px")
                        li_parts.append(f'<p style="{li_s}">• {inner}</p>')
                    if li_parts:
                        inner_rows.append(
                            '<tr><td align="left" bgcolor="#1445ea" style="padding:4px 15px">\n'
                            + '\n'.join(li_parts) + '\n</td></tr>'
                        )
                else:
                    ph = tag_to_email_p(tag, channel_key, campaign, date, color='#ffffff', link_color='#e1fb52', segment=segment)
                    if ph:
                        inner_rows.append(
                            '<tr><td align="left" bgcolor="#1445ea" style="padding:4px 15px">\n'
                            + ph + '\n</td></tr>'
                        )
            elif item['kind'] == 'img':
                img_src = item['src']
                # Google Docs embeds images as base64 data URIs — not suitable for email.
                # Replace with the next user-provided image URL if available.
                if img_src.startswith('data:image'):
                    if images and user_img_idx is not None and user_img_idx[0] < len(images):
                        img_src = images[user_img_idx[0]]
                        user_img_idx[0] += 1
                        # Update the item so meta picks up the resolved URL (not base64)
                        item['src'] = img_src
                    else:
                        continue
                inner_rows.append(
                    '<tr><td align="center" bgcolor="#1445ea" style="padding:8px 10px 4px;font-size:0px">\n'
                    f'<img src="{img_src}" alt="" style="display:block;border:0;max-width:100%;border-radius:8px">\n'
                    '</td></tr>'
                )
            elif item['kind'] == 'btn':
                btn_url_utm = build_utm_url(item['url'], channel_key, campaign, date, segment)
                inner_rows.append(
                    '<tr><td align="center" bgcolor="#1445ea" style="padding:8px 0 12px;margin:0">\n'
                    f'<a href="{btn_url_utm}" target="_blank" style="{BTN_A_STYLE}">{item["text"]}</a>\n'
                    '</td></tr>'
                )

        # Adjust top/bottom padding of first and last rows
        if inner_rows:
            inner_rows[0] = (
                inner_rows[0]
                .replace('style="padding:4px 15px"', 'style="padding:14px 15px 4px"', 1)
                .replace('style="padding:8px 0 12px;margin:0"', 'style="padding:14px 0 12px;margin:0"', 1)
            )
            inner_rows[-1] = (
                inner_rows[-1]
                .replace('style="padding:4px 15px"', 'style="padding:4px 15px 14px"', 1)
                .replace('style="padding:8px 0 12px;margin:0"', 'style="padding:8px 0 18px;margin:0"', 1)
            )

        html = (
            '<tr><td style="padding:5px 10px 10px;margin:0;background-color:#ffffff">\n'
            '<table cellspacing="0" cellpadding="0" width="100%" '
            'style="border-collapse:separate;border-spacing:0;border:10px solid #1445ea;border-radius:20px" '
            'role="presentation">\n'
            + '\n'.join(inner_rows)
            + '\n</table></td></tr>'
        )

        # Meta: collect text content for editing (buttons stored separately)
        text_parts = []
        for item in items:
            if item['kind'] == 'tag':
                ph = tag_to_email_p(item['tag'], channel_key, campaign, date, color='#ffffff', link_color='#e1fb52', segment=segment)
                if ph:
                    text_parts.append(ph)
        paragraphs_html = strip_trailing_empty_paragraphs('\n'.join(text_parts))

        first_btn = next((i for i in items if i['kind'] == 'btn'), None)
        btn_text_meta = first_btn['text'] if first_btn else ''
        btn_url_meta = build_utm_url(first_btn['url'], channel_key, campaign, date, segment) if first_btn else '#'
        all_btns_meta = [
            {'text': i['text'], 'url': build_utm_url(i['url'], channel_key, campaign, date, segment)}
            for i in items if i['kind'] == 'btn'
        ]

        # Store the first embedded image URL for the editor (if any)
        first_img_item = next((i for i in items if i['kind'] == 'img'), None)
        img_url_meta = first_img_item['src'] if first_img_item else ''
        # If it's still base64 at this point (no user image was provided), store empty
        if img_url_meta.startswith('data:image'):
            img_url_meta = ''

        meta = {
            'type': 'block_blue_cta',
            'paragraphs_html': paragraphs_html,
            'btn_text': btn_text_meta,
            'btn_url_utm': btn_url_meta,
            'buttons': all_btns_meta,
            'image_url': img_url_meta,
            'preview_text': preview_text,
        }
        return html, meta

    # Non-CTA blocks: render all tag items as paragraphs
    # Handle standalone image items first (outside CTA context)
    img_items = [i for i in items if i['kind'] == 'img']
    for img_item in img_items:
        img_src = img_item['src']
        if img_src.startswith('data:image'):
            # Replace base64 with next user-provided image URL if available
            if images and user_img_idx is not None and user_img_idx[0] < len(images):
                img_src = images[user_img_idx[0]]
                user_img_idx[0] += 1
            else:
                continue  # no URL supplied — skip
        if img_src.startswith('http'):
            return block_image_center(img_src), {
                'type': 'block_image',
                'image_url': img_src,
                'paragraphs_html': '', 'btn_text': '', 'btn_url_utm': '',
                'preview_text': 'Картинка',
            }

    p_parts = []
    for item in items:
        if item['kind'] != 'tag':
            continue
        tag = item['tag']
        if tag.name in ('ul', 'ol'):
            for li in tag.find_all('li'):
                inner = elem_inner_html_for_email(li)
                inner = inject_utm_in_html(inner, channel_key, campaign, date, segment=segment)
                s = ("margin:0 0 6px 0;padding-left:20px;font-family:roboto,'helvetica neue',"
                     "helvetica,arial,sans-serif;line-height:27px;color:#333333;font-size:18px")
                p_parts.append(f'<p style="{s}">• {inner}</p>')
        else:
            ph = tag_to_email_p(tag, channel_key, campaign, date, segment=segment)
            if ph:
                p_parts.append(ph)

    paragraphs_html = strip_trailing_empty_paragraphs('\n'.join(p_parts))

    # If all tags turned out to be empty (e.g. &nbsp;-only paragraphs), skip this block entirely
    if not paragraphs_html.strip():
        return None, None

    if has_checkmarks:
        meta = {'type': 'block_grey', 'paragraphs_html': paragraphs_html, 'btn_text': '', 'btn_url_utm': '', 'preview_text': preview_text}
        return block_grey(paragraphs_html), meta
    elif starts_with_feature:
        meta = {'type': 'block_dotted', 'paragraphs_html': paragraphs_html, 'btn_text': '', 'btn_url_utm': '', 'preview_text': preview_text}
        return block_dotted(paragraphs_html), meta
    else:
        meta = {'type': 'block_white', 'paragraphs_html': paragraphs_html, 'btn_text': '', 'btn_url_utm': '', 'preview_text': preview_text}
        return block_white(paragraphs_html), meta

def _is_reklama(tag):
    t = tag.get_text(strip=True)
    return _REKLAMA_RE.search(t) or 'ИНН 9715401631' in t

def _cell_para_html(cell, channel_key, campaign, date, font_size=18, segment=''):
    """Extract email-paragraph HTML from a table cell (for 2-col detection).
    Returns (text_html, buttons) where buttons = list of (btn_text, btn_url).
    Paragraphs matching [TEXT] with a hyperlink are extracted as buttons.
    """
    ctags = [t for t in cell.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'ul', 'ol'])
             if t.get_text(strip=True) and not _is_reklama(t)]
    parts = []
    buttons = []
    for tag in ctags:
        text = tag.get_text(strip=True)
        text_clean = _strip_trailing_footnotes(text)
        # Detect [BUTTON TEXT] or EMOJI [BUTTON TEXT] hyperlink pattern
        m_btn = _BTN_BRACKET_RE.match(text_clean)
        if m_btn:
            link = tag.find('a', href=True)
            if link:
                prefix = m_btn.group(1).strip()
                btn_inner = m_btn.group(2).strip()
                btn_text = f'{prefix} {btn_inner}'.strip() if prefix else btn_inner
                raw_url = decode_google_redirect(link.get('href', '#'))
                btn_url = build_utm_url(raw_url, channel_key, campaign, date, segment)
                buttons.append((btn_text, btn_url))
                continue
        # Detect "Кнопка: TEXT" prefix format (possibly multiple via <br> in one tag)
        if re.match(r'^кнопка:\s*', text_clean, re.IGNORECASE):
            labels = [s.strip() for s in re.split(r'(?i)\s*кнопка:\s*', text) if s.strip()]
            links = tag.find_all('a', href=True)
            for i, lbl in enumerate(labels):
                a = links[i] if i < len(links) else None
                if not a:
                    continue
                raw_url = decode_google_redirect(a.get('href', '#'))
                btn_url = build_utm_url(raw_url, channel_key, campaign, date, segment)
                buttons.append((lbl, btn_url))
            continue
        if tag.name in ('ul', 'ol'):
            for li in tag.find_all('li'):
                inner = elem_inner_html_for_email(li)
                inner = re.sub(r'\{(?:<[^>]+>)*([\w]+)(?:<[^>]+>)*\}', r'{\1}', inner)
                inner = inject_utm_in_html(inner, channel_key, campaign, date, segment=segment)
                if CHANNELS.get(channel_key, {}).get('strip_gc_vars'):
                    inner = _strip_first_name(inner)
                if CHANNELS.get(channel_key, {}).get('rename_first_name'):
                    inner = inner.replace('{first_name}', '{firstName}')
                lh = round(font_size * 1.5)
                s = (f"margin:0 0 6px 0;padding-left:20px;"
                     f"font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;"
                     f"line-height:{lh}px;color:#333333;font-size:{font_size}px")
                parts.append(f'<p style="{s}">• {inner}</p>')
        else:
            ph = tag_to_email_p(tag, channel_key, campaign, date, font_size=font_size, segment=segment)
            if ph:
                parts.append(ph)
    return '\n'.join(parts), buttons

def generate_email_html(email_section_html, channel_key, campaign, date, images, subject='', segment=''):
    """
    Build the complete email HTML from parsed section HTML.
    Each top-level <table> in the source maps to one distinct block.
    Standalone <p>/<h*>/<ul>/<ol> tags are grouped between tables.
    """
    soup = BeautifulSoup(email_section_html, 'lxml')
    body = soup.find('body') or soup

    raw_blocks = []  # list of (row_html, meta_dict)
    pending_tags = []
    user_img_idx = [0]  # tracks which user-provided image URL to use next

    # Normalize subject for deduplication — skip if first paragraph repeats it
    _subject_norm = re.sub(r'\s+', ' ', subject or '').strip()
    _first_content_checked = [False]

    def flush_pending():
        if not pending_tags:
            return
        row, meta = render_block_from_tags(list(pending_tags), channel_key, campaign, date, segment, images=images, user_img_idx=user_img_idx)
        if row:
            raw_blocks.append((row, meta))
        pending_tags.clear()

    def process_element(el):
        if not hasattr(el, 'name') or el.name is None:
            return
        # Skip first paragraph if it duplicates the subject line (common in GC docs)
        if not _first_content_checked[0] and _subject_norm and el.name not in ('div',):
            el_txt = re.sub(r'\s+', ' ', el.get_text(strip=True).replace('\xa0', '')).strip()
            if el_txt:
                _first_content_checked[0] = True
                if el_txt == _subject_norm:
                    return  # duplicated subject — skip this element
        if el.name == 'table':
            # Detect 2-column table structure first (before any flush)
            tbody = el.find('tbody') or el
            trows = tbody.find_all('tr', recursive=False)
            two_col_cells = None
            for trow in trows:
                rc = trow.find_all(['td', 'th'], recursive=False)
                if len(rc) == 2:
                    two_col_cells = rc
                    break

            if not two_col_cells:
                # Normal table: check for CTA button before deciding whether to flush
                # Include <p> tags that contain an <img> even when they have no text content.
                def _keep_tag(t):
                    if _is_reklama(t):
                        return False
                    if t.get_text(strip=True):
                        return True
                    # Empty-text <p> that wraps an image (Google Docs embeds images this way)
                    return t.name == 'p' and bool(t.find('img'))
                inner = [t for t in el.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'ul', 'ol'])
                         if _keep_tag(t)]
                has_inner_btn = bool(inner and any(
                    _BTN_BRACKET_RE.match(_strip_trailing_footnotes(t.get_text(strip=True))) for t in inner))
                has_inner_text = bool(inner and any(
                    not _BTN_BRACKET_RE.match(_strip_trailing_footnotes(t.get_text(strip=True))) for t in inner))

                if has_inner_btn and pending_tags and not has_inner_text:
                    # Table has ONLY buttons (no body text of its own) —
                    # merge pre-table paragraphs into the CTA block
                    combined = list(pending_tags) + inner
                    pending_tags.clear()
                    row, meta = render_block_from_tags(combined, channel_key, campaign, date, segment, images=images, user_img_idx=user_img_idx)
                    if row:
                        raw_blocks.append((row, meta))
                else:
                    # Table has its own body text, or no button — flush pending separately
                    flush_pending()
                    if inner:
                        row, meta = render_block_from_tags(inner, channel_key, campaign, date, segment, images=images, user_img_idx=user_img_idx)
                        if row:
                            raw_blocks.append((row, meta))
                return

            # 2-column table: flush pending first, then process
            flush_pending()
            left_html,  left_btns  = _cell_para_html(two_col_cells[0], channel_key, campaign, date, font_size=16, segment=segment)
            right_html, right_btns = _cell_para_html(two_col_cells[1], channel_key, campaign, date, font_size=16, segment=segment)
            l_img = two_col_cells[0].find('img')
            r_img = two_col_cells[1].find('img')
            l_src = l_img.get('src', '') if l_img else ''
            r_src = r_img.get('src', '') if r_img else ''

            row = None
            meta = None
            if left_html.strip() and right_html.strip():
                pv = BeautifulSoup(left_html, 'lxml').get_text(strip=True)[:50]
                meta = {'type': 'block_2col_text_text', 'paragraphs_html': left_html,
                        'col2_html': right_html, 'btn_text': '', 'btn_url_utm': '', 'preview_text': pv}
                row = block_2col_text_text(left_html, right_html)
            elif l_src and not left_html.strip() and right_html.strip():
                img_to_use = images[user_img_idx[0]] if user_img_idx[0] < len(images) else l_src
                if user_img_idx[0] < len(images):
                    user_img_idx[0] += 1
                pv = BeautifulSoup(right_html, 'lxml').get_text(strip=True)[:50]
                meta = {'type': 'block_2col_img_text', 'paragraphs_html': right_html,
                        'image_url': img_to_use, 'btn_text': '', 'btn_url_utm': '', 'preview_text': pv}
                row = block_2col_img_text(img_to_use, right_html)
            elif r_src and not right_html.strip() and left_html.strip():
                img_to_use = images[user_img_idx[0]] if user_img_idx[0] < len(images) else r_src
                if user_img_idx[0] < len(images):
                    user_img_idx[0] += 1
                pv = BeautifulSoup(left_html, 'lxml').get_text(strip=True)[:50]
                meta = {'type': 'block_2col_text_img', 'paragraphs_html': left_html,
                        'image_url': img_to_use, 'btn_text': '', 'btn_url_utm': '', 'preview_text': pv}
                row = block_2col_text_img(left_html, img_to_use)

            if row:
                raw_blocks.append((row, meta))
                for btn_text, btn_url in left_btns + right_btns:
                    raw_blocks.append((
                        block_button(btn_url, btn_text),
                        {'type': 'block_button', 'paragraphs_html': '',
                         'btn_text': btn_text, 'btn_url_utm': btn_url,
                         'preview_text': btn_text[:50]}
                    ))
                return

            # 2-col detected but no pattern matched — fall back to normal table rendering
            inner = [t for t in el.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'ul', 'ol'])
                     if (t.get_text(strip=True) or (t.name == 'p' and t.find('img'))) and not _is_reklama(t)]
            if inner:
                row, meta = render_block_from_tags(inner, channel_key, campaign, date, segment, images=images, user_img_idx=user_img_idx)
                if row:
                    raw_blocks.append((row, meta))
        elif el.name in ('h1', 'h2', 'h3', 'h4', 'ul', 'ol'):
            if el.get_text(strip=True) and not _is_reklama(el):
                pending_tags.append(el)
        elif el.name == 'p':
            # Normalize non-breaking spaces — Google Docs empty paragraphs often
            # contain only &nbsp; which get_text sees as non-empty text.
            raw_txt = el.get_text(strip=True).replace('\xa0', '').strip()
            if _is_reklama(el):
                return
            # Treat standalone footnote references [a], [b], [d], [1] as block separators
            if raw_txt and _FOOTNOTE_REF_RE.match(raw_txt):
                flush_pending()
                return
            # "Кнопка: TEXT" — each becomes its own separate button block
            if raw_txt and re.match(r'^кнопка:\s*', raw_txt, re.IGNORECASE):
                flush_pending()
                labels = [s.strip() for s in re.split(r'(?i)\s*кнопка:\s*', raw_txt) if s.strip()]
                links = el.find_all('a', href=True)
                for i, lbl in enumerate(labels):
                    a = links[i] if i < len(links) else None
                    raw_href = decode_google_redirect(a.get('href', '#')) if a else '#'
                    btn_url = build_utm_url(raw_href, channel_key, campaign, date, segment)
                    raw_blocks.append((
                        block_button(btn_url, lbl),
                        {'type': 'block_button', 'paragraphs_html': '',
                         'btn_text': lbl, 'btn_url_utm': btn_url, 'preview_text': lbl[:50]}
                    ))
                return
            if raw_txt:
                pending_tags.append(el)
            else:
                # Empty paragraph: check if it contains an image (Google Docs exports
                # images as <p><img src="..."/></p> with no text content, or as base64)
                img_tag = el.find('img')
                img_src = img_tag.get('src', '') if img_tag else ''
                if img_src and (img_src.startswith('http') or img_src.startswith('data:image')):
                    pending_tags.append(el)
                else:
                    flush_pending()
        elif el.name == 'div':
            for sub in el.children:
                process_element(sub)

    for child in body.children:
        process_element(child)
    flush_pending()

    # Auto-alternate simple blocks in a 3-step cycle: white → grey → dotted → white → …
    # Non-alternatable blocks influence cycle position:
    #   CTA (blue)  → resets cycle to 0 (next plain block starts at white)
    #   2-col/image → count as white, so next plain block becomes grey (pos=1)
    #   button      → transparent, doesn't affect the cycle
    ALTERNATABLE = {'block_white', 'block_grey', 'block_dotted'}
    _CYCLE = ['block_white', 'block_grey', 'block_dotted']
    _CYCLE_FN = {'block_white': block_white, 'block_grey': block_grey, 'block_dotted': block_dotted}
    cycle_pos = 0
    for i in range(len(raw_blocks)):
        curr_html, curr_meta = raw_blocks[i]
        if not curr_meta:
            continue
        curr_type = curr_meta.get('type', '')
        if curr_type in ALTERNATABLE:
            new_type = _CYCLE[cycle_pos % 3]
            cycle_pos += 1
            if new_type != curr_type:
                ph = curr_meta.get('paragraphs_html', '')
                curr_meta['type'] = new_type
                raw_blocks[i] = (_CYCLE_FN[new_type](ph), curr_meta)
        elif curr_type == 'block_blue_cta':
            cycle_pos = 0  # next plain block starts fresh at white
        elif curr_type.startswith('block_2col') or curr_type == 'block_image':
            cycle_pos = 1  # 2-col counts as white → next plain block is grey
        # block_button is transparent — doesn't affect the cycle

    # Handle adjacent block_blue_cta: convert the second into block_white + block_button
    final_blocks = []
    i = 0
    while i < len(raw_blocks):
        row_html, meta = raw_blocks[i]
        if (meta and meta.get('type') == 'block_blue_cta' and
                i + 1 < len(raw_blocks) and
                raw_blocks[i + 1][1] and raw_blocks[i + 1][1].get('type') == 'block_blue_cta'):
            final_blocks.append((row_html, meta))
            next_meta = raw_blocks[i + 1][1]
            ph = next_meta.get('paragraphs_html', '')
            btn_text = next_meta.get('btn_text', '')
            btn_url = next_meta.get('btn_url_utm', '#')
            if ph:
                dark_ph = ph.replace('color:#ffffff', 'color:#333333').replace('color:#e1fb52', 'color:#1445ea')
                final_blocks.append((block_white(dark_ph), {**next_meta, 'type': 'block_white', 'paragraphs_html': dark_ph}))
            final_blocks.append((
                block_button(btn_url, btn_text),
                {'type': 'block_button', 'paragraphs_html': '',
                 'btn_text': btn_text, 'btn_url_utm': btn_url, 'preview_text': btn_text[:50]}
            ))
            i += 2
        else:
            final_blocks.append((row_html, meta))
            i += 1
    raw_blocks = final_blocks

    # Log block structure for debugging
    block_summary = [(m.get('type','?'), m.get('btn_text','')[:20], m.get('preview_text','')[:40])
                     for _, m in raw_blocks if m]
    logging.debug(f"email blocks [{channel_key}]: {block_summary}")


    # Add default spacer before footer
    spacer_meta = {'type': 'block_spacer', 'height': 20, 'paragraphs_html': '', 'btn_text': '', 'btn_url_utm': '', 'preview_text': 'Отступ'}
    raw_blocks.append((block_spacer(20), spacer_meta))

    # Build final content rows; remaining user images (not used in 2-col blocks) go at the end
    content_rows = []
    blocks_data = []

    for row_html, meta in raw_blocks:
        content_rows.append(row_html)
        blocks_data.append(meta)

    for leftover_img in images[user_img_idx[0]:]:
        content_rows.append(block_image_center(leftover_img))
        blocks_data.append({'type': 'block_image', 'image_url': leftover_img,
                            'paragraphs_html': '', 'btn_text': '', 'btn_url_utm': '',
                            'preview_text': 'Картинка'})

    content_table = (
        '<table cellpadding="0" cellspacing="0" align="center" class="es-content-body" '
        'role="none" style="border-collapse:collapse;border-spacing:0;width:600px;background-color:#ffffff">\n'
        + '\n'.join(content_rows)
        + '\n</table>'
    )

    logo_header = EMAIL_HEADER.replace('{logo_url}', LOGO_URL)
    subject_safe = subject or 'Рассылка ZeroCoder'

    html = (
        EMAIL_WRAPPER_START.replace('{subject}', subject_safe)
        + logo_header
        + content_table
        + EMAIL_FOOTER
        + EMAIL_WRAPPER_END
    )
    return html, blocks_data

# ---------------------------------------------------------------------------
# TG HTML generation
# ---------------------------------------------------------------------------

def clean_tag_for_tg(tag, _in_bold=False):
    """
    Convert a BS4 tag to TG-compatible HTML:
    Only keep <b>, <i>, <a href="...">, <code>, line breaks.
    """
    parts = []
    for child in tag.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag):
            name = child.name
            if name in ('b', 'strong'):
                inner = clean_tag_for_tg(child, _in_bold=True)
                parts.append(f'<b>{inner}</b>')
            elif name in ('i', 'em'):
                inner = clean_tag_for_tg(child, _in_bold=_in_bold)
                parts.append(f'<i>{inner}</i>')
            elif name == 'u':
                inner = clean_tag_for_tg(child, _in_bold=_in_bold)
                parts.append(f'<u>{inner}</u>')
            elif name == 's':
                inner = clean_tag_for_tg(child, _in_bold=_in_bold)
                parts.append(f'<s>{inner}</s>')
            elif name == 'code':
                inner = clean_tag_for_tg(child, _in_bold=_in_bold)
                parts.append(f'<code>{inner}</code>')
            elif name == 'a' and child.get('href'):
                inner = clean_tag_for_tg(child, _in_bold=_in_bold)
                href = decode_google_redirect(child['href'])
                parts.append(f'<a href="{href}">{inner}</a>')
            elif name == 'sup':
                pass
            elif name == 'span':
                style = child.get('style', '')
                is_bold = ('font-weight:700' in style or 'font-weight: 700' in style
                           or 'font-weight:bold' in style or 'font-weight: bold' in style)
                is_italic = 'font-style:italic' in style or 'font-style: italic' in style
                inner = clean_tag_for_tg(child, _in_bold=_in_bold or is_bold)
                if is_bold and not _in_bold:
                    inner = f'<b>{inner}</b>'
                if is_italic:
                    inner = f'<i>{inner}</i>'
                parts.append(inner)
            elif name == 'br':
                parts.append('\n')
            else:
                inner = clean_tag_for_tg(child, _in_bold=_in_bold)
                parts.append(inner)
    return ''.join(parts)

_REKLAMA_RE        = re.compile(r'реклама\s+ооо', re.IGNORECASE)
_SINGLE_PUNCT_BOLD = re.compile(r'\*\*([!?.,;:])\*\*')
_ANY_BOLD_RE       = re.compile(r'\*\*([^*\n]+?)\*\*')
# Detect emoji immediately before a ** bold marker (Neurocat can't render bold after emoji)
_EMOJI_BEFORE_DSTAR_RE = re.compile(
    r'[☀-➿\U0001F300-\U0001F9FF\U0001FA00-\U0001FAFF\U00002702-\U000027B0] *\*\*'
)


def _postprocess_md(text):
    """Fix common Markdown bold artefacts produced from Google Docs spans."""
    # 1. Merge adjacent bold: **A** **B** → **A B**
    #    Collapse closing-then-opening ** separated only by whitespace (incl. \xa0).
    text = re.sub(r'\*\*[ \t\xa0]*\*\*', ' ', text)
    # 2. Drop bold around single punctuation: **!** → !
    text = _SINGLE_PUNCT_BOLD.sub(r'\1', text)
    # 3. Move leading emoji/symbol outside bold: **🔥text** → 🔥**text**
    def _fix_emoji(m):
        content = m.group(1)
        prefix, rest = '', content
        while rest and not (rest[0].isalpha() or rest[0].isdigit() or rest[0] in '{_'):
            prefix += rest[0]
            rest = rest[1:]
        return f'{prefix}**{rest}**' if (prefix and rest) else m.group(0)
    text = _ANY_BOLD_RE.sub(_fix_emoji, text)
    return text


def _md_wrap(marker, inner):
    """Wrap inner text in Markdown markers, keeping spaces outside the markers."""
    if not inner or not inner.strip():
        return inner
    stripped = inner.strip()
    leading  = inner[:len(inner) - len(inner.lstrip())]
    trailing = inner[len(inner.rstrip()):]
    return f'{leading}{marker}{stripped}{marker}{trailing}'


def clean_tag_for_tg_markdown(tag, links_collector, _in_bold=False):
    """
    Convert a BS4 tag to Markdown: **bold**, *italic*.
    Link URLs are appended to links_collector; link text is kept as plain text.
    """
    parts = []
    for child in tag.children:
        if isinstance(child, NavigableString):
            parts.append(str(child))
        elif isinstance(child, Tag):
            name = child.name
            if name in ('b', 'strong'):
                inner = clean_tag_for_tg_markdown(child, links_collector, _in_bold=True)
                parts.append(_md_wrap('**', inner) if not _in_bold else inner)
            elif name in ('i', 'em'):
                inner = clean_tag_for_tg_markdown(child, links_collector, _in_bold=_in_bold)
                parts.append(_md_wrap('*', inner))
            elif name == 'a' and child.get('href'):
                href = decode_google_redirect(child['href'])
                if not href.startswith('#'):
                    links_collector.append(href)
                parts.append(child.get_text())
            elif name == 'sup':
                pass
            elif name == 'span':
                style    = child.get('style', '')
                is_bold  = ('font-weight:700' in style or 'font-weight: 700' in style
                            or 'font-weight:bold' in style or 'font-weight: bold' in style)
                is_italic = 'font-style:italic' in style or 'font-style: italic' in style
                inner = clean_tag_for_tg_markdown(child, links_collector, _in_bold=_in_bold or is_bold)
                if is_bold and not _in_bold:
                    inner = _md_wrap('**', inner)
                if is_italic:
                    inner = _md_wrap('*', inner)
                parts.append(inner)
            elif name == 'br':
                parts.append('\n')
            else:
                parts.append(clean_tag_for_tg_markdown(child, links_collector, _in_bold=_in_bold))
    return ''.join(parts)


def generate_tg_markdown(tg_section_html, channel_key, campaign, date, segment=''):
    """
    Generate Markdown text for Neurocat. Returns (text, utm_links).
    Bold → **text**, italic → *text*, links → collected separately with UTM.
    """
    soup = BeautifulSoup(tg_section_html, 'lxml')
    body = soup.find('body') or soup

    result_parts = []
    raw_links = []

    for tag in body.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'ul', 'ol']):
        if tag.name in ('ul', 'ol'):
            lines = []
            for li in tag.find_all('li'):
                inner = clean_tag_for_tg_markdown(li, raw_links).strip()
                if inner and CHANNELS.get(channel_key, {}).get('strip_gc_vars'):
                    inner = _strip_first_name(inner)
                if inner:
                    lines.append(f'• {inner}')
            if lines:
                result_parts.append('\n'.join(lines))
            continue

        raw_text = tag.get_text(strip=True)
        if _REKLAMA_RE.search(raw_text) or 'ИНН 9715401631' in raw_text:
            continue
        if re.match(r'^\[([a-zA-Z0-9])\]', raw_text):
            continue

        # "Кнопка:" — each link as its own paragraph, no bold markers
        if re.match(r'^кнопка:\s*', raw_text, re.IGNORECASE):
            for a in tag.find_all('a', href=True):
                href = decode_google_redirect(a['href'])
                if not href.startswith('#'):
                    raw_links.append(href)
                link_text = a.get_text(strip=True)
                if link_text:
                    result_parts.append(link_text)
            continue

        inner = _postprocess_md(clean_tag_for_tg_markdown(tag, raw_links).strip())
        if not inner:
            continue
        inner = re.sub(r'\*{0,2}\s*ссылка:\s*\*{0,2}\s*', '', inner, flags=re.IGNORECASE).strip()
        if not inner:
            continue

        # Normalize spaces inside { first_name } before stripping
        inner = re.sub(r'\{\s*first_name\s*\}', '{first_name}', inner)
        if CHANNELS.get(channel_key, {}).get('strip_gc_vars'):
            # ", **{first_name}**" — variable is bold, comma is outside the bold markers
            # e.g. "Привет, **{first_name}**. Я Павел" → "Привет. Я Павел"
            inner = re.sub(r'\s*,\s*\*\*\{first_name\}\*\*', '', inner)
            # "**{first_name}[punct] " — variable at start of bold block
            inner = re.sub(r'\*\*\s*\{first_name\}[,!?.;:\-–—]?\s*', '**', inner)
            inner = _strip_first_name(inner)
            inner = re.sub(r'\*{4,}', '', inner).strip()  # clean up empty **..** remnants
        if not inner:
            continue

        if tag.name in ('h1', 'h2', 'h3', 'h4'):
            inner = f'**{inner}**'

        # When a paragraph starts with emoji then **, move the emoji inside the bold markers
        # so ** is at position 0. Neurocat renders **🔥 text** correctly but not 🔥 **text**.
        if _EMOJI_BEFORE_DSTAR_RE.search(inner):
            inner = re.sub(
                r'^([☀-➿\U0001F300-\U0001F9FF\U0001FA00-\U0001FAFF\U00002702-\U000027B0]+\s*)\*\*',
                r'**\1',
                inner
            )

        result_parts.append(inner)

    text = '\n\n'.join(result_parts)
    # Normalize { first_name } (with spaces/newlines from Google Docs multiline export),
    # then strip per-paragraph to avoid eating \n\n separators with \s+ in _strip_first_name.
    if CHANNELS.get(channel_key, {}).get('strip_gc_vars'):
        paragraphs = text.split('\n\n')
        cleaned = []
        for para in paragraphs:
            para = re.sub(r'\{\s*first_name\s*\}', '{first_name}', para)
            para = re.sub(r'\s*,\s*\*\*\{first_name\}\*\*', '', para)
            para = re.sub(r'\*\*\s*\{first_name\}[,!?.;:\-–—]?\s*', '**', para)
            para = _strip_first_name(para).strip()
            para = re.sub(r'\*{4,}', '', para).strip()
            if para:
                cleaned.append(para)
        text = '\n\n'.join(cleaned)
    text += '\n\nРЕКЛАМА ООО "ЗЕРОКОДЕР"\nИНН 9715401631'

    # De-duplicate links, apply UTM
    seen = set()
    utm_links = []
    for url in raw_links:
        if url not in seen:
            seen.add(url)
            utm_links.append(build_utm_url(url, channel_key, campaign, date, segment))

    return text, utm_links

def generate_tg_html(tg_section_html, channel_key, campaign, date, segment=''):
    """
    Generate <p><b>...</b></p> style HTML for GC mailings / Max.
    """
    soup = BeautifulSoup(tg_section_html, 'lxml')
    body = soup.find('body') or soup

    result_parts = []
    for tag in body.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'ul', 'ol']):
        if tag.name in ('ul', 'ol'):
            # Group list items together — no spacer between bullets
            items = []
            for li in tag.find_all('li'):
                inner = clean_tag_for_tg(li).strip()
                if inner:
                    items.append(f'<p>• {inner}</p>')
            if items:
                result_parts.append('\n'.join(items))
            continue

        raw_text = tag.get_text(strip=True)
        if _REKLAMA_RE.search(raw_text) or 'ИНН 9715401631' in raw_text:
            continue
        if re.match(r'^\[([a-zA-Z0-9])\]', raw_text):
            continue

        # "Кнопка:" — each link becomes its own separate paragraph
        if re.match(r'^кнопка:\s*', raw_text, re.IGNORECASE):
            for a in tag.find_all('a', href=True):
                href = decode_google_redirect(a['href'])
                link_inner = clean_tag_for_tg(a).strip()
                if link_inner and not href.startswith('#'):
                    result_parts.append(f'<p><a href="{href}">{link_inner}</a></p>')
            continue

        inner = clean_tag_for_tg(tag).strip()
        if not inner:
            continue  # empty paragraphs skipped — spacers added uniformly below
        # Also skip paragraphs that are HTML-only with no visible text (e.g. <b></b>, <b>&nbsp;</b>)
        inner_text = re.sub(r'<[^>]+>', '', inner).replace('&nbsp;', '').replace('\xa0', '').strip()
        if not inner_text:
            continue
        inner = re.sub(r'(?:<[^>]+>)*\s*ссылка:\s*(?:<\/[^>]+>)*\s*', '', inner, flags=re.IGNORECASE).strip()
        if not inner:
            continue

        if tag.name in ('h1', 'h2', 'h3', 'h4'):
            inner = f'<b>{inner}</b>'

        # Split at soft returns (\n in text nodes) — Google Docs Shift+Enter puts multiple
        # logical paragraphs into one <p>; each becomes a separate TG paragraph.
        if '\n' in inner:
            sub_parts = [p.strip() for p in inner.split('\n') if p.strip()]
            for part in sub_parts:
                result_parts.append(f'<p>{part}</p>')
        else:
            result_parts.append(f'<p>{inner}</p>')

    # Always insert a blank-line spacer between every content block for TG readability.
    # (Empty paragraphs from Google Docs are filtered in tags_to_html, so we can't rely
    # on them being present — instead we add spacers unconditionally here.)
    output = '\n<p>&nbsp;</p>\n'.join(result_parts)
    output = inject_utm_in_html(output, channel_key, campaign, date, segment=segment)
    # Normalize GC variables split across bold/italic spans: {<b>first_name</b>} → {first_name}
    output = re.sub(r'\{(?:<[^>]+>)*([\w]+)(?:<[^>]+>)*\}', r'{\1}', output)
    if CHANNELS.get(channel_key, {}).get('strip_gc_vars'):
        output = _strip_first_name(output)
    if CHANNELS.get(channel_key, {}).get('rename_first_name'):
        output = output.replace('{first_name}', '{firstName}')

    # Remove empty formatting tags: <b></b>, <b>\n</b> etc.
    output = re.sub(r'<(b|i|u|s)>\s*</\1>', '', output, flags=re.IGNORECASE)
    # Remove stray closing tags right after <p> opening: <p></b>text → <p>text
    output = re.sub(r'(<p>)(\s*(?:</b>|</i>|</u>|</s>))+', r'\1', output, flags=re.IGNORECASE)
    # Remove truly empty paragraphs (no visible text; &nbsp; spacers are preserved)
    output = re.sub(r'<p>(?:\s|</?b>|</?i>|</?u>|</?s>)*</p>\s*', '', output, flags=re.IGNORECASE)
    # Collapse multiple consecutive &nbsp; spacers into one
    output = re.sub(r'(<p>&nbsp;</p>\s*){2,}', '<p>&nbsp;</p>\n', output)
    # Strip leading spacers before first real paragraph
    output = re.sub(r'^(\s*<p>&nbsp;</p>\s*)+', '', output)

    # Legal notice
    output += '\n<p>&nbsp;</p>\n<p>РЕКЛАМА ООО &quot;ЗЕРОКОДЕР&quot;</p>\n<p>ИНН 9715401631</p>'
    return output

def generate_tg_bots(tg_section_html, channel_key, campaign, date, segment=''):
    """
    Generate simplified text (no <p> wrappers) for TG Bots channel.
    Supports <b>, <i>, <a href>.
    """
    soup = BeautifulSoup(tg_section_html, 'lxml')
    body = soup.find('body') or soup

    result_parts = []
    for tag in body.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'ul', 'ol']):
        if tag.name in ('ul', 'ol'):
            lines = []
            for li in tag.find_all('li'):
                inner = clean_tag_for_tg(li).strip()
                if inner:
                    lines.append(f'• {inner}')
            if lines:
                result_parts.append('\n'.join(lines))
            continue

        raw_text = tag.get_text(strip=True)
        if _REKLAMA_RE.search(raw_text) or 'ИНН 9715401631' in raw_text:
            continue
        if re.match(r'^\[([a-zA-Z0-9])\]', raw_text):
            continue

        # "Кнопка:" — each link becomes its own separate entry
        if re.match(r'^кнопка:\s*', raw_text, re.IGNORECASE):
            for a in tag.find_all('a', href=True):
                href = decode_google_redirect(a['href'])
                link_inner = clean_tag_for_tg(a).strip()
                if link_inner and not href.startswith('#'):
                    result_parts.append(f'<a href="{href}">{link_inner}</a>')
            continue

        inner = clean_tag_for_tg(tag).strip()
        if not inner:
            continue
        inner = re.sub(r'(?:<[^>]+>)*\s*ссылка:\s*(?:<\/[^>]+>)*\s*', '', inner, flags=re.IGNORECASE).strip()
        if not inner:
            continue

        if tag.name in ('h1', 'h2', 'h3', 'h4'):
            inner = f'<b>{inner}</b>'

        result_parts.append(inner)

    output = '\n\n'.join(result_parts)
    output = inject_utm_in_html(output, channel_key, campaign, date, segment=segment)
    # Normalize GC variables split across bold/italic spans: {<b>first_name</b>} → {first_name}
    output = re.sub(r'\{(?:<[^>]+>)*([\w]+)(?:<[^>]+>)*\}', r'{\1}', output)
    if CHANNELS.get(channel_key, {}).get('rename_first_name'):
        output = output.replace('{first_name}', '{firstName}')
    # Remove empty formatting tags: <b></b>, <b>\n</b> etc.
    output = re.sub(r'<(b|i|u|s)>\s*</\1>', '', output, flags=re.IGNORECASE)
    # Remove stray closing tags at line start
    output = re.sub(r'^(\s*)(?:</b>|</i>|</u>|</s>)+', r'\1', output, flags=re.IGNORECASE | re.MULTILINE)
    output += '\n\nРЕКЛАМА ООО "ЗЕРОКОДЕР"\nИНН 9715401631'
    return output

def _pick_tg_src(ch_info, tg_variants_list, tg_section_html, email_section_html, email_variants=None):
    """Return the correct TG HTML source for a channel based on tg_variant_index.

    Priority:
    1. TG variants list (keyword-parsed or AI-built): pick by index.
       If index exceeds available variants AND email_variants has a matching slot
       (e.g. tg_voronki wants index=1 but only one TG variant exists, while
        email_variants[1] is the "другие источники" email) — use email_variants[1].
    2. Single TG section: use it for all TG channels.
    3. No TG section at all: TG-GC channels (index=0) get email_gc text;
       voronki/bots channels (index>0) get the matching email variant if present.
    """
    variant_idx = ch_info.get('tg_variant_index')

    # --- Path 1: TG variants available ---
    if tg_variants_list and isinstance(tg_variants_list, list):
        if variant_idx is not None and variant_idx < len(tg_variants_list):
            return tg_variants_list[variant_idx]['html']
        # Requested index not present in TG variants.
        # For variant_idx > 0 (voronki/bots), try matching email variant first —
        # this handles the common pattern where "другие источники" email section
        # also serves as the TG-voronki text when no separate TG-voronki section exists.
        if variant_idx and email_variants and isinstance(email_variants, list) and variant_idx < len(email_variants):
            return email_variants[variant_idx]['html']
        # Otherwise fall back to last available TG variant
        return tg_variants_list[-1]['html']

    # --- Path 2: No TG variants, but a TG section exists ---
    if tg_section_html:
        return tg_section_html

    # --- Path 3: No TG section at all — fall back to email content ---
    # For variant_idx > 0 (voronki/bots): use matching email variant (e.g. Unisender/other sources)
    if variant_idx and email_variants and isinstance(email_variants, list) and variant_idx < len(email_variants):
        return email_variants[variant_idx]['html']
    # For variant_idx == 0 (TG-GC) or no variants: use main email section
    return email_section_html


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    return render_template('index.html', channels=CHANNELS)

_TG_HEADER_RE = re.compile(
    r'^(?:telegram|тг|телеграм|tg|max|нейрокот|помощник|push)\s*[\(\[/\s]',
    re.IGNORECASE,
)

def _strip_ai_tg_header(text):
    """Remove leading lines that are TG section headers (e.g. 'Telegram (в 1 клик)')."""
    if not text:
        return text
    lines = text.split('\n')
    while lines:
        line = lines[0].strip()
        if len(line) < 80 and _TG_HEADER_RE.match(line):
            lines.pop(0)
        else:
            break
    return '\n'.join(lines).strip()


@app.route('/api/debug-log', methods=['GET'])
def api_debug_log():
    """Return last 100 lines of debug.log."""
    log_path = os.path.join(os.path.dirname(__file__), 'debug.log')
    try:
        with open(log_path, encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
        return jsonify({'lines': lines[-100:]})
    except FileNotFoundError:
        return jsonify({'lines': ['(лог пустой — нажми Сгенерировать сначала)']})


@app.route('/api/parse', methods=['POST'])
def api_parse():
    data = request.get_json(force=True)
    url = data.get('url', '').strip()

    if not url:
        return jsonify({'error': 'URL не указан'}), 400

    # Handle already-exported URLs
    if 'export?format=html' in url:
        doc_id = extract_doc_id(url)
        export_url = url
    else:
        doc_id = extract_doc_id(url)
        if not doc_id:
            return jsonify({'error': 'Не удалось извлечь ID документа из ссылки'}), 400
        export_url = None

    try:
        if export_url:
            req_headers = {'User-Agent': 'Mozilla/5.0'}
            resp = requests.get(export_url, headers=req_headers, allow_redirects=True, timeout=30)
            resp.raise_for_status()
            html_content = resp.text
            cd_title = _title_from_content_disposition(resp.headers.get('Content-Disposition', ''))
        else:
            html_content, cd_title = fetch_google_doc_html(doc_id)
    except requests.RequestException as e:
        return jsonify({'error': f'Ошибка загрузки документа: {str(e)}'}), 502

    try:
        parsed = parse_doc_html(html_content)
    except Exception as e:
        return jsonify({'error': f'Ошибка разбора документа: {str(e)}'}), 500

    if not parsed.get('doc_title') and cd_title:
        parsed['doc_title'] = cd_title

    # Try AI-assisted parsing to enrich / fix sections
    ai_result = None
    ai_error = None
    try:
        ai_result = parse_with_ai(html_content)
    except Exception as e:
        ai_error = str(e)

    # Merge AI results into parsed: AI is primary for subject/preview (smarter about typos
    # and free-form text); keyword parser is fallback when AI found nothing.
    if ai_result:
        parsed['subject'] = ai_result.get('subject') or parsed['subject']
        ai_preview = ai_result.get('preview') or ''
        # AI may only correct/clean a preview already found by keyword parser — never invent one
        if ai_preview and len(ai_preview) <= 250 and parsed['preview']:
            parsed['preview'] = ai_preview

        # If AI identified a preview, strip the preheader marker line from email HTML
        # so it doesn't appear in the email body.
        if parsed['preview']:
            _preview_marker_re = re.compile(
                r'(прехедер|прехендер|прехэдер|превью|preview|preheader)\s*:',
                re.IGNORECASE
            )

            def _strip_preview_line(html):
                if not html:
                    return html
                soup = BeautifulSoup(html, 'html.parser')
                for tag in soup.find_all(True):
                    if tag.name in ('p', 'div', 'span', 'h1', 'h2', 'h3', 'h4'):
                        if _preview_marker_re.search(tag.get_text(strip=True)) and len(tag.get_text(strip=True)) < 250:
                            tag.decompose()
                return str(soup)

            parsed['email_html'] = _strip_preview_line(parsed['email_html'])
            if parsed.get('email_variants'):
                for v in parsed['email_variants']:
                    v['html'] = _strip_preview_line(v['html'])

        if not parsed['email_html'] and ai_result.get('email_gc'):
            parsed['email_html'] = ai_result['email_gc']

        if (ai_result.get('email_gc') and ai_result.get('email_unisender')
                and not parsed.get('email_variants') and not parsed['email_html']):
            parsed['email_variants'] = [
                {'name': 'Почта (1 клик)', 'html': ai_result['email_gc']},
                {'name': 'Почта (обычная)', 'html': ai_result['email_unisender']},
            ]

        tg_main_ai = _strip_ai_tg_header(ai_result.get('tg_main') or '')
        tg_voronki_ai = _strip_ai_tg_header(ai_result.get('tg_voronki') or '')

        def _looks_like_email(text):
            """True if the text looks like full email content rather than a short TG message."""
            if not text:
                return False
            # TG messages are short; email content is long
            if len(text) > 1500:
                return True
            # Contains GC offer URL variable — only in email_gc
            if '{offer_url_' in text:
                return True
            # Contains a CTA button marker pattern [ТЕКСТ КНОПКИ]
            if re.search(r'\[[А-ЯA-ZА-яa-z\s]{5,}\]', text):
                return True
            return False

        if _looks_like_email(tg_main_ai):
            logging.warning(f"AI tg_main looks like email content (len={len(tg_main_ai)}) — discarding")
            tg_main_ai = ''
        if _looks_like_email(tg_voronki_ai):
            logging.warning(f"AI tg_voronki looks like email content (len={len(tg_voronki_ai)}) — discarding")
            tg_voronki_ai = ''

        if not parsed['tg_html'] and tg_main_ai:
            parsed['tg_html'] = tg_main_ai

        # Build TG variants only when AI found voronki content and keyword parser didn't
        # already produce variants. Prefer keyword-parsed tg_html for the main variant
        # (it has proper <a href> links); AI tg_main is fallback only.
        if tg_voronki_ai and not parsed.get('tg_variants'):
            main_tg = parsed.get('tg_html') or tg_main_ai
            # Only create two variants if voronki content meaningfully differs from main
            v_short = re.sub(r'\s+', ' ', tg_voronki_ai).strip()[:300]
            m_short = re.sub(r'\s+', ' ', main_tg or '').strip()[:300]
            if main_tg and v_short != m_short:
                parsed['tg_variants'] = [
                    {'name': 'ТГ (основной)', 'html': main_tg},
                    {'name': 'ТГ (Воронки)', 'html': tg_voronki_ai},
                ]

    log_msg = (
        f"parse: email_html={bool(parsed['email_html'])} "
        f"email_variants={len(parsed.get('email_variants') or [])} "
        f"tg_html={bool(parsed['tg_html'])} "
        f"tg_variants={len(parsed.get('tg_variants') or [])} "
        f"doc_title={parsed.get('doc_title', '')!r} "
        f"ai={'ok' if ai_result else ('err:'+ai_error[:60] if ai_error else 'skip')}"
    )
    logging.info(log_msg)
    if ai_result:
        logging.debug(f"AI tg_main first 200: {str(ai_result.get('tg_main',''))[:200]}")
        logging.debug(f"AI email_gc first 200: {str(ai_result.get('email_gc',''))[:200]}")

    response_data = {
        'email_html': parsed['email_html'],
        'email_variants': parsed.get('email_variants'),
        'tg_html': parsed['tg_html'],
        'tg_variants': parsed.get('tg_variants'),
        'subject': parsed['subject'],
        'preview': parsed['preview'],
        'links': parsed['links'][:50],
        'footnotes': parsed['footnotes'],
        'doc_campaign': parsed.get('doc_campaign', ''),
        'doc_date': parsed.get('doc_date', ''),
        'doc_title': parsed.get('doc_title', ''),
        'sender': parsed.get('sender', ''),
    }
    if ai_error:
        response_data['ai_warning'] = f'AI парсинг недоступен: {ai_error}'
    if ai_result:
        response_data['ai_sections'] = ai_result

    return jsonify(response_data)

@app.route('/api/generate', methods=['POST'])
def api_generate():
    data = request.get_json(force=True)
    content = data.get('content', {})
    channels = data.get('channels', list(CHANNELS.keys()))
    campaign = data.get('campaign', '')
    date = data.get('date', '')
    segment = data.get('segment', '')
    images = data.get('images', [])
    subject = content.get('subject', '')

    email_section_html = content.get('email_html', '')
    tg_section_html = content.get('tg_html', '')
    email_variants = content.get('email_variants')  # list of {'name', 'html'} or None
    tg_variants_content = content.get('tg_variants')  # list of {'name', 'html'} or None

    skip_hosts = {'docs.google.com', 'www.google.com', 'google.com', 'drive.google.com'}
    doc_urls = []
    seen_urls = set()

    def _add_doc_url(u):
        if not u or not u.startswith('http'):
            return
        host = urlparse(u).netloc.lstrip('www.')
        if host in skip_hosts:
            return
        if u not in seen_urls:
            seen_urls.add(u)
            doc_urls.append(u)

    # Regular hyperlinks from document body
    for lnk in content.get('links', []):
        _add_doc_url(lnk.get('url', '') if isinstance(lnk, dict) else str(lnk))

    # Footnote/comment-style links (Google Docs [a][b] annotations with real URLs)
    for fn in content.get('footnotes', []):
        _add_doc_url(fn.get('url', '') if isinstance(fn, dict) else '')

    result = {'doc_urls': doc_urls}

    for ch_key in channels:
        ch_info = CHANNELS.get(ch_key)
        if not ch_info:
            continue

        fmt = ch_info.get('format', 'tg_html')

        if fmt == 'email':
            variant_idx = ch_info.get('email_variant_index', 0)
            if email_variants and variant_idx < len(email_variants):
                html_to_use = email_variants[variant_idx]['html']
            else:
                html_to_use = email_section_html
            if html_to_use:
                try:
                    html, blocks = generate_email_html(
                        html_to_use, ch_key, campaign, date, images, subject, segment
                    )
                    result[ch_key] = html
                    result[f'{ch_key}_blocks'] = blocks
                except Exception as e:
                    result[ch_key] = f'<!-- Error generating email: {e} -->'

        elif fmt == 'tg_html':
            src = _pick_tg_src(ch_info, tg_variants_content, tg_section_html, email_section_html, email_variants)
            if src:
                try:
                    result[ch_key] = generate_tg_html(src, ch_key, campaign, date, segment)
                except Exception as e:
                    result[ch_key] = f'<!-- Error: {e} -->'

        elif fmt == 'tg_bots':
            src = _pick_tg_src(ch_info, tg_variants_content, tg_section_html, email_section_html, email_variants)
            if src:
                try:
                    result[ch_key] = generate_tg_bots(src, ch_key, campaign, date, segment)
                except Exception as e:
                    result[ch_key] = f'Error: {e}'

        elif fmt == 'tg_markdown':
            src = _pick_tg_src(ch_info, tg_variants_content, tg_section_html, email_section_html, email_variants)
            if src:
                try:
                    text, links = generate_tg_markdown(src, ch_key, campaign, date, segment)
                    result[ch_key] = text
                    result[f'{ch_key}_links'] = links
                except Exception as e:
                    result[ch_key] = f'Error: {e}'
                    result[f'{ch_key}_links'] = []

    return jsonify(result)

@app.route('/api/assemble-email', methods=['POST'])
def api_assemble_email():
    data = request.get_json(force=True)
    blocks = data.get('blocks', [])
    subject = data.get('subject', '')
    images = data.get('images', [])

    content_rows = []
    for block in blocks:
        btype = block.get('type', 'block_white')
        ph = block.get('paragraphs_html', '')
        btn_text = block.get('btn_text', '')
        btn_url_utm = block.get('btn_url_utm', '#')

        if btype == 'block_blue_cta':
            buttons = block.get('buttons') or [{'text': btn_text, 'url': btn_url_utm}]
            img_url_cta = block.get('image_url', '')
            BTN_A = ("background:#E1FB52;color:#000000;padding:12px 50px;border-radius:30px;"
                     "text-decoration:none;font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;"
                     "font-size:16px;display:inline-block;font-weight:600")
            btn_rows = ''.join(
                f'<tr><td align="center" bgcolor="#1445ea" style="padding:8px 0 12px;margin:0">\n'
                f'<a href="{b["url"]}" target="_blank" style="{BTN_A}">{b["text"]}</a>\n'
                f'</td></tr>\n'
                for b in buttons
            )
            # Optional image row at the top of the CTA block
            img_row = ''
            if img_url_cta:
                img_row = (
                    '<tr><td align="center" bgcolor="#1445ea" style="padding:8px 10px 4px;font-size:0px">\n'
                    f'<img src="{img_url_cta}" alt="" style="display:block;border:0;max-width:100%;border-radius:8px">\n'
                    '</td></tr>\n'
                )
            if ph.strip():
                row = (
                    '<tr><td style="padding:5px 10px 10px;margin:0;background-color:#ffffff">\n'
                    '<table cellspacing="0" cellpadding="0" width="100%" style="border-collapse:separate;'
                    'border-spacing:0;border:10px solid #1445ea;border-radius:20px" role="presentation">\n'
                    + img_row
                    + '<tr><td align="left" bgcolor="#1445ea" style="padding:15px 10px 5px;margin:0;'
                    'font-family:roboto,\'helvetica neue\',helvetica,arial,sans-serif;font-size:18px;'
                    'line-height:27px;color:#ffffff">\n'
                    + ph + '\n</td></tr>\n'
                    + btn_rows
                    + '</table></td></tr>'
                )
            else:
                row = (
                    '<tr><td style="padding:5px 10px 10px;margin:0;background-color:#ffffff">\n'
                    '<table cellspacing="0" cellpadding="0" width="100%" style="border-collapse:separate;'
                    'border-spacing:0;border:10px solid #1445ea;border-radius:20px" role="presentation">\n'
                    + img_row
                    + btn_rows
                    + '</table></td></tr>'
                )
        elif btype == 'block_grey':
            row = block_grey(ph)
        elif btype == 'block_dotted':
            row = block_dotted(ph)
        elif btype == 'block_blue_text':
            row = block_blue_text(ph)
        elif btype == 'block_button':
            row = block_button(btn_url_utm, btn_text)
        elif btype == 'block_spacer':
            row = block_spacer(block.get('height', 20))
        elif btype == 'block_image':
            row = block_image_center(block.get('image_url', ''))
        elif btype == 'block_2col_img_text':
            row = block_2col_img_text(block.get('image_url', ''), ph)
        elif btype == 'block_2col_text_img':
            row = block_2col_text_img(ph, block.get('image_url', ''))
        elif btype == 'block_2col_text_text':
            row = block_2col_text_text(ph, block.get('col2_html', ''))
        elif btype == 'block_3col_text':
            row = block_3col_text(ph, block.get('col2_html', ''), block.get('col3_html', ''))
        else:
            row = block_white(ph)
        content_rows.append(row)

    content_table = (
        '<table cellpadding="0" cellspacing="0" align="center" class="es-content-body" '
        'role="none" style="border-collapse:collapse;border-spacing:0;width:600px;background-color:#ffffff">\n'
        + '\n'.join(content_rows)
        + '\n</table>'
    )
    logo_header = EMAIL_HEADER.replace('{logo_url}', LOGO_URL)
    subject_safe = subject or 'Рассылка ZeroCoder'
    html = (
        EMAIL_WRAPPER_START.replace('{subject}', subject_safe)
        + logo_header
        + content_table
        + EMAIL_FOOTER
        + EMAIL_WRAPPER_END
    )
    return jsonify({'html': html})


@app.route('/api/generate-utm', methods=['POST'])
def api_generate_utm():
    data = request.get_json(force=True)
    base_url = data.get('url', '').strip()
    campaign = data.get('campaign', '')
    date = data.get('date', '')
    segment = data.get('segment', '')

    if not base_url:
        return jsonify({'error': 'URL не указан'}), 400

    result = {}
    for ch_key, ch_info in CHANNELS.items():
        utm_url = build_utm_url(base_url, ch_key, campaign, date, segment)
        result[ch_key] = {
            'name': ch_info['name'],
            'url': utm_url,
        }

    return jsonify(result)

GC_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gc_output')

_GC_TRANSPORT = {
    'email':           'email',
    'email_unisender': 'email',
    'tg_gc':           'tg',
    'max':             'max',
}

@app.route('/api/push-to-gc', methods=['POST'])
def api_push_to_gc():
    data = request.get_json(force=True)
    name = data.get('name', '').strip()
    subject = data.get('subject', '').strip()
    html = data.get('html', '').strip()
    channel_key = data.get('channel_key', 'email')
    transport = _GC_TRANSPORT.get(channel_key, 'email')

    if not name or not html:
        return jsonify({'error': 'Нужны name и html'}), 400

    os.makedirs(GC_OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(GC_OUTPUT_DIR, 'letters_built.json')

    existing = []
    if os.path.exists(out_path):
        try:
            existing = json.loads(open(out_path, encoding='utf-8').read())
        except Exception:
            existing = []

    updated = [e for e in existing if e.get('name') != name]
    updated.append({
        'name': name,
        'subject': subject,
        'html': html,
        'channel_key': channel_key,
        'transport': transport,
    })

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(updated, f, ensure_ascii=False, indent=2)

    return jsonify({'ok': True, 'total': len(updated)})


@app.route('/api/push-to-mail', methods=['POST'])
def api_push_to_mail():
    data = request.get_json(force=True)
    name = data.get('name', '').strip()
    subject = data.get('subject', '').strip()
    html = data.get('html', '').strip()
    channel_key = data.get('channel_key', 'email')
    date_tag = data.get('date_tag', '')
    preheader = data.get('preheader', '').strip()
    sender_name = data.get('sender_name', '').strip() or 'Университет Зерокодер'
    campaign = data.get('campaign', '').strip()

    if not name or not html:
        return jsonify({'error': 'Нужны name и html'}), 400

    mail_url = os.getenv('MAIL_API_URL', 'https://mail.zerocoder.info')
    mail_token = os.getenv('MAIL_API_TOKEN', '')
    if not mail_token:
        return jsonify({'error': 'MAIL_API_TOKEN не настроен в .env'}), 500

    headers = {'Authorization': f'Bearer {mail_token}', 'Content-Type': 'application/json'}
    mailing_tags = ['announce']
    if campaign:
        mailing_tags.append(campaign)
    transport = _GC_TRANSPORT.get(channel_key, 'email')
    mailing = {
        'name': name,
        'subject': subject,
        'html': html,
        'tags': mailing_tags,
    }
    payload = {
        'category': '0',
        'transport': transport,
        'tags': [date_tag] if date_tag else [],
        'mailings': [mailing],
    }

    logging.info(f"push-to-mail: name={name!r} html_len={len(html)} html_snippet={html[:120]!r} preheader={preheader!r}")
    try:
        resp = requests.post(f'{mail_url}/api/mailings', json=payload, headers=headers, timeout=30)
        logging.info(f"push-to-mail response: status={resp.status_code} body={resp.text[:500]!r}")
        if resp.status_code == 401:
            return jsonify({'error': 'Неверный токен авторизации'}), 401
        if resp.status_code == 400:
            return jsonify({'error': 'Некорректный запрос: ' + resp.text}), 400
        if resp.status_code == 422:
            return jsonify({'error': 'Ошибка формата: ' + resp.text}), 422
        resp.raise_for_status()
        result = resp.json()
        job_id = result.get('job_id')
        return jsonify({'ok': True, 'job_id': job_id, 'count': result.get('count', 1)})
    except requests.RequestException as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/job-status/<job_id>')
def api_job_status(job_id):
    mail_url = os.getenv('MAIL_API_URL', 'https://mail.zerocoder.info')
    mail_token = os.getenv('MAIL_API_TOKEN', '')
    gc_domain = os.getenv('GC_DOMAIN', 'university.zerocoder.ru')
    headers = {'Authorization': f'Bearer {mail_token}'}
    try:
        resp = requests.get(f'{mail_url}/api/jobs/{job_id}', headers=headers, timeout=15)
        job_data = resp.json()
        logging.info(f"job-status {job_id}: {job_data}")
        results = job_data.get('results', [])
        gc_url = None
        if results:
            mailing_id = results[0].get('id') or results[0].get('mailing_id')
            if mailing_id:
                gc_url = f'https://{gc_domain}/notifications/control/mailings/update/id/{mailing_id}'
        return jsonify({
            'status': job_data.get('status'),
            'gc_url': gc_url,
            'done': job_data.get('done', 0),
            'total': job_data.get('total', 1),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)
