import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, AudioLines, Captions, CheckCircle2, Download, Eye, ListChecks, Play, Replace, Search, Square } from "lucide-react";
import { api, Chapter, Patch, post, postJson, VoiceItem } from "@/api";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { StatusBadge } from "@/components/common/StatusBadge";
import { VoicePreviewButton } from "@/components/common/VoicePreviewButton";
import { WaveformPreview } from "@/components/common/WaveformPreview";
import { Textarea } from "@/components/ui/textarea";
import { Combobox } from "@/components/ui/combobox";
import { cn } from "@/lib/utils";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  AudioSettings,
  ConfigTab,
  MusicMixState,
  NormalizationSettings,
  TitleNormalizePreview,
  TtsModel,
  VideoConfig,
  VoiceOption,
  YouTubeConfig,
  YouTubeMetadataPreview,
  YouTubeSettings,
  errorText,
} from "./types";
import { CheckField, Field, TabBar, TtsOptionsFields, checkboxClass, fieldClass, selectClass } from "./parts";
import { BackgroundPicker } from "./BackgroundPicker";
import { MusicMixFields } from "./MusicMixFields";
import { ReplaceRulesPanel } from "./ReplaceRulesPanel";
import { AiContentGenerator, YouTubeConfigFields } from "./YouTubeFields";
import { useTtsOptions } from "./useBookDetail";
import { ChunkPreviewBar, PreviewChunk, useChunkPreview } from "./useChunkPreview";

const WAVEFORM_TEMPLATES = [  { id: "bold", name: "Dải nổi", description: "Line sáng trên nền tối", style: "line", layout: "horizontal", color: "#ffffff", background: "#050816", backgroundOpacity: 0.68, position: "bottom", height: 150, opacity: 1 },
  { id: "studio", name: "Studio", description: "Sóng đối xứng giữa khung", style: "cline", layout: "horizontal", color: "#22d3ee", background: "#082f49", backgroundOpacity: 0.62, position: "center", height: 200, opacity: 1 },
  { id: "vertical", name: "Cột nhịp", description: "Dải sóng dọc bên trái", style: "p2p", layout: "vertical", color: "#facc15", background: "#1c1917", backgroundOpacity: 0.72, position: "center", height: 300, opacity: 1 },
  { id: "orbit", name: "Quỹ đạo", description: "Waveform tròn ở trung tâm", style: "line", layout: "circular", color: "#fb7185", background: "#2e1065", backgroundOpacity: 0.58, position: "center", height: 280, opacity: 1 },
] as const;

export function PatchPreviewDialog({
  bookId,
  patch,
  chapters,
  open,
  onOpenChange,
  onMessage,
  data,
  defaultModelId,
  defaultVoiceId,
}: {
  bookId: string;
  patch?: Patch;
  chapters: Chapter[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onMessage: (message: string) => void;
  data: import("./types").Detail;
  defaultModelId: string;
  defaultVoiceId: string;
}) {
  const [chapterTexts, setChapterTexts] = useState<Record<number, string>>({});
  const [loading, setLoading] = useState(false);
  const [search, setSearch] = useState("");
  const [replacement, setReplacement] = useState("");
  const [isRegex, setIsRegex] = useState(false);
  const [replacing, setReplacing] = useState(false);
  const [tocSearch, setTocSearch] = useState("");
  const [quickModelId, setQuickModelId] = useState(defaultModelId);
  const [quickVoiceId, setQuickVoiceId] = useState(defaultVoiceId);
  const [quickGenerating, setQuickGenerating] = useState(false);
  const [completedChunksTts, setCompletedChunksTts] = useState<{ engine?: string; voice?: string; source?: string }>();
  const loadPreviewChunks = useCallback(async () => {
    if (!patch) return null;
    const preview = await api<{
      chunks: (Omit<PreviewChunk, "url"> & { url?: string })[];
      tts: { tts_engine?: string; voice?: string; source?: string };
    }>(`/books/${bookId}/patches/${patch.id}/completed-chunks-preview`);
    setCompletedChunksTts({ engine: preview.tts.tts_engine, voice: preview.tts.voice, source: preview.tts.source });
    // Chunk theo chương có URL riêng (kho books/{id}/chapters); thiếu thì là layout cũ của patch.
    return preview.chunks.map((chunk) => ({
      ...chunk,
      url: chunk.url || `/books/${bookId}/patches/${patch.id}/chunk-audio/${chunk.index}`,
    }));
  }, [bookId, patch]);
  const chunkPreview = useChunkPreview({ load: loadPreviewChunks, onMessage, resetKey: `${open}:${patch?.id}` });
  const quickTts = useTtsOptions(data, quickModelId);

  useEffect(() => {
    if (!open || !patch) return;
    let cancelled = false;
    setChapterTexts({});
    setCompletedChunksTts(undefined);
    setQuickModelId(defaultModelId);
    setQuickVoiceId(defaultVoiceId);
    setLoading(true);
    const patchChapters = chapters.filter(
      (chapter) => chapter.chapter_index >= patch.chapter_start && chapter.chapter_index <= patch.chapter_end
    );
    Promise.all(
      patchChapters.map(async (chapter) => [
        chapter.chapter_index,
        await api<string>(`/books/${bookId}/normalization/preview?chapter_index=${chapter.chapter_index}`),
      ] as const)
    )
      .then((items) => !cancelled && setChapterTexts(Object.fromEntries(items)))
      .catch((error) => !cancelled && onMessage(errorText(error)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [open, patch, chapters, bookId, onMessage, defaultModelId, defaultVoiceId]);

  const percent = patch?.chunk_count ? (patch.next_chunk_index * 100) / patch.chunk_count : 0;
  const patchChapters = patch
    ? chapters.filter(
        (chapter) => chapter.chapter_index >= patch.chapter_start && chapter.chapter_index <= patch.chapter_end
      )
    : [];
  const numberedChapters = patchChapters.filter((chapter) => chapter.chapter_no != null);
  const visibleTocChapters = useMemo(() => {
    const needle = tocSearch.trim().toLocaleLowerCase();
    if (!needle) return patchChapters;
    return patchChapters.filter((chapter) =>
      `${chapter.chapter_no ?? chapter.chapter_index + 1} ${chapter.title}`.toLocaleLowerCase().includes(needle)
    );
  }, [patchChapters, tocSearch]);

  const replaceAllPatches = async () => {
    if (!search) return;
    setReplacing(true);
    try {
      const result = await postJson<{ replacements: number; changed_patches: number }>(
        `/books/${bookId}/text-studio/replace`,
        { search, replace: replacement, is_regex: isRegex }
      );
      onMessage(
        result.replacements
          ? `Đã thay ${result.replacements} chỗ trên ${result.changed_patches} patch.`
          : "Không tìm thấy chuỗi nào để thay."
      );
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setReplacing(false);
    }
  };

  const generatePatchAudio = async () => {
    if (!patch || quickGenerating) return;
    setQuickGenerating(true);
    try {
      const result = await postJson<{ queued: number }>(`/books/${bookId}/tts/generate`, {
        patch_ids: [patch.id],
        model_id: quickModelId,
        voice_id: quickVoiceId || undefined,
      });
      onMessage(result.queued ? `Đã đưa Patch #${patch.patch_index + 1} vào hàng đợi TTS.` : "Patch đã có audio hoặc đang chờ xử lý.");
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setQuickGenerating(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="grid max-h-[90vh] w-[calc(100%-2rem)] max-w-6xl grid-rows-[auto_auto_minmax(0,1fr)] overflow-hidden">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 pr-8">
            <span className="truncate">
              Patch #{patch ? patch.patch_index + 1 : ""} · {patch?.name}
            </span>
            <StatusBadge value={patch?.status} />
          </DialogTitle>
          <DialogDescription>
            {patchChapters.length
              ? `${patchChapters.length} chương · ${numberedChapters.length ? `chương ${numberedChapters[0].chapter_no}–${numberedChapters[numberedChapters.length - 1].chapter_no}` : `mục ${patch!.chapter_start + 1}–${patch!.chapter_end + 1}`}`
              : "Không có chương"} ·{" "}
            {patch?.next_chunk_index}/{patch?.chunk_count} chunk
          </DialogDescription>
        </DialogHeader>

        <Progress value={percent} className="h-1.5 shrink-0" />

        <div className="min-h-0 space-y-4 overflow-y-auto pr-1">
        <div className="grid gap-2 rounded-md border border-border bg-muted/20 p-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-end">
          <Field label="TTS model (chỉ Patch này)">
            <select className={selectClass} value={quickModelId} onChange={(event) => setQuickModelId(event.target.value)}>
              {quickTts.ttsModels.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}
            </select>
          </Field>
          <Field label="Voice ID">
            <Combobox
              value={quickVoiceId}
              options={[
                { value: "", label: "Mặc định của model" },
                ...quickTts.voiceOptions.map((option) => ({ ...option, description: option.value })),
              ]}
              onChange={setQuickVoiceId}
              placeholder="Tìm hoặc chọn voice..."
              aria-label="Voice ID"
            />
          </Field>
          <Button size="sm" disabled={!patch || quickGenerating} onClick={generatePatchAudio}>
            {quickGenerating ? "Đang gửi..." : "Tạo audio Patch"}
          </Button>
        </div>

        <div className="rounded-md border border-border bg-muted/20 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="text-xs font-semibold">Nghe các chunk đã hoàn thành</div>
              <p className="mt-0.5 text-[11px] text-muted-foreground">Nối liên tục với khoảng nghỉ giống audio/video kết quả.</p>
              {completedChunksTts && (
                <p className="mt-1 font-mono text-[10px] text-muted-foreground">
                  TTS: {completedChunksTts.engine || "mặc định"} · Voice: {completedChunksTts.voice || "mặc định"}
                  {completedChunksTts.source === "book_settings" ? " (cấu hình sách hiện tại)" : ""}
                </p>
              )}
            </div>
            <Button size="sm" variant="outline" disabled={!patch || chunkPreview.state === "loading"} onClick={() => void chunkPreview.toggle()}>
              {chunkPreview.state === "playing" ? <Square className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
              {chunkPreview.state === "loading" ? "Đang chuẩn bị..." : chunkPreview.state === "playing" ? "Dừng phát" : "Phát tạm"}
            </Button>
          </div>
          <ChunkPreviewBar preview={chunkPreview} />
        </div>

        <div className="rounded-md border border-border bg-muted/20 p-3">
          <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold">
            <Replace className="h-3.5 w-3.5" /> Tìm và thay thế toàn bộ patches
          </div>
          <div className="grid gap-2 sm:grid-cols-[1fr_1fr_auto_auto] sm:items-center">
            <input
              className={cn(fieldClass, "font-mono text-xs")}
              placeholder="Tìm..."
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            <input
              className={cn(fieldClass, "font-mono text-xs")}
              placeholder="Thay bằng..."
              value={replacement}
              onChange={(event) => setReplacement(event.target.value)}
            />
            <label className="flex items-center gap-1.5 text-xs font-medium">
              <input type="checkbox" className={checkboxClass} checked={isRegex} onChange={(event) => setIsRegex(event.target.checked)} />
              Regex
            </label>
            <Button size="sm" disabled={!search || replacing} onClick={replaceAllPatches}>
              <Search className="h-3.5 w-3.5" /> {replacing ? "Đang thay..." : "Thay tất cả"}
            </Button>
          </div>
          <p className="mt-2 text-[11px] text-muted-foreground">Thao tác này áp dụng cho toàn bộ patch của sách, bao gồm các patch đã sửa thủ công.</p>
        </div>

        <div className="grid h-[60vh] min-h-[280px] overflow-hidden rounded-md border border-border md:grid-cols-[15rem_minmax(0,1fr)]">
          <nav aria-label="Mục lục chương" className="overflow-y-auto border-b border-border bg-muted/20 p-2 md:border-b-0 md:border-r">
            <div className="mb-1 px-2 py-1 text-[11px] font-semibold text-muted-foreground">Mục lục</div>
            <div className="relative mb-2">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3 w-3 -translate-y-1/2 text-muted-foreground" />
              <input
                className="h-8 w-full rounded-md border border-input bg-background pl-7 pr-2 text-[11px] outline-none focus:border-primary focus:ring-2 focus:ring-primary/15"
                placeholder="Tìm chương..."
                value={tocSearch}
                onChange={(event) => setTocSearch(event.target.value)}
                aria-label="Tìm trong mục lục"
              />
            </div>
            <div className="flex gap-1 overflow-x-auto md:block md:space-y-1 md:overflow-visible">
              {visibleTocChapters.map((chapter) => (
                <button
                  key={chapter.id}
                  type="button"
                  onClick={() => document.getElementById(`patch-chapter-${chapter.chapter_index}`)?.scrollIntoView({ block: "start" })}
                  title={chapter.title || undefined}
                  className="shrink-0 max-w-48 rounded-md px-2 py-1.5 text-left text-[11px] font-medium hover:bg-background hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring md:block md:w-full md:max-w-none"
                >
                  <span className="block truncate">
                    {chapter.title || (chapter.chapter_no != null ? `Chương ${chapter.chapter_no}` : `Mục ${chapter.chapter_index + 1}`)}
                  </span>
                </button>
              ))}
              {visibleTocChapters.length === 0 && (
                <div className="px-2 py-3 text-[11px] text-muted-foreground">Không tìm thấy chương.</div>
              )}
            </div>
          </nav>

          <div className="min-w-0 space-y-5 overflow-y-auto p-4 text-xs leading-5">
            {loading ? (
              <div className="py-20 text-center text-muted-foreground">Đang tải nội dung đã chuẩn hóa TTS và thay thế...</div>
            ) : patchChapters.length ? (
              patchChapters.map((chapter) => (
                <section key={chapter.id} id={`patch-chapter-${chapter.chapter_index}`} className="scroll-mt-4">
                  <h3 className="mb-2 font-semibold text-foreground">{chapter.title || `Chương ${chapter.chapter_no ?? chapter.chapter_index + 1}`}</h3>
                  <div className="whitespace-pre-wrap font-mono text-[11px] text-muted-foreground">
                    {chapterTexts[chapter.chapter_index] || "Chương không có nội dung."}
                  </div>
                </section>
              ))
            ) : (
              <div className="py-20 text-center text-muted-foreground">Không có chương nào trong patch.</div>
            )}
          </div>
        </div>

        {patch?.status === "done" && (
          <div className="space-y-3 pb-1">
            <audio controls className="w-full" src={`/books/${bookId}/patches/${patch.id}/audio`} />
            <DialogFooter>
              <Button variant="outline" asChild>
                <a href={`/books/${bookId}/patches/${patch.id}/audio`} download>
                  <Download className="h-4 w-4" /> Tải WAV
                </a>
              </Button>
              <Button variant="outline" asChild>
                <Link to="/queue">
                  <Play className="h-4 w-4" /> Xem hàng đợi
                </Link>
              </Button>
            </DialogFooter>
          </div>
        )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function ConfigDialog({
  bookId,
  open,
  onOpenChange,
  tab,
  onTabChange,
  settings,
  onSettingsChange,
  normalization,
  onNormalizationChange,
  onNormalizationSaved,
  chapterCount,
  ttsModels,
  voiceOptions,
  voiceClipPath,
  onMessage,
  patchIds,
  onSaved,
}: {
  bookId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  tab: ConfigTab;
  onTabChange: (tab: ConfigTab) => void;
  settings: AudioSettings;
  onSettingsChange: (patch: Partial<AudioSettings>) => void;
  normalization: NormalizationSettings;
  onNormalizationChange: (patch: Partial<NormalizationSettings>) => void;
  onNormalizationSaved: () => Promise<void>;
  chapterCount: number;
  ttsModels: TtsModel[];
  voiceOptions: VoiceOption[];
  voiceClipPath?: string | null;
  onMessage: (message: string) => void;
  patchIds: number[];
  onSaved: () => Promise<void>;
}) {
  const [videoConfig, setVideoConfig] = useState<VideoConfig>();
  const [introOutroVoices, setIntroOutroVoices] = useState<VoiceItem[]>([]);
  const [music, setMusic] = useState<MusicMixState>();
  const [ytSettings, setYtSettings] = useState<YouTubeSettings>();
  const [ytPreview, setYtPreview] = useState<YouTubeMetadataPreview>();
  const [ytPreviewLoading, setYtPreviewLoading] = useState(false);
  const [ytPreviewError, setYtPreviewError] = useState("");
  const [normalizationPreview, setNormalizationPreview] = useState("");
  const [previewChapter, setPreviewChapter] = useState(0);
  const [normalizationPreviewLoading, setNormalizationPreviewLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [applyingPodcast, setApplyingPodcast] = useState(false);

  useEffect(() => {
    if (!open) return;
    if (tab === "video" && !videoConfig) {
      Promise.all([
        api<VideoConfig>(`/books/${bookId}/video-config`),
        api<MusicMixState>(`/books/${bookId}/music`),
        api<{ voices: VoiceItem[] }>("/api/ui/media"),
      ])
        .then(([config, musicSettings, voiceMedia]) => {
          setVideoConfig(config);
          setMusic(musicSettings);
          setIntroOutroVoices(voiceMedia.voices || []);
        })
        .catch((error) => onMessage(errorText(error)));
    }
    if (tab === "youtube" && !ytSettings) {
      api<YouTubeSettings>(`/books/${bookId}/youtube-settings`).then(setYtSettings).catch((error) => onMessage(errorText(error)));
    }
  }, [open, tab, bookId, videoConfig, ytSettings, onMessage]);

  const ytConfig = ytSettings?.config;
  // Preview metadata live nhưng debounce để không gửi request theo từng ký tự.
  useEffect(() => {
    if (tab !== "youtube" || !ytConfig) return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      setYtPreviewLoading(true);
      setYtPreviewError("");
      postJson<YouTubeMetadataPreview>(`/books/${bookId}/youtube-metadata-preview`, { config: ytConfig })
        .then((result) => {
          if (!cancelled) setYtPreview(result);
        })
        .catch((error) => {
          if (!cancelled) setYtPreviewError(errorText(error));
        })
        .finally(() => {
          if (!cancelled) setYtPreviewLoading(false);
        });
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [tab, bookId, ytConfig]);

  const saveAudio = async () => {
    setSaving(true);
    try {
      await postJson(`/books/${bookId}/audio-settings`, {
        model_id: settings.modelId,
        voice_id: settings.voiceId,
        max_chars: settings.maxChars ? Number(settings.maxChars) : 0,
        with_effects: settings.withEffects,
        tts_options: settings.ttsOptions,
        chunk_pause_ms: settings.chunkPauseMs === "" ? null : Number(settings.chunkPauseMs),
        chapter_pause_ms: settings.chapterPauseMs === "" ? null : Number(settings.chapterPauseMs),
      });
      onMessage("Đã lưu cấu hình âm thanh.");
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setSaving(false);
    }
  };

  const saveNormalization = async () => {
    setSaving(true);
    try {
      const form = new FormData();
      (Object.entries(normalization) as [keyof NormalizationSettings, boolean][]).forEach(([key, enabled]) => {
        if (enabled) form.append(key, "on");
      });
      await api(`/books/${bookId}/normalization`, {
        method: "POST",
        headers: { "X-Requested-With": "autosave" },
        body: form,
      });
      onMessage("Đã lưu cấu hình chuẩn hóa TTS. Các patch audio đã hoàn thành sẽ cần tạo lại.");
      await onNormalizationSaved();
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setSaving(false);
    }
  };

  const previewNormalization = async () => {
    setNormalizationPreviewLoading(true);
    try {
      setNormalizationPreview(
        await api<string>(`/books/${bookId}/normalization/preview?chapter_index=${previewChapter}`)
      );
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setNormalizationPreviewLoading(false);
    }
  };

  const saveVideo = async () => {
    if (!videoConfig || !music) return;
    setSaving(true);
    try {
      await Promise.all([
        postJson(`/books/${bookId}/video-config`, videoConfig),
        postJson(`/books/${bookId}/music-json`, {
          music_id: music.music_id,
          music_volume: music.music_volume,
        }),
      ]);
      onMessage("Đã lưu cấu hình video, background media và mix nhạc.");
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setSaving(false);
    }
  };

  const saveYoutube = async () => {
    if (!ytSettings) return;
    setSaving(true);
    try {
      await postJson(`/books/${bookId}/youtube-settings`, ytSettings.config);
      onMessage("Đã lưu cấu hình YouTube.");
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setSaving(false);
    }
  };

  /** Đổi kênh YouTube của sách. Playlist là của riêng từng kênh nên playlist đang chọn
   * không còn hợp lệ: bỏ chọn và tắt auto-upload (backend từ chối auto-upload thiếu
   * playlist), rồi nạp lại để lấy danh sách playlist của kênh mới. */
  const changeYoutubeAccount = async (accountId: number | null) => {
    if (!ytSettings) return;
    setSaving(true);
    try {
      await postJson(`/books/${bookId}/youtube-settings`, {
        ...ytSettings.config,
        auto_upload: false,
        playlist: { ...ytSettings.config.playlist, mode: "none", playlist_id: "" },
        youtube_account_id: accountId,
      });
      setYtSettings(await api<YouTubeSettings>(`/books/${bookId}/youtube-settings`));
      onMessage("Đã đổi kênh YouTube của sách. Chọn lại playlist rồi bật lại tự động upload nếu cần.");
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setSaving(false);
    }
  };

  /** Nạp lại cấu hình YouTube từ server — job sinh nội dung vừa ghi description
   * và genre_tags, form phải hiện đúng kết quả đó. useCallback để vòng poll của
   * nút AI không khởi động lại mỗi lần render. */
  const reloadYoutubeSettings = useCallback(async () => {
    try {
      setYtSettings(await api<YouTubeSettings>(`/books/${bookId}/youtube-settings`));
    } catch (error) {
      onMessage(errorText(error));
    }
  }, [bookId, onMessage]);

  /** Đẩy thẳng thiết lập podcast + ảnh bìa lên playlist, không phải chờ tập kế tiếp.
   * Lưu cấu hình trước để YouTube nhận đúng trạng thái vừa chỉnh trên form. */
  const applyPodcast = async () => {
    if (!ytSettings || applyingPodcast) return;
    setApplyingPodcast(true);
    try {
      await postJson(`/books/${bookId}/youtube-settings`, ytSettings.config);
      const result = await api<{ podcast: string; cover: string }>(`/books/${bookId}/podcast/apply`, {
        method: "POST",
      });
      onMessage(
        `Đã áp dụng podcast lên playlist (trạng thái: ${result.podcast}` +
          `${result.cover === "uploaded" ? ", đã tải ảnh bìa mới" : `, ảnh bìa: ${result.cover}`}).`
      );
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setApplyingPodcast(false);
    }
  };

  const voiceClipName = voiceClipPath ? voiceClipPath.split(/[/\\]/).pop() : "";
  // Chỉ model clone mới đụng tới audio mẫu; model có cast riêng (ZeroTTS, VieNeu) hay
  // backend cloud thì ô Voice là giọng của chính nó. Chưa tải được catalog thì coi như
  // model clone để không giấu mất thông tin voice clip.
  const selectedModel = ttsModels.find((model) => model.id === settings.modelId);
  const usesReferenceClip = selectedModel ? selectedModel.supports_reference : true;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex h-[min(90vh,48rem)] max-w-2xl flex-col overflow-hidden">
        <DialogHeader>
          <DialogTitle>Cấu hình sản xuất</DialogTitle>
          <DialogDescription>
            Thiết lập âm thanh, video và YouTube riêng cho sách này. Khi lưu, nhóm cấu hình đó tách khỏi{" "}
            <Link to="/production-defaults" className="underline">Cấu hình mặc định</Link> và chỉ áp dụng cho ebook này.
          </DialogDescription>
        </DialogHeader>

        <TabBar<ConfigTab>
          value={tab}
          onChange={onTabChange}
          tabs={[
            { value: "audio", label: "Âm thanh" },
            { value: "normalization", label: "Chuẩn hóa TTS" },
            { value: "video", label: "Video" },
            { value: "youtube", label: "YouTube" },
          ]}
        />

        <div className="min-h-0 flex-1 overflow-y-auto pr-1">
        {tab === "audio" && (
          <div className="space-y-4">
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Field label="TTS model">
                <select
                  className={selectClass}
                  value={settings.modelId}
                  onChange={(event) => onSettingsChange({ modelId: event.target.value })}
                >
                  {ttsModels.length ? (
                    ttsModels.map((model) => (
                      <option key={model.id} value={model.id}>
                        {model.name}
                      </option>
                    ))
                  ) : (
                    <option value={settings.modelId}>{settings.modelId}</option>
                  )}
                </select>
              </Field>
              <Field label="Voice ID">
                <div className="flex items-center gap-1">
                  <Combobox
                    className="min-w-0 flex-1"
                    value={settings.voiceId}
                    options={voiceOptions.map((option) => ({ ...option, description: option.value }))}
                    onChange={(voiceId) => onSettingsChange({ voiceId })}
                    placeholder="Tìm hoặc chọn voice..."
                    aria-label="Voice ID"
                  />
                  <VoicePreviewButton
                    modelId={settings.modelId}
                    voiceId={settings.voiceId}
                    ttsOptions={settings.ttsOptions}
                  />
                </div>
              </Field>
              <Field label="Max chars" hint="0 = mặc định">
                <input
                  className={fieldClass}
                  type="number"
                  min="0"
                  value={settings.maxChars}
                  onChange={(event) => onSettingsChange({ maxChars: event.target.value })}
                />
              </Field>
              <div className="flex items-end">
                <CheckField
                  checked={settings.withEffects}
                  onChange={(value) => onSettingsChange({ withEffects: value })}
                  label="Thêm hiệu ứng âm thanh"
                />
              </div>
              <Field label="Khoảng lặng giữa chunk (ms)" hint="Nhịp thở giữa hai đoạn liền nhau">
                <input
                  className={fieldClass}
                  type="number"
                  min="0"
                  max="30000"
                  step="50"
                  value={settings.chunkPauseMs ?? ""}
                  onChange={(event) => onSettingsChange({ chunkPauseMs: event.target.value })}
                />
              </Field>
              <Field
                label="Khoảng lặng giữa chương (ms)"
                hint="Chèn trước mỗi chương trong cùng một patch; nhạc nền cuối chương fade ra tại đây"
              >
                <input
                  className={fieldClass}
                  type="number"
                  min="0"
                  max="30000"
                  step="100"
                  value={settings.chapterPauseMs ?? ""}
                  onChange={(event) => onSettingsChange({ chapterPauseMs: event.target.value })}
                />
              </Field>
              <TtsOptionsFields model={selectedModel} value={settings.ttsOptions}
                onChange={(ttsOptions) => onSettingsChange({ ttsOptions })} />
            </div>
            {usesReferenceClip && (
              <div className="rounded-md bg-muted/30 p-3 text-xs text-muted-foreground">
                {settings.voiceId.startsWith("preset:") ? (
                  <>
                    Model clone sẽ đọc một câu mẫu bằng giọng preset đã chọn rồi dùng chính clip đó
                    làm audio mẫu — voice clip của sách không được dùng trong lần chạy này.
                  </>
                ) : settings.voiceId ? (
                  <>
                    Audio mẫu:{" "}
                    <span className="font-medium text-foreground">{settings.voiceId}</span> — giọng chọn
                    ở ô Voice ID được dùng thay cho voice clip của sách.
                  </>
                ) : (
                  <>
                    Voice clip:{" "}
                    {voiceClipName ? <span className="font-medium text-foreground">{voiceClipName}</span> : "Chưa thiết lập"}
                  </>
                )}
              </div>
            )}
          </div>
        )}

        {tab === "normalization" && (
          <div className="space-y-4">
            <div className="space-y-3 rounded-md border border-border p-4">
              <CheckField
                checked={normalization.numbers}
                onChange={(value) => onNormalizationChange({ numbers: value })}
                label="Chuyển số, ngày giờ và đơn vị thành chữ"
              />
              <CheckField
                checked={normalization.junk}
                onChange={(value) => onNormalizationChange({ junk: value })}
                label="Xóa token rác từ EPUB"
              />
              <CheckField
                checked={normalization.spellcheck}
                onChange={(value) => onNormalizationChange({ spellcheck: value })}
                label="Sửa dấu chấm bị chèn trong từ tiếng Việt"
              />
              <CheckField
                checked={normalization.dictionary}
                onChange={(value) => onNormalizationChange({ dictionary: value })}
                label="Áp dụng từ điển tiếng Việt"
              />
              <CheckField
                checked={normalization.transliteration}
                onChange={(value) => onNormalizationChange({ transliteration: value })}
                label="Phiên âm từ nước ngoài"
              />
              <CheckField
                checked={normalization.abbreviations}
                onChange={(value) => onNormalizationChange({ abbreviations: value })}
                label="Mở rộng viết tắt (TP.HCM → Thành phố Hồ Chí Minh)"
              />
              <CheckField
                checked={normalization.breaks}
                onChange={(value) => onNormalizationChange({ breaks: value })}
                label="Thêm cue ngắt nghỉ trong câu (giảm đọc rời rạc)"
              />
            </div>

            <div className="rounded-md bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800">
              Lưu cấu hình sẽ reset các patch audio đã hoàn thành để TTS chạy lại. Nội dung đã sửa thủ công trong
              Text Studio vẫn được giữ nguyên và không normalize lại.
            </div>

            <ReplaceRulesPanel bookId={bookId} onMessage={onMessage} />

            <div className="space-y-3 rounded-md border border-border p-3">
              <div className="flex flex-wrap items-end gap-2">
                <Field label="Chương xem trước">
                  <input
                    className={fieldClass}
                    type="number"
                    min="0"
                    max={Math.max(0, chapterCount - 1)}
                    value={previewChapter}
                    onChange={(event) => setPreviewChapter(Number(event.target.value))}
                  />
                </Field>
                <Button variant="outline" onClick={previewNormalization} disabled={normalizationPreviewLoading || !chapterCount}>
                  {normalizationPreviewLoading ? "Đang tải..." : "Xem kết quả đã lưu"}
                </Button>
              </div>
              {normalizationPreview && (
                <pre className="max-h-52 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted/30 p-3 text-[11px] leading-5 text-muted-foreground">
                  {normalizationPreview}
                </pre>
              )}
            </div>

          </div>
        )}

        {tab === "video" &&
          (videoConfig ? (
            <div className="space-y-4">
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <Field label="Độ phân giải">
                  <select
                    className={selectClass}
                    value={videoConfig.resolution}
                    onChange={(event) => setVideoConfig({ ...videoConfig, resolution: event.target.value })}
                  >
                    <option value="1920x1080">1920×1080 (Full HD · khuyên dùng)</option>
                    <option value="1280x720">1280×720 (16:9)</option>
                    <option value="854x480">854×480 (16:9)</option>
                    <option value="1080x1920">1080×1920 (9:16 — Shorts/Reels)</option>
                    <option value="1080x1080">1080×1080 (1:1 — vuông)</option>
                  </select>
                </Field>
                <Field
                  label="Khung hình nền"
                  hint="Auto: tự chọn cách hiển thị nền ngang trong khung dọc"
                >
                  <select
                    className={selectClass}
                    value={videoConfig.fit_mode || "auto"}
                    onChange={(event) => setVideoConfig({ ...videoConfig, fit_mode: event.target.value as VideoConfig["fit_mode"] })}
                  >
                    <option value="auto">Tự động</option>
                    <option value="contain">Giữ nguyên tỉ lệ (viền đen)</option>
                    <option value="cover">Lấp đầy khung (cắt viền)</option>
                    <option value="blur">Nền mờ phóng to + ảnh giữa</option>
                  </select>
                </Field>
                <Field label="FPS">
                  <select
                    className={selectClass}
                    value={videoConfig.fps}
                    onChange={(event) => setVideoConfig({ ...videoConfig, fps: Number(event.target.value) })}
                  >
                    <option value="24">24</option>
                    <option value="30">30 (khuyên dùng)</option>
                    <option value="60">60</option>
                  </select>
                </Field>
                <Field label="Codec">
                  <select
                    className={selectClass}
                    value={videoConfig.codec}
                    onChange={(event) => setVideoConfig({ ...videoConfig, codec: event.target.value })}
                  >
                    <option value="libx264">libx264 (CPU · chất lượng cao)</option>
                    <option value="h264_nvenc">h264_nvenc (GPU)</option>
                  </select>
                </Field>
                <Field label="Audio bitrate">
                  <select
                    className={selectClass}
                    value={videoConfig.audio_bitrate}
                    onChange={(event) => setVideoConfig({ ...videoConfig, audio_bitrate: event.target.value })}
                  >
                    <option value="128k">128k</option>
                    <option value="192k">192k</option>
                    <option value="256k">256k</option>
                    <option value="320k">320k (khuyên dùng)</option>
                  </select>
                </Field>
                <Field label="Chất lượng" hint="CRF 20 khuyên dùng; số thấp hơn = file lớn hơn">
                  <input
                    className={fieldClass}
                    type="number"
                    min="18"
                    max="28"
                    value={videoConfig.quality}
                    onChange={(event) => setVideoConfig({ ...videoConfig, quality: Number(event.target.value) })}
                  />
                </Field>
                <Field label="Thời lượng ảnh" hint="giây">
                  <input
                    className={fieldClass}
                    type="number"
                    min="1"
                    max="600"
                    value={videoConfig.image_duration_seconds}
                    onChange={(event) =>
                      setVideoConfig({ ...videoConfig, image_duration_seconds: Number(event.target.value) })
                    }
                  />
                </Field>
                <Field label="Animation">
                  <select
                    className={selectClass}
                    value={videoConfig.image_animation}
                    onChange={(event) => setVideoConfig({ ...videoConfig, image_animation: event.target.value })}
                  >
                    <option value="none">Không</option>
                    <option value="static">Static</option>
                    <option value="zoom-in">Zoom in</option>
                    <option value="zoom-out">Zoom out</option>
                    <option value="pan-left">Pan left</option>
                    <option value="pan-right">Pan right</option>
                  </select>
                </Field>
                <Field label="Concurrency">
                  <select
                    className={selectClass}
                    value={videoConfig.concurrency}
                    onChange={(event) => setVideoConfig({ ...videoConfig, concurrency: Number(event.target.value) })}
                  >
                    {[1, 2, 3, 4, 6, 8].map((value) => (
                      <option key={value} value={value}>
                        {value}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>

              {/* Gameplay không còn chọn trực tiếp: clip gameplay được tạo sẵn vào thư viện background. */}
              {videoConfig.background_type === "gameplay" && (
                <div className="flex flex-wrap items-center gap-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2.5 text-xs text-amber-900">
                  <span className="min-w-0 flex-1">
                    Cấu hình này đang dùng nền gameplay dựng trực tiếp (kiểu cũ). Video gameplay giờ được tạo sẵn trong
                    Thư viện background — chuyển sang ảnh/video thư viện để chọn file hoặc thư mục.
                  </span>
                  <Button type="button" size="sm" variant="outline" onClick={() => setVideoConfig({ ...videoConfig, background_type: "media" })}>
                    Dùng ảnh/video thư viện
                  </Button>
                </div>
              )}

              {videoConfig.background_type === "media" && (
                <BackgroundPicker
                  value={videoConfig}
                  onChange={(patch) => setVideoConfig({ ...videoConfig, ...patch })}
                />
              )}

              <div className="grid grid-cols-1 gap-4 rounded-md border border-border p-3 sm:grid-cols-2">
                <Field label="Âm thanh intro" hint="Phát trước nội dung patch">
                  <select
                    className={selectClass}
                    value={videoConfig.intro_voice}
                    onChange={(event) => setVideoConfig({ ...videoConfig, intro_voice: event.target.value })}
                  >
                    <option value="">Không dùng intro</option>
                    {introOutroVoices.map((voice) => (
                      <option key={voice.name} value={voice.name}>{voice.name}</option>
                    ))}
                  </select>
                </Field>
                <Field label="Âm thanh outro" hint="Phát sau nội dung patch">
                  <select
                    className={selectClass}
                    value={videoConfig.outro_voice}
                    onChange={(event) => setVideoConfig({ ...videoConfig, outro_voice: event.target.value })}
                  >
                    <option value="">Không dùng outro</option>
                    {introOutroVoices.map((voice) => (
                      <option key={voice.name} value={voice.name}>{voice.name}</option>
                    ))}
                  </select>
                </Field>
              </div>

              {music && (
                <MusicMixFields
                  music={music}
                  onMusicChange={setMusic}
                  placement={videoConfig}
                  onPlacementChange={(patch) => setVideoConfig({ ...videoConfig, ...patch })}
                />
              )}

              <div className="flex flex-wrap gap-4 rounded-md bg-muted/30 p-3">
                <CheckField
                  checked={videoConfig.crossfade_enabled}
                  onChange={(value) => setVideoConfig({ ...videoConfig, crossfade_enabled: value })}
                  label="Crossfade"
                />
                <CheckField
                  checked={videoConfig.ken_burns_enabled}
                  onChange={(value) => setVideoConfig({ ...videoConfig, ken_burns_enabled: value })}
                  label="Ken Burns"
                />
                <CheckField
                  checked={videoConfig.progress_bar_enabled}
                  onChange={(value) => setVideoConfig({ ...videoConfig, progress_bar_enabled: value })}
                  label="Progress bar"
                />
                <CheckField
                  checked={videoConfig.narrator_credit_enabled || false}
                  onChange={(value) => setVideoConfig({ ...videoConfig, narrator_credit_enabled: value })}
                  label="Hiện tên giọng đọc + TTS model"
                />
              </div>
              {videoConfig.narrator_credit_enabled && (
                <p className="rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-xs">
                  Tự động thêm text overlay dưới mỗi khung hình:{" "}
                  <span className="font-semibold">
                    Giọng đọc: {voiceOptions.find((option) => option.value === settings.voiceId)?.label || settings.voiceId || "—"}
                    {" · "}
                    {ttsModels.find((model) => model.id === settings.modelId)?.name || settings.modelId}
                  </span>
                </p>
              )}

              <section className="space-y-3 rounded-md border border-border p-3">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="flex items-center gap-2 text-xs font-semibold">
                      <AudioLines className="h-4 w-4 text-primary" /> Waveform theo giọng đọc
                    </div>
                    <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
                      Tạo dải sóng chuyển động trực tiếp từ audio narration.
                    </p>
                  </div>
                  <CheckField
                    checked={videoConfig.waveform_enabled}
                    onChange={(value) => setVideoConfig({ ...videoConfig, waveform_enabled: value })}
                    label="Bật"
                  />
                </div>

                <div className={videoConfig.waveform_enabled ? "space-y-4" : "pointer-events-none space-y-4 opacity-45"}>
                  <WaveformPreview settings={videoConfig} height={260} />

                  <div className="flex flex-wrap gap-2">
                    {WAVEFORM_TEMPLATES.map((template) => {
                      const selected = videoConfig.waveform_layout === template.layout && videoConfig.waveform_color === template.color && videoConfig.waveform_position === template.position && videoConfig.waveform_height === template.height;
                      return (
                        <button
                          key={template.id}
                          type="button"
                          aria-pressed={selected}
                          title={template.description}
                          onClick={() => setVideoConfig({ ...videoConfig, waveform_enabled: true, waveform_style: template.style, waveform_layout: template.layout, waveform_color: template.color, waveform_background_color: template.background, waveform_background_opacity: template.backgroundOpacity, waveform_position: template.position, waveform_height: template.height, waveform_opacity: template.opacity })}
                          className={cn("flex items-center gap-2 rounded-full border px-3 py-1.5 text-[11px] font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring", selected ? "border-primary bg-primary/10 text-primary" : "border-border text-muted-foreground hover:border-primary/50 hover:text-foreground")}
                        >
                          <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: template.color, boxShadow: `0 0 6px ${template.color}` }} />
                          {template.name}
                        </button>
                      );
                    })}
                  </div>

                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
                    <Field label="Bố cục">
                      <select className={selectClass} value={videoConfig.waveform_layout} onChange={(event) => setVideoConfig({ ...videoConfig, waveform_layout: event.target.value as VideoConfig["waveform_layout"] })}>
                        <option value="horizontal">Ngang</option><option value="vertical">Dọc</option><option value="circular">Tròn</option>
                      </select>
                    </Field>
                    <Field label="Kiểu sóng">
                      <select className={selectClass} value={videoConfig.waveform_style} onChange={(event) => setVideoConfig({ ...videoConfig, waveform_style: event.target.value as VideoConfig["waveform_style"] })}>
                        <option value="line">Line</option><option value="cline">Center line</option><option value="p2p">Point to point</option><option value="point">Point</option>
                      </select>
                    </Field>
                    <Field label="Vị trí">
                      <select className={selectClass} value={videoConfig.waveform_position} onChange={(event) => setVideoConfig({ ...videoConfig, waveform_position: event.target.value as VideoConfig["waveform_position"] })}>
                        <option value="top">Trên</option><option value="center">Giữa</option><option value="bottom">Dưới</option>
                      </select>
                    </Field>
                    <Field label="Màu">
                      <input className="h-9 w-full cursor-pointer rounded-md border border-border bg-background p-1" type="color" value={videoConfig.waveform_color} onChange={(event) => setVideoConfig({ ...videoConfig, waveform_color: event.target.value })} />
                    </Field>
                    <Field label={`Chiều cao: ${videoConfig.waveform_height}px`}>
                      <input className="w-full accent-primary" type="range" min="40" max="400" step="10" value={videoConfig.waveform_height} onChange={(event) => setVideoConfig({ ...videoConfig, waveform_height: Number(event.target.value) })} />
                    </Field>
                  </div>
                  <div className="grid gap-3 sm:grid-cols-3">
                    <Field label={`Độ rõ waveform: ${Math.round(videoConfig.waveform_opacity * 100)}%`}>
                      <input className="w-full accent-primary" type="range" min="10" max="100" step="5" value={videoConfig.waveform_opacity * 100} onChange={(event) => setVideoConfig({ ...videoConfig, waveform_opacity: Number(event.target.value) / 100 })} />
                    </Field>
                    <Field label="Màu nền">
                      <input className="h-9 w-full cursor-pointer rounded-md border border-border bg-background p-1" type="color" value={videoConfig.waveform_background_color} onChange={(event) => setVideoConfig({ ...videoConfig, waveform_background_color: event.target.value })} />
                    </Field>
                    <Field label={`Độ đậm nền: ${Math.round(videoConfig.waveform_background_opacity * 100)}%`}>
                      <input className="w-full accent-primary" type="range" min="0" max="100" step="5" value={videoConfig.waveform_background_opacity * 100} onChange={(event) => setVideoConfig({ ...videoConfig, waveform_background_opacity: Number(event.target.value) / 100 })} />
                    </Field>
                  </div>
                </div>
              </section>

              <section className="space-y-3 rounded-md border border-border p-3">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <div className="flex items-center gap-2 text-xs font-semibold">
                      <Captions className="h-4 w-4 text-primary" /> Phụ đề tự động
                    </div>
                    <p className="mt-1 text-[11px] leading-4 text-muted-foreground">
                      Tạo trực tiếp từ văn bản gốc và thời lượng từng chunk TTS thật — không dùng nhận diện
                      giọng nói nên không lệch chữ. Đổi cỡ chữ/màu/vị trí/nền không cần tạo lại audio. Câu dài tự xuống hàng trong khung.
                    </p>
                  </div>
                  <CheckField
                    checked={videoConfig.subtitle_enabled}
                    onChange={(value) => setVideoConfig({ ...videoConfig, subtitle_enabled: value })}
                    label="Bật"
                  />
                </div>

                <div className={videoConfig.subtitle_enabled ? "grid gap-3 sm:grid-cols-3" : "pointer-events-none grid gap-3 opacity-45 sm:grid-cols-3"}>
                  <Field label="Vị trí">
                    <select
                      className={selectClass}
                      value={videoConfig.subtitle_position}
                      onChange={(event) => setVideoConfig({ ...videoConfig, subtitle_position: event.target.value as VideoConfig["subtitle_position"] })}
                    >
                      <option value="bottom">Dưới</option>
                      <option value="center">Giữa</option>
                      <option value="top">Trên</option>
                    </select>
                  </Field>
                  <Field label={`Cỡ chữ: ${videoConfig.subtitle_font_size}`}>
                    <input
                      className="w-full accent-primary"
                      type="range"
                      min="20"
                      max="96"
                      step="2"
                      value={videoConfig.subtitle_font_size}
                      onChange={(event) => setVideoConfig({ ...videoConfig, subtitle_font_size: Number(event.target.value) })}
                    />
                  </Field>
                  <Field label="Màu chữ">
                    <input
                      className="h-9 w-full cursor-pointer rounded-md border border-border bg-background p-1"
                      type="color"
                      value={videoConfig.subtitle_color}
                      onChange={(event) => setVideoConfig({ ...videoConfig, subtitle_color: event.target.value })}
                    />
                  </Field>
                  <Field label="Nền phụ đề">
                    <CheckField
                      checked={videoConfig.subtitle_background_enabled}
                      onChange={(value) => setVideoConfig({ ...videoConfig, subtitle_background_enabled: value })}
                      label="Hiện khung nền"
                    />
                  </Field>
                  <Field label="Màu nền">
                    <input
                      className="h-9 w-full cursor-pointer rounded-md border border-border bg-background p-1 disabled:opacity-45"
                      type="color"
                      disabled={!videoConfig.subtitle_background_enabled}
                      value={videoConfig.subtitle_background_color}
                      onChange={(event) => setVideoConfig({ ...videoConfig, subtitle_background_color: event.target.value })}
                    />
                  </Field>
                  <Field label={`Độ đậm nền: ${Math.round(videoConfig.subtitle_background_opacity * 100)}%`}>
                    <input
                      className="w-full accent-primary disabled:opacity-45"
                      type="range"
                      min="0"
                      max="1"
                      step="0.05"
                      disabled={!videoConfig.subtitle_background_enabled}
                      value={videoConfig.subtitle_background_opacity}
                      onChange={(event) => setVideoConfig({ ...videoConfig, subtitle_background_opacity: Number(event.target.value) })}
                    />
                  </Field>
                </div>
              </section>

            </div>
          ) : (
            <div className="py-8 text-center text-xs text-muted-foreground">Đang tải cấu hình video...</div>
          ))}

        {tab === "youtube" &&
          (ytSettings ? (
            <div className="space-y-4">
              {ytSettings.connected ? (
                <div className="flex items-center gap-2 rounded-md bg-emerald-50 px-3 py-2 text-xs text-emerald-700">
                  <CheckCircle2 className="h-3.5 w-3.5" /> Đã kết nối: {ytSettings.channel_name}
                </div>
              ) : (
                <div className="flex items-center gap-2 rounded-md bg-amber-50 px-3 py-2 text-xs text-amber-700">
                  <AlertTriangle className="h-3.5 w-3.5" /> Chưa kết nối YouTube.
                  <Link to="/socials/youtube" className="underline">
                    Kết nối →
                  </Link>
                </div>
              )}

              {(ytSettings.accounts?.length ?? 0) > 1 && (
                <label className="flex flex-col gap-1 text-xs font-medium text-foreground">
                  Kênh YouTube của sách
                  <select
                    className="h-9 rounded-md border border-input bg-background px-2 text-xs"
                    disabled={saving}
                    value={ytSettings.youtube_account_id ?? ""}
                    onChange={(event) => changeYoutubeAccount(event.target.value ? Number(event.target.value) : null)}
                  >
                    <option value="">Kênh mặc định</option>
                    {ytSettings.accounts!.map((account) => (
                      <option key={account.id} value={account.id}>
                        {account.label}
                        {account.is_default ? " (mặc định)" : ""}
                      </option>
                    ))}
                  </select>
                  <span className="font-normal text-muted-foreground">
                    Playlist thuộc về từng kênh: đổi kênh sẽ bỏ chọn playlist và tắt tự động upload cho tới khi
                    bạn chọn lại.
                  </span>
                </label>
              )}

              <YouTubeConfigFields
                config={ytSettings.config}
                onChange={(patch) => setYtSettings({ ...ytSettings, config: { ...ytSettings.config, ...patch } })}
                playlists={ytSettings.playlists}
                podcastAction={
                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      type="button"
                      size="sm"
                      variant="outline"
                      disabled={applyingPodcast || !ytSettings.connected}
                      onClick={applyPodcast}
                    >
                      {applyingPodcast ? "Đang áp dụng..." : "Lưu & áp dụng lên YouTube"}
                    </Button>
                    <span className="text-[11px] text-muted-foreground">
                      Đánh dấu playlist là podcast và tải ảnh bìa ngay, không cần chờ tập kế tiếp.
                    </span>
                  </div>
                }
              />

              <AiContentGenerator
                bookId={bookId}
                status={ytSettings.ai_content}
                onApplied={reloadYoutubeSettings}
                onMessage={onMessage}
              />

              <div className="rounded-md border border-border">
                <div className="flex items-center justify-between gap-2 border-b border-border bg-muted/20 px-3 py-2">
                  <span className="flex items-center gap-1.5 text-xs font-semibold">
                    <Eye className="h-3.5 w-3.5 text-primary" /> Xem trước metadata
                  </span>
                  {ytPreviewLoading && <span className="text-[10px] text-muted-foreground">Đang cập nhật...</span>}
                </div>
                {ytPreviewError ? (
                  <div className="px-3 py-2 text-xs text-red-700">{ytPreviewError}</div>
                ) : ytPreview ? (
                  <div className="space-y-3 px-3 py-3 text-xs">
                    <div className="space-y-1">
                      <div className="font-semibold leading-snug">{ytPreview.title || "—"}</div>
                      <div className="flex flex-wrap gap-1">
                        {ytPreview.tags.map((tag) => (
                          <span key={tag} className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                            {tag}
                          </span>
                        ))}
                        {!ytPreview.tags.length && (
                          <span className="text-[10px] text-muted-foreground">(không có tags)</span>
                        )}
                      </div>
                      <div className="text-[11px] text-muted-foreground">
                        {ytPreview.privacy_status === "public"
                          ? "Công khai"
                          : ytPreview.privacy_status === "unlisted"
                            ? "Không công khai"
                            : "Riêng tư"}{" "}
                        · {ytPreview.youtube.mode === "existing" ? "vào playlist" : "không vào playlist"}
                      </div>
                    </div>
                    <div className="whitespace-pre-wrap break-words rounded-md bg-muted/30 p-3 font-mono text-[11px] leading-relaxed text-muted-foreground">
                      {ytPreview.description || "—"}
                    </div>
                  </div>
                ) : (
                  <div className="px-3 py-2 text-[11px] text-muted-foreground">Chưa có preview.</div>
                )}
              </div>

            </div>
          ) : (
            <div className="py-8 text-center text-xs text-muted-foreground">Đang tải cấu hình YouTube...</div>
          ))}
        </div>
        <DialogFooter className="shrink-0 border-t border-border pt-4">
          <Button
            onClick={tab === "audio" ? saveAudio : tab === "normalization" ? saveNormalization : tab === "video" ? saveVideo : saveYoutube}
            disabled={saving || (tab === "video" && (!videoConfig || !music)) || (tab === "youtube" && !ytSettings)}
          >
            {saving
              ? "Đang lưu..."
              : tab === "audio"
                ? "Lưu cấu hình âm thanh"
                : tab === "normalization"
                  ? "Lưu cấu hình chuẩn hóa"
                  : tab === "video"
                    ? "Lưu cấu hình video"
                    : "Lưu cấu hình YouTube"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function DeletePatchDialog({
  bookId,
  patchIds,
  open,
  onOpenChange,
  onMessage,
  onDeleted,
}: {
  bookId: string;
  patchIds: number[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onMessage: (message: string) => void;
  onDeleted: () => Promise<void>;
}) {
  const [deleting, setDeleting] = useState(false);
  const [toDelete, setToDelete] = useState({ audio: false, video: false, youtube: false });

  const deleteSelected = async () => {
    if (!toDelete.audio && !toDelete.video && !toDelete.youtube) return;
    setDeleting(true);
    try {
      for (const patchId of patchIds) {
        if (toDelete.audio) await post(`/books/${bookId}/patches/${patchId}/audio/delete`);
        if (toDelete.video) await post(`/books/${bookId}/patches/${patchId}/video/delete`);
        if (toDelete.youtube) await post(`/books/${bookId}/patches/${patchId}/youtube/delete`);
      }
      onMessage(`Đã xóa ${patchIds.length} patch thành công.`);
      await onDeleted();
      onOpenChange(false);
    } catch (e) {
      onMessage(errorText(e));
    } finally {
      setDeleting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Xóa output của {patchIds.length} patch</DialogTitle>
          <DialogDescription>Chọn những gì muốn xóa sạch khỏi ổ đĩa và trạng thái pipeline.</DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          <CheckField checked={toDelete.audio} onChange={(audio) => setToDelete({ ...toDelete, audio })} label="Xóa Audio (.wav)" />
          <CheckField checked={toDelete.video} onChange={(video) => setToDelete({ ...toDelete, video })} label="Xóa Video (.mp4)" />
          <CheckField checked={toDelete.youtube} onChange={(youtube) => setToDelete({ ...toDelete, youtube })} label="Xóa trạng thái YouTube" />
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={deleting}>Huỷ</Button>
          <Button variant="destructive" onClick={deleteSelected} disabled={deleting || !Object.values(toDelete).some(Boolean)}>
            {deleting ? "Đang xóa..." : "Xóa ngay"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function TitleNormalizeDialog({
  bookId,
  open,
  onOpenChange,
  onMessage,
  onOpenChapter,
  onApplied,
}: {
  bookId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onMessage: (message: string) => void;
  onOpenChapter: (chapterIndex: number) => void;
  onApplied: () => Promise<void> | void;
}) {
  const [plan, setPlan] = useState<TitleNormalizePreview>();
  const [loading, setLoading] = useState(false);
  const [checked, setChecked] = useState<Set<number>>(new Set());
  const [applying, setApplying] = useState(false);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    api<TitleNormalizePreview>(`/books/${bookId}/chapters/title-normalize/preview`)
      .then((data) => {
        if (cancelled) return;
        setPlan(data);
        setChecked(new Set(data.items.map((item) => item.chapter_index)));
      })
      .catch((error) => !cancelled && onMessage(errorText(error)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [open, bookId, onMessage]);

  const toggle = (chapterIndex: number) => {
    setChecked((current) => {
      const next = new Set(current);
      if (next.has(chapterIndex)) next.delete(chapterIndex);
      else next.add(chapterIndex);
      return next;
    });
  };

  const apply = async () => {
    if (!checked.size) return;
    setApplying(true);
    try {
      const result = await postJson<{ updated: number; patches_recomputed: unknown[] }>(
        `/books/${bookId}/chapters/title-normalize`,
        { chapter_indices: Array.from(checked) }
      );
      onMessage(
        `Đã chuẩn hoá ${result.updated} tiêu đề chương.` +
          (result.patches_recomputed.length ? ` Đã tính lại chunk cho ${result.patches_recomputed.length} patch.` : "")
      );
      onOpenChange(false);
      await onApplied();
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setApplying(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <ListChecks className="h-4 w-4" /> Chuẩn hoá tiêu đề chương
          </DialogTitle>
          <DialogDescription>
            Ghi lại tiêu đề về dạng "Chương N: Tên chương". Bỏ chọn dòng nào thì dòng đó giữ nguyên.
          </DialogDescription>
        </DialogHeader>

        {loading || !plan ? (
          <div className="py-8 text-center text-xs text-muted-foreground">Đang tải danh sách...</div>
        ) : (
          <div className="space-y-4 text-xs">
            {plan.items.length === 0 ? (
              <div className="rounded-md bg-emerald-50 px-3 py-2 text-emerald-700">
                Không có tiêu đề nào cần chuẩn hoá tự động.
              </div>
            ) : (
              <div className="overflow-hidden rounded-md border border-border">
                <div className="max-h-64 overflow-auto">
                  {plan.items.map((item) => (
                    <label
                      key={item.chapter_index}
                      className="flex cursor-pointer items-start gap-2 border-b border-border px-3 py-2 last:border-0 hover:bg-muted/40"
                    >
                      <input
                        type="checkbox"
                        className={`${checkboxClass} mt-0.5`}
                        checked={checked.has(item.chapter_index)}
                        onChange={() => toggle(item.chapter_index)}
                      />
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-muted-foreground line-through decoration-muted-foreground/40">
                          {item.current}
                        </div>
                        <div className="truncate font-medium text-foreground">{item.suggested}</div>
                      </div>
                    </label>
                  ))}
                </div>
              </div>
            )}

            {plan.skipped_items.length > 0 && (
              <div>
                <div className="mb-1.5 flex items-center gap-1.5 font-medium text-amber-800">
                  <AlertTriangle className="h-3.5 w-3.5" /> Phải sửa tay: {plan.skipped_items.length} chương
                </div>
                <div className="max-h-40 overflow-auto rounded-md border border-border">
                  {plan.skipped_items.map((item) => (
                    <button
                      key={item.chapter_index}
                      onClick={() => {
                        onOpenChange(false);
                        onOpenChapter(item.chapter_index);
                      }}
                      className="flex w-full items-center justify-between gap-2 border-b border-border px-3 py-1.5 text-left last:border-0 hover:bg-muted/40"
                    >
                      <span className="truncate">{item.title || "(không có tiêu đề)"}</span>
                      <span className="shrink-0 rounded bg-amber-50 px-1.5 py-0.5 text-[10px] text-amber-800">
                        {item.reason === "unknown" ? "Không có số" : "Thiếu tên"}
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        <DialogFooter>
          <Button size="sm" onClick={apply} disabled={applying || !checked.size}>
            {applying ? "Đang áp dụng..." : `Áp dụng cho ${checked.size} chương`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
