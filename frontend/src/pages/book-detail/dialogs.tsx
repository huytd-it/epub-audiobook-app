import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, CheckCircle2, Download, Play } from "lucide-react";
import { api, Patch, postJson } from "@/api";
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
  TtsModel,
  VideoConfig,
  VoiceOption,
  YouTubeSettings,
  errorText,
} from "./types";
import { CheckField, Field, TabBar, fieldClass, selectClass } from "./parts";

export function PatchPreviewDialog({
  bookId,
  patch,
  open,
  onOpenChange,
  onMessage,
}: {
  bookId: string;
  patch?: Patch;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onMessage: (message: string) => void;
}) {
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open || !patch) return;
    let cancelled = false;
    setText("");
    setLoading(true);
    api<string>(`/books/${bookId}/patches/${patch.id}/text`)
      .then((value) => !cancelled && setText(value))
      .catch((error) => !cancelled && onMessage(errorText(error)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [open, patch, bookId, onMessage]);

  const percent = patch?.chunk_count ? (patch.next_chunk_index * 100) / patch.chunk_count : 0;

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
            Chương {patch ? `${patch.chapter_start + 1}–${patch.chapter_end + 1}` : "—"} ·{" "}
            {patch?.next_chunk_index}/{patch?.chunk_count} chunk
          </DialogDescription>
        </DialogHeader>

        <Progress value={percent} className="h-1.5" />

        <Textarea
          className="min-h-[280px] font-mono text-xs"
          value={text}
          readOnly
          placeholder={loading ? "Đang tải nội dung patch..." : "Không có nội dung."}
        />

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
