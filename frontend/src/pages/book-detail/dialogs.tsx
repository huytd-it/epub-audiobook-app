import React, { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, CheckCircle2, Download, Eye, ListChecks, Play } from "lucide-react";
import { api, Chapter, Patch, postJson } from "@/api";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Textarea } from "@/components/ui/textarea";
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
  ttsModels,
  voiceOptions,
  voiceClipPath,
  onMessage,
}: {
  bookId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  tab: ConfigTab;
  onTabChange: (tab: ConfigTab) => void;
  settings: AudioSettings;
  onSettingsChange: (patch: Partial<AudioSettings>) => void;
  ttsModels: TtsModel[];
  voiceOptions: VoiceOption[];
  voiceClipPath?: string | null;
  onMessage: (message: string) => void;
}) {
  const [videoConfig, setVideoConfig] = useState<VideoConfig>();
  const [ytSettings, setYtSettings] = useState<YouTubeSettings>();
  const [ytPreview, setYtPreview] = useState<YouTubeMetadataPreview>();
  const [ytPreviewLoading, setYtPreviewLoading] = useState(false);
  const [ytPreviewError, setYtPreviewError] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) return;
    if (tab === "video" && !videoConfig) {
      api<VideoConfig>(`/books/${bookId}/video-config`).then(setVideoConfig).catch((error) => onMessage(errorText(error)));
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

  const saveVideo = async () => {
    if (!videoConfig) return;
    setSaving(true);
    try {
      await postJson(`/books/${bookId}/video-config`, videoConfig);
      onMessage("Đã lưu cấu hình video.");
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
          <DialogDescription>Thiết lập âm thanh, video và YouTube riêng cho sách này.</DialogDescription>
        </DialogHeader>

        <TabBar<ConfigTab>
          value={tab}
          onChange={onTabChange}
          tabs={[
            { value: "audio", label: "Âm thanh" },
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
                    <option value="1920x1080">1920×1080</option>
                    <option value="1280x720">1280×720</option>
                    <option value="854x480">854×480</option>
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

              <CheckField
                checked={ytSettings.config.auto_upload}
                onChange={(value) =>
                  setYtSettings({ ...ytSettings, config: { ...ytSettings.config, auto_upload: value } })
                }
                label="Tự động upload sau khi tạo video"
              />

              <div className="grid grid-cols-1 gap-4">
                <Field label="Title template">
                  <input
                    className={fieldClass}
                    value={ytSettings.config.title_template}
                    onChange={(event) =>
                      setYtSettings({
                        ...ytSettings,
                        config: { ...ytSettings.config, title_template: event.target.value },
                      })
                    }
                  />
                </Field>
                <Field label="Mô tả">
                  <Textarea
                    className="min-h-20 text-xs"
                    value={ytSettings.config.description}
                    onChange={(event) =>
                      setYtSettings({
                        ...ytSettings,
                        config: { ...ytSettings.config, description: event.target.value },
                      })
                    }
                  />
                </Field>
                <Field label="Genre tags">
                  <input
                    className={fieldClass}
                    value={ytSettings.config.genre_tags}
                    onChange={(event) =>
                      setYtSettings({
                        ...ytSettings,
                        config: { ...ytSettings.config, genre_tags: event.target.value },
                      })
                    }
                  />
                </Field>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <Field label="Privacy">
                    <select
                      className={selectClass}
                      value={ytSettings.config.privacy_status}
                      onChange={(event) =>
                        setYtSettings({
                          ...ytSettings,
                          config: { ...ytSettings.config, privacy_status: event.target.value },
                        })
                      }
                    >
                      <option value="private">Private</option>
                      <option value="unlisted">Unlisted</option>
                      <option value="public">Public</option>
                    </select>
                  </Field>
                  <Field label="Playlist">
                    <select
                      className={selectClass}
                      value={ytSettings.config.playlist.playlist_id}
                      onChange={(event) =>
                        setYtSettings({
                          ...ytSettings,
                          config: {
                            ...ytSettings.config,
                            playlist: {
                              ...ytSettings.config.playlist,
                              mode: event.target.value ? "existing" : "none",
                              playlist_id: event.target.value,
                            },
                          },
                        })
                      }
                    >
                      <option value="">Không chọn</option>
                      {ytSettings.playlists.map((playlist) => (
                        <option key={playlist.id} value={playlist.id}>
                          {playlist.title}
                        </option>
                      ))}
                    </select>
                  </Field>
                </div>
              </div>

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
