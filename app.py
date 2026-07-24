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

# ---------------------------------------------------------------------------
# Yandex Cloud Storage — загрузка base64-картинок из Google Docs
# ---------------------------------------------------------------------------

def _upload_image_to_yc(data_uri: str):
    """Upload base64 data URI image to Yandex Cloud Storage.
    Returns public URL string on success, None on failure or missing credentials."""
    import base64, hashlib, boto3
    from botocore.client import Config

    key_id  = os.environ.get('YC_ACCESS_KEY_ID', '').strip()
    secret  = os.environ.get('YC_SECRET_ACCESS_KEY', '').strip()
    bucket  = os.environ.get('YC_BUCKET', 'image-lessons').strip()
    region  = os.environ.get('YC_REGION', 'ru-central1').strip()
    storage_url = os.environ.get('YC_STORAGE_URL', 'https://storage.yandexcloud.net').strip()

    logging.debug(f'[YC Upload] called, key_id={"SET" if key_id else "EMPTY"}, data_uri_prefix={data_uri[:30]}')
    if not key_id or not secret:
        logging.warning('[YC Upload] Skipped — YC_ACCESS_KEY_ID or YC_SECRET_ACCESS_KEY not set in .env')
        return None

    if not data_uri.startswith('data:image/'):
        return None

    try:
        header, b64data = data_uri.split(',', 1)
        content_type = header.split(':')[1].split(';')[0]  # data:image/png;base64 → image/png
        ext = content_type.split('/')[1]  # png, jpeg, gif, webp

        img_bytes = base64.b64decode(b64data)
        # Stable filename: same content → same URL, no duplicates in bucket
        img_hash = hashlib.md5(img_bytes).hexdigest()[:12]
        filename = f'gdoc-images/{img_hash}.{ext}'

        session_boto = boto3.session.Session()
        s3 = session_boto.client(
            service_name='s3',
            endpoint_url=storage_url,
            aws_access_key_id=key_id,
            aws_secret_access_key=secret,
            config=Config(signature_version='s3v4'),
            region_name=region,
        )

        # Skip upload if identical file already exists
        try:
            s3.head_object(Bucket=bucket, Key=filename)
            public_url = f'{storage_url}/{bucket}/{filename}'
            logging.debug(f'[YC Upload] Already exists, reusing → {public_url}')
            return public_url
        except Exception:
            pass

        s3.put_object(
            Bucket=bucket,
            Key=filename,
            Body=img_bytes,
            ContentType=content_type,
            ACL='public-read',
        )

        public_url = f'{storage_url}/{bucket}/{filename}'
        logging.info(f'[YC Upload] Uploaded image → {public_url}')
        return public_url
    except Exception as e:
        logging.warning(f'[YC Upload] Error: {e}')
        return None


def _replace_base64_images_with_yc_urls(html):
    """Replace <img src="data:image/...;base64,..."> with an uploaded YC URL.
    Runs at parse time so the huge base64 payload never has to round-trip
    through the browser (parse response → generate request) — Google Docs
    export embeds pasted screenshots as base64, which can bloat a doc to
    10+ MB and get rejected by the proxy's request body size limit.
    Falls back to leaving the data URI untouched if upload fails (e.g. no
    YC credentials) — the existing generate-time upload/fallback logic
    still handles that case."""
    if not html or 'data:image' not in html:
        return html
    soup = BeautifulSoup(html, 'html.parser')
    changed = False
    for img in soup.find_all('img'):
        src = img.get('src', '')
        if src.startswith('data:image'):
            yc_url = _upload_image_to_yc(src)
            if yc_url:
                img['src'] = yc_url
                changed = True
    return str(soup) if changed else html

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
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Сессия истекла, войдите снова', 'auth': False}), 401
        return redirect(url_for('login'))

@app.errorhandler(Exception)
def handle_unhandled_exception(e):
    app.logger.exception('Unhandled exception')
    if request.path.startswith('/api/'):
        return jsonify({'error': f'{type(e).__name__}: {e}'}), 500
    raise e

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
- строки "кампания:", "каналы:", "сегмент:", "исключаем", "включаем", "from:", "от кого:", "отправитель:"
- строки с адресами "@zerocoder", "care@", "getcourse", "unisender", "zerocoder.ru"
- строки "РЕКЛАМА ООО", "ИНН 9715401631" (юридический дисклеймер)
- строки-сноски вида "[a]", "[b]", "[1]" (аннотации к ссылкам)

**EMAIL — ВАРИАНТ ДЛЯ ГК** (поле "email_gc"):

‼️ КЛЮЧЕВОЕ ПРАВИЛО: Если заголовок секции содержит "1 клик", "1click", "в один клик" — эта секция ВСЕГДА идёт в email_gc, НЕЗАВИСИМО от порядка в документе.

‼️ ИСКЛЮЧЕНИЕ из правила "1 клик": если заголовок содержит явные TG/бот-индикаторы ("бот", "тг", "telegram", "max", "ботов") — это TG-секция (tg_main), даже если в заголовке упоминается "1 клик", "с 1 кликом", "без 1 клика". Примеры TG-секций: "Сообщение для ботов ТГ и Макс с 1 кликом", "Сообщение для ботов ТГ и Макс без 1 клика" → tg_main.

Примеры заголовков → email_gc: "Почта (1 клик)", "Email (1 клик)", "В 1 клик", "Контент письма (1 клик)"
Также в email_gc идут секции с заголовками: "контент письма", "текст письма", "почта:", "e-mail:", "письмо:", "для почты", "email:"

Эта секция содержит переменные {first_name}, {offer_url_...} и т.п. — сохранять нетронутыми.

‼️ ВАЖНО: наличие текста "1 клик" или "в 1 клик" ВНУТРИ КНОПКИ или ссылки [ЗАРЕГИСТРИРОВАТЬСЯ В 1 КЛИК] — это КНОПКА, не заголовок секции. Такой текст не определяет принадлежность СЕКЦИИ к email_gc.

**EMAIL — ВАРИАНТ ДЛЯ UNISENDER** (поле "email_unisender"):
Секция с заголовком "другие источники", "другие каналы", "другой источник", "другие боты", "другой текст", "письмо в юнисендер", "письмо для юнисендера", "письмо юнисендер".
Это письмо для Unisender — без GC-переменных {first_name} и т.п.
Если такой секции нет — вернуть null.

‼️ ВАЖНО: "Другие источники" это ОТДЕЛЬНАЯ секция — не путать с кнопками или ссылками внутри email_gc.

**TG — ОСНОВНОЙ** (поле "tg_main"):
Секции с заголовком "телеграм", "telegram", "тг", "tg", "max", "телеграм/max", "тг/max" и т.п.
Также: "ТГ бот (ГК)", "ТГ бот", "ТГ бот ГК", "бот тг", "тг-бот", "для бота тг", "tg bot" и любые похожие варианты.
‼️ ВАЖНО: заголовок TG-секции может стоять в ОДНОМ АБЗАЦЕ с первой строкой контента (слитый параграф). Пример: "ТГ бот (ГК)Соберем видео-завод в прямом эфире..." — здесь "ТГ бот (ГК)" это заголовок секции, а не начало контента. Распознавай первую часть такого абзаца как заголовок, в section_headers верни его точный текст.
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

‼️ ВАЖНО: документ может начинаться с нумерованных структурных заголовков вида "1. Тема рассылки", "2. Каналы", "3. Сегмент отправки", "4. Контент письма". Это МЕТАДАННЫЕ документа, НЕ заголовки контентных секций. Ищи заголовки секций ВНУТРИ раздела "Контент письма".

Вариант A (с 1 кликом):
1. Тема: ...
2. Превью: ...
3. [Почта (1 клик)] → email_gc (с {first_name}, для GetCourse)
4. [Другие источники] → email_unisender (без {first_name}, для Unisender)
   Если внутри "Другие источники" есть короткий TG-блок — это tg_voronki
5. [ТГ / Telegram / Max / ТГ бот (ГК)] → tg_main

Вариант B (без разделения):
1. Тема: ...
2. [Почта / Email / ПОЧТА] → email_gc
3. [ТГ / Telegram / ТГ бот (ГК)] → tg_main

Вариант C (нумерованный документ):
[...нумерованные структурные заголовки 1-4...]
4. Контент письма     ← это мета-заголовок, НЕ секция
   ПОЧТА              ← это заголовок email_gc секции
   Тема: ...
   [email контент]
   ТГ бот (ГК)        ← это заголовок tg_main секции (может быть слит с контентом)
   [TG контент]

=== ПРАВИЛА ОБРАБОТКИ КОНТЕНТА ===

1. Сохраняй переменные {first_name}, {offer_url_...}, {firstName} и любые {переменная} НЕТРОНУТЫМИ.
2. Сохраняй эмодзи в тексте и в кнопках.
3. Сохраняй форматирование (<b>, <i>) если есть в исходнике.
4. Сохраняй ссылки в тексте.
5. Сохраняй маркеры списков и структуру абзацев.
6. НЕ добавляй "РЕКЛАМА ООО ЗЕРОКОДЕР" и "ИНН 9715401631".
7. НЕ включай заголовки секций в контент.
8. Кнопки [ТЕКСТ КНОПКИ] — сохранять как есть, они обрабатываются отдельно.

=== ПОЛЕ section_headers ===

В поле section_headers верни ТОЧНЫЙ текст строки-заголовка каждой найденной секции — так, как он написан в документе (включая любой дополнительный текст на той же строке через мягкий перенос, если он слит с заголовком).
Например: если секция "Другие источники" начинается строкой "Другие источникиТема: Завтра..." — верни именно эту строку целиком.
Если секция не найдена — null.

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
  "neurocat": "текст для Нейрокота или null",
  "section_headers": {
    "email_gc": "точный текст строки-заголовка секции email_gc как в документе или null",
    "email_unisender": "точный текст строки-заголовка секции email_unisender как в документе или null",
    "tg_main": "точный текст строки-заголовка секции tg_main как в документе или null",
    "tg_voronki": "точный текст строки-заголовка секции tg_voronki как в документе или null"
  }
}"""


def parse_with_ai(raw_html):
    api_key = os.getenv('OPENROUTER_API_KEY')
    base_url = os.getenv('OPENROUTER_BASE_URL', 'https://openrouter.ai/api/v1')
    model = os.getenv('OPENROUTER_MODEL', 'google/gemini-2.5-flash-lite')

    if not api_key:
        raise ValueError('OPENROUTER_API_KEY не задан в .env')

    client = OpenAI(api_key=api_key, base_url=base_url)

    soup = BeautifulSoup(raw_html, 'lxml')
    # Build plain text paragraph-by-paragraph using empty separator within each paragraph
    # so words split across adjacent spans (Google Docs artifact) stay intact.
    # '\n'.join keeps document structure visible to the AI.
    _body = soup.find('body') or soup
    _paras = _body.find_all(['p', 'h1', 'h2', 'h3', 'h4', 'li'])
    plain_text = '\n'.join(
        ' '.join(p.get_text('').replace('\xa0', ' ').split())
        for p in _paras
        if p.get_text(strip=True)
    )

    if len(plain_text) > 30000:
        plain_text = plain_text[:30000]

    messages = [
        {'role': 'system', 'content': _AI_SYSTEM_PROMPT},
        {'role': 'user', 'content': f'Текст документа:\n\n{plain_text}'},
    ]

    last_err = None
    for attempt in range(2):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0.1,
            max_tokens=8000,
        )
        raw_answer = response.choices[0].message.content.strip()
        raw_answer = re.sub(r'^```(?:json)?\s*', '', raw_answer)
        raw_answer = re.sub(r'\s*```$', '', raw_answer)
        try:
            return json.loads(raw_answer)
        except json.JSONDecodeError as e:
            last_err = e
            logging.warning(f'AI JSON parse error (attempt {attempt+1}): {e}')

    raise last_err


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
body { margin:0; padding:0; }
img { border:0; outline:none; text-decoration:none; -ms-interpolation-mode:bicubic; }
a { text-decoration:none; }
@media only screen and (max-width:600px) {
  .es-content-body { width:100% !important; }
  .es-footer-body { width:100% !important; }
  .es-left, .es-right { float:none !important; width:100% !important; }
  .esdev-mso-td { display:block !important; width:100% !important; }
  .esdev-mso-table { width:100% !important; }
  .es-col-2, .es-col-3 { display:block !important; width:100% !important; padding:5px 0 !important; }
  .es-col-img { max-width:200px !important; width:auto !important; display:block; margin:0 auto; }
}
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
<img src="{logo_url}" alt="Зерокодер" width="220" style="display:block;border:0;max-width:220px">
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

EMAIL_AD_DISCLAIMER = '''
<table cellspacing="0" cellpadding="0" align="center" role="none" style="border-collapse:collapse;border-spacing:0;background-color:#ffffff;width:600px">
<tr><td align="center" style="padding:16px 20px 16px;margin:0">
<p style="margin:0 0 4px 0;font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;line-height:16px;color:#999999;font-size:11px">РЕКЛАМА ООО &quot;ЗЕРОКОДЕР&quot;</p>
<p style="margin:0;font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;line-height:16px;color:#999999;font-size:11px">ИНН 9715401631</p>
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

def block_image_with_text(image_url, paragraphs_html):
    return (
        '<tr><td align="left" bgcolor="#ffffff" style="padding:10px 20px;margin:0;background-color:#ffffff">\n'
        f'<div style="text-align:center;font-size:0px;padding-bottom:10px">'
        f'<img src="{image_url}" alt="" style="display:inline-block;border:0;max-width:100%;border-radius:10px">'
        f'</div>\n'
        + paragraphs_html
        + '\n</td></tr>'
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

def block_button(btn_url, btn_text, paragraphs_html=''):
    """Standalone button on white background, centered.
    If paragraphs_html is given (e.g. text that follows the button in the
    source doc), it's rendered below the button in the SAME white block
    instead of a separate one."""
    btn_style = (
        "background:#E1FB52;color:#000000;padding:12px 50px;border-radius:30px;"
        "text-decoration:none;font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;"
        "font-size:16px;display:inline-block;font-weight:600"
    )
    if paragraphs_html.strip():
        return (
            '<tr><td align="left" bgcolor="#ffffff" style="padding:8px 20px 20px;margin:0;background-color:#ffffff">\n'
            f'<div align="center" style="padding-bottom:10px"><a href="{btn_url}" target="_blank" style="{btn_style}">{btn_text}</a></div>\n'
            + paragraphs_html
            + '\n</td></tr>'
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

_img_dim_cache = {}

def _get_image_size(image_url):
    """Return (width, height) of image_url, cached in memory. None if it can't be read."""
    if not image_url:
        return None
    if image_url in _img_dim_cache:
        return _img_dim_cache[image_url]
    size = None
    try:
        from PIL import Image
        from io import BytesIO
        if image_url.startswith('http://') or image_url.startswith('https://'):
            resp = requests.get(image_url, timeout=5)
            resp.raise_for_status()
            img = Image.open(BytesIO(resp.content))
        else:
            img = Image.open(image_url)
        size = img.size
    except Exception:
        size = None
    _img_dim_cache[image_url] = size
    return size

def _is_portrait_image(image_url, threshold=1.3):
    """True if image height exceeds width by more than `threshold`x (tall/stretched-looking)."""
    size = _get_image_size(image_url)
    if not size or not size[0]:
        return False
    w, h = size
    return (h / w) > threshold

def _col_img_style(image_url):
    """Style for a 2-col block image: cap by height if portrait so it doesn't
    tower over a short text column, otherwise fill the column width as usual."""
    if _is_portrait_image(image_url):
        return "display:block;border:0;max-height:220px;width:auto;max-width:100%;border-radius:8px;margin:0 auto"
    return "display:block;border:0;width:100%;max-width:260px;border-radius:8px"

def block_2col_img_text(image_url, text_html):
    """Two-column block: image left, text right."""
    col_style = "font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;font-size:16px;line-height:24px;color:#333333"
    img_style = _col_img_style(image_url)
    return (
        '<tr><td align="left" bgcolor="#ffffff" style="padding:10px 20px;background-color:#ffffff">\n'
        '<table cellpadding="0" cellspacing="0" width="100%" role="none" style="border-collapse:collapse;border-spacing:0">\n'
        '<tr>\n'
        '<td class="es-col-2" align="left" valign="top" style="padding-right:12px;width:50%">\n'
        f'<img src="{image_url}" alt="" class="es-col-img" style="{img_style}">\n'
        '</td>\n'
        f'<td class="es-col-2" align="left" valign="top" style="padding-left:12px;width:50%;{col_style}">\n'
        + text_html
        + '\n</td>\n</tr>\n</table>\n</td></tr>'
    )

def block_2col_text_img(text_html, image_url):
    """Two-column block: text left, image right."""
    col_style = "font-family:roboto,'helvetica neue',helvetica,arial,sans-serif;font-size:16px;line-height:24px;color:#333333"
    img_style = _col_img_style(image_url)
    return (
        '<tr><td align="left" bgcolor="#ffffff" style="padding:10px 20px;background-color:#ffffff">\n'
        '<table cellpadding="0" cellspacing="0" width="100%" role="none" style="border-collapse:collapse;border-spacing:0">\n'
        '<tr>\n'
        f'<td class="es-col-2" align="left" valign="top" style="padding-right:12px;width:50%;{col_style}">\n'
        + text_html
        + '\n</td>\n'
        '<td class="es-col-2" align="left" valign="top" style="padding-left:12px;width:50%">\n'
        f'<img src="{image_url}" alt="" class="es-col-img" style="{img_style}">\n'
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
    return ' '.join(tag.get_text(' ', strip=True).replace('\xa0', ' ').split())

def _tight_text_with_br_boundary(tag):
    """Like tag.get_text('') — glues adjacent spans with no separator, so a word
    split across spans without a real space (e.g. spellcheck 'Тем'+'а') still reads
    as one word — but treats <br> as an actual boundary (space) instead of letting
    it vanish. Without this, a Google Docs shape like '<span>БОТ<br/><br/></span>
    <span>Тест-драйв...</span>' collapses into an unmatchable glued token
    'боттест-драйв' instead of 'бот тест-драйв...'."""
    parts = []
    for node in tag.descendants:
        if isinstance(node, NavigableString):
            parts.append(str(node))
        elif getattr(node, 'name', None) == 'br':
            parts.append(' ')
    return ''.join(parts)

# Aliases for the "Другие источники" email section label. Module-level (not local to
# is_section_header) because process_block()'s merged-header content-preservation logic
# needs the same list to recognize the label as discardable, not real content.
OTHER_SRC_LABEL_KW = ['другие источники', 'другие каналы', 'другой источник', 'другие боты',
                      'другой текст', 'для др источников', 'для других источников',
                      'др источники', 'для др. источников', 'другой ист', 'другие ист',
                      'письмо в юнисендер', 'письмо для юнисендера', 'письмо юнисендер']

def is_section_header(tag, ai_hints=None):
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

    # Use a tight (no-separator) join, not get_text_content()'s space-separated one:
    # Google Docs sometimes splits a single label word across two adjacent spans with
    # no actual space character between them (e.g. spellcheck/autoformat splitting
    # "Тема" into "Тем"+"а") — space-joining would turn that into "тем а", which no
    # longer starts with the "тема:" keyword below. Real inter-word spaces survive
    # this join fine since they're literal space characters inside the text nodes.
    text = ' '.join(_tight_text_with_br_boundary(tag).replace('\xa0', ' ').split()).lower()
    # Normalize "тема :" → "тема:" for the (rarer) case of a genuine space typed
    # before the colon in the source.
    text = re.sub(r'\s+:', ':', text)
    if not text:
        return None

    # "БОТ (1 клик)", "БОТ (общий)" etc. — unambiguous TG section headers.
    if re.match(r'^бот\s*\(', text) and len(text) <= 40:
        return 'tg_section'

    # AI hints are intentionally NOT used here. ai_hints is accepted as a parameter
    # for backwards-compatibility but ignored: stochastic AI responses caused intermittent
    # wrong section splits. All section detection is deterministic via keywords below.

    # Early check: merged paragraph where label is the very first word and content follows.
    # Examples: "Телеграм🎉Ты уже в самой продвинутой тусовке...", "ТГ бот (ГК)Соберем...",
    # "ТГ-БОТ..." (hyphenated label — split on the hyphen too, so "тг-бот" isn't read as
    # one glued token "тгбот" that matches nothing).
    # Must run BEFORE the length guard, since merged paragraphs are long.
    # Set matches tg_label_words below (the content-preservation logic for merged TG
    # headers) — that code already expected 'max' to be a possible merged label, but this
    # classification check didn't recognize it as one, so a merged "MAX<br><br>..." header
    # would never have reached the preservation code in the first place. Added
    # 'нейрокот'/'помощник' too since they're valid standalone TG labels (tg_exact below)
    # that could plausibly get glued the same way, even without a confirmed case yet.
    _first_word_raw = text.split()[0] if text.split() else ''
    _first_word_raw = re.split(r'[-–—]', _first_word_raw)[0]
    _first_word_alpha = ''.join(ch for ch in _first_word_raw if ch.isalpha())
    if (_first_word_alpha in {'телеграм', 'telegram', 'тг', 'tg', 'бот', 'bot', 'max', 'нейрокот', 'помощник'}
            and text != _first_word_alpha):
        return 'tg_section'

    # Meta-content labels: skip entirely (neither section header nor content).
    # Must run BEFORE the length guard below: Google Docs sometimes collapses the
    # blank line between two meta lines (e.g. "от кого: ..." + "Тема: ...") into a
    # single over-120-char paragraph via <br><br> nested inside one <p>, instead of
    # emitting two separate short <p> tags. These are startswith checks anchored at
    # the beginning of the text, so it's safe to run them regardless of how long the
    # rest of the (possibly glued-on) paragraph turns out to be.
    meta_label_kw = ['от лица ', 'от лица:', 'в 1 клик', 'в 2 клик', 'в один клик',
                     'обычная рассылка', 'ссылки:', 'список ссылок']
    if any(text.startswith(kw) for kw in meta_label_kw):
        return 'skip'
    if text in ('ссылки',):
        return 'skip'

    meta_kw = ['кампания:', 'каналы ', 'каналы(', 'сегмент ', 'сегмент(',
               'исключаем', 'включаем', 'от кого:', 'from:', 'отправитель:']
    # Skip meta-section headers (campaign, channels, segments, sender info)
    if any(text.startswith(kw) or text == kw.rstrip(':') for kw in meta_kw):
        return 'skip'

    # Merged email header: "ПОЧТА От кого: ...", "Почта ГК", "Почта (обычная)" etc.
    # First word is a recognized email label followed by extra descriptor text.
    # Must run BEFORE the length guard below, since merged paragraphs are long
    # (mirrors the TG version above — this used to run after the guard and never
    # matched when Google Docs glued a Тема:/preview line onto the same paragraph).
    if _first_word_alpha in {'почта', 'письмо'} and text != _first_word_alpha:
        return 'email_section'

    # Merged "Другие источники: ..." header — phrase-based label (multi-word, so it
    # can't reuse the single first-word check above) immediately followed by more
    # content in the same paragraph, e.g. "Другие источники: Тема: ...<br><br>Разберем
    # по шагам...". Must also run before the length guard for the same reason.
    if any(text.startswith(kw) for kw in OTHER_SRC_LABEL_KW):
        return 'email_section'

    # Merged "Тема:"/"Прехедер:" header immediately followed by more real content in the
    # same paragraph, e.g. "Тема: X!<br><br>Разберем по шагам..." — same class of bug as
    # the checks above, found the same day: this used to sit only in the post-guard block
    # below and silently swallowed (or discarded) the real sentence that follows once the
    # merged paragraph grew past 120 chars. Startswith-anchored only here (not "kw in text")
    # to avoid false-positives from body sentences that merely mention these words
    # mid-paragraph — that broader substring match is kept in the post-guard fallback below,
    # bounded to short (<=120 char) text where it's safe.
    subject_kw = ['тема письма', 'тема:', 'темы:', 'subject:']
    preview_kw = ['превью:', 'прехедер:', 'прехендер:', 'preview:', 'preheader:', 'preheader :', 'прехэдер:']
    if any(text.startswith(kw) for kw in subject_kw):
        return 'subject'
    if any(text.startswith(kw) for kw in preview_kw):
        return 'preview'

    # Section headers are short labels, not body sentences
    if len(text) > 120:
        return None

    # Skip rows that are clearly channel entries or service addresses
    skip_kw = ['care@', '@zerocoder', 'getcourse', 'unisender', 'bot (',
               'zerocoder_bot', 'zerocoder.ru', 'newcat.zerocoder']
    if any(s in text for s in skip_kw):
        return None

    email_kw = ['контент письма', 'текст письма', 'текст:', 'текст для почты',
                'почта:', 'почта (', '3.контент', '3. контент', 'e-mail:', 'письмо:',
                'для почты', 'email:', 'email от кого', 'email (гк',
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
             'бота тг', 'бота telegram', 'сообщение для тг', 'сообщение для бота',
             'ботов тг', 'ботов telegram', 'сообщение для ботов']
    tg_only_kw = ['телеграм:', 'telegram:']

    # Standalone email section headers — exact match
    email_exact = {'почта', 'письмо', 'e-mail', 'email', 'почта гк', 'email гк', 'почта (гк)', 'mail'}
    if text in email_exact:
        return 'email_section'

    # Standalone TG section headers — single-word only, exact match to avoid false positives
    tg_exact = {'телеграм', 'telegram', 'тг', 'tg', 'бот', 'max', 'instagram', 'push', 'youtube',
                'инстаграм', 'ютуб', 'нейрокот', 'помощник'}
    if text in tg_exact:
        return 'tg_section'

    # Skip standalone single-word channel-type labels that appear in config tables
    if text in ('telegram/max', 'приложение'):
        return None

    # Broader substring fallback for preview_kw (unlike subject_kw's plain startswith
    # above, this also matches mid-paragraph occurrences) — deliberately left bounded
    # to text that already passed the <=120-char guard, to avoid false-positives from
    # long body sentences that merely mention "прехедер:"/"preview:" somewhere inside.
    for kw in preview_kw:
        if kw in text:
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


def _get_trailing_email_label(tag):
    """
    Detects a merged Google Docs paragraph where body content and an email
    section label (e.g. "Другие источники") share a single <p> tag, separated
    by <br/> line-breaks.  Example DOM shape:
        <p> <span>CTA button text</span> <span><br/><br/></span>
            <span>Другие источники</span> </p>

    Returns (label_name, pre_content_tag) when detected, otherwise None.
    - label_name   : clean text of the section label ("Другие источники")
    - pre_content_tag : a copy of the original tag with the label part removed,
                        ready to be added as content to the current section.
    """
    other_src_kw = [
        'другие источники', 'другие каналы', 'другой источник', 'другие боты',
        'другой текст', 'для др источников', 'для других источников',
        'др источники', 'для др. источников', 'другой ист', 'другие ист',
        'письмо в юнисендер', 'письмо для юнисендера', 'письмо юнисендер',
    ]

    # Only consider tags that contain <br/> tags
    if not tag.find('br'):
        return None

    children = list(tag.children)

    def _match_label(candidate_children):
        """Return the label text if candidate_children's text starts with a
        recognized label keyword and passes the length guard, else None."""
        text = ''.join(
            c.get_text() if hasattr(c, 'get_text') else str(c)
            for c in candidate_children
        ).strip()
        text_lower = text.lower()
        if not any(text_lower.startswith(kw) for kw in other_src_kw):
            return None
        # Label must be short (it's a heading, not body content)
        if len(text) > 60:
            return None
        return text

    # Candidate 1: split right after the LAST direct child that contains a <br/>
    # anywhere in it (the common shape: "...content...<br/><br/>Label", label in
    # its own following span with no <br/> of its own).
    last_br_child_idx = -1
    for i, child in enumerate(children):
        if hasattr(child, 'find') and child.find('br'):
            last_br_child_idx = i

    split_idx = None
    post_text = None
    if last_br_child_idx >= 0:
        post_text = _match_label(children[last_br_child_idx + 1:])
        if post_text is not None:
            split_idx = last_br_child_idx + 1

    # Candidate 2 (fallback): the label span itself carries its own trailing
    # <br/> artifact (a common Google Docs export quirk), so it matched the
    # <br/> scan above and became the "separator" itself, leaving nothing after
    # it to treat as the label. Retry with the very last child in isolation.
    if split_idx is None and len(children) >= 2:
        post_text = _match_label(children[-1:])
        if post_text is not None:
            split_idx = len(children) - 1

    if split_idx is None:
        return None

    # Require non-empty content BEFORE the label
    pre_text = ''.join(
        c.get_text() if hasattr(c, 'get_text') else str(c)
        for c in children[:split_idx]
    ).strip()
    if not pre_text:
        return None

    # Build a copy of the tag with only the pre-label content, cut at the same
    # child index (re-parsing the same serialized string yields identical
    # child structure/order).
    pre_tag = BeautifulSoup(str(tag), 'lxml').find(tag.name)
    if pre_tag:
        pre_children_copy = list(pre_tag.children)
        for child in pre_children_copy[split_idx:]:
            child.extract()

    return (post_text.strip(), pre_tag)


def _get_trailing_tg_label(tag):
    """
    Detects a merged Google Docs paragraph where body content and a TG section
    label (e.g. "Бот (общий)") share a single <p> tag, separated by <br/> tags.
    Example DOM shape:
        <p> <span>...РЕКЛАМА...</span> <span><br/></span>
            <span style="font-weight:700">Бот (общий)</span> </p>

    Returns (label_name, pre_content_tag) when detected, otherwise None.
    """
    if not tag.find('br'):
        return None

    children = list(tag.children)

    def _match_label(candidate_children):
        """Return the label text if candidate_children's text matches the TG
        bot-label pattern and passes the length guard, else None."""
        text = ''.join(
            c.get_text() if hasattr(c, 'get_text') else str(c)
            for c in candidate_children
        ).strip()
        if not re.match(r'^бот\s*\(', text.lower()):
            return None
        if len(text) > 60:
            return None
        return text

    # Candidate 1: split right after the LAST direct child that contains a <br/>
    # anywhere in it (the common shape: "...content...<br/>Label", label in its
    # own following span with no <br/> of its own).
    last_br_child_idx = -1
    for i, child in enumerate(children):
        if hasattr(child, 'find') and child.find('br'):
            last_br_child_idx = i

    split_idx = None
    post_text = None
    if last_br_child_idx >= 0:
        post_text = _match_label(children[last_br_child_idx + 1:])
        if post_text is not None:
            split_idx = last_br_child_idx + 1

    # Candidate 2 (fallback): the label span itself carries its own trailing
    # <br/> artifact (a common Google Docs export quirk, e.g. "БОТ (общий)<br/>"
    # as the very last span), so it matched the <br/> scan above and became the
    # "separator" itself, leaving nothing after it to treat as the label. Retry
    # with the very last child in isolation.
    if split_idx is None and len(children) >= 2:
        post_text = _match_label(children[-1:])
        if post_text is not None:
            split_idx = len(children) - 1

    if split_idx is None:
        return None

    pre_text = ''.join(
        c.get_text() if hasattr(c, 'get_text') else str(c)
        for c in children[:split_idx]
    ).strip()
    if not pre_text:
        return None

    # Build a copy of the tag with only the pre-label content, cut at the same
    # child index (re-parsing the same serialized string yields identical
    # child structure/order).
    pre_tag = BeautifulSoup(str(tag), 'lxml').find(tag.name)
    if pre_tag:
        pre_children_copy = list(pre_tag.children)
        for child in pre_children_copy[split_idx:]:
            child.extract()

    return (post_text.strip(), pre_tag)


def _extract_residual_media(tag):
    """
    Meta-line paragraphs like "Тема: ..." / "Прехедер: ..." are normally
    consumed entirely (their text is captured into subject/preview and the
    paragraph itself is discarded). But sometimes an image is glued to the
    same paragraph as a sibling <span> (e.g. Google Docs export shape:
    <p><span>Прехедер: ...text...</span><span><img/></span></p>), with no
    blank paragraph separating them. In that case the image must not be
    thrown away with the rest of the meta line.

    Returns a copy of `tag` containing only the child nodes that carry an
    <img> (dropping the text-only/label children), or None if the tag has
    no image at all.
    """
    if not tag.find('img'):
        return None
    tag_copy = BeautifulSoup(str(tag), 'lxml').find(tag.name)
    if not tag_copy:
        return None
    kept_any = False
    for child in list(tag_copy.children):
        if hasattr(child, 'find') and child.find('img'):
            kept_any = True
            continue
        if hasattr(child, 'extract'):
            child.extract()
        # bare NavigableString children (stray whitespace) are left as-is;
        # they carry no image and are harmless if kept.
    return tag_copy if kept_any else None


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

        # If prev is just a lone "]" (Google Docs split the button's closing bracket
        # into its own span), the actual button text lives in earlier sibling(s) that
        # contain the matching "[". Walk back and wrap the whole [.."] run in one <a>,
        # instead of wrapping only the "]" span (which breaks button-bracket detection).
        if prev_text == ']':
            run = [prev]
            cursor = prev.previous_sibling
            found_open = False
            steps = 0
            while cursor is not None and steps < 8:
                if hasattr(cursor, 'name') and cursor.name == 'br':
                    break
                run.insert(0, cursor)
                cursor_text = cursor.get_text() if hasattr(cursor, 'get_text') else str(cursor)
                if '[' in cursor_text:
                    found_open = True
                    break
                cursor = cursor.previous_sibling
                steps += 1
            if found_open:
                new_a = soup.new_tag('a', href=url)
                run[0].insert_before(new_a)
                for node in run:
                    new_a.append(node.extract())
                sup.decompose()
                continue

        if isinstance(prev, NavigableString):
            # Wrap the text node in <a>
            new_a = soup.new_tag('a', href=url)
            prev.replace_with(new_a)
            new_a.string = str(prev)
        elif hasattr(prev, 'name') and prev.name in ('span', 'b', 'strong', 'i', 'em'):
            prev.wrap(soup.new_tag('a', href=url))
        elif hasattr(prev, 'name') and prev.name in ('p', 'div', 'h1', 'h2', 'h3', 'h4'):
            # <sup> is a block-level sibling (Google Docs puts it outside <p> in some tables).
            # Wrap the last significant child span inside the block with <a href>.
            last_span = None
            for child in reversed(list(prev.children)):
                if hasattr(child, 'name') and child.name in ('span', 'b', 'strong', 'i', 'em'):
                    if child.get_text(strip=True).replace('\xa0', '').strip():
                        last_span = child
                        break
            if last_span:
                last_span.wrap(soup.new_tag('a', href=url))

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

_WHITE_BG = {'#ffffff', '#fff', 'white', 'transparent', 'none'}

def extract_highlight_classes(soup):
    """
    Returns the set of CSS class names (from the doc's <style> block) that carry
    a non-white background-color — i.e. text highlighted with a marker in Google Docs.
    Used to detect which lines in a "Включаем:"-style list are actually marked,
    regardless of which specific color was used.
    """
    style_tag = soup.find('style')
    if not style_tag:
        return set()
    css_text = style_tag.get_text()
    highlight_cls = set()
    for selector, props in re.findall(r'(\.[\w-]+)\s*\{([^}]+)\}', css_text):
        m = re.search(r'background-color:\s*([^;]+)', props.lower())
        if m and m.group(1).strip() not in _WHITE_BG:
            highlight_cls.add(selector[1:])
    return highlight_cls


def tag_is_highlighted(tag, highlight_classes):
    """True if `tag` itself or any descendant carries a highlight class or an
    inline non-white background-color style."""
    if not highlight_classes:
        return False
    for t in [tag] + tag.find_all(True):
        classes = t.get('class', [])
        if isinstance(classes, str):
            classes = classes.split()
        if any(c in highlight_classes for c in classes):
            return True
        style = t.get('style', '')
        m = re.search(r'background-color:\s*([^;]+)', style.lower())
        if m and m.group(1).strip() not in _WHITE_BG:
            return True
    return False


_SEG_AI_RE = re.compile(r'\bнейро\b')
_SEG_DEV_RE = re.compile(r'\b(?:техно|технари|тех[-/ ]бизнес)\b')

def detect_segment_from_doc(other_tags, highlight_classes):
    """
    Reads the "Сегмент отправки (...)" header and its "Включаем:" list to figure out
    which audience this mailing targets.

    The parenthetical note next to the header names the marking convention used in
    that particular doc:
      "...оставить белым ненужное"      -> mode='include': highlighted = kept in
      "...выделить красным исключение"  -> mode='exclude': highlighted = excluded
    If the doc lists only ONE of Нейро/Технари at all (template trimmed down to the
    relevant option), that one wins outright — no highlighting needed to disambiguate.

    Returns 'ai', 'dev', or '' (base / no audience restriction) — '' is also the
    safe fallback whenever the signal is missing or genuinely ambiguous.
    """
    mode = None
    scan_start = 0
    for i, tag in enumerate(other_tags):
        text = get_text_content(tag).lower()
        if 'сегмент' in text and ('отправ' in text or '(' in text):
            for paren in re.findall(r'\(([^)]*)\)', text):
                if 'бел' in paren:
                    mode = 'include'
                elif 'красн' in paren:
                    mode = 'exclude'
            scan_start = i + 1
            break

    presence_ai = presence_dev = False
    highlighted_ai = highlighted_dev = False
    for tag in other_tags[scan_start:scan_start + 12]:
        if tag.name == 'table':
            break
        text = get_text_content(tag).lower()
        if _SEG_AI_RE.search(text):
            presence_ai = True
            highlighted_ai = highlighted_ai or tag_is_highlighted(tag, highlight_classes)
        elif _SEG_DEV_RE.search(text):
            presence_dev = True
            highlighted_dev = highlighted_dev or tag_is_highlighted(tag, highlight_classes)

    if presence_ai and not presence_dev:
        return 'ai'
    if presence_dev and not presence_ai:
        return 'dev'
    if not presence_ai and not presence_dev:
        return ''

    if mode == 'include':
        included_ai, included_dev = highlighted_ai, highlighted_dev
    elif mode == 'exclude':
        included_ai, included_dev = not highlighted_ai, not highlighted_dev
    elif highlighted_ai != highlighted_dev:
        included_ai, included_dev = highlighted_ai, highlighted_dev
    else:
        return ''

    if included_ai and included_dev:
        return ''
    if included_ai:
        return 'ai'
    if included_dev:
        return 'dev'
    return ''


def inline_gdoc_formatting(soup):
    """
    Google Docs HTML uses CSS class-based formatting (e.g. .c3 {font-weight:400}),
    plus bare tag-selector defaults (e.g. h3{font-weight:700} — the default
    "Heading 3" weight, which a run's own class only overrides when the author
    explicitly changed it, e.g. un-bolding one line inside an otherwise-bold
    heading via font-weight:400/normal).
    Extract those rules and apply them as inline styles so downstream parsers can
    detect them — including the EXPLICIT "not bold" case, not just "bold" — without
    needing to re-parse the <style> block themselves.
    """
    style_tag = soup.find('style')
    if not style_tag:
        return

    css_text = style_tag.get_text()
    bold_cls, normal_cls, italic_cls, center_cls, right_cls = set(), set(), set(), set(), set()
    bold_tags, normal_tags = set(), set()

    for selector, props in re.findall(r'(\.[\w-]+|h[1-6])\s*\{([^}]+)\}', css_text):
        p = props.lower().replace(' ', '')
        is_class = selector.startswith('.')
        name = selector[1:] if is_class else selector
        if 'font-weight:700' in p or 'font-weight:bold' in p:
            (bold_cls if is_class else bold_tags).add(name)
        elif 'font-weight:400' in p or 'font-weight:normal' in p:
            (normal_cls if is_class else normal_tags).add(name)
        if is_class:
            if 'font-style:italic' in p:
                italic_cls.add(name)
            if 'text-align:center' in p:
                center_cls.add(name)
            if 'text-align:right' in p:
                right_cls.add(name)

    if not (bold_cls or normal_cls or italic_cls or center_cls or right_cls or bold_tags or normal_tags):
        return

    for tag in soup.find_all(True):
        classes = tag.get('class', [])
        if isinstance(classes, str):
            classes = classes.split()
        existing = tag.get('style', '')
        additions = []
        if 'font-weight' not in existing:
            # Own class rule wins over the bare tag-name default (CSS specificity);
            # tag-name default (e.g. h3{font-weight:700}) is the fallback.
            if any(c in bold_cls for c in classes):
                additions.append('font-weight:700')
            elif any(c in normal_cls for c in classes):
                additions.append('font-weight:400')
            elif tag.name in bold_tags:
                additions.append('font-weight:700')
            elif tag.name in normal_tags:
                additions.append('font-weight:400')
        if classes:
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


def parse_doc_html(html_content, ai_hints=None):
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
    # Google Docs splits {First name} across 3 spans: <span>{</span><span>First name</span><span>}...
    # Consolidate to {first_name} before BS4 parsing so the variable is a single text node.
    html_content = re.sub(
        r'<span[^>]*>\{</span>\s*<span[^>]*>First\s+name</span>\s*<span([^>]*)>\}',
        r'{first_name}<span\1>',
        html_content,
        flags=re.IGNORECASE
    )
    # Normalize remaining {First name}, {First Name}, {FIRST NAME} etc. → {first_name}
    html_content = re.sub(r'\{first[\s_]name\}', '{first_name}', html_content, flags=re.IGNORECASE)

    soup = BeautifulSoup(html_content, 'lxml')
    highlight_classes = extract_highlight_classes(soup)
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
    doc_campaign_found = ''  # captured from "Кампания: slug" meta line (skipped tag)

    def process_block(tag):
        nonlocal current_section, subject, preview, sender, doc_campaign_found

        def _route(t):
            """Add a content tag to whichever section is currently open."""
            if current_section == 'tg_section' and tg_subsections:
                tg_subsections[-1]['blocks'].append(t)
            elif current_section == 'email_section' and email_subsections:
                email_subsections[-1]['blocks'].append(t)
            else:
                sections['other'].append(t)

        def _consume_leading_meta(tag_copy):
            """Mutates tag_copy in place: strips leading Тема:/Прехедер: meta-line
            children (extracting their values into subject/preview, only if not
            already set) plus any purely-empty spacer children (bare <br> artifacts).
            Stops at the first real content, keeping it and everything after it.
            Returns True if real content remains in tag_copy afterwards.

            Children are grouped by <br> boundaries before matching — not matched
            span-by-span in isolation — because Google Docs sometimes splits a label
            word across two adjacent spans with no <br> between them (e.g. spellcheck
            splitting "Тема" into "Тем"+"а"); matching each span alone would miss that
            and treat the trailing fragment as if real content had already started.
            An <img> found while still consuming stops the scan too (Google Docs
            sometimes glues a picture directly onto a meta line with no blank
            paragraph between them — must not be discarded).

            Shared by the 'subject'/'preview' meta-line handlers and the merged
            "Label: Тема: ...<br><br>content" email-header case, so a paragraph like
            "Другие источники: " + "Тема: X" + "the real first sentence" — all glued
            via <br><br> into one <p> by Google Docs — loses only the label/meta
            lines and keeps the real sentence instead of discarding the whole tag.
            """
            nonlocal subject, preview
            children = list(tag_copy.children)
            groups = []  # list of (children_in_group, joined_raw_text)
            group_children, group_text_parts = [], []
            for child in children:
                group_children.append(child)
                group_text_parts.append(child.get_text('', strip=False) if hasattr(child, 'get_text') else str(child))
                if hasattr(child, 'find') and child.find('br') is not None:
                    groups.append((group_children, ''.join(group_text_parts)))
                    group_children, group_text_parts = [], []
            if group_children:
                groups.append((group_children, ''.join(group_text_parts)))

            to_remove = []
            for grp_children, grp_raw in groups:
                text = ' '.join(grp_raw.replace('\xa0', ' ').split()).strip()
                if not text:
                    if any(hasattr(c, 'find') and c.find('img') for c in grp_children):
                        break
                    to_remove.extend(grp_children)
                    continue
                m_subj = re.match(r'^тема(?:\s+письма)?\s*:\s*(.+)', text, re.IGNORECASE)
                if m_subj:
                    if not subject:
                        subject = m_subj.group(1).strip()
                    to_remove.extend(grp_children)
                    continue
                m_prev = re.match(
                    r'^(?:прехедер|прехендер|прехэдер|превью|preview|preheader)\s*:\s*(.+)',
                    text, re.IGNORECASE)
                if m_prev:
                    if not preview:
                        preview = m_prev.group(1).strip()
                    to_remove.extend(grp_children)
                    continue
                break  # first real content group — stop consuming, keep it and the rest
            for child in to_remove:
                child.extract()
            return bool(get_text_content(tag_copy).strip())

        # Handle merged paragraph: body content + section label (e.g. "Другие источники")
        # in the same <p>, separated by <br/> tags.  Split them before any other detection
        # so that the CTA button stays in the current email section and the label correctly
        # opens a new one.
        if tag.find('br') and tag.name in ('p', 'li', 'h1', 'h2', 'h3', 'h4'):
            split_result = _get_trailing_email_label(tag)
            if split_result:
                label_name, pre_tag = split_result
                if pre_tag and get_text_content(pre_tag).strip():
                    if current_section == 'tg_section' and tg_subsections:
                        tg_subsections[-1]['blocks'].append(pre_tag)
                    elif current_section == 'email_section' and email_subsections:
                        email_subsections[-1]['blocks'].append(pre_tag)
                    else:
                        sections['other'].append(pre_tag)
                email_subsections.append({'name': label_name[:50], 'blocks': []})
                current_section = 'email_section'
                return

            split_result_tg = _get_trailing_tg_label(tag)
            if split_result_tg:
                label_name, pre_tag = split_result_tg
                if pre_tag and get_text_content(pre_tag).strip():
                    if current_section == 'tg_section' and tg_subsections:
                        tg_subsections[-1]['blocks'].append(pre_tag)
                    elif current_section == 'email_section' and email_subsections:
                        email_subsections[-1]['blocks'].append(pre_tag)
                    else:
                        sections['other'].append(pre_tag)
                tg_subsections.append({'name': label_name[:50], 'blocks': []})
                current_section = 'tg_section'
                return

        section_type = is_section_header(tag, ai_hints=ai_hints)

        if section_type == 'skip':
            raw_text = get_text_content(tag).strip()
            # Same text, but joined with an empty separator between spans instead of a
            # space — avoids "Тем"+"а" (a word Google Docs split across two spans, e.g.
            # for spellcheck) turning into "Тем а" and breaking the "тема:" match below.
            tight_text = ' '.join(tag.get_text('').replace('\xa0', ' ').split())
            # Capture sender name from "от кого: ..." / "отправитель: ..." meta line before discarding.
            if not sender:
                m = re.match(r'^(?:от\s+кого|отправитель)\s*[:\s]\s*(.+)', tight_text, re.IGNORECASE)
                if m:
                    val = m.group(1).strip()
                    # Google Docs sometimes collapses the blank line between "от кого: ..."
                    # and the next meta line (e.g. "Тема: ...") into this same paragraph via
                    # <br><br>, instead of a separate <p> — trim any such glued-on tail so it
                    # doesn't end up as part of the sender name.
                    val = re.sub(r'\s*тема(?:\s+письма)?\s*:.*$', '', val, flags=re.IGNORECASE).strip()
                    if 'зерокодер' not in val.lower():
                        val = val + ' из Зерокодера'
                    sender = val
            # Capture campaign slug from "Кампания: slug" meta line (before discarding)
            if not doc_campaign_found:
                m = re.match(r'^кампания\s*[:\s]\s*(.*)', raw_text, re.IGNORECASE)
                if m:
                    val = re.sub(r'^[-•]\s*', '', m.group(1).strip()).strip()
                    if val:
                        doc_campaign_found = val
            # Recover a "Тема: ..." subject line glued into this same meta paragraph
            # (see comment above) — otherwise it would be silently dropped along with
            # the rest of the discarded "от кого:"/meta content.
            if not subject:
                m = re.search(r'тема(?:\s+письма)?\s*:\s*(.+)', tight_text, re.IGNORECASE)
                if m:
                    subject = m.group(1).strip()
            return

        if section_type == 'email_section':
            name = get_text_content(tag).strip()
            # Capture sender from "От кого: ..." suffix embedded in the header line
            if not sender:
                m = re.search(r'от\s+кого\s*[:\s]\s*(.+)', name, re.IGNORECASE)
                if m:
                    val = m.group(1).strip()
                    if 'зерокодер' not in val.lower():
                        val = val + ' из Зерокодера'
                    sender = val
            # Trim "От кого: ..." suffix from sub-variant names
            name = re.sub(r'\s+от кого.*', '', name, flags=re.IGNORECASE).strip()
            email_subsections.append({'name': name[:50], 'blocks': []})
            current_section = 'email_section'

            # If this header is "merged" (label glued via <br><br> to more content in the
            # same <p> — e.g. "Другие источники: " + "Тема: ..." + the real first sentence,
            # all as separate sibling spans), don't discard the whole tag: drop only the
            # leading children that are THEMSELVES recognized labels/numbered structural
            # headings, then hand off to _consume_leading_meta() for any glued Тема:/
            # Прехедер: lines, keeping whatever real content remains as the section's first
            # block.
            #
            # Must recognize the label rather than blindly taking "the first non-empty
            # child" — some documents glue the numbered structural heading ("4. Контент
            # письма", metadata, not a channel label) in front of the REAL label
            # ("ПОЧТА (1 клик)") as a separate preceding child, e.g. "<span>4. Контент
            # письма<br><br></span><span>ПОЧТА (1 клик)</span>". Taking the first child
            # unconditionally would strip only "4. Контент письма" and then treat
            # "ПОЧТА (1 клик)" itself as real content, leaking the label into the email
            # body — confirmed as a regression while testing this fix (2026-07-24) on
            # an existing document unrelated to the case this was written for.
            #
            # If nothing at the front matches a recognized pattern, stay conservative and
            # don't touch the tag at all — better to keep the pre-existing "discard the
            # whole merged header" behavior than risk keeping something we can't identify.
            if tag.find('br'):
                tag_copy = BeautifulSoup(str(tag), 'lxml').find(tag.name)
                if tag_copy:
                    to_remove = []
                    for child in list(tag_copy.children):
                        child_text = (child.get_text(' ', strip=True) if hasattr(child, 'get_text')
                                      else str(child)).replace('\xa0', ' ').strip()
                        if not child_text:
                            to_remove.append(child)
                            continue
                        child_lower = child_text.lower()
                        is_numbered_heading = bool(re.match(r'^\d+[.)]\s', child_lower))
                        is_known_label = (
                            child_lower in ('почта', 'письмо')
                            or child_lower.startswith(('почта ', 'почта(', 'письмо ', 'письмо('))
                            or any(child_lower.startswith(kw) for kw in OTHER_SRC_LABEL_KW)
                        )
                        if is_numbered_heading or is_known_label:
                            to_remove.append(child)
                            continue
                        break  # first child that isn't a recognized label/heading — stop
                    for child in to_remove:
                        child.extract()
                    if to_remove and _consume_leading_meta(tag_copy):
                        email_subsections[-1]['blocks'].append(tag_copy)
            return
        if section_type == 'tg_section' and tag.name == 'li':
            # Suppress premature TG detection from document outline/config lists.
            # Outline items are always <li> elements; real section headers are <p> or headings.
            has_email_content = any(sub['blocks'] for sub in email_subsections)
            if not has_email_content:
                section_type = None
        if section_type == 'tg_section':
            full_text = get_text_content(tag).strip()
            tg_subsections.append({'name': full_text[:50], 'blocks': []})
            current_section = 'tg_section'
            # If merged paragraph (label + content in same <p>), preserve content without label.
            # "Merged" means the paragraph has more than just the section-label word.
            # We detect this by checking that the tag contains at least two distinct spans
            # (the label span and the content span), or the text is longer than the label alone.
            tg_label_words = {'телеграм', 'telegram', 'тг', 'tg', 'max', 'бот', 'bot', 'нейрокот', 'помощник'}
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
                        span_text = get_text_content(span).strip()
                        span_text_alpha = ''.join(ch for ch in span_text if ch.isalpha()).lower()
                        # Exact match: "Телеграм", "Telegram", "Max", "Бот"
                        if span_text_alpha == first_word_alpha:
                            span.decompose()
                            break
                        # Short multi-word label starting with тг/tg or бот/bot: "ТГ бот (ГК)", "БОТ (1 клик)"
                        if (first_word_alpha in ('тг', 'tg', 'бот', 'bot') and
                                span_text_alpha.startswith(first_word_alpha) and
                                len(span_text) <= 40):
                            span.decompose()
                            break
                    # Remove empty wrapper tags left after span removal (e.g. <b></b>)
                    for empty_wrapper in tag_copy.find_all(['b', 'strong', 'i', 'em', 'u', 's']):
                        if not empty_wrapper.get_text(strip=True) and not empty_wrapper.find():
                            empty_wrapper.decompose()
                    if get_text_content(tag_copy).strip():
                        tg_subsections[-1]['blocks'].append(tag_copy)
            return
        if section_type in ('subject', 'preview'):
            # _consume_leading_meta() extracts subject/preview itself from whichever
            # leading groups match Тема:/Прехедер: — handles both the plain single-line
            # case and Google Docs gluing extra real content onto the same paragraph via
            # <br><br> (e.g. "Тема: X!<br><br>Разберем по шагам..." — that trailing
            # sentence used to be silently swallowed into the subject/discarded outright).
            tag_copy = BeautifulSoup(str(tag), 'lxml').find(tag.name)
            if tag_copy and _consume_leading_meta(tag_copy):
                _route(tag_copy)
            else:
                # Fallback: an image glued to the meta line with no <br> group
                # boundary my new scan would recognize — same safety net as before.
                residual = _extract_residual_media(tag)
                if residual is not None:
                    _route(residual)
            return

        # Also detect inline subject/preview in the 'other' or 'email_section' when
        # the keyword and value are on the SAME line (e.g. "Тема: My Subject")
        # Normalize "тема :" → "тема:" to handle span-split formatting artifacts.
        txt = get_text_content(tag).strip()
        txt_lower = re.sub(r'\s+:', ':', txt.lower())
        # Always consume subject-keyword paragraphs — even if subject is already set —
        # so they never bleed into the email body as visible content.
        for kw in ['тема письма:', 'тема:', 'темы:', 'subject:']:
            if txt_lower.startswith(kw):
                if not subject:
                    raw0 = ' '.join(tag.get_text('').replace('\xa0', ' ').split())
                    subject = re.sub(r'^[^:]+\s*:\s*', '', raw0, count=1).strip()
                residual = _extract_residual_media(tag)
                if residual is not None:
                    _route(residual)
                return
        if not preview:
            for kw in ['превью:', 'прехедер:', 'preview:', 'preheader:']:
                if txt_lower.startswith(kw):
                    raw0 = ' '.join(tag.get_text('').replace('\xa0', ' ').split())
                    preview = re.sub(r'^[^:]+\s*:\s*', '', raw0, count=1).strip()
                    residual = _extract_residual_media(tag)
                    if residual is not None:
                        _route(residual)
                    return

        _route(tag)

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
                if any(is_section_header(li, ai_hints=ai_hints) for li in lis):
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
            if txt or t.name in ('ul', 'ol', 'table') or t.find('img'):
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

    # Auto-detect segment from the "Сегмент отправки" block, using highlight-color
    # marking rather than plain keyword search — both "Нейро" and "Технари" are
    # always present as template text, so only the highlighting tells us which
    # one(s) are actually selected for this send. See detect_segment_from_doc().
    segment = detect_segment_from_doc(sections['other'], highlight_classes)

    # Auto-detect utm_campaign (тег активности) and date (дата отправки) from planning section.
    # Two doc formats:
    #   Format 1 (labeled):   list item = "Тег активности: slug"  /  "Дата отправки: dd.mm"
    #   Format 2 (positional): list items = ["campaign-slug", "dd.mm", "HH:MM"]
    from datetime import datetime as _dt
    def _norm_date(raw):
        raw = re.sub(r'\s*\.\s*', '.', raw.strip())
        dm = re.match(r'^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?', raw)
        if dm:
            day = f'{int(dm.group(1)):02d}'
            mon = f'{int(dm.group(2)):02d}'
            y = dm.group(3) or f'{_dt.now().year % 100:02d}'
            if len(y) == 4:
                y = y[2:]
            return f'{day}.{mon}.{y}'
        return raw

    doc_campaign = doc_campaign_found  # from "Кампания: slug" planning label (captured during skip)
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
            elif not doc_campaign and re.match(r'^[a-zA-Zа-яА-ЯёЁ][a-zA-Zа-яА-ЯёЁ0-9\-_.]*$', item):
                doc_campaign = item

    # Pass 2b: date not found in li_items — search p_items (date may be a <p>, not a <li>)
    if not doc_date:
        for item in p_items:
            if not item or re.match(r'^\d{1,2}:\d{2}$', item):
                continue
            dm = re.search(r'(?<![.\d])(\d{1,2}\.\d{1,2})(?![.\d])', item)
            if dm:
                day_str, mon_str = dm.group(1).split('.')
                if 1 <= int(day_str) <= 31 and 1 <= int(mon_str) <= 12:
                    doc_date = _norm_date(dm.group(1))
                    break

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

def _style_is_bold(style):
    """
    Read an inline CSS `style` string and report what it says about font-weight.
    Returns:
      True  — explicitly bold (font-weight:700 / bold)
      False — explicitly NOT bold (font-weight:400 / normal) — an active override
      None  — style says nothing about weight either way (inherit from context)
    Shared by every "Google Doc → channel HTML" converter so bold detection
    (and, critically, the ability to *cancel* inherited bold) behaves identically
    across email, TG and Neurocat markdown.
    """
    if not style:
        return None
    s = style.lower().replace(' ', '')
    if 'font-weight:700' in s or 'font-weight:bold' in s:
        return True
    if 'font-weight:400' in s or 'font-weight:normal' in s:
        return False
    return None

def _style_is_italic(style):
    """True if an inline CSS `style` string explicitly marks italic."""
    if not style:
        return False
    s = style.lower().replace(' ', '')
    return 'font-style:italic' in s

def elem_inner_html_for_email(tag, _in_bold=False, _wrapped=False, link_color='#1445ea'):
    """
    Convert a BS4 tag's contents to email-safe HTML:
    - keep <b>, <strong>, <i>, <em>, <a href>
    - decode Google redirect URLs
    - preserve GC variables

    _in_bold: whether this text should render bold by default (ambient context).
    _wrapped: whether a physical <b> tag already encloses this point in the
              output being assembled (so we don't need to add another one).
    A span with an explicit font-weight:400/normal override cancels inherited
    bold for its own subtree, regardless of what the ambient context says.
    """
    parts = []
    for child in tag.children:
        if isinstance(child, NavigableString):
            text = str(child)
            if _in_bold and not _wrapped and text.strip():
                parts.append(f'<b>{text}</b>')
            else:
                parts.append(text)
        elif isinstance(child, Tag):
            name = child.name
            if name in ('b', 'strong'):
                inner = elem_inner_html_for_email(child, _in_bold=True, _wrapped=True, link_color=link_color)
                parts.append(f'<b>{inner}</b>')
            elif name in ('i', 'em'):
                inner = elem_inner_html_for_email(child, _in_bold=_in_bold, _wrapped=_wrapped, link_color=link_color)
                parts.append(f'<i>{inner}</i>')
            elif name == 'a' and child.get('href'):
                inner = elem_inner_html_for_email(child, _in_bold=_in_bold, _wrapped=_wrapped, link_color=link_color)
                href = decode_google_redirect(child['href'])
                parts.append(f'<a href="{href}" target="_blank" style="color:{link_color};text-decoration:underline">{inner}</a>')
            elif name == 'sup':
                pass
            elif name == 'span':
                style = child.get('style', '')
                bold_state = _style_is_bold(style)
                eff_bold = _in_bold if bold_state is None else bold_state
                is_italic = _style_is_italic(style)
                need_wrap = eff_bold and not _wrapped
                inner = elem_inner_html_for_email(child, _in_bold=eff_bold, _wrapped=_wrapped or need_wrap, link_color=link_color)
                if need_wrap:
                    inner = f'<b>{inner}</b>'
                if is_italic:
                    inner = f'<i>{inner}</i>'
                parts.append(inner)
            elif name == 'br':
                br_style = child.get('style', '').replace(' ', '')
                if 'display:none' not in br_style:
                    parts.append('<br>')
            else:
                inner = elem_inner_html_for_email(child, _in_bold=_in_bold, _wrapped=_wrapped, link_color=link_color)
                parts.append(inner)
    # NOTE: trailing <br> stripping used to happen right here, per recursion level
    # (i.e. per span/b/i/a node). That's wrong: Google Docs sometimes splits a
    # single blank-line break across two adjacent SIBLING spans, one <br> each
    # (e.g. "...👋<br>" then a separate "<br>" span before the next real content).
    # Stripping per-node treats each of those spans as if it were the tail of the
    # whole paragraph, deleting both breaks even though real text follows later in
    # the parent's sibling list — gluing the two lines together with no separator.
    # The real "trailing junk <br> at the true end of the paragraph" cleanup now
    # happens once, on the fully assembled string, in tag_to_email_p().
    return ''.join(parts)

# Matches {first_name} GC variable (plain text, not inside HTML tags)
_FIRST_NAME_VAR = r'\{first_name\}'

def _strip_first_name(text):
    """Remove {first_name} GC variable with smart punctuation/capitalization cleanup.

    Handles patterns like:
      'Привет, {first_name}!'          → 'Привет!'
      '{first_name}, привет!'          → 'Привет!'
      '{first_name}! Текст'            → 'Текст'
      'Привет {first_name}!'           → 'Привет!'
      '<b>{first_name}, текст</b>'     → '<b>Текст</b>'
      'Строка1\\n{first_name}, текст'   → 'Строка1\\nТекст'  (start of a new line, not just start of string)
    """
    text = text.replace('\xa0', ' ')

    def _cap(s):
        s = s.strip()
        if not s:
            return s
        # Skip leading HTML tags to find and capitalize the first actual letter
        return re.sub(
            r'^((?:<[^>]+>)*\s*)([а-яёa-z])',
            lambda m: m.group(1) + m.group(2).upper(),
            s,
            flags=re.UNICODE
        )

    def _strip_line_start(line):
        # {first_name} right after opening tag(s) at line start: <b>{first_name}, текст → <b>Текст
        line, n = re.subn(
            r'^((?:\s*<(?:b|i|em|strong|span|u|s)[^>]*>)+)\s*' + _FIRST_NAME_VAR + r'\s*[,!?.;:\-–—]?\s*',
            r'\1', line
        )
        if n:
            return _cap(line)

        # {first_name} at bare line start + optional separator → remove and capitalize what follows
        line, n = re.subn(r'^\s*' + _FIRST_NAME_VAR + r'\s*[,!?.;:\-–—]?\s*', '', line)
        if n:
            return _cap(line)

        return line

    # Apply the "start" handling per line — {first_name} that opens a new line (right
    # after \n, e.g. a heading's non-bold second line in Нейрокот) is a line/sentence
    # start just like {first_name} at the very start of the whole string, and should get
    # the same capitalization treatment. Occurrences mid-line are untouched here and fall
    # through to the mid-sentence patterns below.
    text = '\n'.join(_strip_line_start(line) for line in text.split('\n'))

    # ", {first_name}" in the middle → remove comma + variable, preserve following punctuation
    text = re.sub(r'\s*,\s*' + _FIRST_NAME_VAR, '', text)
    # " {first_name}," — space before variable, optional separator after
    text = re.sub(r'\s+' + _FIRST_NAME_VAR + r'\s*[,!?.;:\-–—]?', ' ', text)

    # Any remaining bare occurrence
    text = re.sub(_FIRST_NAME_VAR, '', text)

    # Clean up leftover leading punctuation (including after opening tag: <b>, текст → <b>текст)
    text = re.sub(r'^((?:<[^>]+>)+)\s*[,!?.;:\-–—]\s*', r'\1', text)
    text = re.sub(r'^[,\s]+', '', text)
    text = re.sub(r'[^\S\n]{2,}', ' ', text)

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
    # Strip leading/trailing <br> tags — Google Docs artifacts at paragraph boundaries.
    # This is where trailing-<br> cleanup happens now (once, on the fully assembled
    # string) instead of per recursion level inside elem_inner_html_for_email() — see
    # the comment there. The trailing pattern also allows the <br> run to be followed
    # by closing inline tags before the true end of the string (e.g. "...text<br><br></b>"),
    # since a trailing junk <br> is often still wrapped in the last <b>/<i>/<a> of the
    # paragraph rather than sitting bare at the very end.
    inner = re.sub(r"^(\s*<br\s*/?>\s*)+", "", inner)
    inner = re.sub(r"(?:<br\s*/?>\s*)+(?=(?:</[a-zA-Z]+>\s*)*$)", "", inner)
    # Strip <br> that appears right after opening inline tag(s): <i><br/>text → <i>text
    inner = re.sub(r"^((?:\s*<(?:b|i|em|strong|span|u|s|a)[^>]*>\s*)+)<br\s*/?>", r"\1", inner)
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
        # Capitalize first visible letter, skipping any leading HTML tags
        inner = re.sub(
            r'^((?:\s*<[^>]+>)*\s*)([а-яёa-z])',
            lambda m: m.group(1) + m.group(2).upper(),
            inner,
            flags=re.UNICODE
        )
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
_BTN_BRACKET_RE = re.compile(r'^\s*([^\w\[\]]*)\[([^\]]+)\][^\w\[\]]*\s*$', re.DOTALL)

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

def render_block_from_tags(tags, channel_key, campaign, date, segment='', images=None, user_img_idx=None, uploaded_urls=None):
    """
    Given a list of BS4 tags from one logical block, build an email table row.
    Detects [BUTTON TEXT] CTAs, checkmark lists, feature emoji, etc.
    Returns (html_string, meta_dict) or (None, None).
    uploaded_urls: optional list to collect YC Storage URLs uploaded during rendering.
    """
    tags = [t for t in tags if not _is_reklama(t)]
    if not tags:
        return None, None

    combined_text = ' '.join(t.get_text(strip=True) for t in tags)
    first_text = tags[0].get_text(strip=True) if tags else ''
    preview_text = combined_text[:80]

    # Build an ordered items list: {'kind': 'tag', 'tag': tag} or {'kind': 'btn', 'text': str, 'url': str}
    items = []
    def _btn_norm(s):
        """Normalize button text for duplicate detection (collapses nbsp and whitespace)."""
        return re.sub(r'[\xa0\s]+', ' ', s).strip()
    def _btn_already_exists(text):
        return _btn_norm(text) in {_btn_norm(it['text']) for it in items if it['kind'] == 'btn'}

    for tag in tags:
        tag_text = tag.get_text(strip=True)
        # Strip trailing AND inline Google Docs footnote markers before button detection.
        # _strip_button_footnotes also handles the pattern "[КНОПКА][b]Дополнительный текст"
        # where [b] is an inline <sup> footnote ref placed between the button and body text.
        tag_text_clean = _strip_button_footnotes(tag_text)

        # Case -1: paragraph/heading containing only an image (no text). Headings are
        # included because Google Docs sometimes wraps a pasted screenshot in <h1-4>
        # (via a sizing <span>) instead of <p>.
        if not tag_text_clean and tag.name in ('p', 'h1', 'h2', 'h3', 'h4'):
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
                        # Remove any <a> elements wrapping the button (Google Docs wraps spans in <a href>)
                        for _ba in list(tag_copy.find_all('a', href=True)):
                            if _BTN_BRACKET_RE.match(_strip_trailing_footnotes(_ba.get_text(strip=True))):
                                _ba.decompose()
                        # Remove the first span (button) and any <br> and <sup> siblings
                        for child in list(tag_copy.children):
                            if not hasattr(child, 'name') or child.name is None:
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
            a_text_clean = _strip_trailing_footnotes(a.get_text(strip=True))
            m2 = _BTN_BRACKET_RE.match(a_text_clean)
            # Case 2b: no brackets, but the link is the ENTIRE paragraph (common with
            # Google Docs comment-resolved CTAs, e.g. "👉 ЗАБРОНИРОВАТЬ МЕСТО" with the
            # URL attached via a doc comment instead of [bracket] text) — a paragraph
            # that is nothing but one link is unambiguously a button, brackets or not.
            is_whole_link_btn = (
                not m2 and len(tag.find_all('a', href=True)) == 1
                and a_text_clean and _btn_norm(a_text_clean) == _btn_norm(tag_text_clean)
            )
            if m2 or is_whole_link_btn:
                if m2:
                    a_prefix = m2.group(1).strip()
                    btn_inner = m2.group(2).strip()
                    btn_label = f'{a_prefix} {btn_inner}'.strip() if a_prefix else btn_inner
                else:
                    a_prefix = ''
                    btn_label = a_text_clean
                # Also pick up any emoji-only text sibling that precedes the <a> in the tag
                added_prefix = ''
                if not a_prefix and m2:
                    full_m = _BTN_BRACKET_RE.match(tag_text_clean)
                    if full_m and full_m.group(1).strip():
                        added_prefix = full_m.group(1).strip()
                        btn_label = f'{added_prefix} {btn_label}'.strip()
                btn_href = a.get('href', '#')
                tag_copy = BeautifulSoup(str(tag), 'lxml').find(tag.name)
                pre_tag = post_tag = None
                if tag_copy:
                    # Locate the matching button anchor inside the copy and split its
                    # siblings into "before" and "after" groups, so text that comes after
                    # the button in the source (e.g. a line following the CTA link) stays
                    # after the button instead of being glued to the text before it.
                    btn_anchor_copy = None
                    copy_links = tag_copy.find_all('a', href=True)
                    for ba in copy_links:
                        ba_text_clean = _strip_trailing_footnotes(ba.get_text(strip=True))
                        if _BTN_BRACKET_RE.match(ba_text_clean):
                            btn_anchor_copy = ba
                            break
                        if (is_whole_link_btn and len(copy_links) == 1 and ba_text_clean
                                and _btn_norm(ba_text_clean) == _btn_norm(tag_text_clean)):
                            btn_anchor_copy = ba
                            break
                    pre_children, post_children = [], []
                    if btn_anchor_copy is not None:
                        bucket = pre_children
                        for child in list(tag_copy.children):
                            if child is btn_anchor_copy:
                                bucket = post_children
                                continue
                            bucket.append(child)
                    else:
                        pre_children = list(tag_copy.children)

                    # Drop a trailing emoji-only sibling from "pre" if it was folded into
                    # btn_label above, so it isn't rendered twice.
                    if added_prefix and pre_children:
                        last = pre_children[-1]
                        last_text = last.get_text(strip=True) if hasattr(last, 'get_text') else str(last).strip()
                        if last_text == added_prefix:
                            pre_children = pre_children[:-1]

                    def _build_side_tag(children):
                        if not children:
                            return None
                        side = BeautifulSoup(f'<{tag_copy.name}></{tag_copy.name}>', 'lxml').find(tag_copy.name)
                        # Preserve the original tag's attributes (class/style — e.g. text-align)
                        side.attrs = dict(tag_copy.attrs)
                        for c in children:
                            side.append(c.extract() if hasattr(c, 'extract') else c)
                        return side if _strip_trailing_footnotes(side.get_text(strip=True)) else None

                    pre_tag = _build_side_tag(pre_children)
                    post_tag = _build_side_tag(post_children)
                    if pre_tag is not None:
                        items.append({'kind': 'tag', 'tag': pre_tag})
                if not _btn_already_exists(btn_label):
                    items.append({'kind': 'btn', 'text': btn_label, 'url': btn_href})
                if post_tag is not None:
                    items.append({'kind': 'tag', 'tag': post_tag})
                found_btn_anchor = True
                break
        if not found_btn_anchor:
            # Case 2.5: brackets are OUTSIDE the <a> tag — e.g. "[<a>BUTTON</a>]Trailing text".
            # a.get_text() has no brackets so Case 2 misses it; tag_text_clean has [a_text].
            for a in tag.find_all('a', href=True):
                a_text = _strip_trailing_footnotes(a.get_text(strip=True))
                if a_text and f'[{a_text}]' in tag_text_clean:
                    btn_href = a.get('href', '#')
                    bracket_end = tag_text_clean.index(f'[{a_text}]') + len(f'[{a_text}]')
                    trailing = tag_text_clean[bracket_end:].strip()
                    if trailing:
                        trailing_soup = BeautifulSoup(f'<p>{trailing}</p>', 'lxml')
                        trailing_tag = trailing_soup.find('p')
                        if trailing_tag:
                            items.append({'kind': 'tag', 'tag': trailing_tag})
                    if not _btn_already_exists(a_text):
                        items.append({'kind': 'btn', 'text': a_text, 'url': btn_href})
                    found_btn_anchor = True
                    break
        if not found_btn_anchor:
            # Case 3: raw fallthrough. Detect "[BUTTON]trailing text" where BUTTON was
            # already added to items — strip the bracket prefix, keep only trailing text.
            m_pfx = re.match(r'^\s*([^\w\[\]]*)\[([^\]]+)\](.+)', tag_text_clean, re.DOTALL)
            if m_pfx and m_pfx.group(3).strip() and _btn_already_exists(m_pfx.group(2)):
                trailing = m_pfx.group(3).strip()
                trailing_tag = BeautifulSoup(f'<p>{trailing}</p>', 'lxml').find('p')
                if trailing_tag:
                    items.append({'kind': 'tag', 'tag': trailing_tag})
            else:
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
                # Try YC upload first; fall back to user-provided URL if credentials missing.
                if img_src.startswith('data:image'):
                    yc_url = _upload_image_to_yc(img_src)
                    if yc_url:
                        img_src = yc_url
                        item['src'] = img_src
                        if uploaded_urls is not None:
                            uploaded_urls.append(yc_url)
                    elif images and user_img_idx is not None and user_img_idx[0] < len(images):
                        img_src = images[user_img_idx[0]]
                        user_img_idx[0] += 1
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
        # Also track how many rendered paragraphs appear before the first button,
        # so api_assemble_email can restore the original paragraph/button order.
        text_parts = []
        btn_position = 0
        _first_btn_found = False
        for item in items:
            if item['kind'] == 'tag':
                ph = tag_to_email_p(item['tag'], channel_key, campaign, date, color='#ffffff', link_color='#e1fb52', segment=segment)
                if ph:
                    text_parts.append(ph)
                    if not _first_btn_found:
                        btn_position += 1
            elif item['kind'] == 'btn':
                _first_btn_found = True
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
            'btn_position': btn_position,
            'image_url': img_url_meta,
            'preview_text': preview_text,
        }
        return html, meta

    # Non-CTA blocks: render all tag items as paragraphs
    # Handle image items (outside CTA context) — may be combined with text
    img_items = [i for i in items if i['kind'] == 'img']
    if img_items:
        img_item = img_items[0]
        img_src = img_item['src']
        if img_src.startswith('data:image'):
            # Try YC upload first; fall back to user-provided URL if credentials missing.
            yc_url = _upload_image_to_yc(img_src)
            if yc_url:
                img_src = yc_url
                if uploaded_urls is not None:
                    uploaded_urls.append(yc_url)
            elif images and user_img_idx is not None and user_img_idx[0] < len(images):
                img_src = images[user_img_idx[0]]
                user_img_idx[0] += 1
            else:
                img_src = ''
        if img_src.startswith('http'):
            tag_items = [i for i in items if i['kind'] == 'tag']
            if not tag_items:
                return block_image_center(img_src), {
                    'type': 'block_image',
                    'image_url': img_src,
                    'paragraphs_html': '', 'btn_text': '', 'btn_url_utm': '',
                    'preview_text': 'Картинка',
                }
            # Image + text: combine into one white block
            p_parts_img = []
            for item in tag_items:
                tag = item['tag']
                if tag.name in ('ul', 'ol'):
                    for li in tag.find_all('li'):
                        inner = elem_inner_html_for_email(li)
                        inner = inject_utm_in_html(inner, channel_key, campaign, date, segment=segment)
                        s = ("margin:0 0 6px 0;padding-left:20px;font-family:roboto,'helvetica neue',"
                             "helvetica,arial,sans-serif;line-height:27px;color:#333333;font-size:18px")
                        p_parts_img.append(f'<p style="{s}">• {inner}</p>')
                else:
                    ph = tag_to_email_p(tag, channel_key, campaign, date, segment=segment)
                    if ph:
                        p_parts_img.append(ph)
            paragraphs_img = strip_trailing_empty_paragraphs('\n'.join(p_parts_img))
            return block_image_with_text(img_src, paragraphs_img), {
                'type': 'block_image_text',
                'image_url': img_src,
                'paragraphs_html': paragraphs_img,
                'btn_text': '', 'btn_url_utm': '',
                'preview_text': preview_text,
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


def _strip_reklama_from_tag(tag):
    """
    Return a copy of tag with РЕКЛАМА/ИНН legal-notice spans removed.
    When a paragraph mixes real content (text + CTA) with the legal notice block
    in a single <p>, the generator's 'skip if РЕКЛАМА' check would drop the whole
    paragraph.  This function strips only the legal-notice spans so the real
    content survives.  Returns None if nothing remains after stripping.
    """
    cloned = BeautifulSoup(str(tag), 'lxml').find(tag.name)
    if not cloned:
        return None
    for span in list(cloned.find_all('span')):
        t = span.get_text(strip=True)
        if _REKLAMA_RE.search(t) or 'ИНН 9715401631' in t:
            span.decompose()
    return cloned if cloned.get_text(strip=True) else None


def _cell_para_html(cell, channel_key, campaign, date, font_size=18, segment=''):
    """Extract email-paragraph HTML from a table cell (for 2-col detection).
    Returns (text_html, buttons, btn_position) where buttons = list of (btn_text, btn_url)
    and btn_position is the number of text paragraphs that appeared before the first
    button (None if the cell has no button), so callers can restore paragraphs that
    come after the button in the source doc instead of always rendering them first.
    Paragraphs matching [TEXT] with a hyperlink are extracted as buttons.
    """
    ctags = [t for t in cell.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'ul', 'ol'])
             if t.get_text(strip=True) and not _is_reklama(t)]
    parts = []
    buttons = []
    btn_position = None
    for tag in ctags:
        text = tag.get_text(strip=True)
        text_clean = _strip_trailing_footnotes(text)
        # Detect [BUTTON TEXT] or EMOJI [BUTTON TEXT] — extract as button even without a link
        m_btn = _BTN_BRACKET_RE.match(text_clean)
        if m_btn:
            link = tag.find('a', href=True)
            prefix = m_btn.group(1).strip()
            btn_inner = m_btn.group(2).strip()
            btn_text = f'{prefix} {btn_inner}'.strip() if prefix else btn_inner
            raw_url = decode_google_redirect(link.get('href', '#') if link else '#')
            btn_url = build_utm_url(raw_url, channel_key, campaign, date, segment)
            buttons.append((btn_text, btn_url))
            if btn_position is None:
                btn_position = len(parts)
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
            if btn_position is None:
                btn_position = len(parts)
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
    return '\n'.join(parts), buttons, btn_position

def _split_paras_at(html_str, position):
    """Split joined paragraph HTML into (pre, post) at the given paragraph index.
    Mirrors the pre/post split used for block_blue_cta so text after a button
    in the source doc stays after the button instead of jumping above it."""
    if not html_str.strip() or position is None:
        return html_str, ''
    soup_ph = BeautifulSoup(html_str, 'html.parser')
    paras = soup_ph.find_all(['p', 'ul', 'ol'])
    paras_html = [str(p) for p in paras if str(p).strip()] if paras else [html_str]
    pre = '\n'.join(paras_html[:position])
    post = '\n'.join(paras_html[position:])
    return pre, post

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
    uploaded_urls = []  # collects YC Storage URLs uploaded during this generation

    # Normalize subject for deduplication — skip if first paragraph repeats it
    _subject_norm = re.sub(r'\s+', ' ', subject or '').strip()
    _first_content_checked = [False]

    def flush_pending():
        if not pending_tags:
            return
        row, meta = render_block_from_tags(list(pending_tags), channel_key, campaign, date, segment, images=images, user_img_idx=user_img_idx, uploaded_urls=uploaded_urls)
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
                el_txt_no_label = re.sub(
                    r'^(?:тема\s*(?:письма)?\s*:|subject\s*:)\s*', '', el_txt, flags=re.IGNORECASE
                ).strip()
                if el_txt == _subject_norm or el_txt_no_label == _subject_norm:
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

                # A lone pending image (empty-text <p> wrapping an <img>, queued by the
                # 'p' branch below while it waits for following text to merge into) should
                # not be flushed alone just because the next sibling happens to be a table
                # instead of a plain <p>. If the table has plain body text and no button,
                # merge the pending image with the table's content into one block_image_text
                # — mirrors the has_inner_btn merge case just below, for the text case.
                pending_is_lone_image = (
                    len(pending_tags) == 1
                    and pending_tags[0].name == 'p'
                    and not pending_tags[0].get_text(strip=True).replace('\xa0', '').strip()
                    and bool(pending_tags[0].find('img'))
                )

                if has_inner_btn and pending_tags and not has_inner_text:
                    # Table has ONLY buttons (no body text of its own) —
                    # merge pre-table paragraphs into the CTA block
                    combined = list(pending_tags) + inner
                    pending_tags.clear()
                    row, meta = render_block_from_tags(combined, channel_key, campaign, date, segment, images=images, user_img_idx=user_img_idx, uploaded_urls=uploaded_urls)
                    if row:
                        raw_blocks.append((row, meta))
                elif pending_is_lone_image and not has_inner_btn and inner:
                    # Table has plain body text, no button — merge the lone pending image
                    # with the table's paragraphs into a single block_image_text.
                    combined = list(pending_tags) + inner
                    pending_tags.clear()
                    row, meta = render_block_from_tags(combined, channel_key, campaign, date, segment, images=images, user_img_idx=user_img_idx, uploaded_urls=uploaded_urls)
                    if row:
                        raw_blocks.append((row, meta))
                else:
                    # Table has its own body text, or no button — flush pending separately
                    flush_pending()
                    if inner:
                        row, meta = render_block_from_tags(inner, channel_key, campaign, date, segment, images=images, user_img_idx=user_img_idx, uploaded_urls=uploaded_urls)
                        if row:
                            raw_blocks.append((row, meta))
                return

            # 2-column table: flush pending first, then process
            flush_pending()
            left_html,  left_btns,  left_btn_pos  = _cell_para_html(two_col_cells[0], channel_key, campaign, date, font_size=16, segment=segment)
            right_html, right_btns, right_btn_pos = _cell_para_html(two_col_cells[1], channel_key, campaign, date, font_size=16, segment=segment)
            l_img = two_col_cells[0].find('img')
            r_img = two_col_cells[1].find('img')
            l_src = l_img.get('src', '') if l_img else ''
            r_src = r_img.get('src', '') if r_img else ''
            if l_src.startswith('data:image'):
                yc_url = _upload_image_to_yc(l_src)
                if yc_url:
                    l_src = yc_url
                    uploaded_urls.append(yc_url)
            if r_src.startswith('data:image'):
                yc_url = _upload_image_to_yc(r_src)
                if yc_url:
                    r_src = yc_url
                    uploaded_urls.append(yc_url)

            # Paragraphs after the button in the source doc must stay after the
            # button in the output too — split each cell's text around its own
            # button position instead of always rendering all text first.
            left_pre, left_post = _split_paras_at(left_html, left_btn_pos)
            right_pre, right_post = _split_paras_at(right_html, right_btn_pos)

            row = None
            meta = None
            post_text_parts = []
            if left_html.strip() and right_html.strip():
                pv = BeautifulSoup(left_pre or left_html, 'lxml').get_text(strip=True)[:50]
                meta = {'type': 'block_2col_text_text', 'paragraphs_html': left_pre,
                        'col2_html': right_pre, 'btn_text': '', 'btn_url_utm': '', 'preview_text': pv}
                row = block_2col_text_text(left_pre, right_pre)
                post_text_parts = [left_post, right_post]
            elif l_src and not left_html.strip() and right_html.strip():
                img_to_use = images[user_img_idx[0]] if user_img_idx[0] < len(images) else l_src
                if user_img_idx[0] < len(images):
                    user_img_idx[0] += 1
                pv = BeautifulSoup(right_pre or right_html, 'lxml').get_text(strip=True)[:50]
                meta = {'type': 'block_2col_img_text', 'paragraphs_html': right_pre,
                        'image_url': img_to_use, 'btn_text': '', 'btn_url_utm': '', 'preview_text': pv}
                row = block_2col_img_text(img_to_use, right_pre)
                post_text_parts = [right_post]
            elif r_src and not right_html.strip() and left_html.strip():
                img_to_use = images[user_img_idx[0]] if user_img_idx[0] < len(images) else r_src
                if user_img_idx[0] < len(images):
                    user_img_idx[0] += 1
                pv = BeautifulSoup(left_pre or left_html, 'lxml').get_text(strip=True)[:50]
                meta = {'type': 'block_2col_text_img', 'paragraphs_html': left_pre,
                        'image_url': img_to_use, 'btn_text': '', 'btn_url_utm': '', 'preview_text': pv}
                row = block_2col_text_img(left_pre, img_to_use)
                post_text_parts = [left_post]

            if row:
                raw_blocks.append((row, meta))
                all_btns = left_btns + right_btns
                post_text_html = '\n'.join(t for t in post_text_parts if t.strip())
                for i, (btn_text, btn_url) in enumerate(all_btns):
                    # Attach text that follows the button in the source doc to the
                    # SAME white button block, instead of a separate block — but only
                    # when there's exactly one button (unambiguous where it goes).
                    attach_text = post_text_html if (len(all_btns) == 1 and post_text_html.strip()) else ''
                    btn_preview = btn_text
                    if attach_text:
                        btn_preview += ' ' + BeautifulSoup(attach_text, 'lxml').get_text(strip=True)
                    raw_blocks.append((
                        block_button(btn_url, btn_text, attach_text),
                        {'type': 'block_button', 'paragraphs_html': attach_text,
                         'btn_text': btn_text, 'btn_url_utm': btn_url,
                         'preview_text': btn_preview[:50]}
                    ))
                if post_text_html.strip() and len(all_btns) != 1:
                    pv2 = BeautifulSoup(post_text_html, 'lxml').get_text(strip=True)[:50]
                    raw_blocks.append((
                        block_white(post_text_html),
                        {'type': 'block_white', 'paragraphs_html': post_text_html,
                         'btn_text': '', 'btn_url_utm': '', 'preview_text': pv2}
                    ))
                return

            # 2-col detected but no pattern matched — fall back to normal table rendering
            inner = [t for t in el.find_all(['h1', 'h2', 'h3', 'h4', 'p', 'ul', 'ol'])
                     if (t.get_text(strip=True) or (t.name == 'p' and t.find('img'))) and not _is_reklama(t)]
            if inner:
                row, meta = render_block_from_tags(inner, channel_key, campaign, date, segment, images=images, user_img_idx=user_img_idx, uploaded_urls=uploaded_urls)
                if row:
                    raw_blocks.append((row, meta))
        elif el.name in ('h1', 'h2', 'h3', 'h4', 'ul', 'ol'):
            if _is_reklama(el):
                pass
            elif el.get_text(strip=True):
                pending_tags.append(el)
            elif el.name in ('h1', 'h2', 'h3', 'h4'):
                # Empty-text heading wrapping only an image — Google Docs sometimes puts
                # a pasted screenshot inside <h1-4> (e.g. wrapped in a sizing <span>)
                # instead of <p>. Same handling as the empty-<p>-with-image case below.
                img_tag = el.find('img')
                img_src = img_tag.get('src', '') if img_tag else ''
                if img_src and (img_src.startswith('http') or img_src.startswith('data:image')):
                    flush_pending()
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
                    flush_pending()
                    pending_tags.append(el)
                    # no immediate flush — following text accumulates into same block
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
        elif curr_type.startswith('block_2col') or curr_type in ('block_image', 'block_image_text'):
            cycle_pos = 1  # 2-col/image counts as white → next plain block is grey
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

    ad_block = EMAIL_AD_DISCLAIMER if channel_key == 'email_unisender' else ''
    html = (
        EMAIL_WRAPPER_START.replace('{subject}', subject_safe)
        + logo_header
        + content_table
        + EMAIL_FOOTER
        + ad_block
        + EMAIL_WRAPPER_END
    )
    return html, blocks_data, uploaded_urls

# ---------------------------------------------------------------------------
# TG HTML generation
# ---------------------------------------------------------------------------

def _heading_seed_bold(tag):
    """
    Default bold state for a heading tag's (h1-h4) own direct content, resolved
    from its own inline style — set by inline_gdoc_formatting from the doc's
    <style> block, including bare tag-selector rules like h3{font-weight:700}.
    Falls back to True (headings render bold by default) when the source
    document's CSS says nothing about it, e.g. hand-built test HTML with no
    <style> block, preserving the old blanket-bold behaviour as a safe default.
    Per-span explicit overrides (font-weight:400/normal) inside the heading
    still cancel this via clean_tag_for_tg's own tri-state resolution.
    """
    state = _style_is_bold(tag.get('style', ''))
    return True if state is None else state


def clean_tag_for_tg(tag, _in_bold=False, _wrapped=False):
    """
    Convert a BS4 tag to TG-compatible HTML:
    Only keep <b>, <i>, <a href="...">, <code>, line breaks.

    _in_bold: whether this text should render bold by default (ambient context —
              e.g. seeded True for headings whose default weight comes from a
              bare h1-h4 CSS rule rather than an explicit span/class override).
    _wrapped: whether a physical <b> tag already encloses this point in the
              output being assembled, so we don't add a redundant nested one.
    A span with an explicit font-weight:400/normal override cancels inherited
    bold for its own subtree, regardless of what the ambient context says —
    this is what lets a heading with a bold headline + a deliberately
    non-bold second line come out correctly instead of both lines bold.
    """
    parts = []
    for child in tag.children:
        if isinstance(child, NavigableString):
            text = str(child)
            if _in_bold and not _wrapped and text.strip():
                parts.append(f'<b>{text}</b>')
            else:
                parts.append(text)
        elif isinstance(child, Tag):
            name = child.name
            if name in ('b', 'strong'):
                inner = clean_tag_for_tg(child, _in_bold=True, _wrapped=True)
                parts.append(f'<b>{inner}</b>')
            elif name in ('i', 'em'):
                inner = clean_tag_for_tg(child, _in_bold=_in_bold, _wrapped=_wrapped)
                parts.append(f'<i>{inner}</i>')
            elif name == 'u':
                inner = clean_tag_for_tg(child, _in_bold=_in_bold, _wrapped=_wrapped)
                parts.append(f'<u>{inner}</u>')
            elif name == 's':
                inner = clean_tag_for_tg(child, _in_bold=_in_bold, _wrapped=_wrapped)
                parts.append(f'<s>{inner}</s>')
            elif name == 'code':
                inner = clean_tag_for_tg(child, _in_bold=_in_bold, _wrapped=_wrapped)
                parts.append(f'<code>{inner}</code>')
            elif name == 'a' and child.get('href'):
                inner = clean_tag_for_tg(child, _in_bold=_in_bold, _wrapped=_wrapped)
                href = decode_google_redirect(child['href'])
                parts.append(f'<a href="{href}">{inner}</a>')
            elif name == 'sup':
                pass
            elif name == 'span':
                style = child.get('style', '')
                bold_state = _style_is_bold(style)
                eff_bold = _in_bold if bold_state is None else bold_state
                is_italic = _style_is_italic(style)
                need_wrap = eff_bold and not _wrapped
                inner = clean_tag_for_tg(child, _in_bold=eff_bold, _wrapped=_wrapped or need_wrap)
                if need_wrap:
                    inner = f'<b>{inner}</b>'
                if is_italic:
                    inner = f'<i>{inner}</i>'
                parts.append(inner)
            elif name == 'br':
                parts.append('\n')
            else:
                inner = clean_tag_for_tg(child, _in_bold=_in_bold, _wrapped=_wrapped)
                parts.append(inner)
    return ''.join(parts)

_REKLAMA_RE        = re.compile(r'реклама[\s.,]*ооо', re.IGNORECASE)
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
    """Wrap inner text in Markdown markers, keeping spaces outside the markers.
    Trailing punctuation (».,!?;:) is placed after the closing marker so Telegram
    MarkdownV2 parser correctly recognises the closing delimiter."""
    if not inner or not inner.strip():
        return inner
    stripped = inner.strip()
    leading  = inner[:len(inner) - len(inner.lstrip())]
    trailing = inner[len(inner.rstrip()):]
    # Move trailing sentence punctuation outside the closing marker so Telegram
    # recognises the closing delimiter. Closing quotes (») stay inside bold.
    # e.g. **потом».**  →  **потом»**. (only . is moved, » stays inside)
    m = re.match(r'^(.*[^.,;:!?])([\.,;:!?]+)$', stripped, re.DOTALL)
    tail_punct = m.group(2) if m else ''
    if m:
        stripped = m.group(1)
    if not stripped:
        return inner
    return f'{leading}{marker}{stripped}{marker}{tail_punct}{trailing}'


def clean_tag_for_tg_markdown(tag, links_collector, _in_bold=False, _wrapped=False):
    """
    Convert a BS4 tag to Markdown: **bold**, *italic*.
    Link URLs are appended to links_collector; link text is kept as plain text.

    _in_bold: whether this text should render bold by default (ambient context).
    _wrapped: whether a physical ** marker already encloses this point in the
              output being assembled, so we don't add a redundant nested one.
    A span with an explicit font-weight:400/normal override cancels inherited
    bold for its own subtree, regardless of what the ambient context says.
    """
    parts = []
    for child in tag.children:
        if isinstance(child, NavigableString):
            text = str(child)
            if _in_bold and not _wrapped and text.strip():
                parts.append(_md_wrap('**', text))
            else:
                parts.append(text)
        elif isinstance(child, Tag):
            name = child.name
            if name in ('b', 'strong'):
                inner = clean_tag_for_tg_markdown(child, links_collector, _in_bold=True, _wrapped=True)
                parts.append(_md_wrap('**', inner) if not _wrapped else inner)
            elif name in ('i', 'em'):
                inner = clean_tag_for_tg_markdown(child, links_collector, _in_bold=_in_bold, _wrapped=_wrapped)
                parts.append(_md_wrap('*', inner))
            elif name == 'a' and child.get('href'):
                href = decode_google_redirect(child['href'])
                if not href.startswith('#'):
                    links_collector.append(href)
                parts.append(child.get_text())
            elif name == 'sup':
                pass
            elif name == 'span':
                style = child.get('style', '')
                bold_state = _style_is_bold(style)
                eff_bold = _in_bold if bold_state is None else bold_state
                is_italic = _style_is_italic(style)
                need_wrap = eff_bold and not _wrapped
                inner = clean_tag_for_tg_markdown(child, links_collector, _in_bold=eff_bold, _wrapped=_wrapped or need_wrap)
                if need_wrap:
                    inner = _md_wrap('**', inner)
                if is_italic:
                    inner = _md_wrap('*', inner)
                parts.append(inner)
            elif name == 'br':
                parts.append('\n')
            else:
                parts.append(clean_tag_for_tg_markdown(child, links_collector, _in_bold=_in_bold, _wrapped=_wrapped))
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
            tag = _strip_reklama_from_tag(tag)
            if not tag:
                continue
            raw_text = tag.get_text(strip=True)
            if not raw_text:
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

        if tag.name in ('h1', 'h2', 'h3', 'h4'):
            # Seed bold from the heading's own resolved default instead of
            # blanket-wrapping the whole heading text — see generate_tg_html for
            # the full rationale (a deliberately non-bold second line must stay
            # non-bold instead of getting swept into one big ** run).
            raw_inner = clean_tag_for_tg_markdown(tag, raw_links, _in_bold=_heading_seed_bold(tag))
        else:
            raw_inner = clean_tag_for_tg_markdown(tag, raw_links)
        inner = _postprocess_md(raw_inner.strip())
        if not inner:
            continue
        inner = re.sub(r'\*{0,2}\s*ссылка:\s*([^\s*\w]*)\s*\*{0,2}\s*', r'\1', inner, flags=re.IGNORECASE).strip()
        if not inner:
            continue

        # Normalize spaces inside { first_name } before stripping
        inner = re.sub(r'\{\s*first_name\s*\}', '{first_name}', inner)
        if CHANNELS.get(channel_key, {}).get('strip_gc_vars'):
            # punct OUTSIDE closing ** (from _md_wrap moving trailing punct out of bold)
            # e.g. **{first_name}**, привет → Привет
            inner = re.sub(
                r'\*\*[ \t\xa0]*\{first_name\}[ \t\xa0]*\*\*[,!?.;:\-–—]\s*([а-яёa-z])',
                lambda m: m.group(1).upper(), inner
            )
            inner = re.sub(r'\*\*[ \t\xa0]*\{first_name\}[ \t\xa0]*\*\*[,!?.;:\-–—]\s*', '', inner)
            # ", **{first_name}**" — variable is bold, comma is outside the bold markers
            # e.g. "Привет, **{first_name}**. Я Павел" → "Привет. Я Павел"
            inner = re.sub(r'[ \t\xa0]*,[ \t\xa0]*\*\*\{first_name\}\*\*', '', inner)
            # "**{first_name}[punct] " — variable at start of bold block
            # If followed by lowercase letter, remove and capitalize it
            # (uses [ \t\xa0]* rather than \s* right after ** so a heading's CLOSING
            # ** followed on the next line by a non-bold {first_name} run — a real
            # doc pattern once headings can have a bold headline + non-bold body —
            # is never mistaken for an OPENING ** around the variable.)
            inner = re.sub(
                r'\*\*[ \t\xa0]*\{first_name\}[,!?.;:\-–—]?[ \t\xa0]*\*\*\s*([а-яёa-z])',
                lambda m: m.group(1).upper(), inner
            )
            inner = re.sub(r'\*\*[ \t\xa0]*\{first_name\}[,!?.;:\-–—]?[ \t\xa0]*', '**', inner)
            inner = _strip_first_name(inner)
            inner = re.sub(r'\*{4,}', '', inner).strip()  # clean up empty **..** remnants
        if not inner:
            continue

        # When a paragraph starts with emoji then **, move the emoji inside the bold markers
        # so ** is at position 0. Neurocat renders **🔥 text** correctly but not 🔥 **text**.
        if _EMOJI_BEFORE_DSTAR_RE.search(inner):
            inner = re.sub(
                r'^([☀-➿\U0001F300-\U0001F9FF\U0001FA00-\U0001FAFF\U00002702-\U000027B0]+\s*)\*\*',
                r'**\1',
                inner
            )

        # Move opening guillemet inside bold: «**text»** → **«text»**
        # Telegram Markdown requires ** at a word boundary; «** is often not recognised.
        inner = re.sub(r'«\*\*', '**«', inner)

        # <p> tags with <br>-separated items (custom bullet lists) come in as single
        # string with \n inside. Keep as ONE block joined with \n so items are
        # compact (single line-break between them, not a paragraph gap).
        if '\n' in inner:
            sub_lines = [line.strip() for line in inner.split('\n') if line.strip()]
            result_parts.append('\n'.join(sub_lines))
        else:
            result_parts.append(inner)

    text = '\n\n'.join(result_parts)
    # Normalize { first_name } (with spaces/newlines from Google Docs multiline export),
    # then strip per-paragraph to avoid eating \n\n separators with \s+ in _strip_first_name.
    if CHANNELS.get(channel_key, {}).get('strip_gc_vars'):
        paragraphs = text.split('\n\n')
        cleaned = []
        for para in paragraphs:
            para = re.sub(r'\{\s*first_name\s*\}', '{first_name}', para)
            # punct OUTSIDE closing ** (from _md_wrap moving trailing punct out of bold)
            para = re.sub(
                r'\*\*\s*\{first_name\}\s*\*\*[,!?.;:\-–—]\s*([а-яёa-z])',
                lambda m: m.group(1).upper(), para
            )
            para = re.sub(r'\*\*\s*\{first_name\}\s*\*\*[,!?.;:\-–—]\s*', '', para)
            para = re.sub(r'\s*,\s*\*\*\{first_name\}\*\*', '', para)
            # **{first_name},** prefix followed by text: remove and capitalize next word
            para = re.sub(
                r'\*\*\s*\{first_name\}[,!?.;:\-–—]?\s*\*\*\s*([а-яёa-z])',
                lambda m: m.group(1).upper(), para
            )
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


def _strip_empty_format_tags(inner):
    """
    Remove formatting tags (<b>/<i>/<u>/<s>) whose content is only whitespace —
    a Google Docs export artifact, e.g. <span style="font-weight:700"><br/></span>
    becomes <b>\\n</b> after clean_tag_for_tg(), which would otherwise leave a
    stray unbalanced tag once the surrounding text is split on '\\n'.

    Tags that contain 2+ consecutive newlines are deliberately left untouched:
    a double line break is how a real paragraph break shows up here when Google
    Docs collapses a blank line into the same <p> instead of emitting a separate
    empty one (see _split_tg_paragraph_groups) — stripping it would silently
    glue two logical paragraphs together with no separator at all.
    """
    def _repl(m):
        whitespace = m.group(2)
        if whitespace.count('\n') >= 2:
            return m.group(0)
        if '\n' in whitespace:
            # A single soft-return (Shift+Enter) landed alone in its own formatting
            # span (e.g. producer changed style right at the line break) — the tag
            # is pointless but the line break itself is real. Unwrap the tag, keep
            # the newline so downstream split-on-'\n' still sees the line boundary.
            return whitespace
        return ''
    return re.sub(r'<(b|i|u|s)>(\s*)</\1>', _repl, inner)


def _split_tg_paragraph_groups(inner):
    """
    Split a clean_tag_for_tg() output string into paragraph groups for the TG
    generators. A single '\\n' (from one <br/>) is a soft return that must stay
    within the same output paragraph/block. Two or more consecutive '\\n' mark a
    real paragraph break that Google Docs collapsed into the same source <p> via
    <br/><br/> instead of a separate empty <p> — those must become a NEW block so
    downstream code inserts the normal inter-block spacer, rather than being
    glued into the same paragraph with a single soft-return separator.

    Returns a list of groups; each group is a list of balanced, non-empty line
    strings (soft-return lines within one paragraph). Empty groups are dropped,
    so the returned list may be empty if there was no visible content at all.
    """
    groups = []
    for group_raw in re.split(r'\n{2,}', inner):
        lines = [ln.strip() for ln in group_raw.split('\n') if ln.strip()]
        # Skip lines that are only HTML tags with no visible text — artifact from
        # <br/> inside bold/italic wrappers (e.g. <b> alone from <b><br/>text</b> split).
        lines = [ln for ln in lines if re.sub(r'<[^>]+>', '', ln).strip()]
        if not lines:
            continue
        balanced = []
        for line in lines:
            for t in ('i', 'b', 'u', 's'):
                open_count = line.count(f'<{t}>')
                close_count = line.count(f'</{t}>')
                if open_count > close_count:
                    line += f'</{t}>' * (open_count - close_count)
                elif close_count > open_count:
                    line = f'<{t}>' * (close_count - open_count) + line
            balanced.append(line)
        groups.append(balanced)
    return groups


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
            tag = _strip_reklama_from_tag(tag)
            if not tag:
                continue
            raw_text = tag.get_text(strip=True)
            if not raw_text:
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

        if tag.name in ('h1', 'h2', 'h3', 'h4'):
            # Seed bold from the heading's own resolved default (usually True —
            # GDocs headings are bold by default) rather than blanket-wrapping the
            # whole heading text: a span that explicitly overrides to
            # font-weight:400/normal (e.g. a deliberately non-bold second line)
            # must stay non-bold instead of getting swept up in one big <b>.
            inner = clean_tag_for_tg(tag, _in_bold=_heading_seed_bold(tag)).strip()
        else:
            inner = clean_tag_for_tg(tag).strip()
        if not inner:
            continue  # empty paragraphs skipped — spacers added uniformly below
        # Remove empty formatting spans BEFORE splitting — Google Docs exports e.g.
        # <b>\n</b> (a bold span containing only a <br/>), which after split creates
        # a part starting with </b><b> (balanced 1:1, so the balance step ignores it)
        # that ends up as <br></b><b> in the joined output. Tags whose whitespace is
        # 2+ newlines are left alone — see _strip_empty_format_tags.
        inner = _strip_empty_format_tags(inner)
        # Also skip paragraphs that are HTML-only with no visible text (e.g. <b></b>, <b>&nbsp;</b>)
        inner_text = re.sub(r'<[^>]+>', '', inner).replace('&nbsp;', '').replace('\xa0', '').strip()
        if not inner_text:
            continue
        inner = re.sub(r'(?:<[^>]+>)*\s*ссылка:\s*(?:<\/[^>]+>)*\s*', '', inner, flags=re.IGNORECASE).strip()
        if not inner:
            continue

        # Fix Google Docs artifact: hyperlink on trailing space instead of button text.
        # <b>BUTTON</b><a href="URL"> </a>  →  <b><a href="URL">BUTTON</a></b>
        inner = re.sub(
            r'<b>([^<>]+)</b>\s*<a href="([^"]+)">[\s\xa0]*</a>',
            r'<b><a href="\2">\1</a></b>',
            inner
        )

        # Split into paragraph groups: a lone '\n' (soft return) stays within one <p>
        # joined by <br>; 2+ consecutive '\n' (a blank line Google Docs collapsed into
        # this same source <p>) becomes its own separate <p> — spacers between the
        # resulting blocks are added by the uniform join below, same as any other
        # pair of source paragraphs.
        if '\n' in inner:
            for balanced in _split_tg_paragraph_groups(inner):
                result_parts.append(f'<p>{"<br>".join(balanced)}</p>')
        else:
            result_parts.append(f'<p>{inner}</p>')

    # Group consecutive bullet <p>s (▪️/•/◾/etc.) into one result_parts entry so the
    # spacer join below doesn't insert blank lines between bullet items.
    grouped_parts = []
    i = 0
    while i < len(result_parts):
        visible = re.sub(r'<[^>]+>', '', result_parts[i]).strip()
        if visible and visible[0] in '▪•◾►▸▶':
            run = [result_parts[i]]
            j = i + 1
            while j < len(result_parts):
                vis_j = re.sub(r'<[^>]+>', '', result_parts[j]).strip()
                if vis_j and vis_j[0] in '▪•◾►▸▶':
                    run.append(result_parts[j])
                    j += 1
                else:
                    break
            grouped_parts.append('\n'.join(run))
            i = j
        else:
            grouped_parts.append(result_parts[i])
            i += 1
    result_parts = grouped_parts

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

    # Strip trailing spacers — legal notice adds its own leading spacer
    output = re.sub(r'(\s*<p>&nbsp;</p>\s*)+$', '', output)
    # Defensive: balance <b>/<i>/<u>/<s> within each <p>...</p> block
    def _balance_p(m):
        seg = m.group(0)
        inner_seg = seg[3:-4]  # strip leading <p> and trailing </p>
        for t in ('b', 'i', 'u', 's'):
            no = inner_seg.count(f'<{t}>')
            nc = inner_seg.count(f'</{t}>')
            if no > nc:
                inner_seg += f'</{t}>' * (no - nc)
        return f'<p>{inner_seg}</p>'
    output = re.sub(r'<p>.*?</p>', _balance_p, output, flags=re.DOTALL)
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
            tag = _strip_reklama_from_tag(tag)
            if not tag:
                continue
            raw_text = tag.get_text(strip=True)
            if not raw_text:
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

        if tag.name in ('h1', 'h2', 'h3', 'h4'):
            # See generate_tg_html for why we seed bold instead of blanket-wrapping.
            inner = clean_tag_for_tg(tag, _in_bold=_heading_seed_bold(tag)).strip()
        else:
            inner = clean_tag_for_tg(tag).strip()
        if not inner:
            continue
        # Same empty-span cleanup as in generate_tg_html (see comment there)
        inner = _strip_empty_format_tags(inner)
        inner = re.sub(r'(?:<[^>]+>)*\s*ссылка:\s*(?:<\/[^>]+>)*\s*', '', inner, flags=re.IGNORECASE).strip()
        if not inner:
            continue

        # Fix Google Docs artifact: hyperlink on trailing space instead of button text.
        # <b>BUTTON</b><a href="URL"> </a>  →  <b><a href="URL">BUTTON</a></b>
        inner = re.sub(
            r'<b>([^<>]+)</b>\s*<a href="([^"]+)">[\s\xa0]*</a>',
            r'<b><a href="\2">\1</a></b>',
            inner
        )

        # Split into paragraph groups: a lone '\n' (soft return) stays within one
        # entry joined with '\n' (list items stay compact); 2+ consecutive '\n'
        # (a blank line Google Docs collapsed into this same source <p>) becomes
        # its own separate entry, which gets the normal '\n\n' block separator below.
        if '\n' in inner:
            for balanced in _split_tg_paragraph_groups(inner):
                result_parts.append('\n'.join(balanced))
        else:
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
    # Defensive: balance <b>/<i>/<u>/<s> per line
    fixed_lines = []
    for _ln in output.split('\n'):
        for _t in ('b', 'i', 'u', 's'):
            _no = _ln.count(f'<{_t}>')
            _nc = _ln.count(f'</{_t}>')
            if _no > _nc:
                _ln += f'</{_t}>' * (_no - _nc)
            elif _nc > _no:
                _ln = f'<{_t}>' * (_nc - _no) + _ln
        fixed_lines.append(_ln)
    output = '\n'.join(fixed_lines)
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
        # Requested index not present — use the last available TG variant.
        # Never fall back to email_variants here: email content must not appear in TG channels.
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

    # Run AI parser first to get section header hints for the HTML parser
    ai_result = None
    ai_error = None
    ai_section_hints = None
    try:
        ai_result = parse_with_ai(html_content)
        if ai_result and isinstance(ai_result.get('section_headers'), dict):
            ai_section_hints = ai_result['section_headers']
    except Exception as e:
        ai_error = str(e)

    try:
        parsed = parse_doc_html(html_content, ai_hints=ai_section_hints)
    except Exception as e:
        return jsonify({'error': f'Ошибка разбора документа: {str(e)}'}), 500

    if not parsed.get('doc_title') and cd_title:
        parsed['doc_title'] = cd_title

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
            """True if the text looks like full email content rather than a TG message.
            TG messages can legitimately be long (3000+ chars) so length alone is not
            a reliable signal. Use only markers that are exclusive to email_gc content."""
            if not text:
                return False
            # GC offer URL variable only appears in email_gc, never in TG messages
            if '{offer_url_' in text:
                return True
            # GC personalization variable only appears in email_gc
            if '{first_name}' in text:
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

        # Build TG variants only when AI found voronki content AND identified a dedicated
        # section header for it. Without a section header, AI is guessing — it often
        # mistakes the channels-table metadata (telegram/Воронки GC/@bot...) for voronki
        # content. Requiring a non-null section_headers.tg_voronki gates on a real section.
        ai_voronki_header = (ai_result.get('section_headers') or {}).get('tg_voronki')
        if tg_voronki_ai and ai_voronki_header and not parsed.get('tg_variants'):
            main_tg = parsed.get('tg_html') or tg_main_ai
            # Only create two variants if voronki content meaningfully differs from main
            v_short = re.sub(r'\s+', ' ', tg_voronki_ai).strip()[:300]
            m_short = re.sub(r'\s+', ' ', main_tg or '').strip()[:300]
            if main_tg and v_short != m_short:
                parsed['tg_variants'] = [
                    {'name': 'ТГ (основной)', 'html': main_tg},
                    {'name': 'ТГ (Воронки)', 'html': tg_voronki_ai},
                ]

    # Upload any base64 screenshots to YC now, before the payload goes back
    # to the browser — avoids re-sending megabytes of base64 on generate.
    parsed['email_html'] = _replace_base64_images_with_yc_urls(parsed['email_html'])
    parsed['tg_html'] = _replace_base64_images_with_yc_urls(parsed['tg_html'])
    if parsed.get('email_variants'):
        for v in parsed['email_variants']:
            v['html'] = _replace_base64_images_with_yc_urls(v['html'])
    if parsed.get('tg_variants'):
        for v in parsed['tg_variants']:
            v['html'] = _replace_base64_images_with_yc_urls(v['html'])

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
        logging.debug(f"AI section_headers: {json.dumps(ai_result.get('section_headers') or {}, ensure_ascii=False)}")

    response_data = {
        'email_html': parsed['email_html'],
        'email_variants': parsed.get('email_variants'),
        'tg_html': parsed['tg_html'],
        'tg_variants': parsed.get('tg_variants'),
        'subject': parsed['subject'],
        'preview': parsed['preview'],
        'links': parsed['links'][:50],
        'footnotes': parsed['footnotes'],
        'segment': parsed.get('segment', ''),
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
    all_uploaded_image_urls = []

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
                    html, blocks, ch_uploaded_urls = generate_email_html(
                        html_to_use, ch_key, campaign, date, images, subject, segment
                    )
                    result[ch_key] = html
                    result[f'{ch_key}_blocks'] = blocks
                    # Collect unique YC URLs (same image may appear in multiple email channels)
                    for u in ch_uploaded_urls:
                        if u not in all_uploaded_image_urls:
                            all_uploaded_image_urls.append(u)
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

    if all_uploaded_image_urls:
        result['uploaded_image_urls'] = all_uploaded_image_urls

    return jsonify(result)

@app.route('/api/assemble-email', methods=['POST'])
def api_assemble_email():
    data = request.get_json(force=True)
    blocks = data.get('blocks', [])
    subject = data.get('subject', '')
    images = data.get('images', [])
    channel_key = data.get('channel_key', '')

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
            # Build all inner rows as a list — mirrors generate_email_html's inner_rows logic
            # so first/last padding adjustments apply to the same elements.
            inner_cta_rows = []
            if img_url_cta:
                inner_cta_rows.append(
                    '<tr><td align="center" bgcolor="#1445ea" style="padding:8px 10px 4px;font-size:0px">\n'
                    f'<img src="{img_url_cta}" alt="" style="display:block;border:0;max-width:100%;border-radius:8px">\n'
                    '</td></tr>'
                )
            btn_position = block.get('btn_position')
            if ph.strip():
                soup_ph = BeautifulSoup(ph, 'html.parser')
                paras = soup_ph.find_all(['p', 'ul', 'ol'])
                paras_html = [str(p) for p in paras if str(p).strip()] if paras else [ph]
                # Split paragraphs around button position to restore original order
                if btn_position is not None:
                    pre_btn = paras_html[:btn_position]
                    post_btn = paras_html[btn_position:]
                else:
                    pre_btn = paras_html
                    post_btn = []
                for p_html in pre_btn:
                    inner_cta_rows.append(
                        '<tr><td align="left" bgcolor="#1445ea" style="padding:4px 15px">\n'
                        + p_html + '\n</td></tr>'
                    )
                for b in buttons:
                    inner_cta_rows.append(
                        f'<tr><td align="center" bgcolor="#1445ea" style="padding:8px 0 12px;margin:0">\n'
                        f'<a href="{b["url"]}" target="_blank" style="{BTN_A}">{b["text"]}</a>\n'
                        f'</td></tr>'
                    )
                for p_html in post_btn:
                    inner_cta_rows.append(
                        '<tr><td align="left" bgcolor="#1445ea" style="padding:4px 15px">\n'
                        + p_html + '\n</td></tr>'
                    )
            else:
                for b in buttons:
                    inner_cta_rows.append(
                        f'<tr><td align="center" bgcolor="#1445ea" style="padding:8px 0 12px;margin:0">\n'
                        f'<a href="{b["url"]}" target="_blank" style="{BTN_A}">{b["text"]}</a>\n'
                        f'</td></tr>'
                    )
            if inner_cta_rows:
                inner_cta_rows[0] = (
                    inner_cta_rows[0]
                    .replace('style="padding:4px 15px"', 'style="padding:14px 15px 4px"', 1)
                    .replace('style="padding:8px 0 12px;margin:0"', 'style="padding:14px 0 12px;margin:0"', 1)
                )
                inner_cta_rows[-1] = (
                    inner_cta_rows[-1]
                    .replace('style="padding:4px 15px"', 'style="padding:4px 15px 14px"', 1)
                    .replace('style="padding:8px 0 12px;margin:0"', 'style="padding:8px 0 18px;margin:0"', 1)
                )
            row = (
                '<tr><td style="padding:5px 10px 10px;margin:0;background-color:#ffffff">\n'
                '<table cellspacing="0" cellpadding="0" width="100%" style="border-collapse:separate;'
                'border-spacing:0;border:10px solid #1445ea;border-radius:20px" role="presentation">\n'
                + '\n'.join(inner_cta_rows) + '\n'
                + '</table></td></tr>'
            )
        elif btype == 'block_grey':
            row = block_grey(ph)
        elif btype == 'block_dotted':
            row = block_dotted(ph)
        elif btype == 'block_blue_text':
            row = block_blue_text(ph)
        elif btype == 'block_button':
            row = block_button(btn_url_utm, btn_text, ph)
        elif btype == 'block_spacer':
            row = block_spacer(block.get('height', 20))
        elif btype == 'block_image':
            row = block_image_center(block.get('image_url', ''))
        elif btype == 'block_image_text':
            row = block_image_with_text(block.get('image_url', ''), ph)
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
    ad_block = EMAIL_AD_DISCLAIMER if channel_key == 'email_unisender' else ''
    html = (
        EMAIL_WRAPPER_START.replace('{subject}', subject_safe)
        + logo_header
        + content_table
        + EMAIL_FOOTER
        + ad_block
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

# Bot field per GC transport (found via JS dump of select options)
_BOT_FIELD = {
    'ticket': 'ParamsObject[telegram_bot_id]',
    'max':    'ParamsObject[max_bot_id]',
}

def _gc_login_session():
    """Return (authenticated requests.Session, gc_url) or (None, None).
    Uses the real AJAX login flow that GC's JS uses."""
    gc_url = os.getenv('GC_ACCOUNT_URL', '').rstrip('/')
    login  = os.getenv('GC_LOGIN', '')
    passwd = os.getenv('GC_PASSWORD', '')
    if not gc_url or not login or not passwd:
        return None, None
    s = requests.Session()
    s.headers['User-Agent'] = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    try:
        # Step 1: GET login page to obtain requestTime/requestSimpleSign (server-injected)
        resp = s.get(f'{gc_url}/cms/system/login', timeout=15)
        m1 = re.search(r'window\.requestTime\s*=\s*(\d+)', resp.text)
        m2 = re.search(r'window\.requestSimpleSign\s*=\s*["\']([0-9a-f]+)["\']', resp.text)
        if not m1 or not m2:
            logging.warning('[GC Login] requestTime/requestSimpleSign not found in page')
            return None, None
        # Step 2: AJAX POST — same format that GC's user-form JS sends
        post_data = {
            'action': 'processXdget',
            'xdgetId': 'r2039_1_1_1_1',
            'params[action]': 'login',
            'params[url]': f'{gc_url}/cms/system/login',
            'params[email]': login,
            'params[password]': passwd,
            'params[object_type]': 'cms_page',
            'params[object_id]': '-1',
            'params[globalConfirmCheckbox]': '1',
            'requestTime': m1.group(1),
            'requestSimpleSign': m2.group(1),
            'gcSession': '{"id":null,"last_activity":null,"user_id":null,"utm_id":null}',
            'gcVisit': '',
            'gcVisitor': '',
        }
        resp2 = s.post(
            f'{gc_url}/cms/system/login',
            data=post_data,
            headers={
                'X-Requested-With': 'XMLHttpRequest',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'Referer': f'{gc_url}/cms/system/login',
            },
            timeout=15,
        )
        result = resp2.json()
        if result.get('success'):
            logging.info(f'[GC Login] OK user_id={result.get("user_id")}')
            return s, gc_url
        logging.warning(f'[GC Login] Login failed: {result}')
    except Exception as e:
        logging.warning(f'[GC Login] {e}')
    return None, None

def _gc_fix_bot(mailing_id, transport):
    """Set bot to 'Любой бот' (id=0) on a TG or MAX GC mailing draft.
    Reads the full mailing form, flips only the bot select, then saves."""
    bot_field = _BOT_FIELD.get(transport)
    if not bot_field:
        return
    s, gc_url = _gc_login_session()
    if not s:
        logging.warning('[GC Bot Fix] GC login failed')
        return
    url = f'{gc_url}/notifications/control/mailings/update/id/{mailing_id}/part/main'
    try:
        page_resp = s.get(url, timeout=15)
        soup = BeautifulSoup(page_resp.text, 'html.parser')
        form = soup.find('form', id='yw0')
        if not form:
            logging.warning(f'[GC Bot Fix] form#yw0 not found for mailing {mailing_id}')
            return
        # Collect all form field values
        post_data = {}
        for inp in form.find_all(['input', 'textarea', 'select']):
            name = inp.get('name')
            if not name:
                continue
            if inp.name == 'select':
                selected = inp.find('option', selected=True)
                post_data[name] = selected['value'] if selected else (inp.find('option') or {}).get('value', '')
            elif inp.name == 'textarea':
                post_data[name] = inp.get_text()
            else:
                itype = inp.get('type', 'text').lower()
                if itype in ('checkbox', 'radio') and not inp.get('checked'):
                    continue
                post_data[name] = inp.get('value', '')
        # Override bot to "Любой бот"
        post_data[bot_field] = '0'
        post_data['save'] = '1'
        resp = s.post(url, data=post_data, timeout=20, allow_redirects=True)
        logging.info(f'[GC Bot Fix] mailing={mailing_id} field={bot_field} status={resp.status_code}')
    except Exception as e:
        logging.warning(f'[GC Bot Fix] {e}')


def _gc_fix_mailing_playwright(mailing_id, transport, job_id=None):
    """Configure GC mailing via Playwright: set bot (TG/Max) or recipient type (email)."""
    gc_url_base = os.getenv('GC_ACCOUNT_URL', '').rstrip('/')
    if not gc_url_base:
        logging.warning('[GC PW Fix] GC_ACCOUNT_URL not configured')
        return

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logging.warning('[GC PW Fix] playwright not installed; falling back to HTTP fix')
        _gc_fix_bot(mailing_id, transport)
        return

    # Reuse existing HTTP login to get authenticated session cookies
    s, _ = _gc_login_session()
    if not s:
        logging.warning('[GC PW Fix] GC login failed')
        return

    domain = gc_url_base.replace('https://', '').replace('http://', '')
    pw_cookies = [
        {'name': c.name, 'value': c.value, 'domain': domain, 'path': c.path or '/'}
        for c in s.cookies
    ]

    page_url = f'{gc_url_base}/notifications/control/mailings/update/id/{mailing_id}'

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        ctx = browser.new_context(viewport={'width': 1280, 'height': 900})
        ctx.add_cookies(pw_cookies)
        page = ctx.new_page()
        try:
            page.goto(page_url, timeout=30000)
            page.wait_for_load_state('networkidle', timeout=20000)

            if transport in ('ticket', 'max'):
                # TG and Max both use select#mailing_bot_id (only the name attr differs).
                # Select2 hides the native select, so wait for DOM presence then set via jQuery.
                page.wait_for_selector('select#mailing_bot_id', state='attached', timeout=10000)
                page.evaluate(
                    "if (window.jQuery) { jQuery('#mailing_bot_id').val('0').trigger('change'); }"
                )
                page.wait_for_timeout(800)

            elif transport == 'email':
                # Click "Сегмент" radio — recipients_type=segment
                page.wait_for_selector('#ParamsObject_recipients_type_2', timeout=10000)
                page.click('#ParamsObject_recipients_type_2')
                page.wait_for_timeout(600)
                # Click "Всем выбранным адресам" — send_to=all
                page.wait_for_selector('#ParamsObject_send_to_0', timeout=5000)
                page.click('#ParamsObject_send_to_0')
                page.wait_for_timeout(500)

            # Click the real save button (.btn-save-mailing) so GC's submit event handlers
            # (e.g. Select2 serialisation) run — form#yw0.submit() bypasses them.
            page.click('.btn-save-mailing')
            page.wait_for_load_state('networkidle', timeout=20000)

            logging.info(f'[GC PW Fix] mailing={mailing_id} transport={transport} saved OK')
        except Exception as e:
            logging.warning(f'[GC PW Fix] mailing={mailing_id} transport={transport}: {e}')
        finally:
            browser.close()
            if job_id:
                _jobs_set_pw_done(job_id)


_GC_TRANSPORT = {
    'email':           'email',
    'email_unisender': 'email',
    'tg_gc':           'ticket',
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

    try:
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
    except Exception as e:
        app.logger.exception('push-to-gc failed')
        return jsonify({'error': str(e)}), 500


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
        if job_id and (transport in _BOT_FIELD or transport == 'email'):
            _jobs_register(job_id, transport)
        return jsonify({'ok': True, 'job_id': job_id, 'count': result.get('count', 1)})
    except requests.RequestException as e:
        return jsonify({'error': str(e)}), 500
    except ValueError as e:
        logging.exception("push-to-mail: non-JSON response from mail API")
        return jsonify({'error': f'API вернул не-JSON ответ: {e}'}), 500


# File-based job tracking — survives worker restarts and works across multiple gunicorn workers
_JOBS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gc_output', 'pending_jobs.json')
_jobs_lock = __import__('threading').Lock()


def _jobs_register(job_id, transport):
    """Record that this job needs a post-creation fix for the given transport."""
    with _jobs_lock:
        os.makedirs(os.path.dirname(_JOBS_FILE), exist_ok=True)
        try:
            with open(_JOBS_FILE) as f:
                data = json.load(f)
        except Exception:
            data = {}
        data[str(job_id)] = {'transport': transport, 'fixed': False, 'pw_done': False}
        with open(_JOBS_FILE, 'w') as f:
            json.dump(data, f)


def _jobs_claim(job_id, mailing_id=None, gc_url=None):
    """Atomically return transport and mark fix as started. Returns None if already started."""
    with _jobs_lock:
        try:
            with open(_JOBS_FILE) as f:
                data = json.load(f)
        except Exception:
            return None
        job = data.get(str(job_id))
        if job and not job.get('fixed'):
            data[str(job_id)]['fixed'] = True
            if mailing_id:
                data[str(job_id)]['mailing_id'] = str(mailing_id)
            if gc_url:
                data[str(job_id)]['gc_url'] = gc_url
            with open(_JOBS_FILE, 'w') as f:
                json.dump(data, f)
            return job.get('transport')
        return None


def _jobs_get(job_id):
    """Return current job data without modifying it."""
    try:
        with open(_JOBS_FILE) as f:
            data = json.load(f)
        return data.get(str(job_id))
    except Exception:
        return None


def _jobs_set_pw_done(job_id):
    """Mark the playwright fix as completed so job-status can release gc_url."""
    with _jobs_lock:
        try:
            with open(_JOBS_FILE) as f:
                data = json.load(f)
        except Exception:
            return
        if str(job_id) in data:
            data[str(job_id)]['pw_done'] = True
            with open(_JOBS_FILE, 'w') as f:
                json.dump(data, f)


@app.route('/api/job-status/<job_id>')
def api_job_status(job_id):
    import threading
    mail_url = os.getenv('MAIL_API_URL', 'https://mail.zerocoder.info')
    mail_token = os.getenv('MAIL_API_TOKEN', '')
    gc_domain = os.getenv('GC_DOMAIN', 'university.zerocoder.ru')
    headers = {'Authorization': f'Bearer {mail_token}'}
    try:
        # If playwright fix is in progress, don't show the link yet
        existing = _jobs_get(job_id)
        if existing and existing.get('fixed') and not existing.get('pw_done'):
            return jsonify({'status': 'configuring', 'gc_url': None, 'done': 0, 'total': 1})
        # If playwright already done, return stored gc_url immediately
        if existing and existing.get('pw_done') and existing.get('gc_url'):
            return jsonify({'status': 'done', 'gc_url': existing['gc_url'], 'done': 1, 'total': 1})

        resp = requests.get(f'{mail_url}/api/jobs/{job_id}', headers=headers, timeout=15)
        job_data = resp.json()
        logging.info(f"job-status {job_id}: {job_data}")
        results = job_data.get('results', [])
        gc_url = None
        mailing_id = None
        if results:
            mailing_id = results[0].get('id') or results[0].get('mailing_id')
            if mailing_id:
                gc_url = f'https://{gc_domain}/notifications/control/mailings/update/id/{mailing_id}'

        # Start fix once when mailing_id is known; withhold gc_url until fix completes
        transport = _jobs_claim(job_id, mailing_id=mailing_id, gc_url=gc_url) if mailing_id else None
        if transport:
            threading.Thread(
                target=_gc_fix_mailing_playwright,
                args=(mailing_id, transport, job_id),
                daemon=True,
            ).start()
            # Don't return gc_url yet — let playwright finish first
            return jsonify({'status': 'configuring', 'gc_url': None, 'done': 0, 'total': 1})

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
