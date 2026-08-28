import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AudioLines, Captions, Image, Save, Settings2, X } from "lucide-react";
import { api, VoiceItem, postJson } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { VoicePreviewButton } from "@/components/common/VoicePreviewButton";
import { WaveformPreview } from "@/components/common/WaveformPreview";
import {
  BackgroundItem,
  BrandingConfig,
  NormalizationSettings,
  OnlineVoice,
  ProductionGroup,
  ProductionSettings,
  TtsModel,
  VideoConfig,
  VoiceOption,
  YouTubeConfig,
  errorText,
  presetVoiceOptions,
} from "@/pages/book-detail/types";
import { CheckField, Field, TabBar, TtsOptionsFields, checkboxClass, fieldClass, selectClass } from "@/pages/book-detail/parts";
import { YouTubeConfigFields } from "@/pages/book-detail/YouTubeFields";
import { MediaBrowser, MediaEntry } from "@/components/media-browser/MediaBrowser";

type Defaults = ProductionSettings["defaults"];

const GROUPS: { key: ProductionGroup; label: string; hint: string }[] = [
  { key: "audio", label: "Âm thanh", hint: "TTS model, voice, độ dài chunk, hiệu ứng và khoảng lặng khi ghép." },
  { key: "normalization", label: "Chuẩn hóa TTS", hint: "Quy tắc làm sạch văn bản trước khi đọc." },
  { key: "video", label: "Video", hint: "Khung hình, codec, nền, waveform và phụ đề." },
  { key: "youtube", label: "YouTube", hint: "Tiêu đề, description, timeline, tags và playlist." },
  { key: "branding", label: "Thương hiệu", hint: "Watermark văn bản, logo và mục tiêu áp dụng trên thumbnail, podcast và video." },
];

const GAMES = [
  ["snake_arena", "Rắn Săn Mồi · Retro"],
  ["brick_stack", "Xếp Gạch · Retro"],
  ["tank_duel", "Xe Tăng 90 · Retro"],
  ["brick_breaker", "Đập Gạch · Retro"],
  ["star_defender", "Bắn Ruồi · Retro"],
  ["pixel_dash", "Đua Xe · Retro"],
  ["pacman_maze", "Pac-Man · Retro"],
  ["chicken_shooter", "Phi Thuyền Bắn Gà · Retro"],
  ["spaceship_voyager", "Phi Thuyền · Retro"],
  ["flappy_bird", "Flappy Bird · Retro"],
  ["aurora_veil", "Aurora Veil · Procedural"],
  ["plasma_tide", "Plasma Tide · Procedural"],
  ["ripple_pond", "Ripple Pond · Procedural"],
  ["lumen_bloom", "Lumen Bloom · Procedural"],
  ["silk_current", "Silk Current · Procedural"],
  ["starfall_warp", "Starfall Warp · Procedural"],
] as const;

const NORMALIZATION_LABELS: { key: keyof NormalizationSettings; label: string }[] = [
  { key: "numbers", label: "Chuyển số, ngày giờ và đơn vị thành chữ" },
  { key: "junk", label: "Xóa token rác từ EPUB" },
  { key: "spellcheck", label: "Sửa dấu chấm bị chèn trong từ tiếng Việt" },
  { key: "dictionary", label: "Áp dụng từ điển tiếng Việt" },
  { key: "transliteration", label: "Phiên âm từ nước ngoài" },
  { key: "abbreviations", label: "Mở rộng viết tắt (TP.HCM → Thành phố Hồ Chí Minh)" },
  { key: "breaks", label: "Thêm cue ngắt nghỉ trong câu" },
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
  // Model mang sẵn danh sách giọng (VieNeu, ZeroTTS) thì dùng luôn, khỏi gọi mạng.
  const builtInVoices = useMemo(() => selectedModel?.voices || [], [selectedModel]);

  useEffect(() => {
    if (!selectedModel || selectedModel.supports_reference || builtInVoices.length) {
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
  }, [modelId, selectedModel, builtInVoices]);

  const voiceOptions = useMemo<VoiceOption[]>(() => {
    if (builtInVoices.length) {
      return builtInVoices.map((voice) => ({ value: voice.id, label: voice.label || voice.id }));
    }
    if (selectedModel && !selectedModel.supports_reference) {
      return onlineVoices.map((voice) => ({ value: voice.id, label: voice.label || voice.id }));
    }
    // Model clone: audio mẫu trong thư viện + giọng preset của VieNeu/ZeroTTS.
    return [
      ...localVoices.map((voice) => ({ value: voice.name, label: voice.name })),
      ...presetVoiceOptions(ttsModels),
    ];
  }, [selectedModel, builtInVoices, onlineVoices, localVoices, ttsModels]);

  return { ttsModels, voiceOptions };
}

const BRANDING_POSITIONS: { value: BrandingConfig["watermark"]["position"]; label: string }[] = [
  { value: "top-left", label: "Trên trái" },
  { value: "top-right", label: "Trên phải" },
  { value: "bottom-left", label: "Dưới trái" },
  { value: "bottom-right", label: "Dưới phải" },
  { value: "center", label: "Giữa" },
];

function BrandingTab({
  branding,
  onChange,
  logoBrowserOpen,
  onLogoBrowserOpenChange,
}: {
  branding: BrandingConfig;
  onChange: (patch: Partial<BrandingConfig>) => void;
  logoBrowserOpen: boolean;
  onLogoBrowserOpenChange: (open: boolean) => void;
}) {
  const updateWatermark = useCallback(
    (patch: Partial<BrandingConfig["watermark"]>) =>
      onChange({ watermark: { ...branding.watermark, ...patch } }),
    [branding, onChange]
  );
  const updateLogo = useCallback(
    (patch: Partial<BrandingConfig["logo"]>) =>
      onChange({ logo: { ...branding.logo, ...patch } }),
    [branding, onChange]
  );
  const updateTargets = useCallback(
    (patch: Partial<BrandingConfig["targets"]>) =>
      onChange({ targets: { ...branding.targets, ...patch } }),
    [branding, onChange]
  );

  const logoPathDisplay = branding.logo.path
    ? branding.logo.path.split("/").pop() || branding.logo.path
    : "Chưa chọn logo";

  return (
    <div className="space-y-4">
      {/* Watermark */}
      <Card>
        <CardContent className="space-y-4 p-4">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm font-semibold">Watermark văn bản</div>
              <div className="text-xs text-muted-foreground">
                Chèn chữ lên ảnh thumbnail, ảnh bìa podcast hoặc video.
              </div>
            </div>
            <CheckField
              label="Bật"
              checked={branding.watermark.enabled}
              onChange={(enabled) => updateWatermark({ enabled })}
            />
          </div>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <Field label="Nội dung">
              <input
                className={fieldClass}
                value={branding.watermark.text}
                onChange={(event) => updateWatermark({ text: event.target.value })}
                placeholder="Tên kênh, URL..."
                maxLength={200}
              />
            </Field>
            <Field label="Vị trí">
              <select
                className={selectClass}
                value={branding.watermark.position}
                onChange={(event) =>
                  updateWatermark({ position: event.target.value as BrandingConfig["watermark"]["position"] })
                }
              >
                {BRANDING_POSITIONS.map((pos) => (
                  <option key={pos.value} value={pos.value}>
                    {pos.label}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={`Cỡ chữ: ${branding.watermark.font_size}`}>
              <input
                type="range"
                min={12}
                max={120}
                className="w-full accent-primary"
                value={branding.watermark.font_size}
                onChange={(event) => updateWatermark({ font_size: Number(event.target.value) })}
              />
            </Field>
            <Field label="Màu chữ">
              <input
                type="color"
                className={fieldClass}
                value={branding.watermark.text_color}
                onChange={(event) => updateWatermark({ text_color: event.target.value })}
              />
            </Field>
            <Field label={`Độ rõ: ${branding.watermark.opacity}%`}>
              <input
                type="range"
                min={0}
                max={100}
                className="w-full accent-primary"
                value={branding.watermark.opacity}
                onChange={(event) => updateWatermark({ opacity: Number(event.target.value) })}
              />
            </Field>
            <Field label="Khoảng cách viền (px)">
              <input
                type="number"
                min={0}
                max={200}
                className={fieldClass}
                value={branding.watermark.margin}
                onChange={(event) => updateWatermark({ margin: Number(event.target.value) })}
              />
            </Field>
          </div>
          <div className="flex flex-wrap gap-4 rounded-md bg-muted/30 p-3">
            <CheckField
              label="Đổ bóng"
              checked={branding.watermark.shadow_enabled}
              onChange={(shadow_enabled) => updateWatermark({ shadow_enabled })}
            />
            {branding.watermark.shadow_enabled && (
              <Field label="Màu bóng">
                <input
                  type="color"
                  className={fieldClass}
                  value={branding.watermark.shadow_color}
                  onChange={(event) => updateWatermark({ shadow_color: event.target.value })}
                />
              </Field>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Logo */}
      <Card>
        <CardContent className="space-y-4 p-4">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm font-semibold">Logo</div>
              <div className="text-xs text-muted-foreground">
                Chèn ảnh logo lên thumbnail, podcast hoặc video.
              </div>
            </div>
            <CheckField
              label="Bật"
              checked={branding.logo.enabled}
              onChange={(enabled) => updateLogo({ enabled })}
            />
          </div>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            <Field label="Logo path">
              <div className="flex items-center gap-2">
                <input
                  className={fieldClass}
                  value={branding.logo.path}
                  readOnly
                  placeholder="Chưa chọn logo"
                />
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => onLogoBrowserOpenChange(true)}
                >
                  Chọn
                </Button>
              </div>
              {branding.logo.path && (
                <div className="mt-1 text-[10px] text-muted-foreground truncate">
                  {logoPathDisplay}
                </div>
              )}
            </Field>
            <Field label="Vị trí">
              <select
                className={selectClass}
                value={branding.logo.position}
                onChange={(event) =>
                  updateLogo({ position: event.target.value as BrandingConfig["logo"]["position"] })
                }
              >
                {BRANDING_POSITIONS.map((pos) => (
                  <option key={pos.value} value={pos.value}>
                    {pos.label}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={`Kích thước: ${branding.logo.size}px`}>
              <input
                type="range"
                min={16}
                max={500}
                className="w-full accent-primary"
                value={branding.logo.size}
                onChange={(event) => updateLogo({ size: Number(event.target.value) })}
              />
            </Field>
            <Field label={`Độ rõ: ${branding.logo.opacity}%`}>
              <input
                type="range"
                min={0}
                max={100}
                className="w-full accent-primary"
                value={branding.logo.opacity}
                onChange={(event) => updateLogo({ opacity: Number(event.target.value) })}
              />
            </Field>
            <Field label="Khoảng cách viền (px)">
              <input
                type="number"
                min={0}
                max={200}
                className={fieldClass}
                value={branding.logo.margin}
                onChange={(event) => updateLogo({ margin: Number(event.target.value) })}
              />
            </Field>
          </div>
        </CardContent>
      </Card>

      {/* Targets */}
      <Card>
        <CardContent className="space-y-3 p-4">
          <div>
            <div className="text-sm font-semibold">Mục tiêu áp dụng</div>
            <div className="text-xs text-muted-foreground">
              Chọn loại nội dung sẽ nhận watermark và logo.
            </div>
          </div>
          <div className="flex flex-wrap gap-4">
            <CheckField
              label="Thumbnail"
              checked={branding.targets.thumbnail}
              onChange={(thumbnail) => updateTargets({ thumbnail })}
            />
            <CheckField
              label="Ảnh bìa Podcast"
              checked={branding.targets.podcast}
              onChange={(podcast) => updateTargets({ podcast })}
            />
            <CheckField
              label="Video"
              checked={branding.targets.video}
              onChange={(video) => updateTargets({ video })}
            />
          </div>
        </CardContent>
      </Card>

      {/* Logo Browser Dialog */}
      {logoBrowserOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
          <div className="mx-4 flex h-[80vh] w-full max-w-4xl flex-col rounded-lg border border-border bg-card shadow-xl">
            <div className="flex items-center justify-between border-b border-border px-4 py-3">
              <span className="text-sm font-semibold">Chọn logo</span>
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7"
                onClick={() => onLogoBrowserOpenChange(false)}
              >
                <X className="h-4 w-4" />
              </Button>
            </div>
            <div className="flex-1 overflow-hidden">
              <MediaBrowser
                category="logos"
                selectedPath={branding.logo.path || null}
                onSelect={(entry: MediaEntry) => {
                  updateLogo({ path: entry.path });
                  onLogoBrowserOpenChange(false);
                }}
                height="100%"
              />
            </div>
          </div>
        </div>
      )}
    </div>
  );
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
  const [logoBrowserOpen, setLogoBrowserOpen] = useState(false);

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
                <div className="flex items-center gap-1">
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
                  <VoicePreviewButton
                    modelId={draft.audio.model_id}
                    voiceId={draft.audio.voice_id}
                    ttsOptions={draft.audio.tts_options}
                  />
                </div>
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
              <Field label="Khoảng lặng giữa chunk (ms)" hint="Nhịp thở giữa hai đoạn liền nhau">
                <input
                  className={fieldClass}
                  type="number"
                  min="0"
                  max="30000"
                  step="50"
                  value={draft.audio.chunk_pause_ms}
                  onChange={(event) => patchGroup("audio", { chunk_pause_ms: Number(event.target.value) || 0 })}
                />
              </Field>
              <Field
                label="Khoảng lặng giữa chương (ms)"
                hint="Chèn trước mỗi chương trong cùng một patch; cũng là chỗ nhạc nền được chèn vào"
              >
                <input
                  className={fieldClass}
                  type="number"
                  min="0"
                  max="30000"
                  step="100"
                  value={draft.audio.chapter_pause_ms}
                  onChange={(event) => patchGroup("audio", { chapter_pause_ms: Number(event.target.value) || 0 })}
                />
              </Field>
              <TtsOptionsFields model={ttsModels.find((model) => model.id === draft.audio.model_id)}
                value={draft.audio.tts_options || {}}
                onChange={(tts_options) => patchGroup("audio", { tts_options })} />
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
                    {video.gameplay.selection_mode === "rotation" && (
                      <p className="text-xs text-muted-foreground sm:col-span-2">
                        Các game được chọn sẽ luân phiên theo thứ tự trong cùng một video; thứ tự clip được cố định để retry cho ra cùng kết quả.
                      </p>
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

              <div className="space-y-3 rounded-md border border-border p-3">
                <CheckField
                  checked={video.music_gap_only}
                  onChange={(value) => setVideo({ music_gap_only: value })}
                  label="Nhạc nền chỉ chèn vào khoảng lặng"
                />
                <p className="text-xs text-muted-foreground">
                  Bản nhạc của sách chỉ phát ở những quãng im lặng đủ dài (nghỉ giữa chương,
                  giữa chunk) thay vì lặp nền dưới giọng đọc. Tắt để quay lại kiểu mix cũ.
                </p>
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <Field label="Khoảng lặng tối thiểu (ms)" hint="Ngắn hơn mức này thì bỏ qua">
                    <input
                      className={fieldClass}
                      type="number"
                      min="200"
                      max="60000"
                      step="100"
                      disabled={!video.music_gap_only}
                      value={video.music_gap_min_ms}
                      onChange={(event) => setVideo({ music_gap_min_ms: Number(event.target.value) || 0 })}
                    />
                  </Field>
                  <Field label="Fade nhạc (ms)" hint="Vào/ra ở hai đầu mỗi đoạn nhạc">
                    <input
                      className={fieldClass}
                      type="number"
                      min="0"
                      max="5000"
                      step="50"
                      disabled={!video.music_gap_only}
                      value={video.music_gap_fade_ms}
                      onChange={(event) => setVideo({ music_gap_fade_ms: Number(event.target.value) || 0 })}
                    />
                  </Field>
                </div>
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
                <div className={video.waveform_enabled ? undefined : "pointer-events-none opacity-45"}>
                  <WaveformPreview settings={video} height={240} />
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

          {tab === "branding" && (
            <BrandingTab
              branding={draft.branding}
              onChange={(patch) => patchGroup("branding", patch as Partial<BrandingConfig>)}
              logoBrowserOpen={logoBrowserOpen}
              onLogoBrowserOpenChange={setLogoBrowserOpen}
            />
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
