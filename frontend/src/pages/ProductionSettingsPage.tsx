import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AudioLines, Captions, Save, Settings2, X } from "lucide-react";
import { api, VoiceItem, postJson } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  BackgroundItem,
  NormalizationSettings,
  OnlineVoice,
  ProductionGroup,
  ProductionSettings,
  TtsModel,
  VideoConfig,
  VoiceOption,
  YouTubeConfig,
  errorText,
} from "@/pages/book-detail/types";
import { CheckField, Field, TabBar, checkboxClass, fieldClass, selectClass } from "@/pages/book-detail/parts";
import { YouTubeConfigFields } from "@/pages/book-detail/YouTubeFields";

type Defaults = ProductionSettings["defaults"];

const GROUPS: { key: ProductionGroup; label: string; hint: string }[] = [
  { key: "audio", label: "Âm thanh", hint: "TTS model, voice, độ dài chunk và hiệu ứng." },
  { key: "normalization", label: "Chuẩn hóa TTS", hint: "Quy tắc làm sạch văn bản trước khi đọc." },
  { key: "video", label: "Video", hint: "Khung hình, codec, nền, waveform và phụ đề." },
  { key: "youtube", label: "YouTube", hint: "Tiêu đề, description, timeline, tags và playlist." },
];

const GAMES = [
  ["garden_cycle", "Garden Cycle · Pixel"],
  ["aquarium_ecosystem", "Aquarium Ecosystem · Pixel"],
  ["parcel_route", "Parcel Route · Pixel"],
  ["cloud_runner", "Cloud Runner · Pixel"],
  ["orbit_drift", "Orbit Drift · Neon"],
  ["marble_flow", "Marble Flow · Neon"],
  ["territory_bloom", "Territory Bloom · Neon"],
  ["signal_garden", "Signal Garden · Neon"],
] as const;

const NORMALIZATION_LABELS: { key: keyof NormalizationSettings; label: string }[] = [
  { key: "numbers", label: "Chuyển số, ngày giờ và đơn vị thành chữ" },
  { key: "junk", label: "Xóa token rác từ EPUB" },
  { key: "spellcheck", label: "Sửa dấu chấm bị chèn trong từ tiếng Việt" },
  { key: "dictionary", label: "Áp dụng từ điển tiếng Việt" },
  { key: "transliteration", label: "Phiên âm từ nước ngoài" },
];

/** Model TTS + voice khả dụng khi không có sách nào trong phạm vi. */
function useGlobalTtsOptions(modelId: string) {
  const [ttsModels, setTtsModels] = useState<TtsModel[]>([]);
  const [localVoices, setLocalVoices] = useState<VoiceItem[]>([]);
  const [onlineVoices, setOnlineVoices] = useState<OnlineVoice[]>([]);

  useEffect(() => {
    api<{ tts_models: TtsModel[] }>("/api/ui/tts-models")
      .then((res) => setTtsModels(res.tts_models || []))
      .catch(() => {});
    api<{ voices: VoiceItem[] }>("/api/ui/media")
      .then((res) => setLocalVoices(res.voices || []))
      .catch(() => {});
  }, []);

  const selectedModel = ttsModels.find((model) => model.id === modelId) || null;

  useEffect(() => {
    if (!selectedModel || selectedModel.supports_reference) {
      setOnlineVoices([]);
      return;
    }
    let cancelled = false;
    api<{ voices: OnlineVoice[] }>(`/text-studio/light-tts/voices?backend=${encodeURIComponent(modelId)}`)
      .then((res) => !cancelled && setOnlineVoices(res.voices || []))
      .catch(() => !cancelled && setOnlineVoices([]));
    return () => {
      cancelled = true;
    };
  }, [modelId, selectedModel]);

  const voiceOptions = useMemo<VoiceOption[]>(() => {
    if (selectedModel && !selectedModel.supports_reference) {
      return onlineVoices.map((voice) => ({ value: voice.id, label: voice.label || voice.id }));
    }
    return localVoices.map((voice) => ({ value: voice.name, label: voice.name }));
  }, [selectedModel, onlineVoices, localVoices]);

  return { ttsModels, voiceOptions };
}

export function ProductionSettingsPage() {
  const [tab, setTab] = useState<ProductionGroup>("audio");
  const [saved, setSaved] = useState<Defaults>();
  const [draft, setDraft] = useState<Defaults>();
  const [updatedAt, setUpdatedAt] = useState<string | null>(null);
  const [message, setMessage] = useState("");
  const [saving, setSaving] = useState<ProductionGroup>();
  const [backgrounds, setBackgrounds] = useState<BackgroundItem[]>([]);
  const [introOutroVoices, setIntroOutroVoices] = useState<VoiceItem[]>([]);
  const [playlists, setPlaylists] = useState<{ id: string; title: string }[]>([]);

  const { ttsModels, voiceOptions } = useGlobalTtsOptions(draft?.audio.model_id || "");

  useEffect(() => {
    api<ProductionSettings>("/production-settings")
      .then((value) => {
        setSaved(value.defaults);
        setDraft(value.defaults);
        setUpdatedAt(value.updated_at);
      })
      .catch((error) => setMessage(errorText(error)));
    api<{ backgrounds: BackgroundItem[] }>("/video/backgrounds")
      .then((res) => setBackgrounds(res.backgrounds || []))
      .catch(() => {});
    api<{ voices: VoiceItem[] }>("/api/ui/media")
      .then((res) => setIntroOutroVoices(res.voices || []))
      .catch(() => {});
    api<{ items: { id: string; title: string }[] }>("/youtube/api/playlists")
      .then((res) => setPlaylists(res.items || []))
      .catch(() => setPlaylists([]));
  }, []);

  const patchGroup = useCallback(
    <G extends ProductionGroup>(group: G, patch: Partial<Defaults[G]>) =>
      setDraft((current) => (current ? { ...current, [group]: { ...current[group], ...patch } } : current)),
    []
  );

  const save = async (group: ProductionGroup) => {
    if (!draft) return;
    setSaving(group);
    setMessage("");
    try {
      const result = await postJson<ProductionSettings>("/production-settings", {
        groups: { [group]: draft[group] },
      });
      setSaved(result.defaults);
      setDraft((current) => (current ? { ...current, [group]: result.defaults[group] } : result.defaults));
      setUpdatedAt(result.updated_at);
      setMessage(
        `Đã lưu mặc định ${GROUPS.find((item) => item.key === group)?.label.toLowerCase()}. Ebook chưa tùy chỉnh riêng sẽ dùng ngay cấu hình mới.`
      );
    } catch (error) {
      setMessage(errorText(error));
    } finally {
      setSaving(undefined);
    }
  };

  if (!draft || !saved) {
    return (
      <div className="py-16 text-center text-sm text-muted-foreground">
        {message || "Đang tải cấu hình mặc định..."}
      </div>
    );
  }

  const dirty = JSON.stringify(draft[tab]) !== JSON.stringify(saved[tab]);
  const video = draft.video;
  const setVideo = (patch: Partial<VideoConfig>) => patchGroup("video", patch);

  return (
    <div className="space-y-5">
      <header className="space-y-2">
        <div className="flex items-center gap-2 text-primary">
          <Settings2 className="h-5 w-5" />
          <span className="font-mono text-xs uppercase tracking-wider">Production defaults</span>
        </div>
        <h1 className="text-2xl font-bold tracking-tight">Cấu hình mặc định</h1>
        <p className="max-w-3xl text-sm text-muted-foreground">
          Cấu hình sản xuất áp dụng cho mọi ebook mới. Mỗi ebook có thể mở{" "}
          <span className="font-medium text-foreground">Cấu hình sản xuất</span> trong trang chi tiết để tùy chỉnh riêng
          — khi lưu ở đó, nhóm cấu hình tương ứng tách khỏi mặc định và chỉ áp dụng cho ebook đó.
        </p>
        {updatedAt && (
          <p className="font-mono text-[11px] text-muted-foreground">
            Cập nhật lần cuối: {new Date(updatedAt).toLocaleString("vi-VN")}
          </p>
        )}
      </header>

      {message && (
        <div
          role="status"
          className="flex items-start justify-between gap-3 rounded-md border border-border bg-muted/30 px-4 py-3 text-xs"
        >
          <span>{message}</span>
          <Button variant="ghost" size="icon" className="-mr-2 -mt-1 h-6 w-6" onClick={() => setMessage("")}>
            <X className="h-3 w-3" />
          </Button>
        </div>
      )}

      <TabBar<ProductionGroup>
        value={tab}
        onChange={setTab}
        tabs={GROUPS.map((group) => ({ value: group.key, label: group.label }))}
      />

      <Card>
        <CardContent className="space-y-4 p-4">
          <p className="text-xs text-muted-foreground">{GROUPS.find((group) => group.key === tab)?.hint}</p>

          {tab === "audio" && (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <Field label="TTS model">
                <select
                  className={selectClass}
                  value={draft.audio.model_id}
                  onChange={(event) => patchGroup("audio", { model_id: event.target.value })}
                >
                  {ttsModels.length ? (
                    ttsModels.map((model) => (
                      <option key={model.id} value={model.id}>
                        {model.name}
                      </option>
                    ))
                  ) : (
                    <option value={draft.audio.model_id}>{draft.audio.model_id}</option>
                  )}
                </select>
              </Field>
              <Field label="Voice" hint="Bỏ trống = voice mặc định của model">
                <select
                  className={selectClass}
                  value={draft.audio.voice_id}
                  onChange={(event) => patchGroup("audio", { voice_id: event.target.value })}
                >
                  <option value="">—</option>
                  {voiceOptions.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.label}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Max chars" hint="0 = mặc định">
                <input
                  className={fieldClass}
                  type="number"
                  min="0"
                  value={draft.audio.max_chars}
                  onChange={(event) => patchGroup("audio", { max_chars: Number(event.target.value) || 0 })}
                />
              </Field>
              <div className="flex items-end">
                <CheckField
                  checked={draft.audio.with_effects}
                  onChange={(value) => patchGroup("audio", { with_effects: value })}
                  label="Thêm hiệu ứng âm thanh"
                />
              </div>
            </div>
          )}

          {tab === "normalization" && (
            <div className="space-y-3 rounded-md border border-border p-4">
              {NORMALIZATION_LABELS.map(({ key, label }) => (
                <CheckField
                  key={key}
                  checked={draft.normalization[key]}
                  onChange={(value) => patchGroup("normalization", { [key]: value } as Partial<NormalizationSettings>)}
                  label={label}
                />
              ))}
            </div>
          )}

          {tab === "video" && (
            <div className="space-y-4">
              <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
                <Field label="Độ phân giải">
                  <select
                    className={selectClass}
                    value={video.resolution}
                    onChange={(event) => setVideo({ resolution: event.target.value })}
                  >
                    <option value="1920x1080">1920×1080 (16:9)</option>
                    <option value="1280x720">1280×720 (16:9)</option>
                    <option value="854x480">854×480 (16:9)</option>
                    <option value="1080x1920">1080×1920 (9:16 — Shorts/Reels)</option>
                    <option value="1080x1080">1080×1080 (1:1 — vuông)</option>
                  </select>
                </Field>
                <Field label="Khung hình nền" hint="Auto: tự chọn">
                  <select
                    className={selectClass}
                    value={video.fit_mode || "auto"}
                    onChange={(event) => setVideo({ fit_mode: event.target.value as VideoConfig["fit_mode"] })}
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
                    value={video.fps}
                    onChange={(event) => setVideo({ fps: Number(event.target.value) })}
                  >
                    <option value="24">24</option>
                    <option value="30">30</option>
                    <option value="60">60</option>
                  </select>
                </Field>
                <Field label="Codec">
                  <select
                    className={selectClass}
                    value={video.codec}
                    onChange={(event) => setVideo({ codec: event.target.value })}
                  >
                    <option value="libx264">libx264 (CPU)</option>
                    <option value="h264_nvenc">h264_nvenc (GPU)</option>
                  </select>
                </Field>
                <Field label="Audio bitrate">
                  <select
                    className={selectClass}
                    value={video.audio_bitrate}
                    onChange={(event) => setVideo({ audio_bitrate: event.target.value })}
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
                    value={video.quality}
                    onChange={(event) => setVideo({ quality: Number(event.target.value) })}
                  />
                </Field>
                <Field label="Thời lượng ảnh" hint="giây">
                  <input
                    className={fieldClass}
                    type="number"
                    min="1"
                    max="600"
                    value={video.image_duration_seconds}
                    onChange={(event) => setVideo({ image_duration_seconds: Number(event.target.value) })}
                  />
                </Field>
                <Field label="Animation">
                  <select
                    className={selectClass}
                    value={video.image_animation}
                    onChange={(event) => setVideo({ image_animation: event.target.value })}
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
                    value={video.concurrency}
                    onChange={(event) => setVideo({ concurrency: Number(event.target.value) })}
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
                    value={video.background_type}
                    onChange={(event) =>
                      setVideo({ background_type: event.target.value as VideoConfig["background_type"] })
                    }
                  >
                    <option value="media">Ảnh/video</option>
                    <option value="gameplay">Catalog gameplay nhẹ nhàng</option>
                    <option value="battle_royale">Neon Battle Royale (Legacy)</option>
                  </select>
                </Field>
                {video.background_type === "gameplay" && (
                  <div className="mt-3 grid gap-3 sm:grid-cols-2">
                    <Field label="Chế độ chọn game">
                      <select
                        className={selectClass}
                        value={video.gameplay.selection_mode}
                        onChange={(event) =>
                          setVideo({
                            gameplay: {
                              ...video.gameplay,
                              selection_mode: event.target.value as "single" | "rotation",
                            },
                          })
                        }
                      >
                        <option value="single">Một game</option>
                        <option value="rotation">Xoay nhiều game</option>
                      </select>
                    </Field>
                    {video.gameplay.selection_mode === "single" ? (
                      <Field label="Game nền">
                        <select
                          className={selectClass}
                          value={video.gameplay.game_id}
                          onChange={(event) =>
                            setVideo({
                              gameplay: {
                                ...video.gameplay,
                                game_id: event.target.value as typeof video.gameplay.game_id,
                              },
                            })
                          }
                        >
                          {GAMES.map(([id, label]) => (
                            <option key={id} value={id}>
                              {label}
                            </option>
                          ))}
                        </select>
                      </Field>
                    ) : (
                      <div className="space-y-2">
                        <div className="text-xs font-medium">Game trong vòng xoay</div>
                        {GAMES.map(([id, label]) => {
                          const checked = video.gameplay.game_ids.includes(id);
                          return (
                            <label key={id} className="flex items-center gap-2 text-xs">
                              <input
                                type="checkbox"
                                className={checkboxClass}
                                checked={checked}
                                onChange={() =>
                                  setVideo({
                                    gameplay: {
                                      ...video.gameplay,
                                      game_ids: checked
                                        ? video.gameplay.game_ids.filter((value) => value !== id)
                                        : [...video.gameplay.game_ids, id],
                                    },
                                  })
                                }
                              />
                              {label}
                            </label>
                          );
                        })}
                      </div>
                    )}
                  </div>
                )}
              </div>

              {video.background_type === "media" && (
                <div className="space-y-3 rounded-md border border-border p-3">
                  <div className="flex flex-wrap items-end gap-3">
                    <Field label="Thứ tự background media">
                      <select
                        className={selectClass}
                        value={video.background_mode}
                        onChange={(event) =>
                          setVideo({ background_mode: event.target.value as VideoConfig["background_mode"] })
                        }
                      >
                        <option value="sequential">Theo thứ tự</option>
                        <option value="random">Ngẫu nhiên</option>
                      </select>
                    </Field>
                    <span className="pb-2 text-[11px] text-muted-foreground">
                      Đã chọn {video.backgrounds.length} file ảnh/video
                    </span>
                  </div>
                  {backgrounds.length ? (
                    <div className="grid max-h-52 grid-cols-1 gap-2 overflow-auto sm:grid-cols-2">
                      {backgrounds.map((item) => {
                        const checked = video.backgrounds.includes(item.path);
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
                                setVideo({
                                  backgrounds: checked
                                    ? video.backgrounds.filter((path) => path !== item.path)
                                    : [...video.backgrounds, item.path],
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
                </div>
              )}

              <div className="grid grid-cols-1 gap-4 rounded-md border border-border p-3 sm:grid-cols-2">
                <Field label="Âm thanh intro" hint="Phát trước nội dung patch">
                  <select
                    className={selectClass}
                    value={video.intro_voice}
                    onChange={(event) => setVideo({ intro_voice: event.target.value })}
                  >
                    <option value="">Không dùng intro</option>
                    {introOutroVoices.map((voice) => (
                      <option key={voice.name} value={voice.name}>
                        {voice.name}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Âm thanh outro" hint="Phát sau nội dung patch">
                  <select
                    className={selectClass}
                    value={video.outro_voice}
                    onChange={(event) => setVideo({ outro_voice: event.target.value })}
                  >
                    <option value="">Không dùng outro</option>
                    {introOutroVoices.map((voice) => (
                      <option key={voice.name} value={voice.name}>
                        {voice.name}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>

              <div className="flex flex-wrap gap-4 rounded-md bg-muted/30 p-3">
                <CheckField
                  checked={video.crossfade_enabled}
                  onChange={(value) => setVideo({ crossfade_enabled: value })}
                  label="Crossfade"
                />
                <CheckField
                  checked={video.ken_burns_enabled}
                  onChange={(value) => setVideo({ ken_burns_enabled: value })}
                  label="Ken Burns"
                />
                <CheckField
                  checked={video.progress_bar_enabled}
                  onChange={(value) => setVideo({ progress_bar_enabled: value })}
                  label="Progress bar"
                />
              </div>

              <section className="space-y-3 rounded-md border border-border p-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-center gap-2 text-xs font-semibold">
                    <AudioLines className="h-4 w-4 text-primary" /> Waveform theo giọng đọc
                  </div>
                  <CheckField
                    checked={video.waveform_enabled}
                    onChange={(value) => setVideo({ waveform_enabled: value })}
                    label="Bật"
                  />
                </div>
                <div
                  className={
                    video.waveform_enabled
                      ? "grid grid-cols-2 gap-3 sm:grid-cols-4"
                      : "pointer-events-none grid grid-cols-2 gap-3 opacity-45 sm:grid-cols-4"
                  }
                >
                  <Field label="Bố cục">
                    <select
                      className={selectClass}
                      value={video.waveform_layout}
                      onChange={(event) =>
                        setVideo({ waveform_layout: event.target.value as VideoConfig["waveform_layout"] })
                      }
                    >
                      <option value="horizontal">Ngang</option>
                      <option value="vertical">Dọc</option>
                      <option value="circular">Tròn</option>
                    </select>
                  </Field>
                  <Field label="Kiểu sóng">
                    <select
                      className={selectClass}
                      value={video.waveform_style}
                      onChange={(event) =>
                        setVideo({ waveform_style: event.target.value as VideoConfig["waveform_style"] })
                      }
                    >
                      <option value="line">Line</option>
                      <option value="cline">Center line</option>
                      <option value="p2p">Point to point</option>
                      <option value="point">Point</option>
                    </select>
                  </Field>
                  <Field label="Vị trí">
                    <select
                      className={selectClass}
                      value={video.waveform_position}
                      onChange={(event) =>
                        setVideo({ waveform_position: event.target.value as VideoConfig["waveform_position"] })
                      }
                    >
                      <option value="top">Trên</option>
                      <option value="center">Giữa</option>
                      <option value="bottom">Dưới</option>
                    </select>
                  </Field>
                  <Field label="Màu">
                    <input
                      className="h-9 w-full cursor-pointer rounded-md border border-border bg-background p-1"
                      type="color"
                      value={video.waveform_color}
                      onChange={(event) => setVideo({ waveform_color: event.target.value })}
                    />
                  </Field>
                  <Field label={`Chiều cao: ${video.waveform_height}px`}>
                    <input
                      className="w-full accent-primary"
                      type="range"
                      min="40"
                      max="400"
                      step="10"
                      value={video.waveform_height}
                      onChange={(event) => setVideo({ waveform_height: Number(event.target.value) })}
                    />
                  </Field>
                  <Field label={`Độ rõ: ${Math.round(video.waveform_opacity * 100)}%`}>
                    <input
                      className="w-full accent-primary"
                      type="range"
                      min="10"
                      max="100"
                      step="5"
                      value={video.waveform_opacity * 100}
                      onChange={(event) => setVideo({ waveform_opacity: Number(event.target.value) / 100 })}
                    />
                  </Field>
                  <Field label="Màu nền">
                    <input
                      className="h-9 w-full cursor-pointer rounded-md border border-border bg-background p-1"
                      type="color"
                      value={video.waveform_background_color}
                      onChange={(event) => setVideo({ waveform_background_color: event.target.value })}
                    />
                  </Field>
                  <Field label={`Độ đậm nền: ${Math.round(video.waveform_background_opacity * 100)}%`}>
                    <input
                      className="w-full accent-primary"
                      type="range"
                      min="0"
                      max="100"
                      step="5"
                      value={video.waveform_background_opacity * 100}
                      onChange={(event) => setVideo({ waveform_background_opacity: Number(event.target.value) / 100 })}
                    />
                  </Field>
                </div>
              </section>

              <section className="space-y-3 rounded-md border border-border p-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-center gap-2 text-xs font-semibold">
                    <Captions className="h-4 w-4 text-primary" /> Phụ đề tự động
                  </div>
                  <CheckField
                    checked={video.subtitle_enabled}
                    onChange={(value) => setVideo({ subtitle_enabled: value })}
                    label="Bật"
                  />
                </div>
                <div
                  className={
                    video.subtitle_enabled
                      ? "grid gap-3 sm:grid-cols-3"
                      : "pointer-events-none grid gap-3 opacity-45 sm:grid-cols-3"
                  }
                >
                  <Field label="Vị trí">
                    <select
                      className={selectClass}
                      value={video.subtitle_position}
                      onChange={(event) =>
                        setVideo({ subtitle_position: event.target.value as VideoConfig["subtitle_position"] })
                      }
                    >
                      <option value="bottom">Dưới</option>
                      <option value="center">Giữa</option>
                      <option value="top">Trên</option>
                    </select>
                  </Field>
                  <Field label={`Cỡ chữ: ${video.subtitle_font_size}`}>
                    <input
                      className="w-full accent-primary"
                      type="range"
                      min="20"
                      max="96"
                      step="2"
                      value={video.subtitle_font_size}
                      onChange={(event) => setVideo({ subtitle_font_size: Number(event.target.value) })}
                    />
                  </Field>
                  <Field label="Màu chữ">
                    <input
                      className="h-9 w-full cursor-pointer rounded-md border border-border bg-background p-1"
                      type="color"
                      value={video.subtitle_color}
                      onChange={(event) => setVideo({ subtitle_color: event.target.value })}
                    />
                  </Field>
                </div>
              </section>
            </div>
          )}

          {tab === "youtube" && (
            <div className="space-y-4">
              {!playlists.length && (
                <div className="rounded-md bg-muted/40 px-3 py-2 text-[11px] text-muted-foreground">
                  Chưa lấy được danh sách playlist.{" "}
                  <Link to="/youtube" className="underline">
                    Kết nối YouTube
                  </Link>{" "}
                  để chọn playlist mặc định.
                </div>
              )}
              <YouTubeConfigFields
                config={draft.youtube}
                onChange={(patch) => patchGroup("youtube", patch as Partial<YouTubeConfig>)}
                playlists={playlists}
              />
            </div>
          )}

          <div className="flex items-center justify-end gap-2 border-t border-border pt-4">
            {dirty && <span className="mr-auto text-[11px] text-amber-700">Có thay đổi chưa lưu.</span>}
            <Button
              variant="outline"
              disabled={!dirty || saving === tab}
              onClick={() => setDraft({ ...draft, [tab]: saved[tab] })}
            >
              Hoàn tác
            </Button>
            <Button onClick={() => save(tab)} disabled={saving === tab}>
              <Save className="h-4 w-4" />
              {saving === tab ? "Đang lưu..." : `Lưu mặc định ${GROUPS.find((g) => g.key === tab)?.label.toLowerCase()}`}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
