import React, { useEffect, useRef } from "react";
import { cn } from "@/lib/utils";

/** The subset of VideoConfig that describes the narration waveform overlay. */
export type WaveformPreviewSettings = {
  waveform_style: "line" | "cline" | "p2p" | "point";
  waveform_color: string;
  waveform_position: "top" | "center" | "bottom";
  waveform_height: number;
  waveform_opacity: number;
  waveform_layout: "horizontal" | "vertical" | "circular";
  waveform_background_color: string;
  waveform_background_opacity: number;
};

/** One bar every 17px keeps the columns thick and countable instead of hair-thin. */
const BAR_PITCH = 17;
const BAR_WIDTH = 10;
const MIN_BARS = 14;
const MAX_BARS = 56;
const RADIAL_BARS = 32;
/** Band size as a fraction of the stage, mapped from the 40..400px height slider. */
const MIN_FRACTION = 0.34;
const MAX_FRACTION = 0.86;

const LAYOUT_LABELS = { horizontal: "Ngang", vertical: "Dọc", circular: "Tròn" } as const;
const POSITION_LABELS = { top: "trên khung", center: "giữa khung", bottom: "dưới khung" } as const;
const STYLE_LABELS = { line: "Line", cline: "Center line", p2p: "Point to point", point: "Point" } as const;

function clamp(value: number, low: number, high: number): number {
  return Math.min(high, Math.max(low, value));
}

/** #rrggbb (or #rgb) to rgba() so opacity can ride on the fill without a second layer. */
function withAlpha(hex: string, alpha: number): string {
  const raw = String(hex || "").replace("#", "").trim();
  const full = raw.length === 3 ? raw.split("").map((c) => c + c).join("") : raw;
  if (!/^[0-9a-fA-F]{6}$/.test(full)) return `rgba(255,255,255,${alpha})`;
  const r = parseInt(full.slice(0, 2), 16);
  const g = parseInt(full.slice(2, 4), 16);
  const b = parseInt(full.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

/** Speech-shaped fake signal: a slow loudness swell over faster partials, so the
 *  preview moves like narration instead of like a flat test tone. */
function amplitudeAt(index: number, count: number, time: number): number {
  const p = count > 1 ? index / (count - 1) : 0.5;
  // The swell keeps a high floor: a trough near zero would read as a dead preview.
  const swell = 0.74 + 0.26 * Math.sin(time * 1.6 + p * 2.2);
  const detail =
    0.5 * Math.sin(time * 7.3 + index * 0.9) +
    0.3 * Math.sin(time * 11.7 + index * 2.1) +
    0.2 * Math.sin(time * 3.1 + index * 5.7);
  // Taper the ends so the bars read as one wave rather than a solid block.
  const taper = 0.62 + 0.38 * Math.sin(Math.PI * clamp(p, 0, 1));
  return clamp((0.32 + 0.68 * Math.abs(detail)) * swell * taper, 0.12, 1);
}

function roundedRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  radius: number,
) {
  ctx.beginPath();
  if (typeof (ctx as any).roundRect === "function") {
    (ctx as any).roundRect(x, y, w, h, radius);
  } else {
    ctx.moveTo(x + radius, y);
    ctx.arcTo(x + w, y, x + w, y + h, radius);
    ctx.arcTo(x + w, y + h, x, y + h, radius);
    ctx.arcTo(x, y + h, x, y, radius);
    ctx.arcTo(x, y, x + w, y, radius);
  }
  ctx.fill();
}

/** A bar is just a fully rounded rect; kept separate so the caps stay pill-shaped. */
function roundedBar(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number) {
  roundedRect(ctx, x, y, w, h, Math.min(w / 2, h / 2));
}

/** Draw the bar wave inside a rect; the vertical layout reuses this rotated. */
function drawBars(
  ctx: CanvasRenderingContext2D,
  rect: { x: number; y: number; w: number; h: number },
  settings: WaveformPreviewSettings,
  time: number,
) {
  const { x, y, w, h } = rect;
  const count = clamp(Math.round(w / BAR_PITCH), MIN_BARS, MAX_BARS);
  const slot = w / count;
  const barWidth = Math.max(3, Math.min(BAR_WIDTH, slot - 5));
  const style = settings.waveform_style;
  const mid = y + h / 2;
  const reach = (h / 2) * 0.94;
  const color = withAlpha(settings.waveform_color, settings.waveform_opacity);

  ctx.fillStyle = color;
  ctx.strokeStyle = color;
  ctx.shadowColor = withAlpha(settings.waveform_color, settings.waveform_opacity * 0.8);
  ctx.shadowBlur = 14;

  for (let i = 0; i < count; i += 1) {
    const cx = x + (i + 0.5) * slot;
    const amp = amplitudeAt(i, count, time) * reach;

    if (style === "point") {
      const dot = barWidth / 2;
      ctx.beginPath();
      ctx.arc(cx, mid - amp, dot, 0, Math.PI * 2);
      ctx.fill();
      ctx.beginPath();
      ctx.arc(cx, mid + amp, dot, 0, Math.PI * 2);
      ctx.fill();
    } else if (style === "line") {
      // Bars stand on the baseline, the way showwaves=mode=line fills upward.
      const barHeight = Math.max(barWidth, amp * 2);
      roundedBar(ctx, cx - barWidth / 2, y + h - barHeight, barWidth, barHeight);
    } else if (style === "p2p") {
      // Peak-to-peak: a split pair of bars, so it stays distinct from center line.
      const gap = Math.max(3, reach * 0.12);
      const span = Math.max(barWidth, amp - gap);
      roundedBar(ctx, cx - barWidth / 2, mid - gap - span, barWidth, span);
      roundedBar(ctx, cx - barWidth / 2, mid + gap, barWidth, span);
    } else {
      roundedBar(ctx, cx - barWidth / 2, mid - amp, barWidth, Math.max(barWidth, amp * 2));
    }
  }

  ctx.shadowBlur = 0;
}

function drawRadial(
  ctx: CanvasRenderingContext2D,
  center: { cx: number; cy: number; radius: number },
  settings: WaveformPreviewSettings,
  time: number,
) {
  const { cx, cy, radius } = center;
  const inner = radius * 0.3;
  const reach = radius - inner;
  const color = withAlpha(settings.waveform_color, settings.waveform_opacity);
  const barWidth = Math.max(6, Math.min(BAR_WIDTH, (2 * Math.PI * inner) / RADIAL_BARS - 3));

  ctx.strokeStyle = withAlpha(settings.waveform_color, settings.waveform_opacity * 0.45);
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(cx, cy, inner, 0, Math.PI * 2);
  ctx.stroke();

  ctx.fillStyle = color;
  ctx.shadowColor = color;
  ctx.shadowBlur = 14;
  for (let i = 0; i < RADIAL_BARS; i += 1) {
    const angle = (i / RADIAL_BARS) * Math.PI * 2 - Math.PI / 2;
    const amp = Math.max(barWidth, amplitudeAt(i, RADIAL_BARS, time) * reach);
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(angle);
    roundedBar(ctx, inner, -barWidth / 2, amp, barWidth);
    ctx.restore();
  }
  ctx.shadowBlur = 0;
}

/**
 * One large, animated stand-in for the narration waveform burned into the video.
 * It is a close-up rather than a to-scale frame preview: the height slider scales
 * the band inside the stage so the shape stays readable at any setting.
 */
export function WaveformPreview({
  settings,
  className,
  height = 240,
}: {
  settings: WaveformPreviewSettings;
  className?: string;
  height?: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  // The animation loop reads the latest settings without restarting on each edit.
  const settingsRef = useRef(settings);
  settingsRef.current = settings;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const reduceMotion =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let frame = 0;
    let start = 0;

    const render = (now: number) => {
      if (!start) start = now;
      const time = reduceMotion ? 1.4 : (now - start) / 1000;
      const current = settingsRef.current;
      const ratio = window.devicePixelRatio || 1;
      const width = canvas.clientWidth || 1;
      const stageHeight = canvas.clientHeight || height;
      if (
        canvas.width !== Math.round(width * ratio) ||
        canvas.height !== Math.round(stageHeight * ratio)
      ) {
        canvas.width = Math.round(width * ratio);
        canvas.height = Math.round(stageHeight * ratio);
      }
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, width, stageHeight);

      // Stage = the video frame sitting behind the overlay.
      const sky = ctx.createLinearGradient(0, 0, 0, stageHeight);
      sky.addColorStop(0, "#0f172a");
      sky.addColorStop(1, "#020617");
      ctx.fillStyle = sky;
      ctx.fillRect(0, 0, width, stageHeight);

      const fraction =
        MIN_FRACTION +
        (MAX_FRACTION - MIN_FRACTION) * clamp((current.waveform_height - 40) / 360, 0, 1);
      const pad = stageHeight * 0.06;
      const bandSize = stageHeight * fraction;
      const offsetY = {
        top: pad,
        center: (stageHeight - bandSize) / 2,
        bottom: stageHeight - bandSize - pad,
      }[current.waveform_position];
      const panel = withAlpha(current.waveform_background_color, current.waveform_background_opacity);

      if (current.waveform_layout === "circular") {
        const size = Math.min(bandSize, width * 0.9);
        const cx = width / 2;
        const cy = offsetY + bandSize / 2;
        ctx.fillStyle = panel;
        roundedRect(ctx, cx - size / 2 - 16, cy - size / 2 - 16, size + 32, size + 32, 16);
        drawRadial(ctx, { cx, cy, radius: size / 2 }, current, time);
      } else if (current.waveform_layout === "vertical") {
        // The overlay hugs the left edge; draw the same bars under a quarter turn.
        const thickness = Math.min(bandSize, width * 0.6);
        const left = width * 0.06;
        const top = pad;
        const length = stageHeight - pad * 2;
        ctx.fillStyle = panel;
        roundedRect(ctx, left - 12, top - 8, thickness + 24, length + 16, 14);
        ctx.save();
        ctx.translate(left, top + length);
        ctx.rotate(-Math.PI / 2);
        drawBars(ctx, { x: 0, y: 0, w: length, h: thickness }, current, time);
        ctx.restore();
      } else {
        ctx.fillStyle = panel;
        ctx.fillRect(0, offsetY - 10, width, bandSize + 20);
        drawBars(ctx, { x: width * 0.03, y: offsetY, w: width * 0.94, h: bandSize }, current, time);
      }

      if (!reduceMotion) frame = window.requestAnimationFrame(render);
    };

    frame = window.requestAnimationFrame(render);
    return () => window.cancelAnimationFrame(frame);
  }, [height]);

  return (
    <div className={cn("space-y-1.5", className)}>
      <div className="overflow-hidden rounded-lg border border-border bg-slate-950">
        <canvas ref={canvasRef} style={{ height }} className="block w-full" />
      </div>
      <p className="text-[11px] leading-4 text-muted-foreground">
        {LAYOUT_LABELS[settings.waveform_layout]} · {STYLE_LABELS[settings.waveform_style]} ·{" "}
        {POSITION_LABELS[settings.waveform_position]} · cao {settings.waveform_height}px trên khung
        1080p — sóng mô phỏng, bản dựng thật lấy biên độ từ audio narration.
      </p>
    </div>
  );
}
