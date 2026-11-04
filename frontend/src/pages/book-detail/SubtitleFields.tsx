import React from "react";
import { Captions } from "lucide-react";
import { VideoConfig } from "./types";
import { CheckField, Field, selectClass } from "./parts";

/** Font Windows có sẵn và đủ dấu tiếng Việt. libass tìm font theo tên, nên giá trị
 * phải là tên family chứ không phải tên file. */
export const SUBTITLE_FONT_FAMILIES = [
  "Segoe UI",
  "Segoe UI Semibold",
  "Segoe UI Black",
  "Arial",
  "Arial Black",
  "Tahoma",
  "Verdana",
  "Calibri",
  "Cambria",
  "Georgia",
  "Times New Roman",
  "Trebuchet MS",
  "Bahnschrift",
  "Candara",
  "Corbel",
  "Constantia",
];

type SubtitleConfig = Pick<
  VideoConfig,
  | "subtitle_enabled"
  | "subtitle_position"
  | "subtitle_font_size"
  | "subtitle_font_family"
  | "subtitle_color"
  | "subtitle_background_enabled"
  | "subtitle_background_color"
  | "subtitle_background_opacity"
  | "subtitle_background_padding"
>;

/** Khối cấu hình phụ đề dùng chung cho Cấu hình sản xuất của sách và Cấu hình mặc định. */
export function SubtitleFields({
  config,
  onChange,
  description,
}: {
  config: SubtitleConfig;
  onChange: (patch: Partial<SubtitleConfig>) => void;
  description?: React.ReactNode;
}) {
  const fontFamily = config.subtitle_font_family || "Segoe UI";
  const fontOptions = SUBTITLE_FONT_FAMILIES.includes(fontFamily)
    ? SUBTITLE_FONT_FAMILIES
    : [fontFamily, ...SUBTITLE_FONT_FAMILIES];
  const padding = config.subtitle_background_padding ?? 16;
  const box = config.subtitle_background_enabled;

  return (
    <section className="space-y-3 rounded-md border border-border p-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 text-xs font-semibold">
            <Captions className="h-4 w-4 text-primary" /> Phụ đề tự động
          </div>
          {description && <p className="mt-1 text-[11px] leading-4 text-muted-foreground">{description}</p>}
        </div>
        <CheckField checked={config.subtitle_enabled} onChange={(value) => onChange({ subtitle_enabled: value })} label="Bật" />
      </div>

      <div className={config.subtitle_enabled ? "grid gap-3 sm:grid-cols-3" : "pointer-events-none grid gap-3 opacity-45 sm:grid-cols-3"}>
        <Field label="Vị trí">
          <select
            className={selectClass}
            value={config.subtitle_position}
            onChange={(event) => onChange({ subtitle_position: event.target.value as VideoConfig["subtitle_position"] })}
          >
            <option value="bottom">Dưới</option>
            <option value="center">Giữa</option>
            <option value="top">Trên</option>
          </select>
        </Field>
        <Field label="Font chữ">
          <select
            className={selectClass}
            style={{ fontFamily }}
            value={fontFamily}
            onChange={(event) => onChange({ subtitle_font_family: event.target.value })}
          >
            {fontOptions.map((name) => (
              <option key={name} value={name} style={{ fontFamily: name }}>
                {name}
              </option>
            ))}
          </select>
        </Field>
        <Field label={`Cỡ chữ: ${config.subtitle_font_size}`}>
          <input
            className="w-full accent-primary"
            type="range"
            min="20"
            max="96"
            step="2"
            value={config.subtitle_font_size}
            onChange={(event) => onChange({ subtitle_font_size: Number(event.target.value) })}
          />
        </Field>
        <Field label="Màu chữ">
          <input
            className="h-9 w-full cursor-pointer rounded-md border border-border bg-background p-1"
            type="color"
            value={config.subtitle_color}
            onChange={(event) => onChange({ subtitle_color: event.target.value })}
          />
        </Field>
        <Field label="Nền phụ đề">
          <CheckField
            checked={box}
            onChange={(value) => onChange({ subtitle_background_enabled: value })}
            label="Hiện khung nền"
          />
        </Field>
        <Field label="Màu nền">
          <input
            className="h-9 w-full cursor-pointer rounded-md border border-border bg-background p-1 disabled:opacity-45"
            type="color"
            disabled={!box}
            value={config.subtitle_background_color}
            onChange={(event) => onChange({ subtitle_background_color: event.target.value })}
          />
        </Field>
        <Field label={`Độ đậm nền: ${Math.round(config.subtitle_background_opacity * 100)}%`}>
          <input
            className="w-full accent-primary disabled:opacity-45"
            type="range"
            min="0"
            max="1"
            step="0.05"
            disabled={!box}
            value={config.subtitle_background_opacity}
            onChange={(event) => onChange({ subtitle_background_opacity: Number(event.target.value) })}
          />
        </Field>
        <Field label={`Padding nền: ${padding}px`} hint="Khoảng cách chữ tới mép khung">
          <input
            className="w-full accent-primary disabled:opacity-45"
            type="range"
            min="0"
            max="80"
            step="2"
            disabled={!box}
            value={padding}
            onChange={(event) => onChange({ subtitle_background_padding: Number(event.target.value) })}
          />
        </Field>
      </div>
    </section>
  );
}
