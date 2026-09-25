import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  FileAudio2,
  FileSearch,
  Film,
  Layers,
  Mic,
  RotateCcw,
  ScanText,
  Search,
  ShieldAlert,
  Video,
  Wrench,
} from "lucide-react";
import { api, Chapter, Patch, VoiceItem, post, postJson } from "@/api";
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
import { MediaBrowser } from "@/components/media-browser/MediaBrowser";
import {
  OnlineVoice,
  PatchRangeReport,
  PatchRangesReport,
  PatchReport,
  PatchTextCheckSummary,
  PipelineInfo,
  TtsModel,
  VoiceOption,
  errorText,
  stageBlockedReason,
} from "./types";
import { buildVoiceOptions, modelNeedsOnlineVoices } from "./useBookDetail";
import { SectionHead, Field, checkboxClass, fieldClass, selectClass } from "./parts";
import { patchMediaDir } from "./paths";
import { PatchIssuesDialog } from "./PatchIssuesDialog";

type StatusFilter = "all" | "unqueued" | "processing" | "done" | "failed";
type PresenceFilter = "all" | "yes" | "no";

/** Patch chưa từng chạm vào hàng đợi nào: chưa chạy TTS (status pending)
 * đồng thời chưa có pipeline video/YouTube. */
export function isUnqueued(patch: Patch, pipelines: Record<string, PipelineInfo>) {
  return patch.status === "pending" && !pipelines[String(patch.id)];
}

/** Các trạng thái đầu ra dùng chung với badge trong từng dòng và bộ lọc. */
export function hasAudio(patch: Patch) {
  return patch.status === "done";
}

export function hasVideo(pipeline?: PipelineInfo) {
  return pipeline?.video_status === "done";
}

export function hasYoutube(pipeline?: PipelineInfo) {
  return pipeline?.upload_state === "published" || pipeline?.stage === "published";
}

function matchesPresence(filter: PresenceFilter, present: boolean) {
  return filter === "all" || (filter === "yes" ? present : !present);
}

type TextTotals = { totals: Record<string, number>; total: number };

/** Kiểu chọn dòng: "single" chỉ giữ dòng vừa bấm, "toggle" bật/tắt riêng dòng đó,
 * "range" quét từ dòng neo (lần bấm gần nhất) tới dòng hiện tại. */
type SelectMode = "single" | "toggle" | "range";

/** Phần tử tương tác trong dòng giữ hành vi riêng — click ở đó không đổi lựa chọn. */
const INTERACTIVE = "button, a, input, label, select, textarea, [role='button']";

const isInteractive = (target: EventTarget | null) =>
  target instanceof Element && Boolean(target.closest(INTERACTIVE));

type RowProps = {
  patch: Patch;
  chapters: Chapter[];
  pipeline?: PipelineInfo;
  chunkReport?: PatchReport;
  rangeReport?: PatchRangeReport;
  textTotals?: TextTotals;
  selected: boolean;
  busy: boolean;
  onSelect: (patchId: number, mode: SelectMode) => void;
  onOpen: (patch: Patch) => void;
  onOpenIssues: (patch: Patch) => void;
  onOpenMedia: (patch: Patch) => void;
  onUploadVideo: (patch: Patch) => void;
  onRetryPublish: (patch: Patch) => void;
  onRepublish: (patch: Patch) => void;
  voice: VoiceCtx;
};

type VoiceCtx = {
  ttsModels: TtsModel[];
  bookModelId: string;
  bookVoiceId: string;
  bookVoiceName: string;
  localVoices: VoiceItem[];
  onlineCache: Record<string, OnlineVoice[]>;
  ensureOnlineVoices: (modelId: string) => void;
};

/** Nhãn giọng hiệu lực của patch (chỉ hiển thị): giọng riêng đã lưu, nếu không
 *  thì giọng chung của sách. Gán/tự động lưu khi chạy TTS. */
function PatchVoiceLabel({ patch, voice }: { patch: Patch; voice: VoiceCtx }) {
  const { ttsModels, bookModelId, bookVoiceId, bookVoiceName, localVoices, onlineCache, ensureOnlineVoices } = voice;
  const hasOverride = Boolean(patch.tts_model || patch.tts_voice_id);
  const effectiveModel = patch.tts_model || bookModelId;
  const rawVoice = patch.tts_voice_id != null ? patch.tts_voice_id : bookVoiceId;
  const model = ttsModels.find((item) => item.id === effectiveModel) || null;

  useEffect(() => {
    if (modelNeedsOnlineVoices(model)) ensureOnlineVoices(effectiveModel);
  }, [model, effectiveModel, ensureOnlineVoices]);

  const options: VoiceOption[] = useMemo(
    () =>
      buildVoiceOptions({
        ttsModels,
        modelId: effectiveModel,
        localVoices,
        onlineVoices: onlineCache[effectiveModel] || [],
        currentVoiceName: bookVoiceName,
      }),
    [ttsModels, effectiveModel, localVoices, onlineCache, bookVoiceName]
  );

  const voiceLabel = rawVoice
    ? options.find((option) => option.value === rawVoice)?.label || rawVoice
    : "—";
  return (
    <div
      className="min-w-0 max-w-44"
      title={hasOverride ? `Giọng riêng patch này (tự lưu khi chạy TTS): ${model?.name || effectiveModel} · ${voiceLabel}` : `Theo cấu hình chung của sách: ${model?.name || effectiveModel} · ${voiceLabel}`}
    >
      <p className="truncate text-[11px] font-medium">{voiceLabel}</p>
      <p className="truncate font-mono text-[10px] text-muted-foreground">{model?.name || effectiveModel}</p>
      {hasOverride && <span className="text-[10px] font-medium text-primary">Giọng riêng</span>}
    </div>
  );
}

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
  onSelect,
  onOpen,
  onOpenIssues,
  onOpenMedia,
  onUploadVideo,
  onRetryPublish,
  onRepublish,
  voice,
}: RowProps) {
  const percent = patch.chunk_count ? (patch.next_chunk_index * 100) / patch.chunk_count : 0;
  const rangeBad = rangeReport && rangeReport.severity !== "ok";
  const patchChapters = chapters.filter(
    (chapter) => chapter.chapter_index >= patch.chapter_start && chapter.chapter_index <= patch.chapter_end
  );
  const numberedChapters = patchChapters.map((chapter) => chapter.chapter_no).filter((value) => value != null);
  const actualStart = numberedChapters[0];
  const actualEnd = numberedChapters[numberedChapters.length - 1];
  const patchLabel = patch.name || `Patch ${patch.patch_index + 1}`;

  // Bấm vào phần trống của dòng để chọn; nút/link/ô nhập bên trong vẫn giữ hành vi riêng.
  const selectFromEvent = (event: React.MouseEvent, fallback: SelectMode) =>
    onSelect(patch.id, event.shiftKey ? "range" : event.ctrlKey || event.metaKey ? "toggle" : fallback);

  return (
    <TableRow
      className={cn(
        "cursor-pointer",
        selected && "bg-primary/5",
        rangeReport?.severity === "error" && "bg-red-50/40"
      )}
      // Shift+click mặc định bôi đen text — chặn ngay từ mousedown.
      onMouseDown={(event) => {
        if (event.shiftKey && !isInteractive(event.target)) event.preventDefault();
      }}
      onClick={(event) => {
        if (isInteractive(event.target)) return;
        selectFromEvent(event, "single");
      }}
    >
      <TableCell className="w-8 py-2.5 pl-4 pr-0">
        <input
          type="checkbox"
          className={checkboxClass}
          checked={selected}
          // Ô tick luôn bật/tắt một dòng, trừ khi giữ Shift để quét khoảng.
          onClick={(event) => selectFromEvent(event, "toggle")}
          onChange={() => undefined}
          aria-label={`Chọn patch ${patch.patch_index + 1}`}
        />
      </TableCell>

      <TableCell className="w-[50px] min-w-[50px] px-2 py-2.5 text-center font-mono text-xs text-muted-foreground">
        {patch.patch_index + 1}
      </TableCell>

      <TableCell className="min-w-56 py-2.5">
        <button className="block min-w-0 max-w-[18rem] text-left" onClick={() => onOpen(patch)}>
          <span className="block truncate text-xs font-semibold hover:text-primary" title={patchLabel}>
            #{patch.patch_index + 1} · {patchLabel}
          </span>
          <span className="mt-0.5 block truncate font-mono text-[10px] text-muted-foreground">
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
          {hasAudio(patch) && (
            <button onClick={() => onOpen(patch)} title="Nghe audio patch" className="inline-flex items-center gap-1 rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700 hover:bg-emerald-100">
              <FileAudio2 className="h-2.5 w-2.5" /> Audio
            </button>
          )}
          {hasVideo(pipeline) && (
            <button onClick={() => window.open(`/books/${patch.book_id}/patches/${patch.id}/video/preview`, "_blank", "noopener,noreferrer")} title="Xem trước video" className="inline-flex items-center gap-1 rounded bg-blue-50 px-1.5 py-0.5 text-[10px] font-medium text-blue-700 hover:bg-blue-100">
              <Film className="h-2.5 w-2.5" /> Video
            </button>
          )}
          {hasYoutube(pipeline) && (
            <button onClick={() => pipeline?.youtube_video_id && window.open(`https://www.youtube.com/watch?v=${pipeline.youtube_video_id}`, "_blank", "noopener,noreferrer")} title="Mở video trên YouTube" className="inline-flex items-center gap-1 rounded bg-red-50 px-1.5 py-0.5 text-[10px] font-medium text-red-700 hover:bg-red-100">
              <Video className="h-2.5 w-2.5" /> YouTube
            </button>
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

      <TableCell className="min-w-44 py-2.5">
        <PatchVoiceLabel patch={patch} voice={voice} />
      </TableCell>

      <TableCell className="py-2.5 text-right">
        <StatusBadge value={patch.status} />
      </TableCell>

      <TableCell className="py-2.5 pr-4 text-right">
        <div className="flex items-center justify-end gap-1">
          {pipeline && hasVideo(pipeline) && !hasYoutube(pipeline) && (
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
            title="Mở media của patch"
            onClick={() => onOpenMedia(patch)}
          >
            <span className="hidden lg:inline">Media</span>
          </Button>
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
  ttsModels,
  bookModelId,
  bookVoiceId,
  bookVoiceName,
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
  /** Catalog TTS của sách — dựng cột giọng riêng từng patch. */
  ttsModels: TtsModel[];
  /** Model audio hiện tại của sách (kế thừa khi patch không gán riêng). */
  bookModelId: string;
  /** Voice audio hiện tại của sách (kế thừa khi patch không gán riêng). */
  bookVoiceId: string;
  /** Tên file clip mẫu của sách (cho model clone). */
  bookVoiceName: string;
}) {
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [audioFilter, setAudioFilter] = useState<PresenceFilter>("all");
  const [videoFilter, setVideoFilter] = useState<PresenceFilter>("all");
  const [youtubeFilter, setYoutubeFilter] = useState<PresenceFilter>("all");
  const [query, setQuery] = useState("");
  const [importingId, setImportingId] = useState<number>();
  const [chunkReports, setChunkReports] = useState<Record<number, PatchReport>>();
  const [checkingChunks, setCheckingChunks] = useState(false);
  const [ranges, setRanges] = useState<PatchRangesReport>();
  const [textChecks, setTextChecks] = useState<Record<number, TextTotals>>();
  const [checkingText, setCheckingText] = useState(false);
  const [resyncing, setResyncing] = useState(false);

  // Giọng riêng từng patch: clip thư viện tải một lần; giọng online tải theo
  // model khi dòng cần (edge-tts/gTTS), cache theo model để không gọi lặp.
  const [localVoices, setLocalVoices] = useState<VoiceItem[]>([]);
  const [onlineCache, setOnlineCache] = useState<Record<string, OnlineVoice[]>>({});
  const onlineInflight = useRef<Set<string>>(new Set());
  useEffect(() => {
    api<{ voices: VoiceItem[] }>("/api/ui/media")
      .then((res) => setLocalVoices(res.voices || []))
      .catch(() => {});
  }, []);
  const ensureOnlineVoices = useCallback((modelId: string) => {
    if (!modelId) return;
    setOnlineCache((prev) => {
      if (prev[modelId] || onlineInflight.current.has(modelId)) return prev;
      onlineInflight.current.add(modelId);
      api<{ voices: OnlineVoice[] }>(`/text-studio/light-tts/voices?backend=${encodeURIComponent(modelId)}`)
        .then((res) =>
          setOnlineCache((cur) => (cur[modelId] ? cur : { ...cur, [modelId]: res.voices || [] }))
        )
        .catch(() =>
          setOnlineCache((cur) => (cur[modelId] ? cur : { ...cur, [modelId]: [] }))
        )
        .finally(() => {
          onlineInflight.current.delete(modelId);
        });
      return prev;
    });
  }, []);

  const [resettingVoices, setResettingVoices] = useState(false);
  const resetPatchVoices = useCallback(async () => {
    if (!selectedIds.length) {
      onMessage("Chọn ít nhất một patch để reset giọng về theo sách.");
      return;
    }
    setResettingVoices(true);
    onBusyChange(true);
    try {
      const result = await postJson<{ reset: number }>(`/books/${bookId}/patches/reset-voices`, {
        patch_ids: selectedIds,
      });
      onMessage(
        result.reset
          ? `Đã reset ${result.reset} patch về giọng của sách.`
          : "Các patch đã chọn vốn dùng giọng sách — không có gì để reset."
      );
      await onRefresh();
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setResettingVoices(false);
      onBusyChange(false);
    }
  }, [bookId, selectedIds, onBusyChange, onMessage, onRefresh]);

  const voiceCtx = useMemo(
    () => ({
      ttsModels,
      bookModelId,
      bookVoiceId,
      bookVoiceName,
      localVoices,
      onlineCache,
      ensureOnlineVoices,
    }),
    [ttsModels, bookModelId, bookVoiceId, bookVoiceName, localVoices, onlineCache, ensureOnlineVoices]
  );

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

  const counts = useMemo(() => {
    const hasAudioCount = patches.filter((patch) => hasAudio(patch)).length;
    const hasVideoCount = patches.filter((patch) => hasVideo(pipelines[String(patch.id)])).length;
    const hasYoutubeCount = patches.filter((patch) => hasYoutube(pipelines[String(patch.id)])).length;
    return {
      all: patches.length,
      unqueued: patches.filter((patch) => isUnqueued(patch, pipelines)).length,
      processing: patches.filter((patch) => patch.status === "processing").length,
      done: patches.filter((patch) => patch.status === "done").length,
      failed: patches.filter((patch) => patch.status === "failed").length,
      hasAudio: hasAudioCount,
      noAudio: patches.length - hasAudioCount,
      hasVideo: hasVideoCount,
      noVideo: patches.length - hasVideoCount,
      hasYoutube: hasYoutubeCount,
      noYoutube: patches.length - hasYoutubeCount,
    };
  }, [patches, pipelines]);

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

  const [mediaPatch, setMediaPatch] = useState<Patch>();
  const [mediaOpen, setMediaOpen] = useState(false);
  const openMedia = useCallback((patch: Patch) => {
    setMediaPatch(patch);
    setMediaOpen(true);
  }, []);

  const visible = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase("vi-VN").replace(/^#/, "");
    return patches.filter((patch) => {
      const pipeline = pipelines[String(patch.id)];
      if (statusFilter === "unqueued" && !isUnqueued(patch, pipelines)) return false;
      if (statusFilter === "processing" && patch.status !== "processing") return false;
      if (statusFilter === "done" && patch.status !== "done") return false;
      if (statusFilter === "failed" && patch.status !== "failed") return false;
      if (!matchesPresence(audioFilter, hasAudio(patch))) return false;
      if (!matchesPresence(videoFilter, hasVideo(pipeline))) return false;
      if (!matchesPresence(youtubeFilter, hasYoutube(pipeline))) return false;
      if (!needle) return true;

      const searchable = [
        patch.name || "",
        String(patch.id),
        String(patch.patch_index + 1),
        ...chapters.flatMap((chapter) =>
          chapter.chapter_index >= patch.chapter_start && chapter.chapter_index <= patch.chapter_end
            ? [
                chapter.title || "",
                String(chapter.chapter_index + 1),
                chapter.chapter_no == null ? "" : String(chapter.chapter_no),
              ]
            : []
        ),
      ]
        .join(" ")
        .toLocaleLowerCase("vi-VN");
      return searchable.includes(needle);
    });
  }, [audioFilter, chapters, patches, pipelines, query, statusFilter, videoFilter, youtubeFilter]);

  const allVisibleSelected = visible.length > 0 && visible.every((patch) => selectedIds.includes(patch.id));

  // Dòng neo cho shift+click: lần bấm gần nhất không phải quét khoảng.
  const anchorId = useRef<number | undefined>(undefined);
  useEffect(() => {
    anchorId.current = undefined;
  }, [audioFilter, query, statusFilter, videoFilter, youtubeFilter]);

  const select = useCallback(
    (patchId: number, mode: SelectMode) => {
      const visibleIds = visible.map((patch) => patch.id);
      if (mode === "range" && anchorId.current != null) {
        const from = visibleIds.indexOf(anchorId.current);
        const to = visibleIds.indexOf(patchId);
        if (from >= 0 && to >= 0) {
          const span = visibleIds.slice(Math.min(from, to), Math.max(from, to) + 1);
          onSelectionChange(Array.from(new Set([...selectedIds, ...span])));
          return;
        }
      }
      anchorId.current = patchId;
      if (mode === "toggle" || mode === "range") {
        onSelectionChange(
          selectedIds.includes(patchId) ? selectedIds.filter((id) => id !== patchId) : [...selectedIds, patchId]
        );
        return;
      }
      // Click thường: chỉ giữ dòng này; bấm lại khi nó là lựa chọn duy nhất thì bỏ chọn.
      onSelectionChange(selectedIds.length === 1 && selectedIds[0] === patchId ? [] : [patchId]);
    },
    [visible, selectedIds, onSelectionChange]
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
          detail="Lọc nhanh theo đầu ra hoặc tìm patch theo chương."
          action={
            <div className="flex shrink-0 flex-wrap items-center gap-2">
              <Button size="sm" variant="outline" onClick={checkText} disabled={checkingText}>
                <ScanText className={cn("h-3.5 w-3.5", checkingText && "animate-pulse")} /> Kiểm tra chính tả / từ rác
              </Button>
              <Button size="sm" variant="outline" onClick={checkChunks} disabled={checkingChunks}>
                <FileSearch className={cn("h-3.5 w-3.5", checkingChunks && "animate-pulse")} /> Soát chunk
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={resetPatchVoices}
                disabled={resettingVoices || !selectedIds.length}
                title="Xoá giọng riêng của các patch đã chọn — về kế thừa giọng chung của sách"
              >
                <Mic className={cn("h-3.5 w-3.5", resettingVoices && "animate-pulse")} /> Reset giọng
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
        <div className="space-y-2">
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <input
              type="search"
              className={`${fieldClass} pl-9`}
              placeholder="Tìm theo tên chương hoặc patch ID..."
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              aria-label="Tìm theo tên chương hoặc patch ID"
            />
          </div>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Trạng thái">
              <select
                className={selectClass}
                value={statusFilter}
                onChange={(event) => setStatusFilter(event.target.value as StatusFilter)}
                aria-label="Lọc theo trạng thái patch"
              >
                <option value="all">Tất cả ({counts.all})</option>
                <option value="unqueued">Chưa vào hàng đợi ({counts.unqueued})</option>
                <option value="processing">Đang chạy ({counts.processing})</option>
                <option value="done">Hoàn thành ({counts.done})</option>
                <option value="failed">Lỗi ({counts.failed})</option>
              </select>
            </Field>
            <Field label="Audio">
              <select
                className={selectClass}
                value={audioFilter}
                onChange={(event) => setAudioFilter(event.target.value as PresenceFilter)}
                aria-label="Lọc theo audio"
              >
                <option value="all">Tất cả</option>
                <option value="yes">Có audio ({counts.hasAudio})</option>
                <option value="no">Chưa có audio ({counts.noAudio})</option>
              </select>
            </Field>
            <Field label="Video">
              <select
                className={selectClass}
                value={videoFilter}
                onChange={(event) => setVideoFilter(event.target.value as PresenceFilter)}
                aria-label="Lọc theo video"
              >
                <option value="all">Tất cả</option>
                <option value="yes">Có Video ({counts.hasVideo})</option>
                <option value="no">Chưa có Video ({counts.noVideo})</option>
              </select>
            </Field>
            <Field label="YouTube">
              <select
                className={selectClass}
                value={youtubeFilter}
                onChange={(event) => setYoutubeFilter(event.target.value as PresenceFilter)}
                aria-label="Lọc theo YouTube"
              >
                <option value="all">Tất cả</option>
                <option value="yes">Có YT ({counts.hasYoutube})</option>
                <option value="no">Chưa có YT ({counts.noYoutube})</option>
              </select>
            </Field>
          </div>
        </div>
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
                  <TableHead className="w-[50px] min-w-[50px] px-2 text-center" title="Thứ tự">
                    STT
                  </TableHead>
                  <TableHead>Patch</TableHead>
                  <TableHead>Tiến độ</TableHead>
                  <TableHead>Pipeline</TableHead>
                  <TableHead>Giọng đọc</TableHead>
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
                    onSelect={select}
                    onOpen={onOpenPatch}
                    onOpenIssues={openIssues}
                    onOpenMedia={openMedia}
                    onUploadVideo={uploadVideo}
                    onRetryPublish={retryPublish}
                    onRepublish={(patch) => setRepublishPatch(patch)}
                    voice={voiceCtx}
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

      <Dialog open={mediaOpen} onOpenChange={setMediaOpen}>
        <DialogContent className="w-[calc(100%-2rem)] max-w-4xl overflow-hidden">
          <DialogHeader>
            <DialogTitle>Media · Patch {mediaPatch ? `#${mediaPatch.patch_index + 1}` : ""}</DialogTitle>
            <DialogDescription className="break-all font-mono text-[11px]">
              {mediaPatch ? patchMediaDir(mediaPatch) : ""}
            </DialogDescription>
          </DialogHeader>
          {mediaPatch && (
            <MediaBrowser
              key={mediaPatch.id}
              initialPath={patchMediaDir(mediaPatch)}
              height={440}
            />
          )}
          <DialogFooter>
            {mediaPatch && (
              <Button variant="outline" size="sm" asChild>
                <Link to={`/media-browser?path=${encodeURIComponent(patchMediaDir(mediaPatch))}`}>
                  Mở trang Media đầy đủ →
                </Link>
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

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
