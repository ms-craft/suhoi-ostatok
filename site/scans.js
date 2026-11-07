/* Генераторы SVG-«сканов» тетрадных листов.
   Синяя ручка ученика, терракотовый маркер учителя, клетка — как в тетради. */
window.SK = window.SK || {};

(function () {
  let uid = 0;
  const HAND = '#22356E';
  const ERR = '#C34811';
  const GRID = '#D9E4F1';
  const MARGIN = '#E7C6B8';

  function esc(s) {
    return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  }

  function textWidth(text, size) {
    return text.length * size * 0.46;
  }

  /* Основной лист.
     opts: { w,h, cell, name, equations:[{text,y,size,error,kind}] } */
  function sheet(opts) {
    const o = Object.assign({ w: 520, h: 700, cell: 26, name: '', equations: [] }, opts);
    const id = 'g' + (++uid);
    const lines = [];

    // сетка-клетка
    lines.push(
      `<defs><pattern id="${id}" width="${o.cell}" height="${o.cell}" patternUnits="userSpaceOnUse">` +
      `<path d="M ${o.cell} 0 L 0 0 0 ${o.cell}" fill="none" stroke="${GRID}" stroke-width="1"/></pattern></defs>`
    );
    lines.push(`<rect width="${o.w}" height="${o.h}" fill="#FDFCF9"/>`);
    lines.push(`<rect width="${o.w}" height="${o.h}" fill="url(#${id})"/>`);
    // поле (вертикальная линия слева)
    lines.push(`<line x1="52" y1="0" x2="52" y2="${o.h}" stroke="${MARGIN}" stroke-width="1.5"/>`);
    // лёгкое затемнение у верхней кромки — как у листа на фото
    lines.push(`<rect width="${o.w}" height="${o.h}" fill="url(#sh${id})" opacity="0"/>`);

    if (o.name) {
      lines.push(
        `<text x="72" y="72" font-family="Caveat, cursive" font-size="34" fill="${HAND}">${esc(o.name)}</text>`
      );
    }

    for (const eq of o.equations) {
      const size = eq.size || 30;
      const x = eq.x != null ? eq.x : 72;
      const y = eq.y;
      const color = eq.error ? ERR : HAND;
      if (eq.error) {
        const w = textWidth(eq.text, size) + 34;
        lines.push(
          `<rect x="${x - 17}" y="${y - size - 2}" width="${w}" height="${size + 20}" rx="9" ` +
          `fill="#FBE6DB" fill-opacity="0.55" stroke="${ERR}" stroke-width="2.4"/>`
        );
      }
      lines.push(
        `<text x="${x}" y="${y}" font-family="Caveat, cursive" font-size="${size}" ` +
        `fill="${color}"${eq.italic ? ' font-style="italic"' : ''}>${esc(eq.text)}</text>`
      );
    }
    return `<svg viewBox="0 0 ${o.w} ${o.h}" xmlns="http://www.w3.org/2000/svg" preserveAspectRatio="xMidYMid meet">${lines.join('')}</svg>`;
  }

  /* Уменьшенный лист для превью-загрузки (без выражений, только имя и № ) */
  function uploadThumb(name, taskNo) {
    return sheet({
      w: 380, h: 500, cell: 22, name,
      equations: [
        { text: '№ ' + taskNo + '   Контрольная работа', y: 140, size: 22 },
        { text: '…', y: 210, size: 26 },
      ],
    });
  }

  /* Полный лист Ани с решёнными номерами */
  function anyaFull(highlightTask) {
    const eq = [
      { text: '№ 1   Контрольная работа', y: 148, size: 26 },
      { text: '4,7 + 7,7 = 11,4', y: 236, size: 30, error: highlightTask === 1 },
      { text: '№ 2   24 − 3 × 2 = 42', y: 316, size: 26 },
      { text: '№ 3   52 − 5 = 47', y: 380, size: 26 },
      { text: '№ 4   15 × 2 = 30 (км)', y: 444, size: 26 },
    ];
    return sheet({ w: 520, h: 700, cell: 26, name: 'Аня Коваленко', equations: eq });
  }

  function maxFull(highlightTask) {
    const eq = [
      { text: '№ 1   Контрольная работа', y: 148, size: 26 },
      { text: '4,7 + 7,7 = 11,14', y: 236, size: 30, error: true },
    ];
    return sheet({ w: 520, h: 700, cell: 26, name: 'Максим Сидоров', equations: eq });
  }

  function veraFull() {
    const eq = [
      { text: '№ 3', y: 148, size: 26 },
      { text: '52 − 5 = 53', y: 240, size: 32, error: true },
    ];
    return sheet({ w: 520, h: 700, cell: 26, name: 'Вера Пашкевич', equations: eq });
  }

  /* Мини-лист для карточек примеров (кластер) */
  function mini(name, equation, error) {
    return sheet({
      w: 440, h: 420, cell: 22, name,
      equations: [{ text: equation, y: 250, size: 30, error: !!error }],
    });
  }

  window.SK.scan = { sheet, uploadThumb, anyaFull, maxFull, veraFull, mini };
})();
