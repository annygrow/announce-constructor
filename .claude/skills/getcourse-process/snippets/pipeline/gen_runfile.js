// Генерит run_create_mailings.js — функцию для browser_run_code_unsafe({filename})
const fs = require('fs');
const DIR = 'C:/Users/USER/Documents/ClaudeCode/Announce w Eugenia/gc_output/';
const built = JSON.parse(fs.readFileSync(DIR + 'letters_built.json', 'utf8'));
const START_INDEX = parseInt(process.argv[2] || '0', 10);
const MAX_COUNT = parseInt(process.argv[3] || '999', 10);

const LETTERS_JSON = JSON.stringify(built.map(b => ({ name: b.name, subject: b.subject, html: b.html })));

const code = `async (page) => {
  const BASE = 'https://university.zerocoder.ru';
  const CREDS = { email: 'tech@zerocoder.info', password: '+@3+2K2x' }; // заполнить перед запуском (или брать из доступ.txt рядом)
  const START_INDEX = ${START_INDEX};
  const MAX_COUNT = ${MAX_COUNT};
  const LETTERS = ${LETTERS_JSON};

  // login guard
  await page.goto(BASE + '/notifications/control/mailings/new', { waitUntil: 'domcontentloaded' });
  if (/\\/cms\\/system\\/login/.test(page.url())) {
    await page.getByRole('textbox', { name: 'Введите почту' }).fill(CREDS.email);
    await page.getByRole('textbox', { name: 'Введите пароль' }).fill(CREDS.password);
    await page.getByRole('button', { name: 'Войти на платформу' }).click();
    await page.waitForTimeout(1500);
    try { await page.getByRole('button', { name: 'Ok' }).click({ timeout: 3000 }); } catch (e) {}
  }

  const slice = LETTERS.slice(START_INDEX, START_INDEX + MAX_COUNT);
  const results = [];
  for (const m of slice) {
    try {
      await page.goto(BASE + '/notifications/control/mailings/new', { waitUntil: 'domcontentloaded' });
      await page.evaluate((m) => {
        $('#Mailing_title').val(m.name);
        $('#Mailing_sendFrom').val('');
        $('#w1').val('0').trigger('change');
        $('#transport').val('email').trigger('change');
        $('#Mailing_object_type_0').prop('checked', true);
      }, m);
      await Promise.all([
        page.waitForURL('**/mailings/update/**', { timeout: 30000 }),
        page.getByRole('button', { name: 'Создать рассылку' }).click(),
      ]);
      const id = (page.url().match(/update\\/id\\/(\\d+)/) || [])[1];
      await page.evaluate((m) => {
        $('#Mailing_subject').val(m.subject);
        const $c = $('#Mailing_content');
        if ($c.next('.note-editor').length) $c.summernote('code', m.html);
        else $c.val(m.html);
      }, m);
      await Promise.all([
        page.waitForNavigation({ timeout: 30000 }).catch(() => {}),
        page.evaluate(() => {
          const btn = [...document.querySelectorAll('form button')]
            .find(b => b.offsetParent !== null && b.textContent.includes('Сохранить') && !b.textContent.includes('Готово'));
          if (!btn) throw new Error('Сохранить не найдено');
          btn.click();
        }),
      ]);
      results.push({ name: m.name, id, ok: true });
    } catch (e) {
      results.push({ name: m.name, error: String(e).slice(0, 150) });
    }
  }
  return results;
}`;

fs.writeFileSync(DIR + 'run_create_mailings.js', code, 'utf8');
console.log('written run_create_mailings.js  START=' + START_INDEX + ' COUNT=' + MAX_COUNT + ' total=' + built.length);
