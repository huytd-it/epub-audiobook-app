/**
 * Small CSV/JSON helpers for the browser-side export/import flows.
 *
 * Deliberately minimal: the sheets these read and write are the ones this app
 * produced, so only the CSV rules a spreadsheet actually emits are handled
 * (quoted fields, doubled quotes inside them, CRLF line endings).
 */

/** Serialize rows to CSV using `columns` as the header, in that order. */
export function toCsv(rows: Array<Record<string, unknown>>, columns: string[]): string {
  const cell = (value: unknown) => {
    const text = value === null || value === undefined ? "" : String(value);
    return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  return [
    columns.join(","),
    ...rows.map((row) => columns.map((column) => cell(row[column])).join(",")),
  ].join("\n");
}

/** Parse a CSV sheet into row objects keyed by its header. */
export function parseCsv(text: string): Array<Record<string, string>> {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let quoted = false;
  const source = text.replace(/^﻿/, "");

  const endField = () => {
    row.push(field);
    field = "";
  };
  const endRow = () => {
    endField();
    if (row.length > 1 || row[0] !== "") rows.push(row);
    row = [];
  };

  for (let i = 0; i < source.length; i += 1) {
    const char = source[i];
    if (quoted) {
      if (char === '"' && source[i + 1] === '"') {
        field += '"';
        i += 1;
      } else if (char === '"') {
        quoted = false;
      } else {
        field += char;
      }
      continue;
    }
    if (char === '"') quoted = true;
    else if (char === ",") endField();
    else if (char === "\n") endRow();
    else if (char !== "\r") field += char;
  }
  if (field !== "" || row.length > 0) endRow();

  const [header, ...body] = rows;
  if (!header) return [];
  return body.map((cells) =>
    Object.fromEntries(header.map((name, index) => [name.trim(), cells[index] ?? ""]))
  );
}

/**
 * Read an exported sheet back, whichever format it is in. JSON accepts the
 * `{items: [...]}` / `{uploads: [...]}` wrapper the exports use, or a bare array.
 */
export function parseSheet(text: string, format: "json" | "csv"): Array<Record<string, string>> {
  if (format === "csv") return parseCsv(text);
  const payload = JSON.parse(text);
  const rows = Array.isArray(payload) ? payload : payload?.items ?? payload?.uploads;
  if (!Array.isArray(rows)) throw new Error("JSON phải là danh sách bản ghi hoặc có khóa 'items'");
  if (rows.some((row: unknown) => typeof row !== "object" || row === null || Array.isArray(row))) {
    throw new Error("Mỗi bản ghi trong JSON phải là một object");
  }
  return rows as Array<Record<string, string>>;
}

/** Guess the format from a filename, defaulting to JSON. */
export function formatOf(filename: string): "json" | "csv" {
  return filename.toLowerCase().endsWith(".csv") ? "csv" : "json";
}

/** Trigger a browser download of `content` under `filename`. */
export function downloadTextFile(filename: string, content: string, mimeType: string): void {
  // The BOM keeps Excel from mangling Vietnamese titles in CSV exports.
  const body = mimeType.startsWith("text/csv") ? `﻿${content}` : content;
  const url = URL.createObjectURL(new Blob([body], { type: `${mimeType};charset=utf-8` }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

/** `youtube-playlist-20260819-1530` style stamp for generated filenames. */
export function fileStamp(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
}
