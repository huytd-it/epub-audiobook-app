const KEY = "recentBooks";
const LAST_KEY = "lastBookId";
const MAX = 5;

export type RecentBook = { id: number; title: string; filename?: string; at: string };

function safeParse(raw: string | null, fallback: any) {
  try {
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

export function getRecentBooks(): RecentBook[] {
  try {
    const arr = safeParse(localStorage.getItem(KEY), []);
    return Array.isArray(arr) ? arr : [];
  } catch {
    return [];
  }
}

export function getLastBookId(): number | null {
  try {
    const v = localStorage.getItem(LAST_KEY);
    const n = v ? Number(v) : NaN;
    return Number.isFinite(n) ? n : null;
  } catch {
    return null;
  }
}

export function pushRecentBook(book: { id: number; title: string; original_filename?: string }) {
  try {
    const now = new Date().toISOString();
    const entry: RecentBook = { id: book.id, title: book.title, filename: book.original_filename, at: now };
    let arr = getRecentBooks().filter((b) => b.id !== book.id);
    arr.unshift(entry);
    arr = arr.slice(0, MAX);
    localStorage.setItem(KEY, JSON.stringify(arr));
    localStorage.setItem(LAST_KEY, String(book.id));
  } catch {}
}

export function timeAgo(iso: string): string {
  try {
    const t = new Date(iso.includes("T") ? iso : iso.replace(" ", "T") + "Z").getTime();
    if (!t) return "";
    const diff = Date.now() - t;
    const m = Math.floor(diff / 60000);
    if (m < 1) return "vừa xong";
    if (m < 60) return `${m} phút trước`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h} giờ trước`;
    const d = Math.floor(h / 24);
    return `${d} ngày trước`;
  } catch {
    return "";
  }
}
