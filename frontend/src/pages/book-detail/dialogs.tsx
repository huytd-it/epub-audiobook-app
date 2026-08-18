import React, { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, AudioLines, Captions, CheckCircle2, Download, Eye, ListChecks, Play } from "lucide-react";
import { api, Chapter, Patch, postJson, VoiceItem } from "@/api";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Textarea } from "@/components/ui/textarea";
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
  BackgroundItem,
  ConfigTab,
  MusicSettings,
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
import { CheckField, Field, TabBar, checkboxClass, fieldClass, selectClass } from "./parts";
import { ReplaceRulesPanel } from "./ReplaceRulesPanel";
import { YouTubeConfigFields } from "./YouTubeFields";

const WAVEFORM_TEMPLATES = [
  { id: "bold", name: "Dải nổi", description: "Line sáng trên nền tối", style: "line", layout: "horizontal", color: "#ffffff", background: "#050816", backgroundOpacity: 0.68, position: "bottom", height: 150, opacity: 1 },
  { id: "studio", name: "Studio", description: "Sóng đối xứng giữa khung", style: "cline", layout: "horizontal", color: "#22d3ee", background: "#082f49", backgroundOpacity: 0.62, position: "center", height: 200, opacity: 1 },
  { id: "vertical", name: "Cột nhịp", description: "Dải sóng dọc bên trái", style: "p2p", layout: "vertical", color: "#facc15", background: "#1c1917", backgroundOpacity: 0.72, position: "center", height: 300, opacity: 1 },
  { id: "orbit", name: "Quỹ đạo", description: "Waveform tròn ở trung tâm", style: "line", layout: "circular", color: "#fb7185", background: "#2e1065", backgroundOpacity: 0.58, position: "center", height: 280, opacity: 1 },
] as const;

const WAVEFORM_BARS = [18, 38, 24, 58, 34, 72, 42, 86, 48, 64, 30, 76, 44, 92, 54, 70, 36, 80, 46, 62, 28, 52, 34, 20];

export function PatchPreviewDialog({
  bookId,
  patch,
  chapters,
  open,
  onOpenChange,
  onMessage,
}: {
  bookId: string;
  patch?: Patch;
  chapters: Chapter[];
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onMessage: (message: string) => void;
}) {
  const [chapterTexts, setChapterTexts] = useState<Record<number, string>>({});
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open || !patch) return;
    let cancelled = false;
    setChapterTexts({});
    setLoading(true);
    const patchChapters = chapters.filter(
      (chapter) => chapter.chapter_index >= patch.chapter_start && chapter.chapter_index <= patch.chapter_end
    );
    Promise.all(
      patchChapters.map(async (chapter) => [
        chapter.chapter_index,
        await api<string>(`/books/${bookId}/chapters/${chapter.chapter_index}/text`),
      ] as const)
    )
      .then((items) => !cancelled && setChapterTexts(Object.fromEntries(items)))
      .catch((error) => !cancelled && onMessage(errorText(error)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [open, patch, chapters, bookId, onMessage]);

  const percent = patch?.chunk_count ? (patch.next_chunk_index * 100) / patch.chunk_count : 0;
  const patchChapters = patch
    ? chapters.filter(
        (chapter) => chapter.chapter_index >= patch.chapter_start && chapter.chapter_index <= patch.chapter_end
      )
    : [];
  const numberedChapters = patchChapters.filter((chapter) => chapter.chapter_no != null);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-3xl overflow-auto">
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

        <Progress value={percent} className="h-1.5" />

        {patchChapters.length > 0 && (
          <nav aria-label="Đi tới chương" className="flex gap-1 overflow-x-auto border-b border-border pb-2">
            {patchChapters.map((chapter) => (
              <a
                key={chapter.id}
                href={`#patch-chapter-${chapter.chapter_index}`}
                className="shrink-0 rounded-md border border-border bg-background px-2.5 py-1.5 text-[11px] font-medium hover:border-primary hover:text-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                {chapter.chapter_no != null ? `Chương ${chapter.chapter_no}` : `Mục ${chapter.chapter_index + 1}`}
              </a>
            ))}
          </nav>
        )}

        <div className="min-h-[280px] space-y-5 overflow-auto rounded-md border border-border p-4 text-xs leading-5">
          {loading ? (
            <div className="py-20 text-center text-muted-foreground">Đang tải nội dung từng chương...</div>
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

        {patch?.status === "done" && (
          <div className="space-y-3">
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
  const [backgrounds, setBackgrounds] = useState<BackgroundItem[]>([]);
  const [introOutroVoices, setIntroOutroVoices] = useState<VoiceItem[]>([]);
  const [music, setMusic] = useState<MusicSettings & { tracks: { id: number; name: string; duration_sec: number | null }[] }>();
  const [ytSettings, setYtSettings] = useState<YouTubeSettings>();
  const [ytPreview, setYtPreview] = useState<YouTubeMetadataPreview>();
  const [ytPreviewLoading, setYtPreviewLoading] = useState(false);
  const [ytPreviewError, setYtPreviewError] = useState("");
  const [normalizationPreview, setNormalizationPreview] = useState("");
  const [previewChapter, setPreviewChapter] = useState(0);
  const [normalizationPreviewLoading, setNormalizationPreviewLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) return;
    if (tab === "video" && !videoConfig) {
      Promise.all([
        api<VideoConfig>(`/books/${bookId}/video-config`),
        api<{ backgrounds: BackgroundItem[] }>("/video/backgrounds"),
        api<MusicSettings & { tracks: { id: number; name: string; duration_sec: number | null }[] }>(
          `/books/${bookId}/music`
        ),
        api<{ voices: VoiceItem[] }>("/api/ui/media"),
      ])
        .then(([config, media, musicSettings, voiceMedia]) => {
          setVideoConfig(config);
          setBackgrounds(media.backgrounds || []);
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
      await postJson(`/books/${bookId}/audio-settings`, { model_id: settings.modelId, voice_id: settings.voiceId, max_chars: settings.maxChars ? Number(settings.maxChars) : 0, with_effects: settings.withEffects });
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

  const voiceClipName = voiceClipPath ? voiceClipPath.split(/[/\\]/).pop() : "";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-auto">
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
              <Field label="Voice">
                <select
                  className={selectClass}
                  value={settings.voiceId}
                  onChange={(event) => onSettingsChange({ voiceId: event.target.value })}
                >
                  {voiceOptions.length ? (
                    voiceOptions.map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.label}
                      </option>
                    ))
                  ) : (
                    <option value="">—</option>
                  )}
                </select>
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
            </div>
            <div className="rounded-md bg-muted/30 p-3 text-xs text-muted-foreground">
              Voice clip:{" "}
              {voiceClipName ? <span className="font-medium text-foreground">{voiceClipName}</span> : "Chưa thiết lập"}
            </div>
            <DialogFooter>
              <Button onClick={saveAudio} disabled={saving}>
                {saving ? "Đang lưu..." : "Lưu cấu hình âm thanh"}
              </Button>
            </DialogFooter>
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

            <DialogFooter>
              <Button onClick={saveNormalization} disabled={saving}>
                {saving ? "Đang lưu..." : "Lưu cấu hình chuẩn hóa"}
              </Button>
            </DialogFooter>
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
                    <option value="1920x1080">1920×1080 (16:9)</option>
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
                    <option value="30">30</option>
                    <option value="60">60</option>
                  </select>
                </Field>
                <Field label="Codec">
                  <select
                    className={selectClass}
                    value={videoConfig.codec}
                    onChange={(event) => setVideoConfig({ ...videoConfig, codec: event.target.value })}
                  >
                    <option value="libx264">libx264 (CPU)</option>
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
                    <option value="320k">320k</option>
                  </select>
                </Field>
                <Field label="Chất lượng" hint="CRF 18–28">
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

              <div className="rounded-md border border-primary/30 bg-primary/5 p-3">
                <Field label="Loại nền video">
                  <select
                    className={selectClass}
                    value={videoConfig.background_type}
                    onChange={(event) => setVideoConfig({ ...videoConfig, background_type: event.target.value as VideoConfig["background_type"] })}
                  >
                    <option value="media">Ảnh/video</option>
                    <option value="gameplay">Catalog gameplay nhẹ nhàng</option>
                    <option value="battle_royale">Neon Battle Royale (Legacy)</option>
                  </select>
                </Field>
                {videoConfig.background_type === "battle_royale" && (
                  <p className="mt-2 text-xs text-muted-foreground">
                    Chế độ tương thích cũ. Waveform bị tắt; phụ đề, tiến độ và thumbnail vẫn giữ cấu hình hiện tại.
                  </p>
                )}
                {videoConfig.background_type === "gameplay" && (
                  <div className="mt-3 grid gap-3 sm:grid-cols-2">
                    <Field label="Chế độ chọn game">
                      <select className={selectClass} value={videoConfig.gameplay.selection_mode}
                        onChange={(event) => setVideoConfig({ ...videoConfig, gameplay: { ...videoConfig.gameplay, selection_mode: event.target.value as "single" | "rotation" } })}>
                        <option value="single">Một game</option>
                        <option value="rotation">Xoay nhiều game</option>
                      </select>
                    </Field>
                    {videoConfig.gameplay.selection_mode === "single" ? (
                      <Field label="Game nền">
                        <select className={selectClass} value={videoConfig.gameplay.game_id}
                          onChange={(event) => setVideoConfig({ ...videoConfig, gameplay: { ...videoConfig.gameplay, game_id: event.target.value as typeof videoConfig.gameplay.game_id } })}>
                          <option value="garden_cycle">Garden Cycle · Pixel</option>
                          <option value="aquarium_ecosystem">Aquarium Ecosystem · Pixel</option>
                          <option value="parcel_route">Parcel Route · Pixel</option>
                          <option value="cloud_runner">Cloud Runner · Pixel</option>
                          <option value="orbit_drift">Orbit Drift · Neon</option>
                          <option value="marble_flow">Marble Flow · Neon</option>
                          <option value="territory_bloom">Territory Bloom · Neon</option>
                          <option value="signal_garden">Signal Garden · Neon</option>
                        </select>
                      </Field>
                    ) : (
                      <div className="space-y-2">
                        <div className="text-xs font-medium">Game trong vòng xoay</div>
                        {([ ["garden_cycle", "Garden Cycle · Pixel"], ["aquarium_ecosystem", "Aquarium Ecosystem · Pixel"], ["parcel_route", "Parcel Route · Pixel"], ["cloud_runner", "Cloud Runner · Pixel"], ["orbit_drift", "Orbit Drift · Neon"], ["marble_flow", "Marble Flow · Neon"], ["territory_bloom", "Territory Bloom · Neon"], ["signal_garden", "Signal Garden · Neon"] ] as const).map(([id, label]) => {
                          const checked = videoConfig.gameplay.game_ids.includes(id);
                          return <label key={id} className="flex items-center gap-2 text-xs">
                            <input type="checkbox" className={checkboxClass} checked={checked}
                              onChange={() => setVideoConfig({ ...videoConfig, gameplay: { ...videoConfig.gameplay,
                                game_ids: checked ? videoConfig.gameplay.game_ids.filter((value) => value !== id) : [...videoConfig.gameplay.game_ids, id] } })} />
                            {label}
                          </label>;
                        })}
                      </div>
                    )}
                    <p className="text-xs text-muted-foreground sm:col-span-2">
                      Game được chọn deterministic theo patch. Audiobook, phụ đề, tiến độ, thumbnail và pipeline YouTube không thay đổi.
                    </p>
                  </div>
                )}
              </div>

              {videoConfig.background_type === "media" && <div className="space-y-3 rounded-md border border-border p-3">
                <div className="flex flex-wrap items-end gap-3">
                  <Field label="Thứ tự background media">
                    <select
                      className={selectClass}
                      value={videoConfig.background_mode}
                      onChange={(event) => setVideoConfig({ ...videoConfig, background_mode: event.target.value as VideoConfig["background_mode"] })}
                    >
                      <option value="sequential">Theo thứ tự</option>
                      <option value="random">Ngẫu nhiên</option>
                    </select>
                  </Field>
                  <span className="pb-2 text-[11px] text-muted-foreground">
                    Đã chọn {videoConfig.backgrounds.length} file ảnh/video
                  </span>
                </div>
                {backgrounds.length ? (
                  <div className="grid max-h-52 grid-cols-1 gap-2 overflow-auto sm:grid-cols-2">
                    {backgrounds.map((item) => {
                      const checked = videoConfig.backgrounds.includes(item.path);
                      return (
                        <label
                          key={item.path}
                          className="flex cursor-pointer items-center gap-2 rounded-md border border-border px-3 py-2 text-xs hover:bg-muted/40"
                        >
                          <input
                            type="checkbox"
                            className={checkboxClass}
                            checked={checked}
                            onChange={() =>
                              setVideoConfig({
                                ...videoConfig,
                                backgrounds: checked
                                  ? videoConfig.backgrounds.filter((path) => path !== item.path)
                                  : [...videoConfig.backgrounds, item.path],
                              })
                            }
                          />
                          <span className="min-w-0 flex-1 truncate">{item.is_default ? "Mặc định" : item.name}</span>
                          <span className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
                            {item.is_video ? "Video" : "Ảnh"}
                          </span>
                        </label>
                      );
                    })}
                  </div>
                ) : (
                  <div className="text-xs text-muted-foreground">Thư viện chưa có background media.</div>
                )}
              </div>}

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
                <div className="grid grid-cols-1 gap-4 rounded-md border border-border p-3 sm:grid-cols-2">
                  <Field label="Mix nhạc nền">
                    <select
                      className={selectClass}
                      value={music.music_id ?? ""}
                      onChange={(event) =>
                        setMusic({ ...music, music_id: event.target.value ? Number(event.target.value) : null })
                      }
                    >
                      <option value="">Không dùng nhạc</option>
                      {music.tracks.map((track) => (
                        <option key={track.id} value={track.id}>{track.name}</option>
                      ))}
                    </select>
                  </Field>
                  <Field label={`Âm lượng nhạc: ${music.music_volume}%`}>
                    <input
                      className="w-full accent-primary"
                      type="range"
                      min="0"
                      max="100"
                      value={music.music_volume}
                      disabled={music.music_id == null}
                      onChange={(event) => setMusic({ ...music, music_volume: Number(event.target.value) })}
                    />
                  </Field>
                </div>
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
              </div>

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
                  <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                    {WAVEFORM_TEMPLATES.map((template) => {
                      const selected = videoConfig.waveform_layout === template.layout && videoConfig.waveform_color === template.color && videoConfig.waveform_position === template.position && videoConfig.waveform_height === template.height;
                      return (
                        <button
                          key={template.id}
                          type="button"
                          aria-pressed={selected}
                          onClick={() => setVideoConfig({ ...videoConfig, waveform_enabled: true, waveform_style: template.style, waveform_layout: template.layout, waveform_color: template.color, waveform_background_color: template.background, waveform_background_opacity: template.backgroundOpacity, waveform_position: template.position, waveform_height: template.height, waveform_opacity: template.opacity })}
                          className={cn("group overflow-hidden rounded-md border bg-background text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring", selected ? "border-primary ring-1 ring-primary" : "border-border hover:border-primary/50")}
                        >
                          <div className="relative flex h-16 items-center justify-center overflow-hidden bg-slate-950 px-2">
                            <div className="absolute inset-2 rounded" style={{ backgroundColor: template.background, opacity: template.backgroundOpacity }} />
                            <div className={cn("relative z-10 flex items-center justify-center gap-[2px]", template.layout === "circular" && "h-11 w-11 rounded-full border-2", template.layout === "vertical" && "rotate-90")} style={template.layout === "circular" ? { borderColor: template.color, boxShadow: `0 0 12px ${template.color}` } : undefined}>
                              {WAVEFORM_BARS.map((bar, index) => (
                                template.layout !== "circular" && <span key={index} className="w-[2px] rounded-full" style={{ height: `${Math.min(46, Math.max(3, bar * (template.height / 180) * 0.42))}px`, backgroundColor: template.color, opacity: template.opacity, boxShadow: `0 0 5px ${template.color}` }} />
                              ))}
                            </div>
                          </div>
                          <div className="px-2 py-2">
                            <div className="text-[11px] font-semibold">{template.name}</div>
                            <div className="truncate text-[10px] text-muted-foreground">{template.description}</div>
                          </div>
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
                      giọng nói nên không lệch chữ. Đổi cỡ chữ/màu/vị trí không cần tạo lại audio.
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
                </div>
              </section>

              <DialogFooter>
                <Button onClick={saveVideo} disabled={saving}>
                  {saving ? "Đang lưu..." : "Lưu cấu hình video"}
                </Button>
              </DialogFooter>
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
                  <Link to="/youtube" className="underline">
                    Kết nối →
                  </Link>
                </div>
              )}

              <YouTubeConfigFields
                config={ytSettings.config}
                onChange={(patch) => setYtSettings({ ...ytSettings, config: { ...ytSettings.config, ...patch } })}
                playlists={ytSettings.playlists}
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

              <DialogFooter>
                <Button onClick={saveYoutube} disabled={saving}>
                  {saving ? "Đang lưu..." : "Lưu cấu hình YouTube"}
                </Button>
              </DialogFooter>
            </div>
          ) : (
            <div className="py-8 text-center text-xs text-muted-foreground">Đang tải cấu hình YouTube...</div>
          ))}
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
