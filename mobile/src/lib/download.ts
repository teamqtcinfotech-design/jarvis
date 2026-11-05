/**
 * Downloads under every data card: CSV for tables and charts, PDF for the day plan and the briefing, and plain
 * text to share. Files are written to the app's cache and handed to the phone's share sheet (WhatsApp, Drive,
 * email, "save to Files"). Nothing is uploaded anywhere by JARVIS itself.
 */
import { File, Paths } from 'expo-file-system';
import * as Print from 'expo-print';
import * as Sharing from 'expo-sharing';
import { Share } from 'react-native';

import type { DataView } from './types';

const esc = (v: unknown) => {
  const s = String(v ?? '');
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};
const cell = (row: object, key: string): unknown => (row as unknown as Record<string, unknown>)[key];

export function toCsv(view: DataView): string {
  if (view.layout === 'bars' && view.series.length) {
    const head = 'Day,Date,Items,Busy minutes,Heavy,Off';
    const lines = view.series.map((p) => [p.label, p.date, p.value, p.minutes, p.heavy ? 'yes' : '', p.off ? 'yes' : ''].map(esc).join(','));
    return [head, ...lines].join('\n');
  }
  const cols = view.columns.length ? view.columns : [{ key: 'title', label: 'What' }];
  const extra = view.layout === 'progress' ? [{ key: 'progress', label: 'Progress %' }] : [];
  const all = [...cols, ...extra, { key: 'priority', label: 'Priority' }];
  const head = all.map((c) => esc(c.label)).join(',');
  const lines = view.rows.map((r) => all.map((c) => esc(cell(r, c.key))).join(','));
  return [head, ...lines].join('\n');
}

const htmlEsc = (v: unknown) => String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

export function toHtml(view: DataView, extraText = ''): string {
  const stats = view.stats.length
    ? `<div class="tiles">${view.stats.map((t) => `<div class="tile"><div class="v">${htmlEsc(t.value)}</div><div class="l">${htmlEsc(t.label)}</div></div>`).join('')}</div>`
    : '';
  let body = '';
  if (view.layout === 'bars' && view.series.length) {
    const max = Math.max(1, ...view.series.map((p) => p.value));
    body = `<div class="bars">${view.series
      .map((p) => `<div class="bar"><div class="fill${p.heavy ? ' heavy' : ''}" style="height:${Math.round((p.value / max) * 120)}px"></div><div class="n">${p.value}</div><div class="l">${htmlEsc(p.label)}</div></div>`)
      .join('')}</div>`;
  }
  if (view.timeline.length) {
    body += `<table><tr><th>When</th><th>What</th><th>Type</th></tr>${view.timeline
      .map((t) => `<tr><td>${htmlEsc(t.start)} – ${htmlEsc(t.end)}</td><td>${htmlEsc(t.title)}</td><td>${htmlEsc(t.kind)}</td></tr>`)
      .join('')}</table>`;
  }
  if (view.rows.length && !view.timeline.length) {
    const cols = view.columns.length ? view.columns : [{ key: 'title', label: 'What' }];
    body += `<table><tr>${cols.map((c) => `<th>${htmlEsc(c.label)}</th>`).join('')}${view.layout === 'progress' ? '<th>Progress</th>' : ''}</tr>${view.rows
      .map(
        (r) =>
          `<tr>${cols.map((c) => `<td>${htmlEsc(cell(r, c.key))}</td>`).join('')}${
            view.layout === 'progress' ? `<td><div class="p"><div style="width:${r.progress}%"></div></div></td>` : ''
          }</tr>`,
      )
      .join('')}</table>`;
  }
  const extra = extraText ? `<p class="text">${htmlEsc(extraText).replace(/\n/g, '<br/>')}</p>` : '';
  return `<!doctype html><html><head><meta charset="utf-8"><style>
  body{font-family:-apple-system,Roboto,Helvetica,Arial,sans-serif;color:#1a1a2e;padding:28px;}
  h1{font-size:22px;margin:0 0 4px} .sub{color:#6b6b80;margin:0 0 18px} .brand{color:#5b5bd6;font-weight:700;letter-spacing:2px;font-size:12px;margin-bottom:14px}
  .tiles{display:flex;gap:10px;margin:0 0 18px;flex-wrap:wrap} .tile{background:#ececfb;border-radius:12px;padding:10px 14px;min-width:90px}
  .tile .v{font-size:22px;font-weight:700} .tile .l{font-size:11px;color:#6b6b80;text-transform:uppercase;letter-spacing:.6px}
  table{border-collapse:collapse;width:100%;font-size:13px} th{text-align:left;color:#6b6b80;font-weight:600;border-bottom:1px solid #e6e6ef;padding:8px 6px}
  td{padding:8px 6px;border-bottom:1px solid #f0f0f5;vertical-align:top}
  .bars{display:flex;align-items:flex-end;gap:14px;height:170px;margin:10px 0 24px} .bar{text-align:center;width:48px}
  .fill{background:#5b5bd6;border-radius:6px 6px 0 0} .fill.heavy{background:#c0392b} .n{font-size:12px;margin-top:4px} .bar .l{font-size:11px;color:#6b6b80}
  .p{background:#f3f3f7;border-radius:6px;height:8px;width:90px} .p div{background:#5b5bd6;height:8px;border-radius:6px}
  .text{font-size:15px;line-height:1.6;margin-top:18px}
  </style></head><body><div class="brand">JARVIS</div><h1>${htmlEsc(view.title)}</h1><p class="sub">${htmlEsc(view.subtitle)}</p>${stats}${body}${extra}</body></html>`;
}

async function shareFile(uri: string, mimeType: string, title: string) {
  if (await Sharing.isAvailableAsync()) {
    await Sharing.shareAsync(uri, { mimeType, dialogTitle: title });
  } else {
    await Share.share({ url: uri, title });
  }
}

export async function downloadCsv(view: DataView) {
  const file = new File(Paths.cache, `${view.file_name}.csv`);
  file.write(toCsv(view));
  await shareFile(file.uri, 'text/csv', view.title);
}

export async function downloadPdf(view: DataView, extraText = '') {
  const { uri } = await Print.printToFileAsync({ html: toHtml(view, extraText), base64: false });
  // Give it a readable name before sharing.
  try {
    const named = new File(Paths.cache, `${view.file_name}.pdf`);
    if (named.exists) named.delete();
    new File(uri).move(named);
    await shareFile(named.uri, 'application/pdf', view.title);
  } catch {
    await shareFile(uri, 'application/pdf', view.title);
  }
}

export async function shareText(view: DataView, extraText = '') {
  const text = extraText ? `${view.title}\n\n${extraText}` : view.text;
  await Share.share({ message: text, title: view.title });
}
