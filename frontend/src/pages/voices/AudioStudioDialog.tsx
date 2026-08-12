import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AudioWaveform,
  Loader2,
  Play,
  RotateCcw,
  Scissors,
  Square,
  Wand2,
} from "lucide-react";
import { api, postJson, VoiceAudioOps, VoiceInfo, VoiceItem, VoiceProcessResult } from "@/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

const WAVE_BUCKETS = 1400;
const WAVE_HEIGHT = 132;
/** Click tolerance for grabbing a selection edge, in pixels. */
const HANDLE_GRAB_PX = 7;
const SAMPLE_RATES = [16000, 22050, 24000, 32000, 44100, 48000];

const selectClass =
  "h-9 w-full rounded-md border border-input bg-background px-2 text-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/15";

type Selection = { start: number; end: number };
type DragMode = "start" | "end" | "new" | null;

/** Peaks are min/max pairs per bucket, so quiet detail stays visible when the
 *  clip is squeezed into a few hundred pixels. */
type Peaks = { min: Float32Array; max: Float32Array };

function formatTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00.00";
  const mins = Math.floor(seconds / 60);
  const secs = seconds - mins * 60;
  return `${mins}:${secs.toFixed(2).padStart(5, "0")}`;
}

/** Read one HSL theme token off an element so canvas paint follows the theme. */
function themeColor(el: Element | null, token: string, alpha = 1): string {
  const raw = el ? getComputedStyle(el).getPropertyValue(token).trim() : "";
  if (!raw) return alpha < 1 ? `rgba(120,120,120,${alpha})` : "#787878";
  return alpha < 1 ? `hsl(${raw} / ${alpha})` : `hsl(${raw})`;
}

/** Downsample an AudioBuffer to per-bucket min/max, mixing all channels. */
function buildPeaks(buffer: AudioBuffer, buckets: number): Peaks {
  const min = new Float32Array(buckets);
  const max = new Float32Array(buckets);
  const channels: Float32Array[] = [];
  for (let c = 0; c < buffer.numberOfChannels; c += 1) channels.push(buffer.getChannelData(c));
  const frames = buffer.length;
  const perBucket = Math.max(1, Math.floor(frames / buckets));

  for (let b = 0; b < buckets; b += 1) {
    const from = b * perBucket;
    const to = Math.min(frames, from + perBucket);
    let lo = 0;
    let hi = 0;
    for (let i = from; i < to; i += 1) {
      let sum = 0;
      for (let c = 0; c < channels.length; c += 1) sum += channels[c][i];
      const value = sum / channels.length;
      if (value < lo) lo = value;
      if (value > hi) hi = value;
    }
    min[b] = lo;
    max[b] = hi;
  }
  return { min, max };
}

export function AudioStudioDialog({
  voice,
  onClose,
  onSaved,
}: {
  voice: VoiceItem;
  onClose: () => void;
  /** Called after a successful render so the library can reload. */
  onSaved: (result: VoiceProcessResult) => void;
}) {
  // Bumped after an in-place overwrite: the path is unchanged, so without a
  // cache-buster both the <audio> element and the fetch below would replay the
  // stale pre-edit audio.
  const [version, setVersion] = useState(0);
  const fileUrl = `/voices/file/${encodeURIComponent(voice.name)}${version ? `?v=${version}` : ""}`;

  const [info, setInfo] = useState<VoiceInfo | null>(null);
  const [peaks, setPeaks] = useState<Peaks | null>(null);
  const [duration, setDuration] = useState(0);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const [selection, setSelection] = useState<Selection>({ start: 0, end: 0 });
  const [playhead, setPlayhead] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);

  const [denoise, setDenoise] = useState(false);
  const [trimSilence, setTrimSilence] = useState(false);
  const [normalize, setNormalize] = useState(false);
  const [highpass, setHighpass] = useState(false);
  const [lowpass, setLowpass] = useState(false);
  const [mono, setMono] = useState(false);
  const [gainDb, setGainDb] = useState(0);
  const [fadeIn, setFadeIn] = useState(0);
  const [fadeOut, setFadeOut] = useState(0);
  const [sampleRate, setSampleRate] = useState<number | "">("");

  const [saveAsCopy, setSaveAsCopy] = useState(true);
  const [copyName, setCopyName] = useState("");
  const [applying, setApplying] = useState(false);
  const [applyError, setApplyError] = useState<string | null>(null);
  const [applied, setApplied] = useState<string[] | null>(null);

  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const dragRef = useRef<DragMode>(null);
  const dragAnchorRef = useRef(0);
  /** Where playback of the current selection must stop; null = play to the end. */
  const stopAtRef = useRef<number | null>(null);

  // ---------------------------------------------------------------- load clip
  const loadClip = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    let context: AudioContext | null = null;
    try {
      const [probe, response] = await Promise.all([
        api<VoiceInfo>(`/voices/${encodeURIComponent(voice.name)}/info`),
        fetch(fileUrl),
      ]);
      if (!response.ok) throw new Error(`Không đọc được file (lỗi ${response.status})`);
      const bytes = await response.arrayBuffer();
      context = new AudioContext();
      const buffer = await context.decodeAudioData(bytes);
      setInfo(probe);
      setPeaks(buildPeaks(buffer, WAVE_BUCKETS));
      // ffprobe's duration is authoritative for the ops we send to the server;
      // the decoded buffer is the fallback when ffprobe is unavailable.
      const total = probe.duration_sec || buffer.duration;
      setDuration(total);
      setSelection({ start: 0, end: total });
    } catch (err: any) {
      setLoadError(err?.message || "Không giải mã được file âm thanh này.");
    } finally {
      void context?.close();
      setLoading(false);
    }
  }, [fileUrl, voice.name]);

  useEffect(() => {
    void loadClip();
  }, [loadClip]);

  useEffect(() => {
    setCopyName(voice.name.replace(/\.[^.]+$/, "") + "_edited");
  }, [voice.name]);

  // ------------------------------------------------------------------ drawing
  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas || !peaks) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth;
    const height = WAVE_HEIGHT;
    if (canvas.width !== Math.round(width * ratio)) {
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
    }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);

    const mid = height / 2;
    const toX = (seconds: number) => (duration ? (seconds / duration) * width : 0);
    const selFrom = toX(selection.start);
    const selTo = toX(selection.end);

    ctx.fillStyle = themeColor(canvas, "--muted", 0.45);
    ctx.fillRect(0, 0, width, height);
    // Selected span sits on a lighter ground so the discarded parts read as dimmed.
    ctx.fillStyle = themeColor(canvas, "--primary", 0.1);
    ctx.fillRect(selFrom, 0, Math.max(1, selTo - selFrom), height);

    const buckets = peaks.min.length;
    const inside = themeColor(canvas, "--primary");
    const outside = themeColor(canvas, "--muted-foreground", 0.35);
    for (let x = 0; x < width; x += 1) {
      const bucket = Math.min(buckets - 1, Math.floor((x / width) * buckets));
      const top = mid - peaks.max[bucket] * mid * 0.94;
      const bottom = mid - peaks.min[bucket] * mid * 0.94;
      ctx.fillStyle = x >= selFrom && x <= selTo ? inside : outside;
      ctx.fillRect(x, top, 1, Math.max(1, bottom - top));
    }

    ctx.strokeStyle = themeColor(canvas, "--border");
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, mid);
    ctx.lineTo(width, mid);
    ctx.stroke();

    // Selection edges — the grab targets.
    ctx.fillStyle = themeColor(canvas, "--primary");
    ctx.fillRect(Math.max(0, selFrom - 1), 0, 2, height);
    ctx.fillRect(Math.min(width - 2, selTo - 1), 0, 2, height);

    if (playhead !== null) {
      ctx.fillStyle = "#D7FF64";
      ctx.fillRect(toX(playhead), 0, 2, height);
    }
  }, [duration, peaks, playhead, selection]);

  useEffect(() => {
    draw();
  }, [draw]);

  useEffect(() => {
    const onResize = () => draw();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [draw]);

  // ------------------------------------------------------------- interactions
  const secondsAt = (event: React.PointerEvent<HTMLCanvasElement>): number => {
    const canvas = event.currentTarget;
    const rect = canvas.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    return ratio * duration;
  };

  const handlePointerDown = (event: React.PointerEvent<HTMLCanvasElement>) => {
    if (!duration) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const startX = (selection.start / duration) * rect.width;
    const endX = (selection.end / duration) * rect.width;
    const at = secondsAt(event);

    if (Math.abs(x - startX) <= HANDLE_GRAB_PX) {
      dragRef.current = "start";
    } else if (Math.abs(x - endX) <= HANDLE_GRAB_PX) {
      dragRef.current = "end";
    } else {
      dragRef.current = "new";
      dragAnchorRef.current = at;
      setSelection({ start: at, end: at });
    }
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const handlePointerMove = (event: React.PointerEvent<HTMLCanvasElement>) => {
    const mode = dragRef.current;
    if (!mode || !duration) return;
    const at = secondsAt(event);
    setSelection((current) => {
      if (mode === "start") return { start: Math.min(at, current.end), end: current.end };
      if (mode === "end") return { start: current.start, end: Math.max(at, current.start) };
      const anchor = dragAnchorRef.current;
      return { start: Math.min(anchor, at), end: Math.max(anchor, at) };
    });
  };

  const handlePointerUp = (event: React.PointerEvent<HTMLCanvasElement>) => {
    if (!dragRef.current) return;
    dragRef.current = null;
    event.currentTarget.releasePointerCapture(event.pointerId);
    // A plain click (zero-width drag) means "cancel the crop", not "select nothing".
    setSelection((current) =>
      current.end - current.start < 0.02 ? { start: 0, end: duration } : current
    );
  };

  const stop = useCallback(() => {
    const audio = audioRef.current;
    if (audio) audio.pause();
    setPlaying(false);
    setPlayhead(null);
    stopAtRef.current = null;
  }, []);

  const playRange = (from: number, to: number | null) => {
    const audio = audioRef.current;
    if (!audio) return;
    stopAtRef.current = to;
    audio.currentTime = from;
    void audio.play();
    setPlaying(true);
  };

  const handleTimeUpdate = () => {
    const audio = audioRef.current;
    if (!audio) return;
    setPlayhead(audio.currentTime);
    const limit = stopAtRef.current;
    if (limit !== null && audio.currentTime >= limit) stop();
  };

  // --------------------------------------------------------------------- ops
  const selectionLength = Math.max(0, selection.end - selection.start);
  const isFullSelection = selection.start <= 0.001 && selection.end >= duration - 0.001;

  const ops = useMemo<VoiceAudioOps>(() => {
    const payload: VoiceAudioOps = {};
    if (!isFullSelection) {
      payload.trim_start = Number(selection.start.toFixed(3));
      payload.trim_end = Number(selection.end.toFixed(3));
    }
    if (denoise) payload.denoise = true;
    if (trimSilence) payload.trim_silence = true;
    if (normalize) payload.normalize = true;
    if (highpass) payload.highpass = true;
    if (lowpass) payload.lowpass = true;
    if (mono) payload.mono = true;
    if (gainDb) payload.gain_db = gainDb;
    if (fadeIn > 0) payload.fade_in = fadeIn;
    if (fadeOut > 0) payload.fade_out = fadeOut;
    if (sampleRate) payload.sample_rate = sampleRate;
    return payload;
  }, [
    denoise, fadeIn, fadeOut, gainDb, highpass, isFullSelection, lowpass, mono,
    normalize, sampleRate, selection.end, selection.start, trimSilence,
  ]);

  const hasChanges = Object.keys(ops).length > 0;

  /** Clear the cleanup/tuning options, leaving the selection alone. */
  const resetFilters = () => {
    setDenoise(false);
    setTrimSilence(false);
    setNormalize(false);
    setHighpass(false);
    setLowpass(false);
    setMono(false);
    setGainDb(0);
    setFadeIn(0);
    setFadeOut(0);
    setSampleRate("");
  };

  const resetOps = () => {
    resetFilters();
    setSelection({ start: 0, end: duration });
    setApplyError(null);
    setApplied(null);
  };

  const handleApply = async () => {
    if (!hasChanges) return;
    if (!saveAsCopy && !confirm(
      `Ghi đè trực tiếp lên "${voice.name}"? File gốc sẽ không lấy lại được.`
    )) return;

    setApplying(true);
    setApplyError(null);
    setApplied(null);
    stop();
    try {
      const result = await postJson<VoiceProcessResult>(
        `/voices/${encodeURIComponent(voice.name)}/process`,
        { ops, save_as: saveAsCopy ? "copy" : "overwrite", new_name: saveAsCopy ? copyName.trim() : "" }
      );
      onSaved(result);
      resetFilters();
      // Only reload on overwrite - loadClip resets the selection from the new
      // duration, which resetOps must not do here with its stale copy.
      if (!saveAsCopy) setVersion((current) => current + 1);
      setApplied(result.applied);
    } catch (err: any) {
      setApplyError(err?.message || "Xử lý âm thanh thất bại.");
    } finally {
      setApplying(false);
    }
  };

  const techLine = [
    duration ? `${formatTime(duration)}` : null,
    info?.sample_rate ? `${(info.sample_rate / 1000).toFixed(1)} kHz` : null,
    info?.channels ? (info.channels === 1 ? "mono" : `${info.channels} kênh`) : null,
    info?.codec ? info.codec.toUpperCase() : null,
    info?.size ? `${(info.size / 1024 / 1024).toFixed(2)} MB` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-4xl max-h-[92vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <AudioWaveform className="h-4 w-4 text-primary shrink-0" />
            <span className="truncate">Xử lý âm thanh — {voice.name}</span>
          </DialogTitle>
          <DialogDescription>
            Cắt lấy đoạn giọng sạch nhất rồi làm sạch trong một lượt. Kéo trên sóng âm để
            chọn vùng giữ lại.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="rounded-md border border-border bg-muted/20 p-3">
            {loading ? (
              <div className="flex h-[132px] items-center justify-center gap-2 text-xs font-mono text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                Đang giải mã sóng âm...
              </div>
            ) : loadError ? (
              <div className="flex h-[132px] items-center justify-center px-4 text-center text-xs text-destructive">
                {loadError}
              </div>
            ) : (
              <canvas
                ref={canvasRef}
                style={{ height: WAVE_HEIGHT }}
                className="w-full cursor-crosshair rounded select-none touch-none"
                onPointerDown={handlePointerDown}
                onPointerMove={handlePointerMove}
                onPointerUp={handlePointerUp}
                onPointerCancel={handlePointerUp}
              />
            )}
            <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
              <span className="font-mono text-[11px] text-muted-foreground">{techLine || "—"}</span>
              <span className="font-mono text-[11px] text-primary">
                Vùng chọn: {formatTime(selection.start)} → {formatTime(selection.end)} (
                {selectionLength.toFixed(2)}s)
              </span>
            </div>
          </div>

          <audio
            ref={audioRef}
            src={fileUrl}
            preload="auto"
            onTimeUpdate={handleTimeUpdate}
            onEnded={stop}
            className="hidden"
          />

          <div className="flex flex-wrap items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={!!loadError || loading}
              onClick={() => (playing ? stop() : playRange(selection.start, selection.end))}
            >
              {playing ? <Square className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
              {playing ? "Dừng" : "Nghe vùng chọn"}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              disabled={!!loadError || loading}
              onClick={() => playRange(0, null)}
            >
              <Play className="h-3.5 w-3.5" />
              Nghe cả file
            </Button>
            <div className="flex items-center gap-1.5">
              <label className="font-mono text-[11px] text-muted-foreground">Từ (s)</label>
              <Input
                type="number"
                step="0.05"
                min={0}
                max={duration || undefined}
                value={selection.start.toFixed(2)}
                onChange={(e) =>
                  setSelection((c) => ({
                    start: Math.min(Math.max(0, Number(e.target.value) || 0), c.end),
                    end: c.end,
                  }))
                }
                className="h-8 w-24 font-mono text-xs"
              />
              <label className="font-mono text-[11px] text-muted-foreground">Đến (s)</label>
              <Input
                type="number"
                step="0.05"
                min={0}
                max={duration || undefined}
                value={selection.end.toFixed(2)}
                onChange={(e) =>
                  setSelection((c) => ({
                    start: c.start,
                    end: Math.max(Math.min(duration, Number(e.target.value) || 0), c.start),
                  }))
                }
                className="h-8 w-24 font-mono text-xs"
              />
            </div>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => setSelection({ start: 0, end: duration })}
              disabled={isFullSelection}
            >
              <RotateCcw className="h-3.5 w-3.5" />
              Chọn lại cả file
            </Button>
          </div>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <fieldset className="rounded-md border border-border p-3">
              <legend className="px-1 text-xs font-bold uppercase tracking-wider text-muted-foreground">
                Làm sạch
              </legend>
              <div className="space-y-2">
                <Toggle
                  checked={trimSilence}
                  onChange={setTrimSilence}
                  label="Xóa khoảng lặng đầu/cuối"
                  hint="Cắt phần im lặng dưới -45 dB ở hai đầu"
                />
                <Toggle
                  checked={denoise}
                  onChange={setDenoise}
                  label="Giảm nhiễu nền"
                  hint="Khử tiếng ồn phòng, quạt, hiss"
                />
                <Toggle
                  checked={highpass}
                  onChange={setHighpass}
                  label="Lọc tiếng ù trầm"
                  hint="Bỏ rung động dưới 85 Hz"
                />
                <Toggle
                  checked={lowpass}
                  onChange={setLowpass}
                  label="Lọc tiếng rít cao"
                  hint="Bỏ dải trên 12 kHz"
                />
                <Toggle
                  checked={normalize}
                  onChange={setNormalize}
                  label="Chuẩn hóa âm lượng"
                  hint="Cân về -16 LUFS, đỉnh -1.5 dB"
                />
              </div>
            </fieldset>

            <fieldset className="rounded-md border border-border p-3">
              <legend className="px-1 text-xs font-bold uppercase tracking-wider text-muted-foreground">
                Tinh chỉnh
              </legend>
              <div className="space-y-3">
                <Slider
                  label="Âm lượng"
                  value={gainDb}
                  min={-24}
                  max={24}
                  step={0.5}
                  suffix="dB"
                  onChange={setGainDb}
                />
                <Slider
                  label="Fade in"
                  value={fadeIn}
                  min={0}
                  max={5}
                  step={0.05}
                  suffix="s"
                  onChange={setFadeIn}
                />
                <Slider
                  label="Fade out"
                  value={fadeOut}
                  min={0}
                  max={5}
                  step={0.05}
                  suffix="s"
                  onChange={setFadeOut}
                />
                <div className="grid grid-cols-2 items-end gap-2">
                  <label className="block text-xs font-medium">
                    Tần số lấy mẫu
                    <select
                      className={`${selectClass} mt-1.5`}
                      value={sampleRate}
                      onChange={(e) => setSampleRate(e.target.value ? Number(e.target.value) : "")}
                    >
                      <option value="">Giữ nguyên</option>
                      {SAMPLE_RATES.map((rate) => (
                        <option key={rate} value={rate}>
                          {rate} Hz
                        </option>
                      ))}
                    </select>
                  </label>
                  <Toggle checked={mono} onChange={setMono} label="Trộn về mono" />
                </div>
              </div>
            </fieldset>
          </div>

          <fieldset className="rounded-md border border-border p-3">
            <legend className="px-1 text-xs font-bold uppercase tracking-wider text-muted-foreground">
              Lưu kết quả
            </legend>
            <div className="space-y-2">
              <label className="flex cursor-pointer items-start gap-2 text-xs">
                <input
                  type="radio"
                  className="mt-0.5 h-3.5 w-3.5 accent-primary"
                  checked={saveAsCopy}
                  onChange={() => setSaveAsCopy(true)}
                />
                <span>
                  <span className="font-semibold">Lưu thành file mới</span>
                  <span className="block text-muted-foreground">Giữ nguyên file gốc để so sánh.</span>
                </span>
              </label>
              {saveAsCopy && (
                <Input
                  value={copyName}
                  onChange={(e) => setCopyName(e.target.value)}
                  placeholder="Tên file mới (không cần phần mở rộng)"
                  className="h-8 text-xs"
                />
              )}
              <label className="flex cursor-pointer items-start gap-2 text-xs">
                <input
                  type="radio"
                  className="mt-0.5 h-3.5 w-3.5 accent-primary"
                  checked={!saveAsCopy}
                  onChange={() => setSaveAsCopy(false)}
                />
                <span>
                  <span className="font-semibold">Ghi đè file gốc</span>
                  <span className="block text-muted-foreground">
                    Các sách đang dùng giọng này sẽ nhận bản đã xử lý ngay.
                  </span>
                </span>
              </label>
            </div>
          </fieldset>

          {applied && (
            <div className="rounded-md border border-emerald-500/40 bg-emerald-500/10 p-3 text-xs">
              <span className="font-semibold">Đã xử lý xong:</span> {applied.join(", ")}
            </div>
          )}
          {applyError && (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-xs text-destructive">
              {applyError}
            </div>
          )}
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={resetOps} disabled={!hasChanges || applying}>
            <RotateCcw className="h-4 w-4" />
            Đặt lại
          </Button>
          <Button variant="outline" onClick={onClose} disabled={applying}>
            Đóng
          </Button>
          <Button
            variant="default"
            onClick={handleApply}
            disabled={!hasChanges || applying || !!loadError || loading}
            title={hasChanges ? undefined : "Chưa chọn thao tác nào để áp dụng"}
          >
            {applying ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : isFullSelection ? (
              <Wand2 className="h-4 w-4" />
            ) : (
              <Scissors className="h-4 w-4" />
            )}
            {applying ? "Đang xử lý..." : "Áp dụng"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Toggle({
  checked,
  onChange,
  label,
  hint,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
  hint?: string;
}) {
  return (
    <label className="flex cursor-pointer items-start gap-2 text-xs">
      <input
        type="checkbox"
        className="mt-0.5 h-4 w-4 shrink-0 cursor-pointer rounded border-input accent-primary"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span>
        <span className="font-medium">{label}</span>
        {hint && <span className="block text-[11px] text-muted-foreground">{hint}</span>}
      </span>
    </label>
  );
}

function Slider({
  label,
  value,
  min,
  max,
  step,
  suffix,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  suffix: string;
  onChange: (value: number) => void;
}) {
  return (
    <label className="block text-xs font-medium">
      <span className="flex items-baseline justify-between gap-2">
        {label}
        <span className="font-mono text-[11px] text-muted-foreground">
          {value > 0 && suffix === "dB" ? "+" : ""}
          {value.toFixed(suffix === "dB" ? 1 : 2)} {suffix}
        </span>
      </span>
      <input
        type="range"
        className="mt-1 w-full accent-primary"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </label>
  );
}
