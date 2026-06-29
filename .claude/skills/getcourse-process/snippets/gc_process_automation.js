/*
 * GetCourse: рассылки + процессы — готовые сниппеты для Playwright MCP.
 * Все функции принимают `page` (mcp__playwright__browser_run_code_unsafe).
 * BASE — origin аккаунта, напр. 'https://lilyacreates-ugc.getcourse.ru'.
 * Проверено на аккаунте lilyacreates-ugc (июнь 2026).
 *
 * Это РЕФЕРЕНС: копируй нужные функции в browser_run_code_unsafe и адаптируй данные.
 */

// ───────────────────────── ЛОГИН ─────────────────────────
async function login(page, BASE, email, password) {
  await page.goto(BASE + '/cms/system/login', {waitUntil:'domcontentloaded'});
  await page.getByRole('textbox', {name:'Введите почту'}).fill(email);
  await page.getByRole('textbox', {name:'Введите пароль'}).fill(password);
  await page.getByRole('button', {name:'Войти на платформу'}).click();
  await page.waitForTimeout(1500);
  // закрыть плашку cookies (перехватывает клики)
  try { await page.getByRole('button', {name:'Ok'}).click({timeout:3000}); } catch(e){}
}

// ──────────────────── РАССЫЛКИ (пачкой) ────────────────────
// list: [{title, transport:'email'|'chatium', subject, body}]
// category: '0' (Общие) | '-1' (Уведомления). objectType radio id: Mailing_object_type_0 = Пользователи.
// Возвращает [{title, id, ok}]. Черновики, получатель «Никому», НЕ «Готово к отправке».
async function createMailings(page, BASE, list, category) {
  category = category || '0';
  const results = [];
  for (const m of list) {
    try {
      await page.goto(BASE + '/notifications/control/mailings/new', {waitUntil:'domcontentloaded'});
      await page.evaluate(({m, category}) => {
        $('#Mailing_title').val(m.title);
        $('#Mailing_sendFrom').val('');           // отправитель по умолчанию
        $('#w1').val(category).trigger('change');  // категория (select id=w1)
        $('#transport').val(m.transport).trigger('change');
        $('#Mailing_object_type_0').prop('checked', true); // Пользователи
      }, {m, category});
      await Promise.all([
        page.waitForURL('**/mailings/update/**', {timeout:30000}),
        page.getByRole('button', {name:'Создать рассылку'}).click(),
      ]);
      const id = (page.url().match(/update\/id\/(\d+)/) || [])[1];
      // редактор: тема + тело (summernote) → для ТГ/MAX выставить «Любой бот» → Сохранить
      await page.evaluate((m) => {
        if ($('#Mailing_subject').length) $('#Mailing_subject').val(m.subject);
        const $c = $('#Mailing_content');
        if ($c.length) {
          if ($c.next('.note-editor').length) $c.summernote('code', m.body);
          else $c.val(m.body);
        }
        // Telegram и MAX: выставить «Любой бот» (id=0) — через API это не передаётся
        if (m.transport === 'ticket') {
          const s = document.querySelector('select[name="ParamsObject[telegram_bot_id]"]');
          if (s) s.value = '0';
        } else if (m.transport === 'max') {
          const s = document.querySelector('select[name="ParamsObject[max_bot_id]"]');
          if (s) s.value = '0';
        }
      }, m);
      await Promise.all([
        page.waitForNavigation({timeout:30000}).catch(()=>{}),
        page.evaluate(() => {
          const btn = [...document.querySelectorAll('form button')]
            .find(b => b.offsetParent !== null && b.textContent.includes('Сохранить') && !b.textContent.includes('Готово'));
          if (!btn) throw new Error('кнопка Сохранить не найдена');
          btn.click();
        }),
      ]);
      results.push({title:m.title, id, ok:true});
    } catch(e) { results.push({title:m.title, error:String(e).slice(0,150)}); }
  }
  return results;
}

// ──────── HTML-РАССЫЛКИ по дизайн-шаблону (пачкой) ────────
// list: [{name, subject, html}] — html = готовая inline-вёрстка (таблицы).
// summernote GC сохраняет таблицы без потерь. Черновики, получатель «Никому».
// Большой list лучше эмитить в файл и звать browser_run_code_unsafe({filename}).
async function createHtmlMailings(page, BASE, list, category) {
  category = category || '0';
  const results = [];
  for (const m of list) {
    try {
      await page.goto(BASE + '/notifications/control/mailings/new', {waitUntil:'domcontentloaded'});
      await page.evaluate(({m, category}) => {
        $('#Mailing_title').val(m.name);
        $('#Mailing_sendFrom').val('');
        $('#w1').val(category).trigger('change');
        $('#transport').val('email').trigger('change');
        $('#Mailing_object_type_0').prop('checked', true);
      }, {m, category});
      await Promise.all([
        page.waitForURL('**/mailings/update/**', {timeout:30000}),
        page.getByRole('button', {name:'Создать рассылку'}).click(),
      ]);
      const id = (page.url().match(/update\/id\/(\d+)/) || [])[1];
      await page.evaluate((m) => {
        $('#Mailing_subject').val(m.subject);
        const $c = $('#Mailing_content');
        if ($c.next('.note-editor').length) $c.summernote('code', m.html);
        else $c.val(m.html);
        // Telegram и MAX: выставить «Любой бот» (id=0) — через API это не передаётся
        if (m.transport === 'ticket') {
          const s = document.querySelector('select[name="ParamsObject[telegram_bot_id]"]');
          if (s) s.value = '0';
        } else if (m.transport === 'max') {
          const s = document.querySelector('select[name="ParamsObject[max_bot_id]"]');
          if (s) s.value = '0';
        }
      }, m);
      await Promise.all([
        page.waitForNavigation({timeout:30000}).catch(()=>{}),
        page.evaluate(() => {
          const btn = [...document.querySelectorAll('form button')]
            .find(b => b.offsetParent !== null && b.textContent.includes('Сохранить') && !b.textContent.includes('Готово'));
          if (!btn) throw new Error('Сохранить не найдено');
          btn.click();
        }),
      ]);
      results.push({name:m.name, id, ok:true});  // компактный возврат (вывод раздувается эхом кода)
    } catch(e) { results.push({name:m.name, error:String(e).slice(0,150)}); }
  }
  return results;
}
// проверка, что вёрстка сохранилась (на странице редактора рассылки)
async function verifyMailingHtml(page) {
  return await page.evaluate(() => {
    const v = $('#Mailing_content').val() || '';
    return {length:v.length, tables:(v.match(/<table/gi)||[]).length};
  });
}

// ──────────────────── ПРОЦЕСС: создать ────────────────────
// objectTypeId: '41' Пользователи | '42' Заказы | '58' Покупки | '27' Звонки
// Возвращает missionId.
async function createMission(page, BASE, title, objectTypeId) {
  await page.goto(BASE + '/pl/tasks/mission/create', {waitUntil:'domcontentloaded'});
  await page.evaluate(({title, objectTypeId}) => {
    $('#mission-title').val(title);
    $('#mission-object_type_id').val(objectTypeId).trigger('change');
  }, {title, objectTypeId});
  await Promise.all([
    page.waitForNavigation({timeout:30000}).catch(()=>{}),
    page.getByRole('button', {name:'Создать'}).click(),
  ]);
  return (page.url().match(/[?&]id=(\d+)/) || [])[1];
}

// ─────────── helpers внутри страницы процесса ───────────
// modalVisible / waitмодалки — через флаги; modal DOM = $(inst.scriptModalEl)[0]
const _waitModal = `(() => { const i=$('#flowchart').flowchartPlugin('instance'); return i && $(i.scriptModalEl).is(':visible'); })`;
const _modalGone = `(() => { const i=$('#flowchart').flowchartPlugin('instance'); return !$(i.scriptModalEl).is(':visible'); })`;
function _clickSave(){ // выполнять внутри page.evaluate
  const m = $('#flowchart').flowchartPlugin('instance'); const el = $(m.scriptModalEl)[0];
  const btn = el.querySelector('button[name=save]') ||
    [...el.querySelectorAll('button')].find(b => b.offsetParent!==null && b.textContent.includes('Сохранить'));
  btn.click();
}

// ───────── БЛОК «Отправить письмо по рассылке» (пачкой) ─────────
// На странице /pl/tasks/mission/process?id=<missionId>
// mailings: [{id, name}]  (id рассылки из createMailings)
async function addMailingBlocks(page, mailings) {
  const out = [];
  for (const ml of mailings) {
    try {
      await page.evaluate(()=>document.querySelector('.btn-add-block[data-block-type="operation"]').click());
      await page.waitForFunction(()=>{ const i=$('#flowchart').flowchartPlugin('instance'); const m=$(i.scriptModalEl)[0];
        return m && $(i.scriptModalEl).is(':visible') && m.querySelector('input[name=operationType][value="add_mailing_message"]'); }, {timeout:15000});
      await page.evaluate(()=>{ const i=$('#flowchart').flowchartPlugin('instance'); const m=$(i.scriptModalEl)[0];
        const r=[...m.querySelectorAll('input[name=operationType]')].find(x=>x.value==='add_mailing_message'); r.checked=true; r.click(); });
      await page.waitForTimeout(300);
      await page.evaluate(()=>{ const i=$('#flowchart').flowchartPlugin('instance'); $(i.scriptModalEl)[0].querySelector('button[name=save]').click(); });
      await page.waitForFunction(()=>{ const i=$('#flowchart').flowchartPlugin('instance'); return $(i.scriptModalEl)[0].querySelector('#addmailingmessageoperation-mailing_id'); }, {timeout:15000});
      await page.evaluate((ml)=>{ const i=$('#flowchart').flowchartPlugin('instance'); const m=$(i.scriptModalEl)[0];
        $('#addmailingmessageoperation-mailing_id').select2('data', {id:ml.id, text:ml.name});
        m.querySelector('input[name=title]').value = ml.name; }, ml);
      await page.waitForTimeout(200);
      await page.evaluate(()=>{ const i=$('#flowchart').flowchartPlugin('instance'); $(i.scriptModalEl)[0].querySelector('button[name=save]').click(); });
      await page.waitForFunction(()=>{ const i=$('#flowchart').flowchartPlugin('instance'); return !$(i.scriptModalEl).is(':visible'); }, {timeout:15000});
      await page.waitForTimeout(400);
      out.push({name:ml.name, ok:true});
    } catch(e){ out.push({name:ml.name, error:String(e).slice(0,120)}); }
  }
  return out;
}

// ───────── БЛОК «Задержка» (пачкой) ─────────
// delays: [{days:'', hours:'1', minutes:'', title:'Пауза 1 час'}]  (3 поля без name: days,hours,minutes по порядку)
async function addDelayBlocks(page, delays) {
  const out = [];
  for (const d of delays) {
    try {
      await page.evaluate(()=>document.querySelector('.btn-add-block[data-block-type="delayed"]').click());
      await page.waitForFunction(()=>{ const i=$('#flowchart').flowchartPlugin('instance'); const m=$(i.scriptModalEl)[0];
        return m && $(i.scriptModalEl).is(':visible') && [...m.querySelectorAll('input[type=text]')].filter(x=>x.offsetParent&&x.name!=='title').length>=3; }, {timeout:15000});
      await page.evaluate((d)=>{ const i=$('#flowchart').flowchartPlugin('instance'); const m=$(i.scriptModalEl)[0];
        const txt=[...m.querySelectorAll('input[type=text]')].filter(x=>x.offsetParent&&x.name!=='title');
        const set=(el,v)=>{el.value=v; el.dispatchEvent(new Event('input',{bubbles:true})); el.dispatchEvent(new Event('change',{bubbles:true}));};
        set(txt[0], d.days||''); set(txt[1], d.hours||''); set(txt[2], d.minutes||'');
        if (d.title) m.querySelector('input[name=title]').value=d.title; }, d);
      await page.waitForTimeout(200);
      await page.evaluate(()=>{ const i=$('#flowchart').flowchartPlugin('instance'); $(i.scriptModalEl)[0].querySelector('button[name=save]').click(); });
      await page.waitForFunction(()=>{ const i=$('#flowchart').flowchartPlugin('instance'); return !$(i.scriptModalEl).is(':visible'); }, {timeout:15000});
      await page.waitForTimeout(400);
      out.push({ok:true});
    } catch(e){ out.push({error:String(e).slice(0,120)}); }
  }
  return out;
}

// ───────── список блоков (для маппинга id → название) ─────────
async function getBlocks(page, missionId) {
  return await page.evaluate(async (missionId)=>{
    const r=await fetch('/pl/tasks/mission/flowchart-data?id='+missionId,{method:'POST',headers:{'X-Requested-With':'XMLHttpRequest'}});
    const j=await r.json();
    return j.data.flowchartData.blocks.map(b=>({id:b.id, name:b.name, cls:(b.cssClass||'').trim()}));
  }, missionId);
}

// ───────── РАСКЛАДКА (только координаты) ─────────
// coordMap: { '<blockId>': {left:'120', top:'140'} }  (значения — строки)
async function layout(page, missionId, coordMap) {
  return await page.evaluate(({missionId, coordMap})=>{
    const inst = $('#flowchart').flowchartPlugin('instance');
    let n=0; inst.blocks.forEach(b=>{ const c=coordMap[String(b.id)]; if(c){ b.coord=c; n++; } });
    const a=window.alert; window.alert=()=>{}; inst.saveScripts(missionId); setTimeout(()=>window.alert=a,2000);
    return {applied:n, total:inst.blocks.length};
  }, {missionId, coordMap});
}

// ───────── ДОП. ВЫХОДЫ у блока (для веток) ─────────
// Открыть блок, выставить число выходов. Для proxy/старта select name=resultsCount.
// После — перезагрузить страницу: доп. выходы получат uuid '<id>-result1', '<id>-result2', …
async function setBlockOutputs(page, missionId, blockId, count) {
  await page.evaluate(async ({missionId, blockId})=>{
    const inst=$('#flowchart').flowchartPlugin('instance');
    $(inst.scriptModalEl).scriptWindow('openScript', missionId, blockId);
    await new Promise(f=>setTimeout(f,1500));
  }, {missionId, blockId});
  await page.evaluate((count)=>{
    const m=$('#flowchart').flowchartPlugin('instance'); const el=$(m.scriptModalEl)[0];
    const sel=el.querySelector('select[name=resultsCount]'); sel.value=String(count); $(sel).trigger('change');
    el.querySelector('button[name=save]').click();
  }, count);
  await page.waitForTimeout(1000);
}

// ───────── СВЯЗИ (стрелки) ─────────
// edges: [['<srcId>','<dstId>'], ...]. srcOutput по умолчанию 'success';
// для доп. выходов передавай ['<srcId>','<dstId>','result1'].
// ВАЖНО: source = объект эндпоинта, target = DOM-элемент 'fwb<dst>'.
async function connectEdges(page, edges) {
  const out=[];
  for (const e of edges) {
    const [s,t,outp] = e; const port = outp||'success';
    const r = await page.evaluate(({s,t,port})=>{
      try {
        const jp=$('#flowchart').flowchartPlugin('instance').instance;
        let src=jp.getEndpoint(s+'-'+port); if(Array.isArray(src)) src=src[0];
        const tgt=document.getElementById('fwb'+t);
        if(!src) return {err:'нет эндпоинта '+s+'-'+port}; if(!tgt) return {err:'нет блока '+t};
        return {ok: !!jp.connect({source:src, target:tgt})};
      } catch(err){ return {err:String(err).slice(0,100)}; }
    }, {s,t,port});
    out.push({edge:`${s}->${t}`, ...r});
    await page.waitForTimeout(350);
  }
  return out;
}

// ───────── ПРОВЕРКА ─────────
async function verify(page, missionId) {
  return await page.evaluate(async (missionId)=>{
    const r=await fetch('/pl/tasks/mission/flowchart-data?id='+missionId,{method:'POST',headers:{'X-Requested-With':'XMLHttpRequest'}});
    const j=await r.json(); const d=j.data;
    return {blocks:d.flowchartData.blocks.length, connections:d.flowchartData.connections.length, issetScriptErrors:d.issetScriptErrors};
  }, missionId);
}

/* ─────────────────────────────────────────────────────────────
 * ПРИМЕР сборки «2 параллельные ветки по 5 писем с паузами 1ч»:
 *
 * const mail = await createMailings(page, BASE, [
 *   {title:'Email — тема 1', transport:'email', subject:'тема 1', body:'текст 1'}, ... ,
 *   {title:'App — тема 1', transport:'chatium', subject:'тема 1', body:'текст 1'}, ...
 * ], '0');
 * const missionId = await createMission(page, BASE, 'Рассылки по пользователям', '41');
 * await page.goto(BASE+'/pl/tasks/mission/process?id='+missionId);
 * await addMailingBlocks(page, [{id:'<id E1>', name:'Email — тема 1'}, ...]);
 * await addDelayBlocks(page, Array(8).fill({hours:'1', title:'Пауза 1 час'}));
 * const blocks = await getBlocks(page, missionId);  // взять id блоков по именам/типам
 * // start block — это cls 'start-flowchart-block'
 * await layout(page, missionId, coordMap);          // 2 колонки сверху вниз
 * await setBlockOutputs(page, missionId, startId, 2); // старту 2 выхода
 * // reload страницы, затем:
 * await connectEdges(page, [
 *   [startId, emailHead, 'success'], [startId, appHead, 'result1'],
 *   ...пары по цепочке каждой ветки...
 * ]);
 * await verify(page, missionId); // {blocks, connections, issetScriptErrors:false}
 * ───────────────────────────────────────────────────────────── */
