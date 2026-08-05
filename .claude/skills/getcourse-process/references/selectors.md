# Карта селекторов и полей GetCourse (admin)

Проверено на аккаунте lilyacreates-ugc, июнь 2026. При расхождениях — пересними структуру через `browser_evaluate` (дамп form/inputs).

## Логин
- URL: `/cms/system/login`
- Поля: `textbox "Введите почту"`, `textbox "Введите пароль"`; кнопка `Войти на платформу`.
- После входа: закрыть плашку cookies (`button "Ok"`) — иначе перехватывает клики.

## Рассылка — форма создания (`/notifications/control/mailings/new`, БЕЗ `/pl/`)
POST `Mailing[...]`:
- `#Mailing_title` — `Mailing[title]` (Название) *
- `#Mailing_sendFrom` — `Mailing[sendFrom]` (Имя отправителя; пусто = по умолчанию)
- `#w1` — `Mailing[category_id]` select: `0`=Общие рассылки, `-1`=Уведомления *
- `Mailing[object_type]` radio: `#Mailing_object_type_0`=Пользователи(UserContext), `_1`=Заказы(DealContext), `_2`=Покупки(UserProductContext)
- `#transport` — `Mailing[transport]`: `email`=Email, `sms`=SMS, `ticket`=Telegram, `fb_messenger`=Facebook, `vk`=Vkontakte, `chatium`=Приложение, `whatsapp`=WhatsApp/Max/WABA(Getloo.ru), `max`=MAX. Проверено JS: `Array.from(document.querySelector('#transport').options).map(o=>o.value+'='+o.text)`.
- Кнопка `Создать рассылку` → редирект на `/notifications/control/mailings/update/id/<ID>/part/main`

## Бот для рассылок (Telegram и MAX)

**Telegram** (`transport=ticket`): `select[name="ParamsObject[telegram_bot_id]"]`
- `0` = "Любой бот" ← всегда ставить
- `63686` = "zerocoder_call_bot"
- `71831` = "zerocoder_university_bot"
- `12930` = "zerocoder_study_bot"

**MAX** (`transport=max`): `select[name="ParamsObject[max_bot_id]"]`
- `0` = "Любой бот" ← всегда ставить
- `48` = "[dev] Бот безопасности"
- `223` = "Зерокот"
- `491` = "Зерокодер"

Устанавливать через JS: `document.querySelector('select[name="ParamsObject[telegram_bot_id]"]').value = '0'` (или `max_bot_id` для MAX), затем нажать «Сохранить».

## Рассылка — редактор (`.../update/id/<ID>/part/main`)
- `#Mailing_subject` — `Mailing[subject]` (Тема)
- `#Mailing_content` — `Mailing[content]` (тело; **summernote** — ставить `$('#Mailing_content').summernote('code', body)`, если есть `.note-editor` рядом)
- Получатель: `ParamsObject[recipients_type]` radio — `nobody` (по умолчанию, оставлять для черновика), `all`, `segment`
- Расписание: `Mailing[schedule_type]` — `immediately` / `in_time` / `user_in_rule` / `from_mission`
- Кнопки: «Сохранить» (черновик; `<button>` часто БЕЗ `type=submit`, текст с глиф-иконкой → искать по `textContent.includes('Сохранить')`), «Готово к отправке» = `button[name=ready]` — **НЕ нажимать** (активирует).
- chatium-поля: `#ParamsObject_chatium_title`, `#ParamsObject_chatium_annotate`, `#ParamsObject_chatium_link`.

## Процесс — создание (`/pl/tasks/mission/create`)
- `#mission-title` — `Mission[title]`
- `#mission-object_type_id`: `41`=Пользователи, `42`=Заказы, `58`=Покупки, `27`=Звонки
- Кнопка `Создать` → `/pl/tasks/mission/update?id=<missionId>`

## Процесс — общее (`/pl/tasks/mission/update?id=`)
- «Массовое создание задач»: radio `Отключено` (по умолчанию — оставлять) / `Единоразово после запуска` / `Периодическая проверка`
- «Одобрено» — checkbox (не ставить без задачи запуска)

## Процесс — конструктор (`/pl/tasks/mission/process?id=`)
- Инстанс: `$('#flowchart').flowchartPlugin('instance')` (далее `inst`).
- jsPlumb: `inst.instance`. Модалка блока: `$(inst.scriptModalEl)[0]`; видимость `$(inst.scriptModalEl).is(':visible')`.
- Добавить блок: меню-кнопка «Добавить блок», пункты `a.btn-add-block[data-block-type=...]`:
  `question` (Вопрос менеджеру), `operation` (Операция), `callbackOperation`, `condition` (Условие), `note` (Заметка),
  `delayed` (Задержка), `waitCondition` (Ожидание условия), `currentTime` (Текущее время), `proxy` (Прокси-скрипт),
  `subprocess` (Подпроцесс), `finish` (Завершение), `voice`, `section`.
- Блок DOM-элемент: `#fwb<blockId>`. Выходной эндпоинт (uuid): `<blockId>-success`; доп. выходы `<blockId>-result1`, `-result2` …
- Эндпоинты конструктора: `inst.endpoints` (ключи `<id>-success`, `<id>-resultN`).

### Модалка «Операция» → «Отправить письмо по рассылке»
- Контекст: radio `contextKey` = `object` (Пользователь, по умолч.) / `task` (Задача)
- Тип операции: radio `operationType` = `add_mailing_message` (см. также `user_addtogroup`, `user_removefromgroup`, `add_to_mailing_category`, отправки в мессенджеры и т.д.)
- После выбора operationType и save модалки появляется конфиг: скрытый `AddMailingMessageOperation[mailing_id]` (`#addmailingmessageoperation-mailing_id`) + select2 «Рассылка». Ставить: `$('#addmailingmessageoperation-mailing_id').select2('data',{id,text})`.
- `input[name=title]` — название блока. `button[name=save]` — сохранить. `button[name=copy]`, `button[name=delete-process]`.

### Модалка «Задержка»
- radio `delay_type`: пусто = «От текущего момента», `absolute` = «Фиксированное время».
- 3 текст-инпута **без name** (по порядку): дни, часы, минуты. Минуты по умолчанию `1`.
- `input[name=title]`, save аналогично.

### Прокси / старт «Начало работы»
- `select[name=resultsCount]` (1..4) — число выходов. После сохранения и перезагрузки доп. выходы = uuid `<id>-resultN`.

## Эндпоинты сохранения
- `POST /pl/tasks/mission/flowchart-data?id=<missionId>` — отдаёт `{data:{flowchartData:{blocks,connections,endpoints}, issetScriptErrors, processAnalysis}}`.
- `POST /pl/tasks/mission/save-scripts?id=<missionId>` — сохраняет блоки как `{id, coord}` (только раскладка). Вызывается `inst.saveScripts(missionId)`.
- `POST /pl/tasks/mission/create-connection` — авто при `inst.instance.connect(...)`.

## Связь (стрелка) — рабочий способ
```js
const jp = inst.instance;
let src = jp.getEndpoint('<srcId>-success'); if (Array.isArray(src)) src = src[0];
const tgt = document.getElementById('fwb<dstId>');
jp.connect({ source: src, target: tgt });   // → авто POST create-connection
```
Вариант `jp.connect({uuids:['<src>-success','<dst>-target']})` НЕненадёжен — не использовать.
Один `-success` = одна исходящая связь; для нескольких веток увеличить `resultsCount` и брать `-resultN`.
