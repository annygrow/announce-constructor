# -*- coding: utf-8 -*-
"""
mitmproxy-аддон: запрещает ИИ-агенту активацию в GetCourse, оставляя всё остальное.
Блокирует ровно два действия (по сигнатуре HTTP-запроса, а не по UI):
  1) Рассылка — «Готово к отправке»  -> POST .../mailings/update/...  с полем  ready
     (обычное «Сохранить» поля ready НЕ шлёт -> проходит, черновики работают)
  2) Процесс  — «Одобрить/активировать» -> POST .../tasks/mission/update  с  ParamsObject[approved]=1
     (обычное сохранение шлёт approved=0 -> проходит, сборка процесса работает)

Запрос режется на сетевом слое, поэтому обойти из браузера нельзя:
клик, отправка формы и даже fetch из произвольного JS идут через прокси.

Запуск:
    pip install mitmproxy
    mitmdump -s block_gc.py --listen-port 8080
Браузер агента направить через http://127.0.0.1:8080 (см. README.md рядом).
"""
from mitmproxy import http

# аккаунт(ы), к которым применяем правила
HOSTS = {"university.zerocoder.ru"}


def _field_value(r, name):
    """Значение поля формы из urlencoded или multipart, иначе None."""
    try:
        if name in r.urlencoded_form:
            return r.urlencoded_form[name]
    except Exception:
        pass
    try:
        v = r.multipart_form.get(name.encode())
        if v is not None:
            return v.decode("utf-8", "ignore")
    except Exception:
        pass
    return None


def _field_present(r, name):
    """Есть ли вообще поле с таким именем (учитывая сырое multipart-тело)."""
    if _field_value(r, name) is not None:
        return True
    raw = r.raw_content or b""
    return ('name="%s"' % name).encode() in raw


def _field_is(r, name, val):
    """Поле name имеет значение val (надёжно для urlencoded и multipart)."""
    v = _field_value(r, name)
    if v is not None:
        return v == val
    raw = r.raw_content or b""
    pats = [
        ('name="%s"\r\n\r\n%s' % (name, val)).encode(),                 # multipart
        ('%s=%s' % (name, val)).encode(),                               # urlencoded (как есть)
        ('%s=%s' % (name.replace("[", "%5B").replace("]", "%5D"), val)).encode(),  # urlencoded (escaped)
    ]
    return any(p in raw for p in pats)


def _deny(flow, msg):
    flow.response = http.Response.make(
        403, msg.encode("utf-8"), {"Content-Type": "text/plain; charset=utf-8"}
    )


def request(flow: http.HTTPFlow):
    r = flow.request
    if r.pretty_host not in HOSTS or r.method != "POST":
        return

    # 1) Рассылка: «Готово к отправке»
    if "/mailings/update/" in r.path and _field_present(r, "ready"):
        _deny(flow, "Активация рассылки отключена политикой агента (Готово к отправке).")
        return

    # 2) Процесс: одобрение/активация
    if "/tasks/mission/update" in r.path and _field_is(r, "ParamsObject[approved]", "1"):
        _deny(flow, "Активация процесса отключена политикой агента (Одобрено=1).")
        return


def response(flow: http.HTTPFlow):
    # Косметика: спрятать сами кнопки (необязательно — запрос уже заблокирован выше).
    if flow.request.pretty_host not in HOSTS:
        return
    ct = flow.response.headers.get("content-type", "")
    if "text/html" in ct and flow.response.content and b"</head>" in flow.response.content:
        css = (
            b"<style>"
            b'button[name="ready"],'
            b".btn-normalize,.btn-activate,.btn-approve,"
            b'#paramsobject-approved,label[for="paramsobject-approved"]'
            b"{display:none!important}"
            b"</style>"
        )
        flow.response.content = flow.response.content.replace(b"</head>", css + b"</head>", 1)
