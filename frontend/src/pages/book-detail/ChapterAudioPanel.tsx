import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { CheckCircle2, CircleDashed, FolderOpen, Layers, Play, RefreshCw, Square, Trash2, TriangleAlert } from "lucide-react";
import { api, del, postJson } from "@/api";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { ChapterAudioDetail, ChapterAudioPatch, errorText } from "./types";
import { chapterChunkDir } from "./paths";
import { ChunkPreviewBar, PreviewChunk, useChunkPreview } from "./useChunkPreview";

/** Chunk đã tổng hợp của một chương (khớp text hiện tại) theo thứ tự phát. */
export async function loadChapterPreviewChunks(bookId: string, chapterIndex: number): Promise<PreviewChunk[]> {
  const detail = await api<ChapterAudioDetail>(`/books/${bookId}/chapter-audio/${chapterIndex}`);
  return detail.chunks
    .filter((chunk) => chunk.status === "done" && chunk.url)
    .map((chunk) => ({ index: chunk.index, text: chunk.text, pause_ms: chunk.pause_ms, url: chunk.url! }));
}

/** Reset + xếp hàng TTS các patch phủ những chương này. Chunk theo chương được giữ,
 * nên job chỉ tổng hợp phần thiếu rồi ghép lại. */
export async function rebuildChapterPatches(bookId: string, chapterIndexes: number[]) {
  const result = await postJson<{ queued: number; patches: ChapterAudioPatch[]; skipped_processing: number }>(
    `/books/${bookId}/chapter-audio/rebuild-patches`,
    { chapter_indexes: chapterIndexes }
  );
  const names = result.patches.map((patch) => `#${patch.patch_index + 1}`).join(", ");
  const skipped = result.skipped_processing ? ` Bỏ qua ${result.skipped_processing} patch đang chạy.` : "";
  return result.patches.length
    ? `Đã đưa patch ${names} vào hàng đợi TTS — chỉ tổng hợp chunk còn thiếu rồi ghép lại.${skipped}`
    : `Không có patch nào phủ chương đã chọn để ghép lại.${skipped}`;
}

export function affectedPatchesText(patches: ChapterAudioPatch[]) {
  return patches.length
    ? ` Audio của patch ${patches.map((patch) => `#${patch.patch_index + 1}`).join(", ")} chưa đổi cho tới khi ghép lại.`
    : "";
}

const STATUS_META = {
  done: { label: "Đã có", icon: CheckCircle2, className: "text-emerald-600" },
  stale: { label: "Cũ — text đã đổi", icon: TriangleAlert, className: "text-amber-600" },
  missing: { label: "Chưa có", icon: CircleDashed, className: "text-muted-foreground" },
} as const;

/** Tab Audio của ChapterDialog: bản chuẩn hoá theo chunk, phát tạm, xoá chunk, ghép
 * lại patch phủ chương. */
export function ChapterAudioPanel({
  bookId,
  chapterIndex,
  onMessage,
  onChanged,
}: {
  bookId: string;
  chapterIndex: number;
  onMessage: (message: string) => void;
  /** Kho chunk đổi (xoá / ghép lại) — để bảng Mục lục làm mới cột Audio. */
  onChanged: () => void;
}) {
  const [detail, setDetail] = useState<ChapterAudioDetail>();
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setDetail(await api<ChapterAudioDetail>(`/books/${bookId}/chapter-audio/${chapterIndex}`));
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setLoading(false);
    }
  }, [bookId, chapterIndex, onMessage]);

  useEffect(() => {
    setDetail(undefined);
    void reload();
  }, [reload]);

  const load = useCallback(() => loadChapterPreviewChunks(bookId, chapterIndex), [bookId, chapterIndex]);
  const preview = useChunkPreview({ load, onMessage, resetKey: `${bookId}:${chapterIndex}` });

  const run = async (action: () => Promise<string>) => {
    if (busy) return;
    setBusy(true);
    try {
      preview.stop(true);
      onMessage(await action());
      onChanged();
      await reload();
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setBusy(false);
    }
  };

  const deleteChunk = (index: number) =>
    run(async () => {
      const result = await del(`/books/${bookId}/chapter-audio/${chapterIndex}/chunks/${index}`) as {
        affected_patches: ChapterAudioPatch[];
      };
      return `Đã xoá chunk ${index + 1}.${affectedPatchesText(result.affected_patches)}`;
    });

  const deleteAll = () => {
    if (!window.confirm("Xoá toàn bộ chunk audio của chương này? Lần TTS sau sẽ tổng hợp lại cả chương.")) return;
    void run(async () => {
      const result = await del(`/books/${bookId}/chapter-audio/${chapterIndex}`) as {
        deleted: number;
        affected_patches: ChapterAudioPatch[];
      };
      return `Đã xoá ${result.deleted} chunk.${affectedPatchesText(result.affected_patches)}`;
    });
  };

  const chunks = detail?.chunks || [];
  const done = chunks.filter((chunk) => chunk.status === "done").length;
  const stale = chunks.filter((chunk) => chunk.status === "stale").length;
  const duration = chunks.reduce((sum, chunk) => sum + (chunk.status === "done" ? chunk.duration || 0 : 0), 0);
  const playingIndex = preview.current?.index;

  return (
    <div className="space-y-3">
      <div className="rounded-md border border-border bg-muted/20 p-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0 text-xs">
            <div className="font-semibold">
              {done}/{chunks.length} chunk đã có{stale ? ` · ${stale} cũ` : ""}
              {duration > 0 ? ` · ${Math.round(duration)}s` : ""}
            </div>
            <div className="mt-0.5 font-mono text-[10px] text-muted-foreground">
              TTS: {detail?.engine || "—"} · Voice: {detail?.voice || "—"} · tối đa {detail?.max_chars ?? "—"} ký tự/chunk
            </div>
            <Link
              to={`/media-browser?path=${encodeURIComponent(chapterChunkDir(bookId, chapterIndex))}`}
              className="mt-1 inline-flex items-center gap-1 break-all font-mono text-[10px] text-primary hover:underline"
            >
              <FolderOpen className="h-3 w-3 shrink-0" /> data/books/{detail?.directory || `${bookId}/chapters`}
            </Link>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={!done || preview.state === "loading"}
              onClick={() => void preview.toggle()}
            >
              {preview.state === "playing" ? <Square className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
              {preview.state === "loading" ? "Đang chuẩn bị..." : preview.state === "playing" ? "Dừng phát" : "Phát tạm"}
            </Button>
            <Button size="sm" variant="ghost" disabled={loading} onClick={() => void reload()} title="Làm mới">
              <RefreshCw className={cn("h-3.5 w-3.5", loading && "animate-spin")} />
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="text-red-700 hover:bg-red-50 hover:text-red-800"
              disabled={busy || (!done && !stale && !detail?.orphans)}
              onClick={deleteAll}
            >
              <Trash2 className="h-3.5 w-3.5" /> Xoá tất cả
            </Button>
          </div>
        </div>
        <ChunkPreviewBar preview={preview} />
      </div>

      {detail && detail.patches.length > 0 && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border px-3 py-2 text-xs">
          <span className="flex min-w-0 items-center gap-1.5">
            <Layers className="h-3.5 w-3.5 shrink-0 text-primary" />
            Thuộc patch {detail.patches.map((patch) => `#${patch.patch_index + 1}`).join(", ")}
          </span>
          <Button
            size="sm"
            variant="outline"
            disabled={busy || detail.patches.every((patch) => patch.status === "processing")}
            title="Reset audio/video của patch rồi chạy TTS lại — chunk đã có được dùng lại, chỉ tổng hợp phần thiếu"
            onClick={() => void run(() => rebuildChapterPatches(bookId, [chapterIndex]))}
          >
            <RefreshCw className="h-3.5 w-3.5" /> Ghép lại patch
          </Button>
        </div>
      )}

      {detail?.is_excluded && (
        <div className="rounded-md bg-muted px-3 py-2 text-xs text-muted-foreground">
          Chương đang bị đánh dấu bỏ qua — không được đọc trong patch.
        </div>
      )}

      <div className="space-y-1.5">
        {chunks.map((chunk) => {
          const meta = STATUS_META[chunk.status];
          const Icon = meta.icon;
          return (
            <div
              key={chunk.index}
              className={cn(
                "flex items-start gap-2 rounded-md border px-3 py-2 text-xs",
                playingIndex === chunk.index ? "border-primary/50 bg-primary/5" : "border-border"
              )}
            >
              <span className="w-8 shrink-0 pt-0.5 font-mono text-[10px] text-muted-foreground">#{chunk.index + 1}</span>
              <Icon className={cn("mt-0.5 h-3.5 w-3.5 shrink-0", meta.className)} aria-label={meta.label} />
              <p className="min-w-0 flex-1 whitespace-pre-wrap leading-5">{chunk.text}</p>
              <span className="shrink-0 pt-0.5 font-mono text-[10px] tabular-nums text-muted-foreground">
                {chunk.duration ? `${chunk.duration.toFixed(1)}s` : `${chunk.text.length} kt`}
              </span>
              <div className="flex shrink-0 gap-1">
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-6 px-1.5"
                  disabled={chunk.status !== "done" || preview.state === "loading"}
                  title="Phát từ chunk này"
                  aria-label={`Phát từ chunk ${chunk.index + 1}`}
                  onClick={() =>
                    playingIndex === chunk.index ? preview.stop() : void preview.toggle(chunk.index)
                  }
                >
                  {playingIndex === chunk.index ? <Square className="h-3 w-3" /> : <Play className="h-3 w-3" />}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-6 px-1.5 text-red-700 hover:bg-red-50 hover:text-red-800"
                  disabled={chunk.status === "missing" || busy}
                  title="Xoá chunk — lần TTS sau chỉ tổng hợp lại chunk này"
                  aria-label={`Xoá chunk ${chunk.index + 1}`}
                  onClick={() => void deleteChunk(chunk.index)}
                >
                  <Trash2 className="h-3 w-3" />
                </Button>
              </div>
            </div>
          );
        })}
        {detail && !chunks.length && (
          <div className="rounded-md border border-dashed border-border px-3 py-6 text-center text-xs text-muted-foreground">
            Chương không có nội dung đọc được sau chuẩn hoá.
          </div>
        )}
        {detail && detail.orphans > 0 && (
          <div className="text-[11px] text-muted-foreground">
            {detail.orphans} file chunk thừa từ lần tách trước — sẽ được dọn ở lần TTS tới, hoặc bấm Xoá tất cả.
          </div>
        )}
      </div>
    </div>
  );
}
