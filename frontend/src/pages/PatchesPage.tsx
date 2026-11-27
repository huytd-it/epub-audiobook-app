import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ChevronLeft, ChevronRight, Layers, ListPlus, Play, RefreshCw } from "lucide-react";
import { api, postJson, ContinuePatchesResult, PatchGroup, PatchOverview, PatchOverviewItem, PatchPhase } from "@/api";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { Input } from "@/components/ui/input";
import { TabBar, checkboxClass } from "@/pages/book-detail/parts";
import { showToast } from "@/components/ui/toast";
import { timeAgo } from "@/lib/recentBooks";

const GROUP_LABELS: Array<[PatchGroup, string]> = [
  ["unfinished", "Dang dở"],
  ["not_started", "Chưa bắt đầu"],
  ["queued", "Trong queue"],
  ["complete", "Hoàn tất"],
  ["all", "Tất cả"],
];

const PHASE_META: Record<PatchPhase, { label: string; variant: "default" | "secondary" | "destructive" | "outline" | "success" | "warning" | "lime" }> = {
  queued: { label: "Trong queue", variant: "lime" },
  tts_partial: { label: "TTS dở", variant: "warning" },
  tts_failed: { label: "TTS lỗi", variant: "destructive" },
  not_started: { label: "Chưa TTS", variant: "outline" },
  publish_failed: { label: "Publish lỗi", variant: "destructive" },
  publish_pending: { label: "Chờ publish", variant: "warning" },
  complete: { label: "Xong", variant: "success" },
};

const SKIP_LABELS: Record<string, string> = {
  already_queued: "đã trong queue",
  complete: "đã hoàn tất",
  not_found: "không tồn tại",
  no_automation: "sách tắt tự động video/upload",
  waiting_config: "thiếu cấu hình video/YouTube",
  awaiting_republish_confirmation: "cần xác nhận publish lại",
  already_uploaded: "đã upload",
  audio_missing: "thiếu file audio",
  tts_not_queued: "đã có audio hoặc trùng job",
  tts_config_invalid: "cấu hình TTS không hợp lệ",
};

const CONTINUABLE = new Set<PatchPhase>(["tts_partial", "tts_failed", "not_started", "publish_failed", "publish_pending"]);
const PER_PAGE = 50;

function chapterLabel(item: PatchOverviewItem) {
  if (item.chapter_no_start != null && item.chapter_no_end != null) return `Ch. ${item.chapter_no_start}–${item.chapter_no_end}`;
  return `Mục ${item.chapter_start + 1}–${item.chapter_end + 1}`;
}

function continueLabel(item: PatchOverviewItem) {
  return item.status === "done" ? "Publish" : item.next_chunk_index > 0 ? "Tiếp TTS" : "Chạy TTS";
}

function summarize(result: ContinuePatchesResult) {
  const parts: string[] = [];
  if (result.tts_queued) parts.push(`${result.tts_queued} TTS`);
  if (result.enqueued_render) parts.push(`${result.enqueued_render} render video`);
  if (result.enqueued_upload) parts.push(`${result.enqueued_upload} upload`);
  if (result.already_live) parts.push(`${result.already_live} đang chạy sẵn`);
  const head = parts.length ? `Đã thêm vào queue: ${parts.join(", ")}.` : "Không có job mới nào được thêm.";
  if (!result.skipped.length) return head;
  const reasons = new Map<string, number>();
  for (const skip of result.skipped) {
    const label = SKIP_LABELS[skip.reason] ?? skip.reason;
    reasons.set(label, (reasons.get(label) ?? 0) + (skip.count ?? 1));
  }
  return `${head} Bỏ qua: ${[...reasons].map(([label, n]) => `${n} ${label}`).join(", ")}.`;
}

/* ── Memoized row to avoid re-renders when only selection changes ── */

const PatchRow = React.memo(function PatchRow({
  item,
  checked,
  busy,
  onToggle,
  onContinue,
}: {
  item: PatchOverviewItem;
  checked: boolean;
  busy: boolean;
  onToggle: (patchId: number, on: boolean) => void;
  onContinue: (patchIds: number[]) => void;
}) {
  const meta = PHASE_META[item.phase];
  const canContinue = CONTINUABLE.has(item.phase);
  const ttsDone = item.status === "done";
  const percent = ttsDone ? 100 : item.chunk_count ? (item.next_chunk_index * 100) / item.chunk_count : 0;
  const job = item.live_job;

  const pipelineText = item.pipeline
    ? `v: ${item.pipeline.video_status} · u: ${item.youtube_video_id ? "done" : item.pipeline.upload_status}`
    : ttsDone ? "chưa tạo" : null;

  return (
    <div className="group flex items-center gap-2 border-b border-border px-3 py-2 text-sm last:border-b-0 hover:bg-muted/40">
      {/* Checkbox */}
      <input
        type="checkbox"
        className={checkboxClass}
        checked={checked}
        disabled={!canContinue}
        onChange={(e) => onToggle(item.patch_id, e.target.checked)}
        aria-label={`Chọn patch #${item.patch_index + 1}`}
      />

      {/* Main info: book + patch name */}
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <Link
            to={`/books/${item.book_id}`}
            className="truncate font-medium hover:underline"
            title={item.book_title}
          >
            {item.book_title}
          </Link>
          <span className="shrink-0 font-mono text-[11px] text-muted-foreground">#{item.patch_index + 1}</span>
        </div>
        <div className="truncate text-xs text-muted-foreground" title={item.name}>
          {item.name || chapterLabel(item)}
        </div>
      </div>

      {/* Progress bar + chunk info — compact inline */}
      <div className="hidden w-28 shrink-0 sm:block">
        <Progress value={percent} className="h-1" />
        <div className="mt-0.5 text-center font-mono text-[10px] text-muted-foreground">
          {ttsDone ? "audio ✓" : `${item.next_chunk_index}/${item.chunk_count}`}
        </div>
      </div>

      {/* Status badge + optional error / job info */}
      <div className="w-24 shrink-0 text-center">
        <Badge variant={meta.variant} className="text-[10px]">{meta.label}</Badge>
        {job && (
          <div className="mt-0.5 font-mono text-[10px] text-muted-foreground">
            {job.status}{job.progress_total ? ` ${job.progress_current}/${job.progress_total}` : ""}
          </div>
        )}
        {item.error_message && item.phase !== "complete" && (
          <div className="mt-0.5 truncate text-[10px] text-red-600" title={item.error_message}>
            {item.error_message}
          </div>
        )}
      </div>

      {/* Pipeline + updated_at — merged into one slim column */}
      <div className="hidden w-20 shrink-0 text-right text-[11px] text-muted-foreground lg:block">
        {pipelineText && <div className="truncate" title={pipelineText}>{pipelineText}</div>}
        <div title={item.updated_at}>{timeAgo(item.updated_at)}</div>
      </div>

      {/* Action */}
      <div className="w-20 shrink-0 text-right">
        {canContinue && (
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            className="h-7 px-2 text-xs"
            onClick={() => onContinue([item.patch_id])}
          >
            <Play className="mr-1 h-3 w-3" /> {continueLabel(item)}
          </Button>
        )}
      </div>
    </div>
  );
});

/* ── Page ── */

export function PatchesPage() {
  const [group, setGroup] = useState<PatchGroup>("unfinished");
  const [bookId, setBookId] = useState<string>("");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<PatchOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => { setQuery(search.trim()); setPage(1); }, 300);
    return () => window.clearTimeout(timer);
  }, [search]);

  const load = useCallback(async () => {
    const params = new URLSearchParams({ group, page: String(page), per_page: String(PER_PAGE) });
    if (bookId) params.set("book_id", bookId);
    if (query) params.set("q", query);
    try {
      setData(await api<PatchOverview>(`/api/ui/patches?${params}`));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, [group, page, bookId, query]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => { setSelected(new Set()); }, [group, bookId, query]);

  const hasLive = useMemo(() => !!data?.items.some((item) => item.phase === "queued"), [data]);
  useEffect(() => {
    if (group !== "queued" && !hasLive) return;
    const timer = window.setInterval(load, 5000);
    return () => window.clearInterval(timer);
  }, [group, hasLive, load]);

  const continuable = useMemo(() => (data?.items ?? []).filter((item) => CONTINUABLE.has(item.phase)), [data]);
  const allSelected = continuable.length > 0 && continuable.every((item) => selected.has(item.patch_id));

  const toggle = useCallback((patchId: number, on: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) next.add(patchId); else next.delete(patchId);
      return next;
    });
  }, []);

  const continuePatches = useCallback(async (patchIds: number[]) => {
    if (!patchIds.length || busy) return;
    setBusy(true);
    try {
      const result = await postJson<ContinuePatchesResult>("/api/ui/patches/continue", { patch_ids: patchIds });
      const queued = result.tts_queued + result.enqueued_render + result.enqueued_upload;
      showToast(summarize(result), queued ? "success" : "info");
      setSelected(new Set());
      await load();
    } catch (err) {
      showToast(err instanceof Error ? err.message : String(err), "error");
    } finally {
      setBusy(false);
    }
  }, [busy, load]);

  const selectAll = useCallback(() => {
    setSelected((prev) => {
      const allChecked = continuable.length > 0 && continuable.every((item) => prev.has(item.patch_id));
      return allChecked ? new Set() : new Set(continuable.map((item) => item.patch_id));
    });
  }, [continuable]);

  const tabs = GROUP_LABELS.map(([value, label]) => ({ value, label, badge: data?.counts[value] }));

  return (
    <div className="space-y-3">
      <Header
        title={<span className="flex items-center gap-2"><Layers className="h-5 w-5" /> Quản lý Patch</span>}
        subtitle="Tiếp tục phần dang dở hoặc thêm nhanh vào hàng đợi."
        action={
          <Button variant="outline" size="sm" onClick={load}>
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" /> Làm mới
          </Button>
        }
      />

      <TabBar tabs={tabs} value={group} onChange={(next) => { setGroup(next); setPage(1); }} />

      {/* Toolbar: search + filter + batch action */}
      <div className="flex flex-wrap items-center gap-2">
        <Input
          className="h-8 w-56"
          placeholder="Tìm sách hoặc patch..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <select
          className="h-8 rounded-md border border-input bg-background px-2 text-sm"
          value={bookId}
          onChange={(e) => { setBookId(e.target.value); setPage(1); }}
          aria-label="Lọc theo sách"
        >
          <option value="">Tất cả sách</option>
          {data?.books.map((book) => <option key={book.id} value={book.id}>{book.title}</option>)}
        </select>
        <div className="ml-auto flex items-center gap-2">
          {selected.size > 0 && <span className="text-xs text-muted-foreground">{selected.size} đã chọn</span>}
          <Button size="sm" disabled={!selected.size || busy} onClick={() => continuePatches([...selected])}>
            <ListPlus className="mr-1 h-3.5 w-3.5" />
            {busy ? "Đang gửi..." : `Thêm ${selected.size || ""} vào queue`}
          </Button>
        </div>
      </div>

      {error && <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</div>}

      {!data && !error ? (
        <LoadingState />
      ) : data && data.items.length === 0 ? (
        <EmptyState text={group === "unfinished" ? "Không có patch nào dang dở." : "Không có patch nào."} />
      ) : data ? (
        <Card>
          <CardContent className="p-0">
            {/* List header */}
            <div className="flex items-center gap-2 border-b border-border bg-muted/30 px-3 py-1.5 text-[11px] font-medium text-muted-foreground">
              <input
                type="checkbox"
                className={checkboxClass}
                checked={allSelected}
                disabled={!continuable.length}
                onChange={selectAll}
                aria-label="Chọn tất cả"
              />
              <div className="min-w-0 flex-1">Sách / Patch</div>
              <div className="hidden w-28 shrink-0 text-center sm:block">TTS</div>
              <div className="w-24 shrink-0 text-center">Trạng thái</div>
              <div className="hidden w-20 shrink-0 text-right lg:block">Info</div>
              <div className="w-20 shrink-0" />
            </div>
            {/* Rows */}
            {data.items.map((item) => (
              <PatchRow
                key={item.patch_id}
                item={item}
                checked={selected.has(item.patch_id)}
                busy={busy}
                onToggle={toggle}
                onContinue={continuePatches}
              />
            ))}
          </CardContent>
        </Card>
      ) : null}

      {data && data.total_pages > 1 && (
        <div className="flex items-center justify-end gap-2 text-xs text-muted-foreground">
          <span>Trang {data.page}/{data.total_pages} · {data.total} patch</span>
          <Button size="sm" variant="outline" disabled={data.page <= 1} onClick={() => setPage(data.page - 1)} aria-label="Trang trước">
            <ChevronLeft className="h-3.5 w-3.5" />
          </Button>
          <Button size="sm" variant="outline" disabled={data.page >= data.total_pages} onClick={() => setPage(data.page + 1)} aria-label="Trang sau">
            <ChevronRight className="h-3.5 w-3.5" />
          </Button>
        </div>
      )}
    </div>
  );
}
