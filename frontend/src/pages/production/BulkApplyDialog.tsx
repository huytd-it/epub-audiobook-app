import React, { useEffect, useMemo, useState } from "react";
import { AlertTriangle, CheckCircle2, Search } from "lucide-react";
import { api, postJson } from "@/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { ProductionGroup, ProductionMode, errorText } from "@/pages/book-detail/types";
import { checkboxClass, fieldClass } from "@/pages/book-detail/parts";

type BookRow = {
  id: number;
  title: string;
  status: string;
  modes: Record<ProductionGroup, ProductionMode>;
};

type BulkResult = {
  updated: number[];
  failed: { book_id: number; title?: string; error: string }[];
};

type ApplyMode = "values" | "inherit";

/** Một mục chọn được để áp: gom các khóa cấu hình liên quan thành một ô tick. */
type FieldSection = { id: string; label: string; keys: string[] };

/** Cách gom khóa thành mục cho từng nhóm. Khóa không nằm trong mục nào thành mục riêng. */
const SECTION_DEFS: Partial<Record<ProductionGroup, { label: string; match: (key: string) => boolean }[]>> = {
  video: [
    { label: "Phụ đề (font, cỡ chữ, màu, nền, padding…)", match: (k) => k.startsWith("subtitle_") },
    { label: "Waveform", match: (k) => k.startsWith("waveform_") },
    {
      label: "Khung hình & mã hoá (độ phân giải, FPS, codec, chất lượng…)",
      match: (k) => ["resolution", "fps", "fit_mode", "codec", "audio_bitrate", "quality", "concurrency"].includes(k),
    },
    {
      label: "Nền video (ảnh/video nền, gameplay, thời lượng ảnh, animation)",
      match: (k) => k.startsWith("background") || ["gameplay", "image_duration_seconds", "image_animation"].includes(k),
    },
    { label: "Nhạc nền & intro/outro", match: (k) => k.startsWith("music_") || k === "intro_voice" || k === "outro_voice" },
    {
      label: "Hiệu ứng (crossfade, Ken Burns, thanh tiến độ, credit giọng đọc)",
      match: (k) => k.startsWith("crossfade") || ["ken_burns_enabled", "progress_bar_enabled", "narrator_credit_enabled", "bg_video_variation_enabled"].includes(k),
    },
  ],
  audio: [
    { label: "TTS model, voice & tuỳ chọn model", match: (k) => ["model_id", "voice_id", "tts_options"].includes(k) },
    { label: "Độ dài chunk & hiệu ứng", match: (k) => k === "max_chars" || k === "with_effects" },
    { label: "Khoảng lặng khi ghép", match: (k) => k.endsWith("_pause_ms") },
  ],
};

const KEY_LABELS: Record<string, string> = {
  numbers: "Chuyển số, ngày giờ, đơn vị thành chữ",
  junk: "Xóa token rác từ EPUB",
  spellcheck: "Sửa dấu chấm chèn trong từ",
  dictionary: "Từ điển tiếng Việt",
  transliteration: "Phiên âm từ nước ngoài",
  abbreviations: "Mở rộng viết tắt",
  breaks: "Cue ngắt nghỉ trong câu",
  title_template: "Mẫu tiêu đề",
  description: "Description",
  genre_tags: "Thể loại / tags",
  privacy_status: "Chế độ hiển thị",
  timeline_enabled: "Timeline chương",
  description_extra: "Nội dung mở rộng (bản quyền…)",
  podcast: "Podcast",
  auto_ai_labels: "Nhãn AI tự động",
  playlist_sort_mode: "Sắp xếp playlist",
  auto_sort_episode: "Tự sắp xếp tập",
  watermark: "Watermark chữ",
  logo: "Logo",
  frame: "Ảnh khung",
  targets: "Phạm vi áp dụng (thumbnail/podcast/video)",
};

/** Khóa YouTube gắn với kênh của từng sách — backend bỏ qua khi áp hàng loạt. */
const SKIPPED_KEYS: Partial<Record<ProductionGroup, string[]>> = { youtube: ["playlist", "auto_upload"] };

function buildSections(group: ProductionGroup, keys: string[]): FieldSection[] {
  const skipped = new Set(SKIPPED_KEYS[group] || []);
  const remaining = keys.filter((key) => !skipped.has(key));
  const sections: FieldSection[] = [];
  for (const def of SECTION_DEFS[group] || []) {
    const matched = remaining.filter(def.match);
    if (matched.length) sections.push({ id: def.label, label: def.label, keys: matched });
  }
  const used = new Set(sections.flatMap((section) => section.keys));
  for (const key of remaining) {
    if (!used.has(key)) sections.push({ id: key, label: KEY_LABELS[key] || key, keys: [key] });
  }
  return sections;
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

export function BulkApplyDialog({
  open,
  onOpenChange,
  group,
  groupLabel,
  draft,
  saved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  group: ProductionGroup;
  groupLabel: string;
  /** Giá trị đang chỉnh trên form (có thể chưa lưu làm mặc định). */
  draft: Record<string, unknown>;
  /** Giá trị mặc định đã lưu. */
  saved: Record<string, unknown>;
}) {
  const [books, setBooks] = useState<BookRow[]>();
  const [loadError, setLoadError] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [mode, setMode] = useState<ApplyMode>("values");
  const [sectionIds, setSectionIds] = useState<Set<string>>(new Set());
  const [applying, setApplying] = useState(false);
  const [result, setResult] = useState<BulkResult>();
  const [error, setError] = useState("");

  const sections = useMemo(() => buildSections(group, Object.keys(draft)), [group, draft]);
  const dirty = !same(draft, saved);

  useEffect(() => {
    if (!open) return;
    setResult(undefined);
    setError("");
    setQuery("");
    setSelected(new Set());
    setMode("values");
    // Mặc định tick sẵn các mục vừa sửa so với mặc định đã lưu.
    setSectionIds(new Set(sections.filter((s) => s.keys.some((k) => !same(draft[k], saved[k]))).map((s) => s.id)));
    setLoadError("");
    api<{ items: BookRow[] }>("/production-settings/books")
      .then((res) => setBooks(res.items || []))
      .catch((err) => setLoadError(errorText(err)));
    // Chỉ khởi tạo lại khi mở dialog hoặc đổi nhóm, không theo từng lần sửa draft.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, group]);

  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const list = books || [];
    return needle ? list.filter((b) => `${b.title} #${b.id}`.toLowerCase().includes(needle)) : list;
  }, [books, query]);

  const allFilteredSelected = filtered.length > 0 && filtered.every((b) => selected.has(b.id));
  const someFilteredSelected = filtered.some((b) => selected.has(b.id));

  const toggleAll = () => {
    setSelected((current) => {
      const next = new Set(current);
      if (allFilteredSelected) filtered.forEach((b) => next.delete(b.id));
      else filtered.forEach((b) => next.add(b.id));
      return next;
    });
  };

  const toggleBook = (id: number) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const toggleSection = (id: string) =>
    setSectionIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const values = useMemo(() => {
    const out: Record<string, unknown> = {};
    sections.filter((s) => sectionIds.has(s.id)).forEach((s) => s.keys.forEach((k) => (out[k] = draft[k])));
    return out;
  }, [sections, sectionIds, draft]);

  const canApply = selected.size > 0 && (mode === "inherit" || Object.keys(values).length > 0) && !applying;

  const apply = async () => {
    setApplying(true);
    setError("");
    setResult(undefined);
    try {
      const res = await postJson<BulkResult>("/production-settings/bulk-apply", {
        group,
        mode,
        book_ids: Array.from(selected),
        values: mode === "values" ? values : {},
      });
      setResult(res);
      const fresh = await api<{ items: BookRow[] }>("/production-settings/books").catch(() => undefined);
      if (fresh) setBooks(fresh.items || []);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setApplying(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[min(90vh,46rem)] max-w-2xl flex-col overflow-hidden">
        <DialogHeader>
          <DialogTitle>Áp dụng {groupLabel.toLowerCase()} cho nhiều ebook</DialogTitle>
          <DialogDescription>
            Chọn ebook và cách áp dụng. Video/thumbnail đã render không tự làm lại; lần render kế tiếp sẽ dùng cấu hình
            mới.
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto pr-1">
          <div className="grid gap-2 sm:grid-cols-2">
            {(
              [
                {
                  value: "values",
                  title: "Ghi đè các mục đã chọn",
                  detail: "Lấy giá trị đang chỉnh trên form; các mục khác của từng ebook giữ nguyên.",
                },
                {
                  value: "inherit",
                  title: "Theo cấu hình mặc định",
                  detail: "Bỏ tùy chỉnh riêng, ebook dùng toàn bộ nhóm này từ mặc định đã lưu.",
                },
              ] as const
            ).map((option) => (
              <button
                key={option.value}
                type="button"
                onClick={() => setMode(option.value)}
                className={cn(
                  "rounded-md border p-3 text-left transition-colors",
                  mode === option.value ? "border-primary bg-primary/5" : "border-border hover:bg-muted/40"
                )}
              >
                <div className="text-xs font-semibold">{option.title}</div>
                <div className="mt-1 text-[11px] leading-4 text-muted-foreground">{option.detail}</div>
              </button>
            ))}
          </div>

          {mode === "values" ? (
            <div className="space-y-2 rounded-md border border-border p-3">
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-semibold">Mục sẽ ghi đè</span>
                <div className="flex gap-1">
                  <Button variant="ghost" size="sm" className="h-6 px-2 text-[11px]" onClick={() => setSectionIds(new Set(sections.map((s) => s.id)))}>
                    Chọn hết
                  </Button>
                  <Button variant="ghost" size="sm" className="h-6 px-2 text-[11px]" onClick={() => setSectionIds(new Set())}>
                    Bỏ hết
                  </Button>
                </div>
              </div>
              <div className="grid gap-1.5 sm:grid-cols-2">
                {sections.map((section) => {
                  const changed = section.keys.some((k) => !same(draft[k], saved[k]));
                  return (
                    <label key={section.id} className="flex cursor-pointer items-start gap-2 text-xs">
                      <input
                        type="checkbox"
                        className={cn(checkboxClass, "mt-0.5")}
                        checked={sectionIds.has(section.id)}
                        onChange={() => toggleSection(section.id)}
                      />
                      <span>
                        {section.label}
                        {changed && <span className="ml-1 text-[10px] text-amber-700">(đã sửa)</span>}
                      </span>
                    </label>
                  );
                })}
              </div>
              {group === "youtube" && (
                <p className="text-[11px] text-muted-foreground">
                  Playlist và tự động upload gắn với kênh của từng sách nên không áp hàng loạt.
                </p>
              )}
              {group === "normalization" && (
                <p className="text-[11px] text-muted-foreground">
                  Audio đã tạo không bị xoá; chỉ patch tạo audio sau đó dùng quy tắc mới.
                </p>
              )}
            </div>
          ) : (
            dirty && (
              <div className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-[11px] text-amber-800">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                Form đang có thay đổi chưa lưu. Ebook sẽ theo mặc định <b>đã lưu</b> — lưu mặc định trước nếu muốn dùng
                giá trị mới.
              </div>
            )
          )}

          <div className="space-y-2 rounded-md border border-border p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <label className="flex cursor-pointer items-center gap-2 text-xs font-semibold">
                <input
                  type="checkbox"
                  className={checkboxClass}
                  checked={allFilteredSelected}
                  ref={(el) => {
                    if (el) el.indeterminate = !allFilteredSelected && someFilteredSelected;
                  }}
                  onChange={toggleAll}
                  disabled={!filtered.length}
                />
                Chọn tất cả{query.trim() ? " (đang lọc)" : ""} · {filtered.length}
              </label>
              <span className="font-mono text-[11px] text-muted-foreground">Đã chọn {selected.size}</span>
            </div>
            <div className="relative">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <input
                className={cn(fieldClass, "pl-8")}
                placeholder="Lọc theo tên hoặc ID..."
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </div>
            <div className="max-h-64 divide-y divide-border overflow-y-auto rounded-md border border-border">
              {loadError ? (
                <div className="p-3 text-xs text-destructive">{loadError}</div>
              ) : !books ? (
                <div className="p-3 text-xs text-muted-foreground">Đang tải danh sách ebook...</div>
              ) : !filtered.length ? (
                <div className="p-3 text-xs text-muted-foreground">Không có ebook phù hợp.</div>
              ) : (
                filtered.map((book) => (
                  <label key={book.id} className="flex cursor-pointer items-center gap-2 px-3 py-2 text-xs hover:bg-muted/40">
                    <input
                      type="checkbox"
                      className={checkboxClass}
                      checked={selected.has(book.id)}
                      onChange={() => toggleBook(book.id)}
                    />
                    <span className="font-mono text-[11px] text-muted-foreground">#{book.id}</span>
                    <span className="min-w-0 flex-1 truncate">{book.title}</span>
                    <span
                      className={cn(
                        "shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium",
                        book.modes[group] === "custom" ? "bg-amber-50 text-amber-800" : "bg-muted text-muted-foreground"
                      )}
                    >
                      {book.modes[group] === "custom" ? "Riêng" : "Mặc định"}
                    </span>
                  </label>
                ))
              )}
            </div>
          </div>

          {error && <div className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">{error}</div>}
          {result && (
            <div className="space-y-1 rounded-md border border-border bg-muted/30 px-3 py-2 text-xs">
              <div className="flex items-center gap-1.5 font-medium text-emerald-700">
                <CheckCircle2 className="h-3.5 w-3.5" /> Đã áp dụng cho {result.updated.length} ebook.
              </div>
              {result.failed.map((item) => (
                <div key={item.book_id} className="text-destructive">
                  #{item.book_id} {item.title || ""}: {item.error}
                </div>
              ))}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Đóng
          </Button>
          <Button onClick={apply} disabled={!canApply}>
            {applying ? "Đang áp dụng..." : `Áp dụng cho ${selected.size} ebook`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
