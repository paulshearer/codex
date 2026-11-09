import { zipSync, strToU8 } from 'fflate';

const xml = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&apos;');
function reference(row, col) {
  let name = '';
  for (let value = col + 1; value; value = Math.floor((value - 1) / 26)) name = String.fromCharCode(65 + (value - 1) % 26) + name;
  return `${name}${row + 1}`;
}

// EIF's export uses inline strings, numeric cells, column widths and an autofilter.
// Generated strings beginning with '=' stay literal and cannot become formulas.
export function tableWorkbook({ headers, rows, alignments = [] }) {
  const ns = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main';
  const rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';
  const all = [headers, ...rows], last = reference(rows.length, headers.length - 1);
  const cols = headers.map((_, col) => `<col min="${col + 1}" max="${col + 1}" width="${Math.min(60, all.reduce((width, row) => Math.max(width, String(row[col] ?? '').length + 2), 10))}" customWidth="1"/>`).join('');
  const data = all.map((row, r) => `<row r="${r + 1}">${headers.map((_, c) => {
    const value = String(row[c] ?? '');
    const numeric = r > 0 && /^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(value) && value.replace(/\D/g, '').length < 16;
    const style = (r === 0 ? 3 : 0) + (alignments[c] === 'right' ? 2 : alignments[c] === 'center' ? 1 : 0);
    return `<c r="${reference(r, c)}" s="${style}"${numeric ? '' : ' t="inlineStr"'}>${numeric ? `<v>${xml(value)}</v>` : `<is><t xml:space="preserve">${xml(value)}</t></is>`}</c>`;
  }).join('')}</row>`).join('');
  const files = {
    '[Content_Types].xml': `<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>`,
    '_rels/.rels': `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="${rel}/officeDocument" Target="xl/workbook.xml"/></Relationships>`,
    'xl/workbook.xml': `<workbook xmlns="${ns}" xmlns:r="${rel}"><sheets><sheet name="Table" sheetId="1" r:id="rId1"/></sheets></workbook>`,
    'xl/_rels/workbook.xml.rels': `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="${rel}/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="${rel}/styles" Target="styles.xml"/></Relationships>`,
    'xl/worksheets/sheet1.xml': `<worksheet xmlns="${ns}"><dimension ref="A1:${last}"/><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews><cols>${cols}</cols><sheetData>${data}</sheetData>${rows.length ? `<autoFilter ref="A1:${last}"/>` : ''}</worksheet>`,
    'xl/styles.xml': `<styleSheet xmlns="${ns}"><fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="6">${[0, 1].flatMap(font => ['left', 'center', 'right'].map(alignment => `<xf numFmtId="0" fontId="${font}" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment horizontal="${alignment}" wrapText="1"/></xf>`)).join('')}</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>`,
  };
  return zipSync(Object.fromEntries(Object.entries(files).map(([name, content]) => [name, strToU8(`<?xml version="1.0" encoding="UTF-8" standalone="yes"?>${content}`)])));
}
