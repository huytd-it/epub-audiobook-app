import React, { useCallback, useEffect, useRef, useState } from "react";
import { Upload, X } from "lucide-react";
import { api, post, postForm, postJson } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { DialogFooter } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { CheckField, Field, fieldClass, selectClass } from "./parts";
import {
  BackgroundItem,
  BrandingConfig,
  BrandingPosition,
  OverlayConfig,
  OverlayConfigResponse,
  OverlayLayer,
  PODCAST_COVER_SIZES,
  PodcastCover,
  ProductionMode,
  errorText,
} from "./types";
import { MediaBrowser, MediaEntry } from "@/components/media-browser/MediaBrowser";

/** Backend luôn trả về podcast_cover; giữ default cho cấu hình cũ chưa có khóa này. */
const DEFAULT_PODCAST_COVER: PodcastCover = { enabled: false, focus_x: 50, focus_y: 50, size: 1280 };

const emptyLayer = (): OverlayLayer => ({
  text: "{book_title} - {patch_name}",
  position: "bottom",
  alignment: "center",
  font_size: 100,
  font_path: "",
  text_transform: "none",
  line_spacing: 8,
  max_width: 90,
  stroke_width: 0,
  stroke_color: "#000000",
  text_color: "#FFFFFF",
  margin: 40,
  offset_x: 0,
  offset_y: 0,
  shadow: { enabled: true, color: "#000000", offset: 3 },
  box: { enabled: false, color: "#000000", opacity: 60, padding_x: 24, padding_y: 12, radius: 12 },
});

const boxTemplates = [
  {
    name: "Điện ảnh",
    description: "Nền đen đậm",
    swatch: "bg-neutral-950 text-white",
    text_color: "#FFFFFF",
    box: { enabled: true, color: "#000000", opacity: 78, padding_x: 40, padding_y: 22, radius: 8 },
    shadow: { enabled: true, color: "#000000", offset: 4 },
  },
  {
    name: "Vàng nổi bật",
    description: "Tương phản mạnh",
    swatch: "bg-amber-300 text-amber-950",
    text_color: "#111111",
    box: { enabled: true, color: "#FACC15", opacity: 96, padding_x: 38, padding_y: 20, radius: 10 },
    shadow: { enabled: false, color: "#000000", offset: 0 },
  },
  {
    name: "Đỏ tiêu điểm",
    description: "Ấm và giàu năng lượng",
    swatch: "bg-red-700 text-white",
    text_color: "#FFFFFF",
    box: { enabled: true, color: "#B91C1C", opacity: 94, padding_x: 40, padding_y: 20, radius: 6 },
    shadow: { enabled: true, color: "#450A0A", offset: 3 },
  },
  {
    name: "Sáng tối giản",
    description: "Sạch, dễ đọc",
    swatch: "bg-white text-neutral-950 ring-1 ring-inset ring-neutral-300",
    text_color: "#111111",
    box: { enabled: true, color: "#FFFFFF", opacity: 92, padding_x: 36, padding_y: 18, radius: 18 },
    shadow: { enabled: true, color: "#000000", offset: 2 },
  },
] as const;

const typeTemplates = [
  { name: "Tiêu đề lớn", font_size: 120, text_transform: "uppercase", line_spacing: 10, max_width: 86, stroke_width: 2 },
  { name: "Kể chuyện", font_size: 92, text_transform: "none", line_spacing: 18, max_width: 78, stroke_width: 0 },
  { name: "Gọn mạnh", font_size: 104, text_transform: "uppercase", line_spacing: 4, max_width: 70, stroke_width: 5 },
  { name: "Phụ đề", font_size: 68, text_transform: "none", line_spacing: 12, max_width: 90, stroke_width: 2 },
] as const;

const BRANDING_POSITIONS: { value: BrandingPosition; label: string }[] = [
  { value: "top-left", label: "Trên trái" },
  { value: "top-right", label: "Trên phải" },
  { value: "bottom-left", label: "Dưới trái" },
  { value: "bottom-right", label: "Dưới phải" },
  { value: "center", label: "Giữa" },
];

function BrandingEditor({
  branding,
  onChange,
  logoBrowserOpen,
  onLogoBrowserOpenChange,
}: {
  branding: BrandingConfig;
  onChange: (b: BrandingConfig) => void;
  logoBrowserOpen: boolean;
  onLogoBrowserOpenChange: (open: boolean) => void;
}) {
  const updateWatermark = (patch: Partial<BrandingConfig["watermark"]>) =>
    onChange({ ...branding, watermark: { ...branding.watermark, ...patch } });
  const updateLogo = (patch: Partial<BrandingConfig["logo"]>) =>
    onChange({ ...branding, logo: { ...branding.logo, ...patch } });
  const updateTargets = (patch: Partial<BrandingConfig["targets"]>) =>
    onChange({ ...branding, targets: { ...branding.targets, ...patch } });

  return (
    <div className="space-y-3">
      <div className="rounded-md border border-border p-3 space-y-3">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold">Watermark văn bản</span>
          <CheckField label="Bật" checked={branding.watermark.enabled} onChange={(enabled) => updateWatermark({ enabled })} />
        </div>
        {branding.watermark.enabled && (
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Field label="Nội dung">
              <input className={fieldClass} value={branding.watermark.text} onChange={(e) => updateWatermark({ text: e.target.value })} maxLength={200} />
            </Field>
            <Field label="Vị trí">
              <select className={selectClass} value={branding.watermark.position} onChange={(e) => updateWatermark({ position: e.target.value as BrandingPosition })}>
                {BRANDING_POSITIONS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
              </select>
            </Field>
            <Field label={`Cỡ chữ: ${branding.watermark.font_size}`}>
              <input type="range" min={12} max={120} className="w-full accent-primary" value={branding.watermark.font_size} onChange={(e) => updateWatermark({ font_size: Number(e.target.value) })} />
            </Field>
            <Field label={`Độ rõ: ${branding.watermark.opacity}%`}>
              <input type="range" min={0} max={100} className="w-full accent-primary" value={branding.watermark.opacity} onChange={(e) => updateWatermark({ opacity: Number(e.target.value) })} />
            </Field>
            <Field label="Màu chữ">
              <input type="color" className={fieldClass} value={branding.watermark.text_color} onChange={(e) => updateWatermark({ text_color: e.target.value })} />
            </Field>
            <Field label="Khoảng cách viền">
              <input type="number" min={0} max={200} className={fieldClass} value={branding.watermark.margin} onChange={(e) => updateWatermark({ margin: Number(e.target.value) })} />
            </Field>
          </div>
        )}
      </div>

      <div className="rounded-md border border-border p-3 space-y-3">
        <div className="flex items-center justify-between">
          <span className="text-xs font-semibold">Logo</span>
          <CheckField label="Bật" checked={branding.logo.enabled} onChange={(enabled) => updateLogo({ enabled })} />
        </div>
        {branding.logo.enabled && (
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            <Field label="Logo">
              <div className="flex items-center gap-1">
                <input className={fieldClass} value={branding.logo.path} readOnly placeholder="Chọn logo..." />
                <Button type="button" variant="outline" size="sm" onClick={() => onLogoBrowserOpenChange(true)}>Chọn</Button>
              </div>
            </Field>
            <Field label="Vị trí">
              <select className={selectClass} value={branding.logo.position} onChange={(e) => updateLogo({ position: e.target.value as BrandingPosition })}>
                {BRANDING_POSITIONS.map((p) => <option key={p.value} value={p.value}>{p.label}</option>)}
              </select>
            </Field>
            <Field label={`Kích thước: ${branding.logo.size}px`}>
              <input type="range" min={16} max={500} className="w-full accent-primary" value={branding.logo.size} onChange={(e) => updateLogo({ size: Number(e.target.value) })} />
            </Field>
            <Field label={`Độ rõ: ${branding.logo.opacity}%`}>
              <input type="range" min={0} max={100} className="w-full accent-primary" value={branding.logo.opacity} onChange={(e) => updateLogo({ opacity: Number(e.target.value) })} />
            </Field>
          </div>
        )}
      </div>

      <div className="rounded-md border border-border p-3">
        <div className="text-xs font-semibold mb-2">Mục tiêu áp dụng</div>
        <div className="flex flex-wrap gap-4">
          <CheckField label="Thumbnail" checked={branding.targets.thumbnail} onChange={(v) => updateTargets({ thumbnail: v })} />
          <CheckField label="Podcast" checked={branding.targets.podcast} onChange={(v) => updateTargets({ podcast: v })} />
          <CheckField label="Video" checked={branding.targets.video} onChange={(v) => updateTargets({ video: v })} />
        </div>
      </div>
    </div>
  );
}

export function OverlayEditor({
  bookId,
  patchIds,
  onMessage,
  onSaved,
}: {
  bookId: string;
  patchIds: number[];
  onMessage: (message: string) => void;
  onSaved: () => Promise<void>;
}) {
  const [response, setResponse] = useState<OverlayConfigResponse>();
  const [config, setConfig] = useState<OverlayConfig>();
  const [background, setBackground] = useState<BackgroundItem>();
  const [preview, setPreview] = useState("");
  const [coverPreview, setCoverPreview] = useState("");
  const [thumbnailRevision, setThumbnailRevision] = useState(0);
  const [thumbnailFile, setThumbnailFile] = useState<File>();
  const [thumbnailPreview, setThumbnailPreview] = useState("");
  const [thumbnailApplying, setThumbnailApplying] = useState(false);
  const [thumbnailProgress, setThumbnailProgress] = useState(0);
  const thumbnailInputRef = useRef<HTMLInputElement>(null);
  const [saving, setSaving] = useState(false);
  const layers = config?.overlays?.length ? config.overlays : config ? [config] : [];
  const podcast = config?.podcast_cover || DEFAULT_PODCAST_COVER;

  // Branding state
  const [brandingMode, setBrandingMode] = useState<ProductionMode>("inherit");
  const [branding, setBranding] = useState<BrandingConfig | null>(null);
  const [brandingDraft, setBrandingDraft] = useState<BrandingConfig | null>(null);
  const [brandingSaving, setBrandingSaving] = useState(false);
  const [logoBrowserOpen, setLogoBrowserOpen] = useState(false);

  // The branding config to pass to live previews: draft in custom mode, otherwise saved effective
  const previewBranding = brandingMode === "custom" && brandingDraft ? brandingDraft : branding;

  useEffect(() => {
    let cancelled = false;
    api<OverlayConfigResponse>(`/books/${bookId}/overlay-config`)
      .then((value) => {
        if (cancelled) return;
        setResponse(value);
        setConfig(value.config);
        setBackground(
          value.backgrounds.find((item) => item.path === value.background_path && !item.is_video)
            || value.backgrounds.find((item) => !item.is_video)
        );
      })
      .catch((error) => !cancelled && onMessage(errorText(error)));
    return () => {
      cancelled = true;
    };
  }, [bookId, onMessage]);

  // Load branding config for this book
  useEffect(() => {
    let cancelled = false;
    api<{ defaults: { branding: BrandingConfig }; modes?: Record<string, ProductionMode>; effective?: { branding: BrandingConfig } }>(
      `/production-settings?book_id=${bookId}`
    )
      .then((value) => {
        if (cancelled) return;
        const mode = value.modes?.branding || "inherit";
        setBrandingMode(mode);
        const effective = value.effective?.branding || value.defaults.branding;
        setBranding(effective);
        if (mode === "custom") {
          setBrandingDraft(effective);
        }
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [bookId]);

  useEffect(() => {
    if (!config || !background || background.is_video) return;
    const timer = window.setTimeout(async () => {
      const params = new URLSearchParams({
        live: "1",
        overlays_json: JSON.stringify(config.overlays.length ? config.overlays : [config]),
        background_path: background.path,
      });
      if (previewBranding) {
        params.set("branding_json", JSON.stringify(previewBranding));
      }
      try {
        const blob = await fetch(`/books/${bookId}/overlay-preview?${params}`).then((result) => {
          if (!result.ok) throw new Error(`Lỗi ${result.status}`);
          return result.blob();
        });
        const url = URL.createObjectURL(blob);
        setPreview((old) => {
          if (old) URL.revokeObjectURL(old);
          return url;
        });
      } catch (error) {
        onMessage(errorText(error));
      }
    }, 250);
    return () => window.clearTimeout(timer);
  }, [bookId, config, background, onMessage, previewBranding]);

  useEffect(() => () => {
    if (preview) URL.revokeObjectURL(preview);
  }, [preview]);

  // Ảnh bìa podcast là chính thumbnail cắt vuông, nên preview đi theo cùng
  // cấu hình overlay — chỉ khác khung cắt.
  useEffect(() => {
    if (!config || !background || background.is_video || !config.podcast_cover?.enabled) {
      setCoverPreview((old) => {
        if (old) URL.revokeObjectURL(old);
        return "";
      });
      return;
    }
    const cover = config.podcast_cover;
    const timer = window.setTimeout(async () => {
      const params = new URLSearchParams({
        live: "1",
        overlays_json: JSON.stringify(config.overlays.length ? config.overlays : [config]),
        background_path: background.path,
        podcast_cover_enabled: "on",
        podcast_focus_x: String(cover.focus_x),
        podcast_focus_y: String(cover.focus_y),
        podcast_cover_size: String(cover.size),
      });
      if (previewBranding) {
        params.set("branding_json", JSON.stringify(previewBranding));
      }
      try {
        const blob = await fetch(`/books/${bookId}/podcast-cover-preview?${params}`).then((result) => {
          if (!result.ok) throw new Error(`Lỗi ${result.status}`);
          return result.blob();
        });
        const url = URL.createObjectURL(blob);
        setCoverPreview((old) => {
          if (old) URL.revokeObjectURL(old);
          return url;
        });
      } catch (error) {
        onMessage(errorText(error));
      }
    }, 250);
    return () => window.clearTimeout(timer);
  }, [bookId, config, background, onMessage, previewBranding]);

  useEffect(() => () => {
    if (coverPreview) URL.revokeObjectURL(coverPreview);
  }, [coverPreview]);

  useEffect(() => () => {
    if (thumbnailPreview) URL.revokeObjectURL(thumbnailPreview);
  }, [thumbnailPreview]);

  const update = (index: number, patch: Partial<OverlayLayer>) =>
    setConfig((current) =>
      current
        ? { ...current, overlays: layers.map((layer, layerIndex) => layerIndex === index ? { ...layer, ...patch } : layer) }
        : current
    );

  const updatePodcast = (patch: Partial<PodcastCover>) =>
    setConfig((current) =>
      current ? { ...current, podcast_cover: { ...(current.podcast_cover || DEFAULT_PODCAST_COVER), ...patch } } : current
    );

  const selectThumbnail = (file?: File) => {
    if (!file) return;
    const nextPreview = URL.createObjectURL(file);
    setThumbnailFile(file);
    setThumbnailPreview((current) => {
      if (current) URL.revokeObjectURL(current);
      return nextPreview;
    });
    setThumbnailProgress(0);
  };

  const applyThumbnailToAll = async () => {
    if (!thumbnailFile || !patchIds.length || thumbnailApplying) return;
    setThumbnailApplying(true);
    setThumbnailProgress(0);
    let completed = 0;
    let firstError: unknown;

    try {
      for (const patchId of patchIds) {
        const formData = new FormData();
        formData.append("image", thumbnailFile);
        try {
          await postForm(`/books/${bookId}/patches/${patchId}/image`, formData);
          completed += 1;
          setThumbnailProgress(completed);
        } catch (error) {
          if (!firstError) firstError = error;
        }
      }

      if (completed) {
        setThumbnailRevision((current) => current + 1);
        await onSaved();
      }
      onMessage(
        completed === patchIds.length
          ? `Đã dùng ảnh mới làm thumbnail cho ${completed} patch.`
          : `Đã cập nhật ${completed}/${patchIds.length} patch. ${errorText(firstError)}`
      );
    } finally {
      setThumbnailApplying(false);
    }
  };

  const save = async (regenerate: boolean) => {
    if (!config) return;
    setSaving(true);
    try {
      // Branding đang sửa (draft) phải được lưu trước — nếu không thumbnail
      // tạo lại ngay sau đó sẽ render bằng branding cũ và mất chỉnh sửa.
      if (brandingMode === "custom" && brandingDraft) {
        const brandingResult = await postJson<{ effective: BrandingConfig; purged_patch_ids: number[] }>(
          `/books/${bookId}/branding-config`,
          { branding: brandingDraft },
        );
        setBranding(brandingResult.effective);
        setBrandingDraft(brandingResult.effective);
      }
      const form = new FormData();
      form.append("overlays_json", JSON.stringify(config.overlays.length ? config.overlays : [config]));
      if (background && !background.is_video) form.append("background_path", background.path);
      if (podcast.enabled) form.append("podcast_cover_enabled", "on");
      form.append("podcast_focus_x", String(podcast.focus_x));
      form.append("podcast_focus_y", String(podcast.focus_y));
      form.append("podcast_cover_size", String(podcast.size));
      await api(`/books/${bookId}/overlay-config`, { method: "POST", body: form });
      if (regenerate && patchIds.length) {
        await postJson(`/books/${bookId}/thumbnails/regenerate`, { patch_ids: patchIds });
        setThumbnailRevision((current) => current + 1);
      }
      if (regenerate && podcast.enabled) {
        await post(`/books/${bookId}/podcast-cover/regenerate`);
        setThumbnailRevision((current) => current + 1);
      }
      onMessage(regenerate
        ? podcast.enabled
          ? "Đã lưu cấu hình overlay, tạo lại thumbnail và ảnh bìa podcast."
          : "Đã lưu cấu hình overlay và đưa thumbnail vào hàng đợi tạo lại."
        : "Đã lưu đầy đủ cấu hình thumbnail overlay.");
      await onSaved();
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setSaving(false);
    }
  };

  const saveBranding = async () => {
    if (!brandingDraft) return;
    setBrandingSaving(true);
    try {
      const result = await postJson<{ effective: BrandingConfig; purged_patch_ids: number[] }>(
        `/books/${bookId}/branding-config`,
        { branding: brandingDraft },
      );
      setBranding(result.effective);
      setBrandingDraft(result.effective);
      setBrandingMode("custom");
      // Backend đã xoá cache nhưng chưa render lại file mới — gọi regenerate
      // để thumbnail/podcast cover thực sự mang branding vừa lưu.
      if (patchIds.length) {
        await postJson(`/books/${bookId}/thumbnails/regenerate`, { patch_ids: patchIds });
      }
      if (podcast.enabled) {
        await post(`/books/${bookId}/podcast-cover/regenerate`);
      }
      setThumbnailRevision((c) => c + 1);
      onMessage("Đã lưu branding và tạo lại thumbnail.");
      await onSaved();
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setBrandingSaving(false);
    }
  };

  const resetBrandingToInherit = async () => {
    try {
      await postJson(`/books/${bookId}/production-settings-mode`, { group: "branding", mode: "inherit" });
      const settings = await api<{ defaults: { branding: BrandingConfig }; effective?: { branding: BrandingConfig } }>(
        `/production-settings?book_id=${bookId}`
      );
      const effective = settings.effective?.branding || settings.defaults.branding;
      setBrandingMode("inherit");
      setBranding(effective);
      setBrandingDraft(null);
      if (patchIds.length) {
        await postJson(`/books/${bookId}/thumbnails/regenerate`, { patch_ids: patchIds });
        setThumbnailRevision((c) => c + 1);
      }
      if (podcast.enabled) {
        await post(`/books/${bookId}/podcast-cover/regenerate`);
        setThumbnailRevision((c) => c + 1);
      }
      onMessage("Đã chuyển về mặc định toàn cục.");
      await onSaved();
    } catch (error) {
      onMessage(errorText(error));
    }
  };

  if (!response || !config) {
    return <div className="py-8 text-center text-xs text-muted-foreground">Đang tải cấu hình thumbnail...</div>;
  }

  return (
    <div className="space-y-4">
      <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">
        Placeholder: {response.placeholders.map((item) => `{${item.key}}`).join(", ")}
      </div>
      <Field label="Background preview">
        <select
          aria-label="Background preview"
          className={selectClass}
          value={background?.path || ""}
          onChange={(event) => setBackground(response.backgrounds.find((item) => item.path === event.target.value))}
        >
          {response.backgrounds.map((item) => (
            <option key={item.path} value={item.path}>{item.name}{item.is_video ? " (video)" : ""}</option>
          ))}
        </select>
      </Field>
      {background?.is_video && (
        <div role="alert" className="text-xs text-amber-700">
          Video background không hỗ trợ preview overlay; overlay sẽ được dùng khi tạo thumbnail ảnh.
        </div>
      )}
      {preview && <img src={preview} alt="Overlay preview" className="max-h-64 w-full rounded-md object-contain" />}

      <section className="grid gap-4 border-y border-border py-4 lg:grid-cols-[minmax(0,320px)_1fr] lg:items-center">
        <figure className="overflow-hidden rounded-md border bg-muted/20">
          <div className="aspect-video bg-muted">
            {patchIds.length > 0 || thumbnailPreview ? (
              <img
                src={thumbnailPreview || `/books/${bookId}/patches/${patchIds[0]}/overlay-image?v=${thumbnailRevision}`}
                alt={thumbnailPreview ? "Thumbnail mới đã chọn" : "Thumbnail hiện tại của patch đầu tiên"}
                className="h-full w-full object-contain"
              />
            ) : (
              <div className="flex h-full items-center justify-center px-4 text-center text-xs text-muted-foreground">
                Chưa có patch để áp dụng thumbnail.
              </div>
            )}
          </div>
          <figcaption className="border-t px-3 py-2 text-[11px] text-muted-foreground">
            {thumbnailFile ? thumbnailFile.name : "Ảnh thumbnail hiện tại"}
          </figcaption>
        </figure>

        <div className="space-y-3">
          <div>
            <div className="text-sm font-semibold">Dùng một ảnh cho nhiều patch</div>
            <p className="mt-1 max-w-xl text-xs leading-5 text-muted-foreground">
              Tải ảnh JPG, PNG hoặc WebP, kiểm tra preview rồi áp dụng cùng lúc cho toàn bộ {patchIds.length} patch.
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <input
              ref={thumbnailInputRef}
              type="file"
              className="hidden"
              accept=".jpg,.jpeg,.png,.webp"
              disabled={thumbnailApplying}
              onChange={(event) => {
                selectThumbnail(event.target.files?.[0]);
                event.currentTarget.value = "";
              }}
            />
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={thumbnailApplying}
              onClick={() => thumbnailInputRef.current?.click()}
            >
              <Upload className="h-3.5 w-3.5" />
              Tải thumbnail mới
            </Button>
            <Button
              type="button"
              size="sm"
              disabled={!thumbnailFile || !patchIds.length || thumbnailApplying}
              onClick={applyThumbnailToAll}
            >
              {thumbnailApplying
                ? `Đang áp dụng ${thumbnailProgress}/${patchIds.length}...`
                : `Dùng cho tất cả ${patchIds.length} patch`}
            </Button>
          </div>
        </div>
      </section>

      <Card>
        <CardContent className="space-y-4 p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="text-sm font-semibold">Ảnh bìa Podcast (1:1)</div>
              <div className="text-xs text-muted-foreground">
                Dùng chung artwork với thumbnail, chỉ khác khung cắt — YouTube Podcasts chỉ nhận một ảnh vuông.
              </div>
            </div>
            <CheckField
              label="Tạo ảnh bìa podcast"
              checked={podcast.enabled}
              onChange={(enabled) => updatePodcast({ enabled })}
            />
          </div>

          {podcast.enabled && (
            <div className="grid gap-4 lg:grid-cols-[minmax(0,240px)_1fr]">
              <figure className="overflow-hidden rounded-md border bg-muted/20">
                <div className="aspect-square bg-muted">
                  {coverPreview ? (
                    <img src={coverPreview} alt="Ảnh bìa podcast" className="h-full w-full object-cover" />
                  ) : (
                    <div className="flex h-full items-center justify-center px-3 text-center text-[11px] text-muted-foreground">
                      {background?.is_video ? "Background video không preview được" : "Đang dựng preview..."}
                    </div>
                  )}
                </div>
                <figcaption className="border-t px-3 py-2 text-[11px] text-muted-foreground">
                  {podcast.size}×{podcast.size} px
                </figcaption>
              </figure>

              <div className="space-y-3">
                <Field label="Tâm khung cắt ngang" hint={`${podcast.focus_x}%`}>
                  <input
                    type="range"
                    min={0}
                    max={100}
                    aria-label="Tâm khung cắt ngang"
                    className="w-full accent-primary"
                    value={podcast.focus_x}
                    onChange={(event) => updatePodcast({ focus_x: Number(event.target.value) })}
                  />
                </Field>
                <Field label="Tâm khung cắt dọc" hint={`${podcast.focus_y}%`}>
                  <input
                    type="range"
                    min={0}
                    max={100}
                    aria-label="Tâm khung cắt dọc"
                    className="w-full accent-primary"
                    value={podcast.focus_y}
                    onChange={(event) => updatePodcast({ focus_y: Number(event.target.value) })}
                  />
                </Field>
                <p className="text-[11px] leading-4 text-muted-foreground">
                  Khung cắt là hình vuông lớn nhất lọt trong ảnh, nên chỉ cạnh dài hơn mới trượt được — ảnh 16:9 chỉ
                  đổi được theo chiều ngang.
                </p>
                <Field label="Kích thước ảnh">
                  <select
                    className={selectClass}
                    value={podcast.size}
                    onChange={(event) => updatePodcast({ size: Number(event.target.value) })}
                  >
                    {PODCAST_COVER_SIZES.map((size) => (
                      <option key={size} value={size}>
                        {size}×{size} px
                      </option>
                    ))}
                  </select>
                </Field>
                <p className="text-[11px] leading-4 text-muted-foreground">
                  Ảnh chỉ được đẩy lên khi bật Podcast ở <strong>Cấu hình → YouTube</strong>; playlist của sách sẽ được
                  đánh dấu là podcast và nhận ảnh này làm bìa.
                </p>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {layers.map((layer, index) => (
        <Card key={index}>
          <CardContent className="space-y-4 p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <strong className="text-sm">Layer {index + 1}</strong>
              <div className="flex gap-1">
                <Button type="button" size="sm" variant="outline" disabled={index === 0} onClick={() => setConfig({ ...config, overlays: layers.map((item, itemIndex) => itemIndex === index - 1 ? layers[index] : itemIndex === index ? layers[index - 1] : item) })}>Lên</Button>
                <Button type="button" size="sm" variant="outline" disabled={index === layers.length - 1} onClick={() => setConfig({ ...config, overlays: layers.map((item, itemIndex) => itemIndex === index ? layers[index + 1] : itemIndex === index + 1 ? layers[index] : item) })}>Xuống</Button>
                <Button type="button" size="sm" variant="ghost" onClick={() => setConfig({ ...config, overlays: layers.filter((_, itemIndex) => itemIndex !== index) })}>Xóa</Button>
              </div>
            </div>

            <div>
              <div className="mb-2 text-xs font-medium">Mẫu box nổi bật</div>
              <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
                {boxTemplates.map((template) => {
                  const active = layer.box.enabled
                    && layer.box.color.toLowerCase() === template.box.color.toLowerCase()
                    && layer.text_color.toLowerCase() === template.text_color.toLowerCase();
                  return (
                    <button
                      key={template.name}
                      type="button"
                      aria-pressed={active}
                      onClick={() => update(index, { text_color: template.text_color, box: { ...template.box }, shadow: { ...template.shadow } })}
                      className={cn(
                        "rounded-md border p-2 text-left transition-colors hover:border-primary/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                        active ? "border-primary bg-primary/5" : "bg-card"
                      )}
                    >
                      <span className={cn("flex h-9 items-center justify-center rounded px-2 text-xs font-extrabold", template.swatch)}>Aa</span>
                      <span className="mt-2 block text-xs font-semibold">{template.name}</span>
                      <span className="mt-0.5 block text-[10px] text-muted-foreground">{template.description}</span>
                    </button>
                  );
                })}
              </div>
            </div>

            <Field label="Text template"><input className={fieldClass} value={layer.text} onChange={(event) => update(index, { text: event.target.value })} /></Field>
            <div>
              <div className="mb-2 text-xs font-medium">Kiểu chữ nhanh</div>
              <div className="flex flex-wrap gap-2">
                {typeTemplates.map((template) => (
                  <Button
                    key={template.name}
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={() => update(index, { ...template, text_transform: template.text_transform as OverlayLayer["text_transform"] })}
                  >
                    {template.name}
                  </Button>
                ))}
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
              <Field label="Position"><select className={selectClass} value={layer.position} onChange={(event) => update(index, { position: event.target.value as OverlayLayer["position"] })}><option value="top">Top</option><option value="center">Center</option><option value="bottom">Bottom</option></select></Field>
              <Field label="Alignment"><select className={selectClass} value={layer.alignment} onChange={(event) => update(index, { alignment: event.target.value as OverlayLayer["alignment"] })}><option value="left">Left</option><option value="center">Center</option><option value="right">Right</option></select></Field>
              <Field label="Font size"><input type="number" min={12} max={200} className={fieldClass} value={layer.font_size} onChange={(event) => update(index, { font_size: Number(event.target.value) })} /></Field>
              <Field label="Font"><select className={selectClass} value={layer.font_path} onChange={(event) => update(index, { font_path: event.target.value })}><option value="">Mặc định</option>{response.fonts.map((font) => <option key={font.path} value={font.path}>{font.name}</option>)}</select></Field>
              <Field label="Kiểu chữ"><select className={selectClass} value={layer.text_transform || "none"} onChange={(event) => update(index, { text_transform: event.target.value as OverlayLayer["text_transform"] })}><option value="none">Giữ nguyên</option><option value="uppercase">CHỮ HOA</option><option value="lowercase">chữ thường</option><option value="titlecase">Viết Hoa Từng Từ</option></select></Field>
              <Field label="Khoảng cách dòng"><input type="number" min={0} max={100} className={fieldClass} value={layer.line_spacing ?? 8} onChange={(event) => update(index, { line_spacing: Number(event.target.value) })} /></Field>
              <Field label="Chiều rộng tối đa (%)"><input type="number" min={20} max={100} className={fieldClass} value={layer.max_width ?? 90} onChange={(event) => update(index, { max_width: Number(event.target.value) })} /></Field>
              <Field label="Độ dày viền chữ"><input type="number" min={0} max={20} className={fieldClass} value={layer.stroke_width ?? 0} onChange={(event) => update(index, { stroke_width: Number(event.target.value) })} /></Field>
              <Field label="Màu viền chữ"><input type="color" className={fieldClass} value={layer.stroke_color || "#000000"} onChange={(event) => update(index, { stroke_color: event.target.value })} /></Field>
              <Field label="Text color"><input type="color" className={fieldClass} value={layer.text_color} onChange={(event) => update(index, { text_color: event.target.value })} /></Field>
              <Field label="Margin"><input type="number" className={fieldClass} value={layer.margin} onChange={(event) => update(index, { margin: Number(event.target.value) })} /></Field>
              <Field label="Offset X"><input type="number" className={fieldClass} value={layer.offset_x} onChange={(event) => update(index, { offset_x: Number(event.target.value) })} /></Field>
              <Field label="Offset Y"><input type="number" className={fieldClass} value={layer.offset_y} onChange={(event) => update(index, { offset_y: Number(event.target.value) })} /></Field>
            </div>

            <div className="grid gap-4 border-t pt-4 lg:grid-cols-2">
              <div className="space-y-3">
                <CheckField label="Đổ bóng" checked={layer.shadow.enabled} onChange={(enabled) => update(index, { shadow: { ...layer.shadow, enabled } })} />
                <div className="grid grid-cols-2 gap-3">
                  <Field label="Màu bóng"><input type="color" className={fieldClass} value={layer.shadow.color} onChange={(event) => update(index, { shadow: { ...layer.shadow, color: event.target.value } })} /></Field>
                  <Field label="Độ lệch"><input type="number" min={0} max={20} className={fieldClass} value={layer.shadow.offset} onChange={(event) => update(index, { shadow: { ...layer.shadow, offset: Number(event.target.value) } })} /></Field>
                </div>
              </div>
              <div className="space-y-3">
                <CheckField label="Hiện box nền" checked={layer.box.enabled} onChange={(enabled) => update(index, { box: { ...layer.box, enabled } })} />
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
                  <Field label="Màu"><input type="color" className={fieldClass} value={layer.box.color} onChange={(event) => update(index, { box: { ...layer.box, color: event.target.value } })} /></Field>
                  <Field label="Opacity"><input type="number" min={0} max={100} className={fieldClass} value={layer.box.opacity} onChange={(event) => update(index, { box: { ...layer.box, opacity: Number(event.target.value) } })} /></Field>
                  <Field label="Padding X"><input type="number" min={0} max={200} className={fieldClass} value={layer.box.padding_x} onChange={(event) => update(index, { box: { ...layer.box, padding_x: Number(event.target.value) } })} /></Field>
                  <Field label="Padding Y"><input type="number" min={0} max={200} className={fieldClass} value={layer.box.padding_y} onChange={(event) => update(index, { box: { ...layer.box, padding_y: Number(event.target.value) } })} /></Field>
                  <Field label="Bo góc"><input type="number" min={0} max={200} className={fieldClass} value={layer.box.radius} onChange={(event) => update(index, { box: { ...layer.box, radius: Number(event.target.value) } })} /></Field>
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      ))}

      <Button type="button" variant="outline" onClick={() => setConfig({ ...config, overlays: [...layers, emptyLayer()] })}>Thêm text layer</Button>

      {/* Branding section */}
      <Card>
        <CardContent className="space-y-4 p-4">
          <div className="flex items-center justify-between">
            <div>
              <div className="text-sm font-semibold">Thương hiệu (Branding)</div>
              <div className="text-xs text-muted-foreground">
                {brandingMode === "inherit"
                  ? "Đang dùng mặc định toàn cục. Chuyển sang tùy chỉnh để riêng sách này."
                  : "Sách này có cấu hình thương hiệu riêng."}
              </div>
            </div>
            <div className="flex items-center gap-2">
              {brandingMode === "custom" && (
                <Button type="button" variant="ghost" size="sm" onClick={resetBrandingToInherit}>
                  Về mặc định
                </Button>
              )}
              <Button
                type="button"
                size="sm"
                variant={brandingMode === "inherit" ? "default" : "outline"}
                onClick={() => {
                  if (brandingMode === "inherit") {
                    setBrandingDraft(branding ? JSON.parse(JSON.stringify(branding)) : null);
                    setBrandingMode("custom");
                  }
                }}
              >
                {brandingMode === "inherit" ? "Tùy chỉnh" : "Đang tùy chỉnh"}
              </Button>
            </div>
          </div>

          {brandingMode === "custom" && brandingDraft && (
            <>
              <BrandingEditor
                branding={brandingDraft}
                onChange={setBrandingDraft}
                logoBrowserOpen={logoBrowserOpen}
                onLogoBrowserOpenChange={setLogoBrowserOpen}
              />
              <div className="flex flex-wrap items-center gap-2">
                <Button type="button" size="sm" disabled={brandingSaving} onClick={saveBranding}>
                  {brandingSaving ? "Đang lưu branding..." : "Lưu branding & tạo lại thumbnail"}
                </Button>
                <span className="text-[11px] text-muted-foreground">
                  Lưu branding cũng tạo lại thumbnail/podcast cover (nếu bật) để thấy ngay.
                </span>
              </div>
            </>
          )}
          {brandingMode === "inherit" && branding && (
            <div className="rounded-md bg-muted/30 p-3 text-xs text-muted-foreground">
              <div className="font-medium text-foreground mb-1">Giá trị hiện tại (inherit):</div>
              <div>Watermark: {branding.watermark.enabled ? `"${branding.watermark.text}"` : "Tắt"}</div>
              <div>Logo: {branding.logo.enabled ? (branding.logo.path.split("/").pop() || "Đã chọn") : "Tắt"}</div>
              <div>Mục tiêu: {[
                branding.targets.thumbnail && "Thumbnail",
                branding.targets.podcast && "Podcast",
                branding.targets.video && "Video",
              ].filter(Boolean).join(", ") || "Không"}</div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Logo Browser Dialog for per-book branding */}
      {logoBrowserOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
          <div className="mx-4 flex h-[80vh] w-full max-w-4xl flex-col rounded-lg border border-border bg-card shadow-xl">
            <div className="flex items-center justify-between border-b border-border px-4 py-3">
              <span className="text-sm font-semibold">Chọn logo</span>
              <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => setLogoBrowserOpen(false)}>
                <X className="h-4 w-4" />
              </Button>
            </div>
            <div className="flex-1 overflow-hidden">
              <MediaBrowser
                category="logos"
                selectedPath={brandingDraft?.logo.path || null}
                onSelect={(entry: MediaEntry) => {
                  setBrandingDraft((prev) => prev ? { ...prev, logo: { ...prev.logo, path: entry.path } } : prev);
                  setLogoBrowserOpen(false);
                }}
                height="100%"
              />
            </div>
          </div>
        </div>
      )}

      <DialogFooter>
        <Button type="button" variant="outline" disabled={saving} onClick={() => save(false)}>Lưu cấu hình</Button>
        <Button type="button" disabled={saving || !patchIds.length} onClick={() => save(true)}>{saving ? "Đang lưu..." : "Lưu & tạo lại thumbnail"}</Button>
      </DialogFooter>
    </div>
  );
}
