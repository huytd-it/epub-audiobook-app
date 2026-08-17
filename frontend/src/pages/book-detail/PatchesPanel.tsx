import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  CloudDownload,
  FileAudio2,
  FileSearch,
  Film,
  Layers,
  Replace,
  RotateCcw,
  ScanText,
  ShieldAlert,
  Upload,
  Video,
  Wrench,
} from "lucide-react";
import { api, Chapter, Patch, post, postForm, postJson } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { EmptyState } from "@/components/common/Header";
import { Progress } from "@/components/ui/progress";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import {
  PatchRangeReport,
  PatchRangesReport,
  PatchReport,
  PatchTextCheckSummary,
  PipelineInfo,
  errorText,
  stageBlockedReason,
} from "./types";
import { SectionHead, TabBar, checkboxClass } from "./parts";
import { PatchIssuesDialog } from "./PatchIssuesDialog";
import { FindReplaceDialog } from "./FindReplaceDialog";

type Filter = "all" | "processing" | "done" | "failed";

type TextTotals = { totals: Record<string, number>; total: number };

type RowProps = {
  patch: Patch;
  chapters: Chapter[];
  pipeline?: PipelineInfo;
  chunkReport?: PatchReport;
  rangeReport?: PatchRangeReport;
  textTotals?: TextTotals;
  selected: boolean;
  busy: boolean;
  onToggle: (patchId: number) => void;
  onOpen: (patch: Patch) => void;
  onOpenIssues: (patch: Patch) => void;
  onOpenFindReplace: (patch: Patch) => void;
  onImportDrive: (patch: Patch) => void;
  onImportFiles: (patch: Patch, files: FileList | null) => void;
  onUploadVideo: (patch: Patch) => void;
  onRetryPublish: (patch: Patch) => void;
  onRepublish: (patch: Patch) => void;
};

/** Memo hoá theo từng dòng: nhịp polling chỉ vẽ lại patch thực sự đổi. */
const PatchRow = React.memo(function PatchRow({
  patch,
  chapters,
  pipeline,
  chunkReport,
  rangeReport,
  textTotals,
  selected,
  busy,
  onToggle,
  onOpen,
  onOpenIssues,
  onOpenFindReplace,
  onImportDrive,
  onImportFiles,
  onUploadVideo,
  onRetryPublish,
  onRepublish,
}: RowProps) {
  const percent = patch.chunk_count ? (patch.next_chunk_index * 100) / patch.chunk_count : 0;
  const rangeBad = rangeReport && rangeReport.severity !== "ok";
  const patchChapters = chapters.filter(
    (chapter) => chapter.chapter_index >= patch.chapter_start && chapter.chapter_index <= patch.chapter_end
  );
  const numberedChapters = patchChapters.map((chapter) => chapter.chapter_no).filter((value) => value != null);
  const actualStart = numberedChapters[0];
  const actualEnd = numberedChapters[numberedChapters.length - 1];

  return (
    <TableRow className={cn(selected && "bg-primary/5", rangeReport?.severity === "error" && "bg-red-50/40")}>
      <TableCell className="w-8 py-2.5 pl-4 pr-0">
        <input
          type="checkbox"
          className={checkboxClass}
          checked={selected}
          onChange={() => onToggle(patch.id)}
          aria-label={`Chọn patch ${patch.patch_index + 1}`}
        />
      </TableCell>

      <TableCell className="min-w-56 py-2.5">
        <button className="block text-left" onClick={() => onOpen(patch)}>
          <span className="text-xs font-semibold hover:text-primary">
            #{patch.patch_index + 1} · {patch.name || `Patch ${patch.patch_index + 1}`}
          </span>
          <span className="mt-0.5 block font-mono text-[10px] text-muted-foreground">
             {actualStart != null
               ? `Chương ${actualStart}${actualEnd !== actualStart ? `–${actualEnd}` : ""}`
               : `Mục ${patch.chapter_start + 1}–${patch.chapter_end + 1}`}
             <span className="ml-2 text-muted-foreground">({patchChapters.length} chương)</span>
          </span>
        </button>

        {(rangeBad || textTotals?.total) && (
          <div className="mt-1 flex flex-wrap gap-1">
            {rangeBad && (
              <button
                onClick={() => onOpenIssues(patch)}
                title={rangeReport!.issues.map((issue) => issue.message).join("\n")}
                className={cn(
                  "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium",
                  rangeReport!.severity === "error"
                    ? "bg-red-100 text-red-800 hover:bg-red-200"
                    : "bg-amber-100 text-amber-800 hover:bg-amber-200"
                )}
              >
                <AlertTriangle className="h-2.5 w-2.5" />
                {rangeReport!.issues.some((issue) => issue.code === "chapter_no_desync")
                  ? "Lệch khoảng chương"
                  : rangeReport!.issues.some((issue) => issue.code === "range_gap")
                  ? "Hở khoảng chương"
                  : rangeReport!.issues.some((issue) => issue.code === "range_overlap")
                  ? "Chồng khoảng chương"
                  : "Khoảng chương bất thường"}
              </button>
            )}
            {Boolean(textTotals?.total) && (
              <button
                onClick={() => onOpenIssues(patch)}
                title={Object.entries(textTotals!.totals)
                  .map(([kind, count]) => `${kind}: ${count}`)
                  .join("\n")}
                className="inline-flex items-center gap-1 rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-800 hover:bg-amber-100"
              >
                <Wrench className="h-2.5 w-2.5" /> {textTotals!.total} lỗi chữ
              </button>
            )}
          </div>
        )}

        {patch.error_message && (
          <div className="mt-1 flex max-w-xs items-start gap-1 text-[10px] text-red-600">
            <AlertTriangle className="mt-px h-3 w-3 shrink-0" />
            <span className="truncate font-mono" title={patch.error_message}>
              {patch.error_message}
            </span>
          </div>
        )}
      </TableCell>

      <TableCell className="min-w-36 py-2.5">
        <div className="mb-1 flex justify-between font-mono text-[10px] text-muted-foreground">
          <span>
            {patch.next_chunk_index}/{patch.chunk_count}
          </span>
          <span className="font-semibold text-foreground">{Math.round(percent)}%</span>
        </div>
        <Progress value={percent} className="h-1.5" />
      </TableCell>

      <TableCell className="min-w-28 py-2.5">
        <div className="flex flex-wrap gap-1">
          {patch.status === "done" && (
            <span className="inline-flex items-center gap-1 rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">
              <FileAudio2 className="h-2.5 w-2.5" /> Audio
            </span>
          )}
          {pipeline?.video_status === "done" && (
            <span className="inline-flex items-center gap-1 rounded bg-blue-50 px-1.5 py-0.5 text-[10px] font-medium text-blue-700">
              <Film className="h-2.5 w-2.5" /> Video
            </span>
          )}
          {pipeline?.upload_state === "published" && (
            <span className="inline-flex items-center gap-1 rounded bg-red-50 px-1.5 py-0.5 text-[10px] font-medium text-red-700">
              <Video className="h-2.5 w-2.5" /> YouTube
            </span>
          )}
          {pipeline && (stageBlockedReason(pipeline.stage) || pipeline.last_error) && (
            <span
              className={cn(
                "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium",
                pipeline.last_error ? "bg-red-50 text-red-700" : "bg-amber-50 text-amber-800"
              )}
              title={pipeline.last_error || stageBlockedReason(pipeline.stage) || undefined}
            >
              <ShieldAlert className="h-2.5 w-2.5" />
              {stageBlockedReason(pipeline.stage) || "Lỗi pipeline"}
            </span>
          )}
          {pipeline?.attempt_count != null && pipeline.attempt_count > 0 && (
            <span
              className="inline-flex items-center gap-1 rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground"
              title={`Đã thử ${pipeline.attempt_count} lần${pipeline.next_retry_at ? ` · thử lại sau ${new Date(pipeline.next_retry_at).toLocaleTimeString("vi-VN")}` : ""}`}
            >
              <RotateCcw className="h-2.5 w-2.5" /> {pipeline.attempt_count} lần thử
            </span>
          )}
          {pipeline?.attempt_count != null && pipeline.attempt_count > 0 && (
            <span
              className="inline-flex items-center gap-1 rounded bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground"
              title={`Đã thử ${pipeline.attempt_count} lần${pipeline.next_retry_at ? ` · thử lại sau ${new Date(pipeline.next_retry_at).toLocaleTimeString("vi-VN")}` : ""}`}
            >
              <RotateCcw className="h-2.5 w-2.5" /> {pipeline.attempt_count} lần thử
            </span>
          )}
          {!pipeline && patch.status !== "done" && <span className="text-[10px] text-muted-foreground">—</span>}
          {chunkReport && chunkReport.severity !== "ok" && (
            <span
              className={cn(
                "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium",
                chunkReport.severity === "error" ? "bg-red-50 text-red-700" : "bg-amber-50 text-amber-800"
              )}
              title={`${chunkReport.chunk_count} chunk · ${chunkReport.total_chars} ký tự · quá dài ${chunkReport.oversized_chunks} · rỗng ${chunkReport.empty_chunks} · không đọc được ${chunkReport.unspeakable_chunks}`}
            >
              <FileSearch className="h-2.5 w-2.5" /> {chunkReport.severity === "error" ? "Lỗi chunk" : "Cảnh báo chunk"}
            </span>
          )}
        </div>
      </TableCell>

      <TableCell className="py-2.5 text-right">
        <StatusBadge value={patch.status} />
      </TableCell>

      <TableCell className="py-2.5 pr-4 text-right">
        <div className="flex items-center justify-end gap-1">
          {pipeline?.video_status === "done" && pipeline.upload_state !== "published" && (
            <Button
              size="sm"
              variant="ghost"
              className="h-7 px-2 text-[11px] text-red-700"
              disabled={busy || pipeline.upload_state === "active" || pipeline.upload_state === "postprocessing"}
              onClick={() => onUploadVideo(patch)}
              title="Upload video lên YouTube"
            >
              <Video className="h-3 w-3" />
              <span className="hidden lg:inline">
                {pipeline.upload_state === "active" || pipeline.upload_state === "postprocessing" ? "Đang upload" : "YouTube"}
              </span>
            </Button>
          )}
          {pipeline?.last_error && pipeline.upload_state !== "active" && (
            <Button
              size="sm"
              variant="ghost"
              className="h-7 px-2 text-[11px] text-emerald-700"
              disabled={busy}
              onClick={() => onRetryPublish(patch)}
              title="Thử lại bước pipeline bị lỗi (không đăng lại video đã hoàn thành)"
            >
              <RotateCcw className="h-3 w-3" />
              <span className="hidden lg:inline">Retry</span>
            </Button>
          )}
          {pipeline?.can_force_new && (
            <Button
              size="sm"
              variant="ghost"
              className="h-7 px-2 text-[11px] text-red-700"
              disabled={busy}
              onClick={() => onRepublish(patch)}
              title="Đăng lại với tư cách video mới (xác nhận trước khi thực hiện)"
            >
              <Video className="h-3 w-3" />
              <span className="hidden lg:inline">Đăng lại</span>
            </Button>
          )}
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-[11px]"
            disabled={busy || patch.status === "processing"}
            onClick={() => onOpenFindReplace(patch)}
            title="Tìm & thay trong text của patch"
          >
            <Replace className="h-3 w-3" />
            <span className="hidden lg:inline">Tìm & thay</span>
          </Button>
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-[11px]"
            disabled={busy || patch.status === "processing"}
            onClick={() => onImportDrive(patch)}
            title="Quét kết quả từ Drive Desktop"
          >
            <CloudDownload className="h-3 w-3" />
            <span className="hidden lg:inline">Drive</span>
          </Button>
          <label className="inline-flex">
            <input
              className="hidden"
              type="file"
              multiple
              accept=".wav,.zip"
              onChange={(event) => {
                onImportFiles(patch, event.target.files);
                event.currentTarget.value = "";
              }}
            />
            <span
              className="inline-flex h-7 cursor-pointer items-center gap-1 rounded-md px-2 text-[11px] font-medium text-muted-foreground hover:bg-muted hover:text-foreground"
              title="Tải chunk WAV lên"
            >
              <Upload className="h-3 w-3" />
              <span className="hidden lg:inline">Upload</span>
            </span>
          </label>
        </div>
      </TableCell>
    </TableRow>
  );
});

export function PatchesPanel({
  bookId,
  patches,
  chapters,
  pipelines,
  selectedIds,
  onSelectionChange,
  onOpenPatch,
  onMessage,
  onRefresh,
  onBusyChange,
}: {
  bookId: string;
  patches: Patch[];
  chapters: Chapter[];
  pipelines: Record<string, PipelineInfo>;
  selectedIds: number[];
  onSelectionChange: (ids: number[]) => void;
  onOpenPatch: (patch: Patch) => void;
  onMessage: (message: string) => void;
  onRefresh: () => Promise<void> | void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [importingId, setImportingId] = useState<number>();
  const [chunkReports, setChunkReports] = useState<Record<number, PatchReport>>();
  const [checkingChunks, setCheckingChunks] = useState(false);
  const [ranges, setRanges] = useState<PatchRangesReport>();
  const [textChecks, setTextChecks] = useState<Record<number, TextTotals>>();
  const [checkingText, setCheckingText] = useState(false);
  const [resyncing, setResyncing] = useState(false);

  const checkChunks = useCallback(async () => {
    setCheckingChunks(true);
    try {
      const result = await api<{ patches: PatchReport[] }>(`/books/${bookId}/validation`);
      const byId: Record<number, PatchReport> = {};
      for (const item of result.patches) byId[item.patch_id] = item;
      setChunkReports(byId);
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setCheckingChunks(false);
    }
  }, [bookId, onMessage]);

  // Soát khoảng chương rẻ (không dựng chunk plan) nên chạy ngay khi mở tab.
  const loadRanges = useCallback(async () => {
    try {
      setRanges(await api<PatchRangesReport>(`/books/${bookId}/patches/ranges`));
    } catch {
      // bổ trợ thôi — hỏng thì bảng patch vẫn dùng được như cũ
    }
  }, [bookId]);

  useEffect(() => {
    loadRanges();
  }, [loadRanges]);

  const checkText = useCallback(async () => {
    setCheckingText(true);
    try {
      const result = await api<PatchTextCheckSummary>(`/books/${bookId}/patches/text-check`);
      const byId: Record<number, TextTotals> = {};
      for (const item of result.patches) byId[item.patch_id] = { totals: item.totals, total: item.total };
      setTextChecks(byId);
      const flagged = result.patches.filter((item) => item.total > 0).length;
      onMessage(
        flagged
          ? `${flagged}/${result.patches.length} patch có lỗi chữ ảnh hưởng TTS — bấm vào cảnh báo ở từng dòng để xem chi tiết.`
          : "Không phát hiện lỗi chữ nào ảnh hưởng TTS."
      );
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setCheckingText(false);
    }
  }, [bookId, onMessage]);

  const resyncRanges = useCallback(async () => {
    setResyncing(true);
    onBusyChange(true);
    try {
      const result = await post(`/books/${bookId}/patches/resync-ranges`);
      const updated = (result as { updated: number }).updated;
      onMessage(
        updated
          ? `Đã căn lại khoảng chương cho ${updated} patch theo số chương đã neo.`
          : "Mọi patch đã bám đúng khoảng chương — không cần căn lại."
      );
      await Promise.all([loadRanges(), onRefresh()]);
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setResyncing(false);
      onBusyChange(false);
    }
  }, [bookId, loadRanges, onMessage, onRefresh, onBusyChange]);

  const counts = useMemo(
    () => ({
      all: patches.length,
      processing: patches.filter((patch) => patch.status === "processing").length,
      done: patches.filter((patch) => patch.status === "done").length,
      failed: patches.filter((patch) => patch.status === "failed").length,
    }),
    [patches]
  );

  const rangeSummary = ranges?.summary;
  const rangeByPatchId = useMemo(() => {
    const map: Record<number, PatchRangeReport> = {};
    for (const item of ranges?.patches || []) map[item.patch_id] = item;
    return map;
  }, [ranges]);

  const [issuesPatch, setIssuesPatch] = useState<Patch>();
  const [issuesOpen, setIssuesOpen] = useState(false);
  const openIssues = useCallback((patch: Patch) => {
    setIssuesPatch(patch);
    setIssuesOpen(true);
  }, []);

  const [findReplacePatch, setFindReplacePatch] = useState<Patch>();
  const [findReplaceOpen, setFindReplaceOpen] = useState(false);
  const openFindReplace = useCallback((patch: Patch) => {
    setFindReplacePatch(patch);
    setFindReplaceOpen(true);
  }, []);

  const visible = useMemo(
    () => (filter === "all" ? patches : patches.filter((patch) => patch.status === filter)),
    [patches, filter]
  );

  const allVisibleSelected = visible.length > 0 && visible.every((patch) => selectedIds.includes(patch.id));

  const toggle = useCallback(
    (patchId: number) => {
      onSelectionChange(
        selectedIds.includes(patchId) ? selectedIds.filter((id) => id !== patchId) : [...selectedIds, patchId]
      );
    },
    [selectedIds, onSelectionChange]
  );

  const toggleAll = useCallback(() => {
    const visibleIds = visible.map((patch) => patch.id);
    onSelectionChange(
      allVisibleSelected
        ? selectedIds.filter((id) => !visibleIds.includes(id))
        : Array.from(new Set([...selectedIds, ...visibleIds]))
    );
  }, [visible, allVisibleSelected, selectedIds, onSelectionChange]);

  const runImport = useCallback(
    async (patch: Patch, action: () => Promise<unknown>, done: string) => {
      setImportingId(patch.id);
      onBusyChange(true);
      try {
        await action();
        onMessage(done);
        await onRefresh();
      } catch (error) {
        onMessage(errorText(error));
      } finally {
        setImportingId(undefined);
        onBusyChange(false);
      }
    },
    [onBusyChange, onMessage, onRefresh]
  );

  const importDrive = useCallback(
    (patch: Patch) =>
      runImport(
        patch,
        () => post(`/books/${bookId}/patches/${patch.id}/import`),
        `Đã quét Drive Desktop cho patch ${patch.patch_index + 1}.`
      ),
    [bookId, runImport]
  );

  const importFiles = useCallback(
    (patch: Patch, files: FileList | null) => {
      if (!files?.length) return;
      const form = new FormData();
      Array.from(files).forEach((file) => form.append("files", file));
      return runImport(
        patch,
        () => postForm(`/books/${bookId}/patches/${patch.id}/import-local`, form),
        `Đã upload ${files.length} chunk cho patch ${patch.patch_index + 1}.`
      );
    },
    [bookId, runImport]
  );

  const uploadVideo = useCallback(
    (patch: Patch) =>
      runImport(
        patch,
        () => post(`/books/${bookId}/patches/${patch.id}/youtube-upload`),
        `Đã đưa video patch ${patch.patch_index + 1} vào hàng đợi YouTube.`
      ),
    [bookId, runImport]
  );

  const retryPublish = useCallback(
    (patch: Patch) =>
      runImport(
        patch,
        () => post(`/books/${bookId}/patches/${patch.id}/publish/retry`),
        `Đã thử lại bước đăng dang dở của patch ${patch.patch_index + 1}.`
      ),
    [bookId, runImport]
  );

  const [republishPatch, setRepublishPatch] = useState<Patch>();
  const [republishing, setRepublishing] = useState(false);

  // Đăng lại với force_new: về phía API, bước này chỉ làm sạch trạng thái upload
  // và hậu kỳ (thumbnail/playlist) — không bao giờ upload lại video đã hoàn thành
  // trừ khi người dùng xác nhận ở dialog này.
  const republish = useCallback(
    async (patch: Patch) => {
      setRepublishing(true);
      onBusyChange(true);
      try {
        await postJson(`/books/${bookId}/patches/${patch.id}/publish`, { force_new: true });
        onMessage(`Đã đưa patch ${patch.patch_index + 1} vào hàng đợi đăng lại (video mới).`);
        await onRefresh();
        setRepublishPatch(undefined);
      } catch (error) {
        onMessage(errorText(error));
      } finally {
        setRepublishing(false);
        onBusyChange(false);
      }
    },
    [bookId, onBusyChange, onMessage, onRefresh]
  );

  return (
    <Card>
      <CardHeader className="gap-4 border-b border-border bg-muted/20">
        <SectionHead
          icon={Layers}
          title={`Patches (${patches.length})`}
          detail="Chọn patch để chạy hành động hàng loạt ở thanh dưới màn hình."
          action={
            <div className="flex shrink-0 flex-wrap items-center gap-2">
              <Button size="sm" variant="outline" onClick={checkText} disabled={checkingText}>
                <ScanText className={cn("h-3.5 w-3.5", checkingText && "animate-pulse")} /> Kiểm tra chính tả / từ rác
              </Button>
              <Button size="sm" variant="outline" onClick={checkChunks} disabled={checkingChunks}>
                <FileSearch className={cn("h-3.5 w-3.5", checkingChunks && "animate-pulse")} /> Soát chunk
              </Button>
              <Link to="/queue" className="text-xs text-primary hover:underline">
                Hàng đợi →
              </Link>
            </div>
          }
        />

        {rangeSummary && rangeSummary.patches_error + rangeSummary.patches_warning > 0 && (
          <div
            className={cn(
              "flex flex-wrap items-center gap-2 rounded-md px-3 py-2 text-xs",
              rangeSummary.patches_error ? "bg-red-50 text-red-800" : "bg-amber-50 text-amber-900"
            )}
          >
            <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
            <span>
              Khoảng chương bất thường ở{" "}
              {[
                rangeSummary.patches_error && `${rangeSummary.patches_error} patch lỗi`,
                rangeSummary.patches_warning && `${rangeSummary.patches_warning} patch cảnh báo`,
              ]
                .filter(Boolean)
                .join(" và ")}
              . Bấm vào cảnh báo trên từng dòng để xem chi tiết.
            </span>
            {rangeSummary.needs_resync > 0 && (
              <Button size="sm" variant="outline" className="ml-auto" onClick={resyncRanges} disabled={resyncing}>
                <Wrench className={cn("h-3.5 w-3.5", resyncing && "animate-pulse")} /> Căn lại{" "}
                {rangeSummary.needs_resync} patch theo số chương
              </Button>
            )}
          </div>
        )}
        <TabBar<Filter>
          value={filter}
          onChange={setFilter}
          className="bg-background"
          tabs={[
            { value: "all", label: "Tất cả", badge: counts.all },
            { value: "processing", label: "Đang chạy", badge: counts.processing },
            { value: "done", label: "Hoàn thành", badge: counts.done },
            { value: "failed", label: "Lỗi", badge: counts.failed },
          ]}
        />
      </CardHeader>

      <CardContent className="p-0">
        {visible.length === 0 ? (
          <EmptyState text={patches.length === 0 ? "Chưa khởi tạo patch nào" : "Không có patch khớp bộ lọc"} />
        ) : (
          <div className="max-h-[32rem] overflow-auto">
            <Table>
              <TableHeader className="sticky top-0 z-10 bg-card">
                <TableRow>
                  <TableHead className="w-8 pl-4 pr-0">
                    <input
                      type="checkbox"
                      className={checkboxClass}
                      checked={allVisibleSelected}
                      onChange={toggleAll}
                      aria-label="Chọn tất cả patch đang hiển thị"
                    />
                  </TableHead>
                  <TableHead>Patch</TableHead>
                  <TableHead>Tiến độ</TableHead>
                  <TableHead>Pipeline</TableHead>
                  <TableHead className="text-right">Trạng thái</TableHead>
                  <TableHead className="pr-4 text-right">Thao tác</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visible.map((patch) => (
                  <PatchRow
                    key={patch.id}
                    patch={patch}
                    chapters={chapters}
                    pipeline={pipelines[String(patch.id)]}
                    chunkReport={chunkReports?.[patch.id]}
                    rangeReport={rangeByPatchId[patch.id]}
                    textTotals={textChecks?.[patch.id]}
                    selected={selectedIds.includes(patch.id)}
                    busy={importingId === patch.id}
                    onToggle={toggle}
                    onOpen={onOpenPatch}
                    onOpenIssues={openIssues}
                    onOpenFindReplace={openFindReplace}
                    onImportDrive={importDrive}
                    onImportFiles={importFiles}
                    onUploadVideo={uploadVideo}
                    onRetryPublish={retryPublish}
                    onRepublish={(patch) => setRepublishPatch(patch)}
                  />
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>

      <PatchIssuesDialog
        bookId={bookId}
        patchId={issuesPatch?.id}
        rangeReport={issuesPatch ? rangeByPatchId[issuesPatch.id] : undefined}
        chapters={
          issuesPatch
            ? chapters.filter(
                (chapter) =>
                  chapter.chapter_index >= issuesPatch.chapter_start && chapter.chapter_index <= issuesPatch.chapter_end
              )
            : []
        }
        open={issuesOpen}
        onOpenChange={setIssuesOpen}
        onMessage={onMessage}
      />

      <FindReplaceDialog
        bookId={bookId}
        patch={findReplacePatch}
        open={findReplaceOpen}
        onOpenChange={setFindReplaceOpen}
        onMessage={onMessage}
        onSaved={onRefresh}
      />

      <Dialog open={Boolean(republishPatch)} onOpenChange={(open) => !open && setRepublishPatch(undefined)}>
        <DialogContent className="max-h-[90vh] max-w-md overflow-auto">
          <DialogHeader>
            <DialogTitle>Đăng lại patch {republishPatch ? `#${republishPatch.patch_index + 1}` : ""}?</DialogTitle>
            <DialogDescription>
              Patch này đã được đăng lên YouTube. Đăng lại sẽ tạo một video mới và upload lên kênh — bản đăng trước
              không bị xoá, nhưng sẽ xuất hiện video trùng nội dung trên kênh. Thumbnail, tiêu đề và playlist vẫn dùng
              cấu hình hiện tại.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2">
            <Button variant="outline" size="sm" onClick={() => setRepublishPatch(undefined)} disabled={republishing}>
              Huỷ
            </Button>
            <Button
              size="sm"
              variant="destructive"
              disabled={republishing || !republishPatch}
              onClick={() => republishPatch && republish(republishPatch)}
            >
              {republishing ? "Đang đưa vào hàng đợi..." : "Đăng lại (video mới)"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
