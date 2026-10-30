import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Captions, SkipBack, SkipForward } from "lucide-react";
import { Button } from "@/components/ui/button";
import { errorText } from "./types";

/** Một chunk WAV đã tổng hợp, phát tạm theo thứ tự; pause_ms là khoảng nghỉ chèn
 * trước chunk — khớp với kế hoạch ghép audio thật. */
export type PreviewChunk = { index: number; text: string; pause_ms: number; url: string };

export type ChunkPreviewState = "idle" | "loading" | "playing";

function formatPreviewTime(totalSeconds: number) {
  const total = Math.max(0, Math.floor(totalSeconds));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

/** Phát tạm nối nhiều file chunk liên tiếp thay vì tạo file ghép tạm. Dùng chung cho
 * patch (PatchPreviewDialog) và chương (ChapterDialog / bảng Mục lục).
 *
 * `load` trả danh sách chunk đã có (null/[] = chưa có gì để phát); `resetKey` đổi thì
 * quên mọi thứ đã nạp (đóng dialog, đổi patch/chương). */
export function useChunkPreview({
  load,
  onMessage,
  resetKey,
}: {
  load: () => Promise<PreviewChunk[] | null>;
  onMessage: (message: string) => void;
  resetKey: unknown;
}) {
  const [state, setState] = useState<ChunkPreviewState>("idle");
  const [caption, setCaption] = useState("");
  // Progress/seek tính trên tổng thời lượng: durations nạp sẵn qua metadata, pos/time
  // bám theo audio đang phát.
  const [chunks, setChunks] = useState<PreviewChunk[]>([]);
  const [durations, setDurations] = useState<Record<number, number>>({});
  const [pos, setPos] = useState(0);
  const [time, setTime] = useState(0);
  // Mirror đồng bộ của chunks/durations cho engine (tránh đọc state cũ ngay sau load).
  const data = useRef<{ chunks: PreviewChunk[]; durations: Record<number, number> }>({ chunks: [], durations: {} });
  const run = useRef<{ audio: HTMLAudioElement | null; cancelled: boolean } | null>(null);
  const runId = useRef(0);

  const stop = useCallback((fullReset = false) => {
    runId.current += 1;
    const current = run.current;
    if (current?.audio) {
      try {
        current.audio.pause();
      } catch {
        // Bỏ qua lỗi pause trên audio đã hủy.
      }
      current.audio.src = "";
      current.audio = null;
    }
    run.current = null;
    setState("idle");
    setCaption("");
    if (fullReset) {
      data.current = { chunks: [], durations: {} };
      setChunks([]);
      setDurations({});
      setPos(0);
      setTime(0);
    }
    // Dừng tay thì giữ nguyên vị trí thanh tua để bấm phát lại tiếp tục từ đó.
  }, []);

  useEffect(() => () => stop(true), [resetKey, stop]);

  const total = useMemo(
    () => chunks.reduce((sum, chunk) => sum + (durations[chunk.index] || 0), 0),
    [chunks, durations]
  );
  const elapsed = useMemo(() => {
    let acc = 0;
    for (let i = 0; i < pos && i < chunks.length; i++) acc += durations[chunks[i].index] || 0;
    return acc + time;
  }, [chunks, durations, pos, time]);

  const playChunk = useCallback(
    (chunk: PreviewChunk, offset: number) =>
      new Promise<void>((resolve, reject) => {
        const current = run.current;
        if (!current) {
          reject(new Error("Phiên phát đã kết thúc."));
          return;
        }
        const audio = new Audio(chunk.url);
        audio.preload = "auto";
        current.audio = audio;
        const detach = () => {
          audio.removeEventListener("timeupdate", onTimeUpdate);
          audio.removeEventListener("ended", onEnded);
          audio.removeEventListener("error", onError);
        };
        const onTimeUpdate = () => setTime(audio.currentTime);
        const onEnded = () => {
          detach();
          resolve();
        };
        const onError = () => {
          detach();
          reject(new Error(`Không thể phát chunk ${chunk.index + 1}.`));
        };
        audio.addEventListener("timeupdate", onTimeUpdate);
        audio.addEventListener("ended", onEnded);
        audio.addEventListener("error", onError);
        const start = () => {
          try {
            if (offset > 0 && Number.isFinite(audio.duration) && offset < audio.duration) {
              audio.currentTime = offset;
            }
          } catch {
            // Metadata chưa sẵn — phát từ đầu chunk.
          }
          setTime(audio.currentTime || 0);
          audio.play().catch(reject);
        };
        if (audio.readyState >= 1) start();
        else audio.addEventListener("loadedmetadata", start, { once: true });
      }),
    []
  );

  const loop = useCallback(
    async (id: number, list: PreviewChunk[], startPos: number, startOffset: number) => {
      let at = startPos;
      let offset = startOffset;
      const alive = () => Boolean(run.current && !run.current.cancelled && runId.current === id);
      while (at < list.length) {
        if (!alive()) return;
        const chunk = list[at];
        setPos(at);
        // Tua vào giữa chunk thì bỏ khoảng nghỉ đầu chunk đó.
        if (offset <= 0 && chunk.pause_ms) {
          await new Promise((resolve) => window.setTimeout(resolve, chunk.pause_ms));
          if (!alive()) return;
        }
        setCaption(chunk.text);
        try {
          await playChunk(chunk, offset);
        } catch (error) {
          if (alive()) onMessage(errorText(error));
          return;
        }
        at += 1;
        offset = 0;
      }
      if (alive()) stop();
    },
    [playChunk, onMessage, stop]
  );

  /** Nhảy tới chunk/offset bất kỳ: đang phát thì tua tiếp, đang dừng thì phát từ đó. */
  const jump = useCallback(
    (to: number, offset = 0) => {
      const list = data.current.chunks;
      if (!list.length) return;
      const clamped = Math.max(0, Math.min(list.length - 1, to));
      runId.current += 1;
      const current = run.current;
      if (current?.audio) {
        try {
          current.audio.pause();
        } catch {
          // Bỏ qua lỗi pause trên audio đã hủy.
        }
        current.audio.src = "";
        current.audio = null;
      }
      const id = runId.current;
      run.current = { audio: null, cancelled: false };
      setState("playing");
      void loop(id, list, clamped, Math.max(0, offset));
    },
    [loop]
  );

  const seek = useCallback(
    (fraction: number) => {
      const { chunks: list, durations: known } = data.current;
      const sum = list.reduce((acc, chunk) => acc + (known[chunk.index] || 0), 0);
      if (!list.length || sum <= 0) return;
      const target = Math.min(Math.max(fraction, 0), 1) * sum;
      let acc = 0;
      for (let i = 0; i < list.length; i++) {
        const duration = known[list[i].index] || 0;
        if (target < acc + duration || i === list.length - 1) {
          jump(i, Math.max(0, target - acc));
          return;
        }
        acc += duration;
      }
    },
    [jump]
  );

  const step = useCallback(
    (delta: number) => {
      if (!data.current.chunks.length) return;
      jump(pos + delta, 0);
    },
    [pos, jump]
  );

  const reload = useCallback(async () => {
    const list = await load();
    if (!list || !list.length) {
      onMessage("Chưa có chunk audio hoàn thành để phát thử.");
      return null;
    }
    // Nạp sẵn thời lượng từng chunk để thanh tua biết tổng độ dài.
    const known: Record<number, number> = {};
    await Promise.all(
      list.map(
        (chunk) =>
          new Promise<void>((resolve) => {
            const probe = new Audio(chunk.url);
            probe.preload = "metadata";
            const done = () => {
              if (Number.isFinite(probe.duration) && probe.duration > 0) known[chunk.index] = probe.duration;
              probe.src = "";
              resolve();
            };
            probe.addEventListener("loadedmetadata", done, { once: true });
            probe.addEventListener("error", done, { once: true });
            window.setTimeout(done, 8000);
          })
      )
    );
    data.current = { chunks: list, durations: known };
    setChunks(list);
    setDurations(known);
    return { chunks: list, durations: known };
  }, [load, onMessage]);

  /** Phát/dừng. Nạp mới danh sách chunk mỗi lần phát; `fromIndex` (index chunk) phát
   * từ chunk đó, không truyền thì giữ tỉ lệ thanh tua đang đứng. */
  const toggle = useCallback(
    async (fromIndex?: number) => {
      if (state === "loading") return;
      if (state === "playing" && fromIndex === undefined) {
        stop();
        return;
      }
      const keptFraction = total > 0 ? elapsed / total : 0;
      // Đang phát mà chọn chỗ khác (chunk/chương khác): cắt phiên cũ trước khi nạp mới.
      runId.current += 1;
      if (run.current?.audio) {
        try {
          run.current.audio.pause();
        } catch {
          // Bỏ qua lỗi pause trên audio đã hủy.
        }
        run.current.audio.src = "";
      }
      run.current = null;
      setCaption("");
      setState("loading");
      try {
        const loaded = await reload();
        if (!loaded) {
          setState("idle");
          return;
        }
        let startPos = 0;
        let startOffset = 0;
        if (fromIndex !== undefined) {
          startPos = Math.max(0, loaded.chunks.findIndex((chunk) => chunk.index === fromIndex));
        } else {
          const sum = loaded.chunks.reduce((acc, chunk) => acc + (loaded.durations[chunk.index] || 0), 0);
          const target = keptFraction * sum;
          let acc = 0;
          for (let i = 0; i < loaded.chunks.length; i++) {
            const duration = loaded.durations[loaded.chunks[i].index] || 0;
            if (target < acc + duration || i === loaded.chunks.length - 1) {
              startPos = i;
              startOffset = Math.max(0, target - acc);
              break;
            }
            acc += duration;
          }
        }
        runId.current += 1;
        const id = runId.current;
        run.current = { audio: null, cancelled: false };
        setPos(startPos);
        setTime(startOffset);
        setState("playing");
        await loop(id, loaded.chunks, startPos, startOffset);
      } catch (error) {
        onMessage(errorText(error));
        stop();
      }
    },
    [state, total, elapsed, reload, loop, onMessage, stop]
  );

  const current = state === "playing" ? chunks[pos] : undefined;
  return { state, caption, chunks, pos, elapsed, total, current, toggle, stop, step, seek };
}

export type ChunkPreview = ReturnType<typeof useChunkPreview>;

/** Phụ đề + thanh tua của phiên phát tạm. */
export function ChunkPreviewBar({ preview }: { preview: ChunkPreview }) {
  const { state, caption, chunks, pos, elapsed, total, step, seek } = preview;
  return (
    <>
      {caption && (
        <div className="mt-3 rounded bg-background px-3 py-2 text-xs leading-5 text-foreground" aria-live="polite">
          <div className="mb-1 flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
            <Captions className="h-3 w-3" /> Phụ đề
          </div>
          {caption}
        </div>
      )}
      {chunks.length > 0 && (
        <div className="mt-3 space-y-1.5" aria-label="Tua nhanh audio nghe thử">
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="ghost"
              className="h-7 shrink-0 px-1.5"
              disabled={state === "loading" || pos <= 0}
              onClick={() => step(-1)}
              title="Lùi một chunk"
              aria-label="Lùi một chunk"
            >
              <SkipBack className="h-3.5 w-3.5" />
            </Button>
            <input
              type="range"
              min={0}
              max={1000}
              aria-label="Tua tới vị trí"
              className="w-full accent-primary"
              disabled={state === "loading" || total <= 0}
              value={total > 0 ? Math.round((elapsed / total) * 1000) : 0}
              onChange={(event) => seek(Number(event.target.value) / 1000)}
            />
            <Button
              size="sm"
              variant="ghost"
              className="h-7 shrink-0 px-1.5"
              disabled={state === "loading" || pos >= chunks.length - 1}
              onClick={() => step(1)}
              title="Tới một chunk"
              aria-label="Tới một chunk"
            >
              <SkipForward className="h-3.5 w-3.5" />
            </Button>
            <span className="shrink-0 font-mono text-[10px] tabular-nums text-muted-foreground">
              {formatPreviewTime(elapsed)} / {formatPreviewTime(total)}
            </span>
          </div>
          <p className="font-mono text-[10px] text-muted-foreground">
            Chunk {Math.min(pos + 1, chunks.length)}/{chunks.length}
            {state === "idle" ? " · bấm Phát tạm để nghe từ vị trí đã tua" : ""}
          </p>
        </div>
      )}
    </>
  );
}
