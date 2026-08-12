import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  CheckCircle2,
  FileText,
  Film,
  Layers,
  Mic,
  Settings,
  Video,
  X,
} from "lucide-react";
import { api, Patch, post, postJson } from "@/api";
import { Header, LoadingState } from "@/components/common/Header";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { StatusBadge } from "@/components/common/StatusBadge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { AudioSettings, ConfigTab, NormalizationSettings, errorText } from "./book-detail/types";
import { useBookDetail, useChapterValidation, useTtsOptions } from "./book-detail/useBookDetail";
import { CheckField, LiveIndicator, TabBar } from "./book-detail/parts";
import { PatchesPanel } from "./book-detail/PatchesPanel";
import { BuildPanel } from "./book-detail/BuildPanel";
import { ExportPanel } from "./book-detail/ExportPanel";
import { ChaptersPanel } from "./book-detail/ChaptersPanel";
import { ChapterDialog } from "./book-detail/ChapterDialog";
import { ConfigDialog, PatchPreviewDialog, TitleNormalizeDialog } from "./book-detail/dialogs";
import { OverlayEditor } from "./book-detail/OverlayEditor";

type MainTab = "patches" | "build" | "chapters" | "thumbnail";

/** Tùy chọn tự động hoá khi chạy hàng loạt TTS: dựng video (bắt buộc nếu muốn
 * auto-upload) và/hoặc upload YouTube. */
export type BatchTtsAutomation = {
  autoCreateVideo: boolean;
  autoUploadYoutube: boolean;
  retryCount: number;
};

const DEFAULT_AUTOMATION: BatchTtsAutomation = {
  autoCreateVideo: false,
  autoUploadYoutube: false,
  retryCount: 2,
};

/** Hộp thoại xác nhận TTS hàng loạt kèm thiết lập tự động hoá patch pipeline.
 * Ràng buộc: upload YouTube bắt buộc kéo theo dựng video (upload => create). */
export function BatchTtsDialog({
  targets,
  automation,
  onAutomationChange,
  open,
  onOpenChange,
  onConfirm,
}: {
  targets: number;
  automation: BatchTtsAutomation;
  onAutomationChange: (patch: Partial<BatchTtsAutomation>) => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  const toggleUpload = (value: boolean) => {
    onAutomationChange({ autoUploadYoutube: value, autoCreateVideo: value ? true : automation.autoCreateVideo });
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-lg overflow-auto">
        <DialogHeader>
          <DialogTitle>Tạo audio cho {targets} patch</DialogTitle>
          <DialogDescription>
            Đưa các patch vào hàng đợi TTS. Bật tự động hoá để nối tiếp dây chuyền video/YouTube ngay sau khi audio
            hoàn thành.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-3 rounded-md border border-border p-3">
            <CheckField
              checked={automation.autoCreateVideo}
              disabled={automation.autoUploadYoutube}
              onChange={(value) => onAutomationChange({ autoCreateVideo: value })}
              label="Tự động dựng video sau khi audio xong"
            />
            <CheckField
              checked={automation.autoUploadYoutube}
              onChange={toggleUpload}
              label="Tự động upload lên YouTube sau khi dựng video"
            />
            {automation.autoUploadYoutube && (
              <div className="rounded-md bg-muted/40 px-3 py-2 text-[11px] text-muted-foreground">
                Đã bật kéo theo dựng video bắt buộc — video là tiền đề của upload, nên không thể tắt mục trên khi
                upload vẫn bật.
              </div>
            )}
          </div>

          <label className="block text-xs font-medium">
            <span className="flex items-baseline justify-between gap-2">
              Số lần thử lại khi lỗi
              <span className="font-normal text-muted-foreground">0–10 · mặc định 2</span>
            </span>
            <input
              type="number"
              min="0"
              max="10"
              className="mt-1.5 h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/15"
              value={automation.retryCount}
              onChange={(event) =>
                onAutomationChange({ retryCount: Math.max(0, Math.min(10, Number(event.target.value) || 0)) })
              }
            />
          </label>
        </div>

        <DialogFooter>
          <Button onClick={onConfirm}>Đưa {targets} patch vào hàng đợi</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function BookDetail() {
  const { id } = useParams();
  const bookId = id || "";

  const [tab, setTab] = useState<MainTab>("patches");
  const [message, setMessage] = useState("");
  const [selectedIds, setSelectedIds] = useState<number[]>([]);
  const [previewPatch, setPreviewPatch] = useState<Patch>();
  const [previewOpen, setPreviewOpen] = useState(false);
  const [configOpen, setConfigOpen] = useState(false);
  const [configTab, setConfigTab] = useState<ConfigTab>("audio");
  const [chapterOpen, setChapterOpen] = useState(false);
  const [chapterIndex, setChapterIndex] = useState<number>();
  const [normalizeOpen, setNormalizeOpen] = useState(false);
  const [busyCount, setBusyCount] = useState(0);
  const [running, setRunning] = useState<"audio" | "video" | "youtube" | "thumbnail">();
  const [ttsOpen, setTtsOpen] = useState(false);
  const [ttsTargets, setTtsTargets] = useState(0);
  const [automation, setAutomation] = useState<BatchTtsAutomation>(DEFAULT_AUTOMATION);
  const [settings, setSettings] = useState<AudioSettings>({
    modelId: "edge-tts",
    voiceId: "",
    maxChars: "400",
    withEffects: false,
  });
  const [exportSettings, setExportSettings] = useState<AudioSettings>({
    modelId: "omnivoice",
    voiceId: "",
    maxChars: "1200",
    withEffects: false,
  });
  const [normalization, setNormalization] = useState<NormalizationSettings>({
    numbers: true,
    junk: true,
    spellcheck: true,
    dictionary: false,
    transliteration: false,
  });

  const setBusy = useCallback(
    (busy: boolean) => setBusyCount((count) => Math.max(0, count + (busy ? 1 : -1))),
    []
  );

  // Dừng polling khi đang mở dialog hoặc đang chạy thao tác: tránh ghi đè state giữa chừng.
  const paused = previewOpen || configOpen || chapterOpen || normalizeOpen || ttsOpen || busyCount > 0;
  const { data, exports, pipeline, loading, error, live, setLive, updatedAt, refreshing, refresh } = useBookDetail(
    bookId,
    paused
  );
  const { ttsModels, voiceOptions, currentVoiceName } = useTtsOptions(data, settings.modelId);
  const chapterVal = useChapterValidation(bookId);

  useEffect(() => {
    if (!data) return;
    let cancelled = false;
    api<{ model_id: string; voice_id: string; max_chars: number; with_effects: boolean }>(
      `/books/${bookId}/audio-settings`
    )
      .then((saved) => {
        if (cancelled) return;
        setSettings({
          modelId: saved.model_id,
          voiceId: saved.voice_id || "",
          maxChars: saved.max_chars ? String(saved.max_chars) : "",
          withEffects: saved.with_effects,
        });
      })
      .catch((err) => !cancelled && setMessage(errorText(err)));
    return () => {
      cancelled = true;
    };
  }, [bookId, Boolean(data)]);

  useEffect(() => {
    if (!data) return;
    let cancelled = false;
    api<{ model_id: string; voice_id: string; max_chars: number; with_effects: boolean }>(
      `/books/${bookId}/export-audio-settings`
    )
      .then((saved) => {
        if (cancelled) return;
        setExportSettings({
          modelId: saved.model_id,
          voiceId: saved.voice_id || "",
          maxChars: saved.max_chars ? String(saved.max_chars) : "",
          withEffects: saved.with_effects,
        });
      })
      .catch((err) => !cancelled && setMessage(errorText(err)));
    return () => {
      cancelled = true;
    };
  }, [bookId, Boolean(data)]);

  useEffect(() => {
    if (!data) return;
    setNormalization({
      numbers: Boolean(data.book.normalize_numbers_enabled),
      junk: Boolean(data.book.normalize_junk_enabled),
      spellcheck: Boolean(data.book.normalize_spellcheck_enabled),
      dictionary: Boolean(data.book.normalize_dictionary_enabled),
      transliteration: Boolean(data.book.normalize_transliteration_enabled),
    });
  }, [
    data?.book.id,
    data?.book.normalize_numbers_enabled,
    data?.book.normalize_junk_enabled,
    data?.book.normalize_spellcheck_enabled,
    data?.book.normalize_dictionary_enabled,
    data?.book.normalize_transliteration_enabled,
  ]);

  const updateSettings = useCallback(
    (patch: Partial<AudioSettings>) => setSettings((current) => ({ ...current, ...patch })),
    []
  );
  const updateExportSettings = useCallback(
    (patch: Partial<AudioSettings>) => setExportSettings((current) => ({ ...current, ...patch })),
    []
  );

  const exportTtsOptions = useTtsOptions(data, exportSettings.modelId);

  // Model mặc định phải nằm trong danh sách backend trả về.
  useEffect(() => {
    if (!ttsModels.length) return;
    if (ttsModels.some((model) => model.id === settings.modelId)) return;
    updateSettings({ modelId: ttsModels[0].id });
  }, [ttsModels, settings.modelId, updateSettings]);

  // Voice tự chọn lại khi đổi model, nhưng không đè lên lựa chọn hợp lệ của người dùng.
  useEffect(() => {
    if (!ttsModels.length) return;
    if (settings.voiceId && voiceOptions.some((option) => option.value === settings.voiceId)) return;
    const next = voiceOptions[0]?.value || currentVoiceName;
    if (next !== settings.voiceId) updateSettings({ voiceId: next });
  }, [ttsModels, voiceOptions, settings.modelId, settings.voiceId, currentVoiceName, updateSettings]);

  useEffect(() => {
    if (!exportTtsOptions.ttsModels.length) return;
    if (exportTtsOptions.ttsModels.some((model) => model.id === exportSettings.modelId)) return;
    updateExportSettings({ modelId: exportTtsOptions.ttsModels[0].id });
  }, [exportSettings.modelId, exportTtsOptions.ttsModels, updateExportSettings]);

  useEffect(() => {
    if (!exportTtsOptions.ttsModels.length) return;
    if (
      exportSettings.voiceId &&
      exportTtsOptions.voiceOptions.some((option) => option.value === exportSettings.voiceId)
    ) return;
    const next = exportTtsOptions.voiceOptions[0]?.value || exportTtsOptions.currentVoiceName;
    if (next !== exportSettings.voiceId) updateExportSettings({ voiceId: next });
  }, [exportSettings, exportTtsOptions, updateExportSettings]);

  const patches = useMemo(() => data?.patches || [], [data]);
  const patchIds = useMemo(() => patches.map((patch) => patch.id), [patches]);

  // Chỉ loại bỏ patch đã biến mất — giữ nguyên lựa chọn qua các nhịp làm mới.
  useEffect(() => {
    setSelectedIds((current) => {
      const next = current.filter((patchId) => patchIds.includes(patchId));
      return next.length === current.length ? current : next;
    });
  }, [patchIds]);

  const stats = useMemo(() => {
    const done = patches.filter((patch) => patch.status === "done").length;
    const failed = patches.filter((patch) => patch.status === "failed").length;
    const pipelines = pipeline ? Object.values(pipeline.pipelines) : [];
    const videos = pipelines.filter(
      (item) => item.video_status === "done" || item.stage === "video_done" || item.stage === "published"
    ).length;
    const uploads = pipelines.filter((item) => item.upload_state === "published").length;
    return {
      done,
      failed,
      videos,
      uploads,
      hasAudio: Boolean(pipeline?.has_final_audio) || done > 0,
      hasVideo: videos > 0 || Boolean(data?.book.final_video_path),
      hasYoutube: uploads > 0,
    };
  }, [patches, pipeline, data?.book.final_video_path]);

  const runBatchThumbnail = useCallback(
    async () => {
      const targets = selectedIds.length ? selectedIds : patches.map((patch) => patch.id);
      if (!targets.length) {
        setMessage("Không có patch để cập nhật thumbnails.");
        return;
      }
      setRunning("thumbnail");
      setBusy(true);
      try {
        const result = await postJson<{
          generated: number[];
          failed: { patch_id: number; error: string }[];
          invalid_ids: number[];
        }>(`/books/${bookId}/thumbnails/regenerate`, { patch_ids: targets });
        const issues = result.failed.length + result.invalid_ids.length;
        setMessage(
          issues
            ? `Đã tạo ${result.generated.length}/${targets.length} thumbnail; ${issues} mục lỗi hoặc không hợp lệ.`
            : `Đã tạo lại ${result.generated.length} thumbnail YouTube.`
        );
        await refresh();
      } catch (err) {
        setMessage(errorText(err));
      } finally {
        setRunning(undefined);
        setBusy(false);
      }
    },
    [bookId, patches, selectedIds, refresh, setBusy]
  );

  const runBatch = useCallback(
    async (kind: "audio" | "video" | "youtube") => {
      const fallback =
        kind === "audio" ? patches : patches.filter((patch) => patch.status === "done");
      const targets = selectedIds.length ? selectedIds : fallback.map((patch) => patch.id);
      if (!targets.length) {
        setMessage(
          kind === "audio" ? "Không có patch để tạo âm thanh." : "Không có patch đã có audio để chạy bước này."
        );
        return;
      }

      // TTS hàng loạt cần xác nhận + thiết lập tự động hoá (dựng video / upload
      // YouTube). Các bước video/YouTube chạy ngay như cũ.
      if (kind === "audio") {
        setTtsTargets(targets.length);
        setTtsOpen(true);
        return;
      }

      setRunning(kind);
      setBusy(true);
      try {
        const endpoint = kind === "video" ? "generate-video" : "youtube-upload";
        const label = kind === "video" ? "video" : "YouTube";
        let queued = 0;
        let firstError: unknown;
        // Gửi tuần tự để hàng đợi giữ đúng thứ tự patch.
        for (const patchId of targets) {
          try {
            await post(`/books/${bookId}/patches/${patchId}/${endpoint}`);
            queued++;
          } catch (err) {
            if (!firstError) firstError = err;
          }
        }
        setMessage(
          queued === targets.length
            ? `Đã đưa ${queued} patch vào hàng đợi ${label}.`
            : `Đã đưa ${queued}/${targets.length} patch vào hàng đợi ${label}. ${errorText(firstError)}`
        );
        await refresh();
      } catch (err) {
        setMessage(errorText(err));
      } finally {
        setRunning(undefined);
        setBusy(false);
      }
    },
    [bookId, patches, selectedIds, settings, refresh, setBusy]
  );

  const openPatch = useCallback((patch: Patch) => {
    setPreviewPatch(patch);
    setPreviewOpen(true);
  }, []);

  const openConfig = useCallback((next: ConfigTab) => {
    setConfigTab(next);
    setConfigOpen(true);
  }, []);

  /** Chạy TTS hàng loạt với tự động hoá đã chọn trong dialog xác nhận. */
  const confirmBatchTts = useCallback(async () => {
    const targets = selectedIds.length ? selectedIds : patches.map((patch) => patch.id);
    if (!targets.length || running) return;
    const nextAutomation = {
      autoCreateVideo: automation.autoUploadYoutube || automation.autoCreateVideo,
      autoUploadYoutube: automation.autoUploadYoutube,
      retryCount: automation.retryCount,
    };
    setRunning("audio");
    setBusy(true);
    try {
      const result = await postJson<{
        queued: number;
        auto_create_video: boolean;
        auto_upload_youtube: boolean;
        retry_count: number;
      }>(`/books/${bookId}/tts/generate`, {
        patch_ids: targets,
        model_id: settings.modelId,
        voice_id: settings.voiceId || undefined,
        max_chars: settings.maxChars ? Number(settings.maxChars) : undefined,
        with_effects: settings.withEffects,
        auto_create_video: nextAutomation.autoCreateVideo,
        auto_upload_youtube: nextAutomation.autoUploadYoutube,
        retry_count: nextAutomation.retryCount,
      });
      const chain = result.auto_upload_youtube
        ? " → tự động dựng video và upload YouTube"
        : result.auto_create_video
          ? " → tự động dựng video"
          : "";
      setMessage(
        `Đã đưa ${result.queued} patch vào hàng đợi TTS${chain}.${
          result.retry_count ? ` Tối đa ${result.retry_count + 1} lần thử mỗi patch.` : ""
        }`
      );
      await refresh();
    } catch (err) {
      setMessage(errorText(err));
    } finally {
      setRunning(undefined);
      setBusy(false);
      setTtsOpen(false);
    }
  }, [automation, bookId, patches, refresh, running, selectedIds, setBusy, settings]);

  const openChapter = useCallback((index: number) => {
    setChapterIndex(index);
    setChapterOpen(true);
  }, []);

  // Sau khi ghi chương (sửa nội dung hoặc chuẩn hoá tiêu đề): làm mới báo cáo kiểm tra
  // trước (không bị inFlight-guard chặn), rồi làm mới data chính — best-effort.
  const onChapterSaved = useCallback(async () => {
    await chapterVal.reload();
    await refresh();
  }, [chapterVal, refresh]);

  if (loading && !data) return <LoadingState text={`Đang mở hồ sơ sách #${bookId}...`} />;
  if (!data)
    return (
      <div className="space-y-4">
        <Button asChild variant="ghost" size="sm" className="px-0">
          <Link to="/books" className="gap-1.5 text-xs">
            <ArrowLeft className="h-3.5 w-3.5" /> Trở lại Thư viện
          </Link>
        </Button>
        <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-800">
          Không tải được hồ sơ sách #{bookId}. {error}
        </div>
        <Button size="sm" onClick={refresh}>
          Thử lại
        </Button>
      </div>
    );

  const chapterMax = Math.max(0, data.chapters.length - 1);
  const selectionLabel = selectedIds.length ? `${selectedIds.length} patch đã chọn` : "Toàn bộ patch";

  const steps = [
    {
      key: "audio" as const,
      icon: Mic,
      label: "Âm thanh",
      detail: `${stats.done}/${patches.length} patch`,
      done: stats.hasAudio,
      action: stats.hasAudio ? "Tạo lại" : "Tạo âm thanh",
      disabled: false,
    },
    {
      key: "video" as const,
      icon: Film,
      label: "Video",
      detail: stats.videos ? `${stats.videos} video` : "Chưa dựng",
      done: stats.hasVideo,
      action: stats.hasVideo ? "Dựng lại" : "Dựng video",
      disabled: !stats.hasAudio,
    },
    {
      key: "youtube" as const,
      icon: Video,
      label: "YouTube",
      detail: stats.uploads ? `${stats.uploads} đã đăng` : "Chưa đăng",
      done: stats.hasYoutube,
      action: stats.hasYoutube ? "Đăng lại" : "Upload",
      disabled: !stats.hasVideo,
    },
  ];

  const tiles = [
    { icon: FileText, value: data.chapters.length, label: "Chương" },
    { icon: Layers, value: patches.length, label: "Patches" },
    { icon: CheckCircle2, value: stats.done, label: "Audio xong", accent: stats.done > 0 },
    { icon: AlertTriangle, value: stats.failed, label: "Lỗi", danger: stats.failed > 0 },
    { icon: Film, value: stats.videos, label: "Video", accent: stats.videos > 0 },
    { icon: Video, value: stats.uploads, label: "YouTube", accent: stats.uploads > 0 },
  ];

  return (
    <div className={cn("space-y-5", selectedIds.length && "pb-24")}>
      <Button asChild variant="ghost" size="sm" className="-mb-2 px-0">
        <Link to="/books" className="gap-1.5 text-xs">
          <ArrowLeft className="h-3.5 w-3.5" /> Trở lại Thư viện
        </Link>
      </Button>

      <Header
        title={
          <span className="flex flex-wrap items-center gap-2">
            {data.book.title}
            {chapterVal.report && !chapterVal.report.numbering.is_continuous && (
              <button
                onClick={() => setTab("chapters")}
                title={`Thiếu ${chapterVal.report.numbering.missing_count} số · trùng ${chapterVal.report.numbering.duplicate_count} số`}
                className="inline-flex items-center gap-1 rounded-md bg-amber-100 px-2 py-1 text-xs font-semibold text-amber-900 hover:bg-amber-200"
              >
                <AlertTriangle className="h-3.5 w-3.5" /> Chương không liên tục
              </button>
            )}
            {chapterVal.report &&
              chapterVal.report.titles.fixable + chapterVal.report.titles.no_name + chapterVal.report.titles.unknown > 0 && (
                <button
                  onClick={() => setTab("chapters")}
                  className="inline-flex items-center gap-1 rounded-md bg-amber-50 px-2 py-1 text-xs font-semibold text-amber-800 hover:bg-amber-100"
                >
                  {chapterVal.report.titles.fixable + chapterVal.report.titles.no_name + chapterVal.report.titles.unknown}{" "}
                  tiêu đề sai định dạng
                </button>
              )}
          </span>
        }
        subtitle={`#${bookId} · ${data.book.original_filename} · ${new Date(data.book.created_at).toLocaleDateString("vi-VN")}`}
        action={
          <div className="flex items-center gap-2">
            <LiveIndicator
              live={live}
              refreshing={refreshing}
              updatedAt={updatedAt}
              onToggle={() => setLive(!live)}
              onRefresh={refresh}
            />
            <StatusBadge value={data.book.status} />
          </div>
        }
      />

      {message && (
        <div
          role="status"
          className="flex items-start justify-between gap-3 rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-xs text-amber-900"
        >
          <span>{message}</span>
          <Button variant="ghost" size="icon" className="-mr-2 -mt-1 h-6 w-6" onClick={() => setMessage("")}>
            <X className="h-3 w-3" />
          </Button>
        </div>
      )}

      {Boolean(data.last_error) && (
        <div className="flex items-start gap-3 rounded-md border border-red-200 bg-red-50 px-4 py-3 text-xs text-red-800">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-red-600" />
          <div className="min-w-0">
            <div className="font-bold">Lỗi xử lý gần nhất</div>
            <div className="mt-1 break-words font-mono">
              {String(
                (data.last_error as { detail?: string; message?: string }).detail ||
                  (data.last_error as { message?: string }).message ||
                  JSON.stringify(data.last_error)
              )}
            </div>
          </div>
        </div>
      )}

      <div className="grid grid-cols-3 gap-2 sm:gap-3 lg:grid-cols-6">
        {tiles.map(({ icon: Icon, value, label, accent, danger }) => (
          <Card key={label} className="shadow-none">
            <CardContent className="flex items-center gap-2.5 p-3 sm:gap-3 sm:p-4">
              <Icon
                className={cn(
                  "h-4 w-4 shrink-0",
                  danger ? "text-red-600" : accent ? "text-emerald-600" : "text-primary"
                )}
              />
              <div className="min-w-0">
                <div className="font-mono text-lg font-bold leading-none">{value}</div>
                <div className="mt-1 truncate text-[11px] text-muted-foreground">{label}</div>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Dây chuyền sản xuất: audio → video → YouTube */}
      <Card>
        <CardContent className="flex flex-col gap-3 p-4 lg:flex-row lg:items-stretch">
          {steps.map((step, index) => (
            <React.Fragment key={step.key}>
              {index > 0 && (
                <div className="hidden items-center px-1 text-muted-foreground/40 lg:flex">
                  <ArrowRight className="h-4 w-4" />
                </div>
              )}
              <div
                className={cn(
                  "flex flex-1 items-center gap-3 rounded-md border px-3 py-2.5",
                  step.done ? "border-emerald-200 bg-emerald-50/50" : "border-border bg-muted/20"
                )}
              >
                <step.icon className={cn("h-4 w-4 shrink-0", step.done ? "text-emerald-600" : "text-muted-foreground")} />
                <div className="min-w-0 flex-1">
                  <div className="text-xs font-semibold">{step.label}</div>
                  <div className="truncate font-mono text-[10px] text-muted-foreground">{step.detail}</div>
                </div>
                <Button
                  size="sm"
                  variant={step.done ? "outline" : "default"}
                  disabled={step.disabled || Boolean(running)}
                  onClick={() => runBatch(step.key)}
                >
                  {running === step.key ? "Đang gửi..." : step.action}
                </Button>
              </div>
            </React.Fragment>
          ))}
          <div className="flex items-center lg:pl-1">
            <Button variant="ghost" size="sm" className="w-full lg:w-auto" onClick={() => openConfig("audio")}>
              <Settings className="h-3.5 w-3.5" /> Cấu hình
            </Button>
          </div>
        </CardContent>
      </Card>

      <TabBar<MainTab>
        value={tab}
        onChange={setTab}
        tabs={[
          { value: "patches", label: "Patches & Export", badge: patches.length },
          { value: "build", label: "Xây dựng" },
          { value: "chapters", label: "Mục lục", badge: data.chapters.length },
          { value: "thumbnail", label: "Thumbnail" },
        ]}
      />

      {tab === "patches" && (
        <div className="space-y-5">
          <PatchesPanel
            bookId={bookId}
            patches={patches}
            chapters={data.chapters}
            pipelines={pipeline?.pipelines || {}}
            selectedIds={selectedIds}
            onSelectionChange={setSelectedIds}
            onOpenPatch={openPatch}
            onMessage={setMessage}
            onRefresh={refresh}
            onBusyChange={setBusy}
          />
          <ExportPanel
            bookId={bookId}
            patches={patches}
            selectedIds={selectedIds}
            accounts={exports.accounts}
            syncTargets={exports.sync_targets}
            settings={exportSettings}
            onSettingsChange={updateExportSettings}
            ttsModels={exportTtsOptions.ttsModels}
            voiceOptions={exportTtsOptions.voiceOptions}
            onMessage={setMessage}
            onRefresh={refresh}
            onBusyChange={setBusy}
          />
        </div>
      )}

      {tab === "build" && (
        <BuildPanel
          bookId={bookId}
          chapterMax={chapterMax}
          failedCount={stats.failed}
          onMessage={setMessage}
          onRefresh={refresh}
          onBusyChange={setBusy}
        />
      )}

      {tab === "chapters" && (
        <ChaptersPanel
          chapters={data.chapters}
          report={chapterVal.report}
          loading={chapterVal.loading}
          onAnalyze={chapterVal.reload}
          onOpenChapter={openChapter}
          onOpenNormalize={() => setNormalizeOpen(true)}
        />
      )}

      {tab === "thumbnail" && (
        <OverlayEditor bookId={bookId} patchIds={patchIds} onMessage={setMessage} onSaved={refresh} />
      )}

      {/* Thanh hành động theo lựa chọn: chỉ hiện khi có patch được chọn. */}
      {selectedIds.length > 0 && (
        <div className="fixed inset-x-0 bottom-0 z-40 border-t border-border bg-card/95 backdrop-blur-sm md:left-64">
          <div className="mx-auto flex max-w-7xl items-center gap-2 overflow-x-auto px-4 py-2.5 sm:px-6 lg:px-8">
            <span className="shrink-0 text-xs font-medium">{selectionLabel}</span>
            <Button variant="ghost" size="sm" className="shrink-0" onClick={() => setSelectedIds([])}>
              Bỏ chọn
            </Button>
            <div className="ml-auto flex shrink-0 items-center gap-2">
              <Button size="sm" disabled={Boolean(running)} onClick={() => runBatch("audio")}>
                <Mic className="h-3.5 w-3.5" /> Âm thanh
              </Button>
              <Button size="sm" variant="outline" disabled={Boolean(running)} onClick={() => runBatch("video")}>
                <Film className="h-3.5 w-3.5" /> Video
              </Button>
              <Button size="sm" variant="outline" disabled={Boolean(running)} onClick={() => runBatchThumbnail()}>
                <Film className="h-3.5 w-3.5" /> Thumbnails
              </Button>
              <Button size="sm" variant="outline" disabled={Boolean(running)} onClick={() => runBatch("youtube")}>
                <Video className="h-3.5 w-3.5" /> YouTube
              </Button>
            </div>
          </div>
        </div>
      )}

<PatchPreviewDialog
        bookId={bookId}
        patch={previewPatch}
        chapters={data.chapters}
        open={previewOpen}
        onOpenChange={setPreviewOpen}
        onMessage={setMessage}
      />

      <BatchTtsDialog
        targets={ttsTargets}
        automation={automation}
        onAutomationChange={(patch) => setAutomation((current) => ({ ...current, ...patch }))}
        open={ttsOpen}
        onOpenChange={setTtsOpen}
        onConfirm={confirmBatchTts}
      />

      <ConfigDialog
        bookId={bookId}
        open={configOpen}
        onOpenChange={setConfigOpen}
        tab={configTab}
        onTabChange={setConfigTab}
        settings={settings}
        onSettingsChange={updateSettings}
        normalization={normalization}
        onNormalizationChange={(patch) => setNormalization((current) => ({ ...current, ...patch }))}
        onNormalizationSaved={refresh}
        chapterCount={data.chapters.length}
        ttsModels={ttsModels}
        voiceOptions={voiceOptions}
         voiceClipPath={data.book.voice_clip_path}
         onMessage={setMessage}
         patchIds={patchIds}
         onSaved={refresh}
       />

      <ChapterDialog
        bookId={bookId}
        chapterIndex={chapterIndex}
        chapterCount={data.chapters.length}
        open={chapterOpen}
        onOpenChange={setChapterOpen}
        onChapterIndexChange={setChapterIndex}
        onMessage={setMessage}
        onSaved={onChapterSaved}
      />

      <TitleNormalizeDialog
        bookId={bookId}
        open={normalizeOpen}
        onOpenChange={setNormalizeOpen}
        onMessage={setMessage}
        onOpenChapter={openChapter}
        onApplied={onChapterSaved}
      />
    </div>
  );
}
