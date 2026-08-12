import React, { useEffect, useState } from "react";
import { api, postJson } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { DialogFooter } from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { CheckField, Field, fieldClass, selectClass } from "./parts";
import { BackgroundItem, OverlayConfig, OverlayConfigResponse, OverlayLayer, errorText } from "./types";

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
    swatch: "bg-amber-300 text-neutral-950",
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
  const [thumbnailRevision, setThumbnailRevision] = useState(0);
  const [saving, setSaving] = useState(false);
  const layers = config?.overlays?.length ? config.overlays : config ? [config] : [];

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

  useEffect(() => {
    if (!config || !background || background.is_video) return;
    const timer = window.setTimeout(async () => {
      const params = new URLSearchParams({
        live: "1",
        overlays_json: JSON.stringify(config.overlays.length ? config.overlays : [config]),
        background_path: background.path,
      });
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
  }, [bookId, config, background, onMessage]);

  useEffect(() => () => {
    if (preview) URL.revokeObjectURL(preview);
  }, [preview]);

  const update = (index: number, patch: Partial<OverlayLayer>) =>
    setConfig((current) =>
      current
        ? { ...current, overlays: layers.map((layer, layerIndex) => layerIndex === index ? { ...layer, ...patch } : layer) }
        : current
    );

  const save = async (regenerate: boolean) => {
    if (!config) return;
    setSaving(true);
    try {
      const form = new FormData();
      form.append("overlays_json", JSON.stringify(config.overlays.length ? config.overlays : [config]));
      if (background && !background.is_video) form.append("background_path", background.path);
      await api(`/books/${bookId}/overlay-config`, { method: "POST", body: form });
      if (regenerate && patchIds.length) {
        await postJson(`/books/${bookId}/thumbnails/regenerate`, { patch_ids: patchIds });
        setThumbnailRevision((current) => current + 1);
      }
      onMessage(regenerate
        ? "Đã lưu cấu hình overlay và đưa thumbnail vào hàng đợi tạo lại."
        : "Đã lưu đầy đủ cấu hình thumbnail overlay.");
      await onSaved();
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setSaving(false);
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

      {patchIds.length > 0 && (
        <Card>
          <CardContent className="space-y-3 p-4">
            <div>
              <div className="text-sm font-semibold">Thumbnail các patch</div>
              <div className="text-xs text-muted-foreground">Ảnh preview của patch đầu tiên, dùng cho video và YouTube.</div>
            </div>
            <figure className="max-w-xl overflow-hidden rounded-md border bg-muted/20">
              <div className="aspect-video bg-muted">
                <img
                  src={`/books/${bookId}/patches/${patchIds[0]}/overlay-image?v=${thumbnailRevision}`}
                  alt="Thumbnail patch 1"
                  className="h-full w-full object-contain"
                />
              </div>
              <figcaption className="border-t px-3 py-2 text-xs font-medium">Patch 1</figcaption>
            </figure>
          </CardContent>
        </Card>
      )}

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
      <DialogFooter>
        <Button type="button" variant="outline" disabled={saving} onClick={() => save(false)}>Lưu cấu hình</Button>
        <Button type="button" disabled={saving || !patchIds.length} onClick={() => save(true)}>{saving ? "Đang lưu..." : "Lưu & tạo lại thumbnail"}</Button>
      </DialogFooter>
    </div>
  );
}
