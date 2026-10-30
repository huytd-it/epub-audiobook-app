import React, { useCallback, useMemo, useRef, useState } from "react";
import { FileText, ListChecks, Play, RefreshCw, RotateCcw, ScanText, Search, Square, Trash2, Wand2 } from "lucide-react";
import { Chapter, postJson } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { EmptyState } from "@/components/common/Header";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { ChapterAudioPatch, ChapterAudioSummary, ChapterReport, ChaptersValidation, errorText } from "./types";
import { affectedPatchesText, loadChapterPreviewChunks, rebuildChapterPatches } from "./ChapterAudioPanel";
import { ChunkPreviewBar, useChunkPreview } from "./useChunkPreview";
import { SectionHead, SeverityTag, TabBar, Tile, checkboxClass, fieldClass } from "./parts";

type ChapterFilter = "all" | "error" | "warning" | "title" | "excluded";

/** Ô Audio của một chương: số chunk + thời lượng, phát tạm, mở tab Audio để quản lý. */
function ChapterAudioCell({
  summary,
  playing,
  onPlay,
  onOpen,
}: {
  summary?: ChapterAudioSummary;
  playing: boolean;
  onPlay: () => void;
  onOpen: () => void;
}) {
  if (!summary || !summary.chunks) {
    return <span className="text-muted-foreground">—</span>;
  }
  return (
    <div className="flex items-center gap-1">
      <Button
        size="sm"
        variant="ghost"
        className="h-6 px-1.5"
        title={playing ? "Dừng phát" : "Phát tạm chương"}
        aria-label={playing ? "Dừng phát" : "Phát tạm chương"}
        onClick={onPlay}
      >
        {playing ? <Square className="h-3 w-3" /> : <Play className="h-3 w-3" />}
      </Button>
      <button
        className={cn(
          "font-mono text-[11px] tabular-nums hover:underline",
          summary.ready ? "text-emerald-700" : "text-amber-700"
        )}
        title={`${summary.ready ? "Đủ chunk cho nội dung hiện tại" : "Thiếu chunk hoặc nội dung đã đổi"} · ${
          summary.engine || "?"
        } · ${summary.voice || "mặc định"} — mở tab Audio`}
        onClick={onOpen}
      >
        {summary.chunks} chunk
        {summary.duration ? ` · ${Math.round(summary.duration)}s` : ""}
        {summary.ready ? "" : " · chưa đủ"}
      </button>
    </div>
  );
}

export function ChaptersPanel({
  bookId,
  chapters,
  audio,
  onAudioChanged,
  onMessage,
  report,
  loading,
  onAnalyze,
  onOpenChapter,
  onOpenNormalize,
  onReimport,
  canReimport,
}: {
  bookId: string;
  chapters: Chapter[];
  /** Kho chunk TTS theo chương (chapter_index -> trạng thái), xem useChapterAudio. */
  audio: Record<string, ChapterAudioSummary>;
  onAudioChanged: () => void;
  onMessage: (message: string) => void;
  report?: ChaptersValidation;
  loading: boolean;
  onAnalyze: () => void;
  onOpenChapter: (chapterIndex: number, tab?: "audio") => void;
  onOpenNormalize: () => void;
  /** Mở hộp thoại xác nhận xóa mục lục hiện tại và nạp lại từ EPUB gốc. */
  onReimport: () => void;
  /** Sách còn EPUB gốc — không có thì chỉ còn đường upload lại tệp. */
  canReimport: boolean;
}) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<ChapterFilter>("all");

  const reportByIndex = useMemo(() => {
    const map = new Map<number, ChapterReport>();
    for (const item of report?.chapters || []) map.set(item.chapter_index, item);
    return map;
  }, [report]);

  const counts = useMemo(() => {
    const rows = report?.chapters || [];
    return {
      all: chapters.length,
      error: rows.filter((r) => r.severity === "error").length,
      warning: rows.filter((r) => r.severity === "warning").length,
      title: rows.filter((r) => r.title_state !== "canonical").length,
      excluded: chapters.filter((c) => c.is_excluded).length,
    };
  }, [chapters, report]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return chapters.filter((chapter) => {
      if (needle && !chapter.title.toLowerCase().includes(needle) && !String(chapter.chapter_index + 1).includes(needle)) {
        return false;
      }
      if (filter === "all") return true;
      const row = reportByIndex.get(chapter.chapter_index);
      if (filter === "excluded") return chapter.is_excluded;
      if (filter === "error") return row?.severity === "error";
      if (filter === "warning") return row?.severity === "warning";
      if (filter === "title") return row ? row.title_state !== "canonical" : false;
      return true;
    });
  }, [chapters, query, filter, reportByIndex]);

  const totalChars = useMemo(() => chapters.reduce((sum, chapter) => sum + chapter.char_count, 0), [chapters]);

  // Một phiên phát tạm dùng chung cho cả bảng; chương đang phát giữ trong ref để
  // `load` luôn đọc đúng chương vừa bấm.
  const [selected, setSelected] = useState<number[]>([]);
  const [audioBusy, setAudioBusy] = useState(false);
  const [playingChapter, setPlayingChapter] = useState<number>();
  const playingRef = useRef<number | undefined>(undefined);
  const loadPlaying = useCallback(
    () => (playingRef.current === undefined ? Promise.resolve(null) : loadChapterPreviewChunks(bookId, playingRef.current)),
    [bookId]
  );
  const preview = useChunkPreview({ load: loadPlaying, onMessage, resetKey: bookId });
  const audioChapters = useMemo(() => Object.keys(audio).length, [audio]);

  const playChapter = (chapterIndex: number) => {
    if (playingRef.current === chapterIndex && preview.state === "playing") {
      preview.stop();
      return;
    }
    playingRef.current = chapterIndex;
    setPlayingChapter(chapterIndex);
    void preview.toggle(0);
  };

  const toggleSelected = (chapterIndex: number) =>
    setSelected((current) =>
      current.includes(chapterIndex) ? current.filter((index) => index !== chapterIndex) : [...current, chapterIndex]
    );

  const runAudio = async (action: () => Promise<string>) => {
    if (audioBusy) return;
    setAudioBusy(true);
    try {
      preview.stop(true);
      onMessage(await action());
      setSelected([]);
      onAudioChanged();
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setAudioBusy(false);
    }
  };

  const deleteSelectedAudio = () => {
    if (!window.confirm(`Xoá toàn bộ chunk audio của ${selected.length} chương đã chọn?`)) return;
    void runAudio(async () => {
      const result = await postJson<{ deleted: number; affected_patches: ChapterAudioPatch[] }>(
        `/books/${bookId}/chapter-audio/delete`,
        { chapter_indexes: selected }
      );
      return `Đã xoá ${result.deleted} chunk của ${selected.length} chương.${affectedPatchesText(result.affected_patches)}`;
    });
  };
  const numbering = report?.numbering;
  const titles = report?.titles;

  return (
    <div className="space-y-5">
      <Card>
        <CardHeader className="gap-4 border-b border-border bg-muted/20">
          <SectionHead
            icon={FileText}
            title={`Mục lục (${chapters.length})`}
            detail={`Chương trích xuất từ EPUB · ${totalChars.toLocaleString("vi-VN")} ký tự.`}
            action={
              <div className="flex shrink-0 flex-wrap gap-2">
                <Button size="sm" variant="outline" onClick={onAnalyze} disabled={loading}>
                  <ScanText className={cn("h-3.5 w-3.5", loading && "animate-pulse")} /> Phân tích nội dung
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={onOpenNormalize}
                  disabled={!titles || titles.fixable === 0}
                >
                  <Wand2 className="h-3.5 w-3.5" /> Chuẩn hoá tiêu đề{titles?.fixable ? ` (${titles.fixable})` : ""}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={onReimport}
                  disabled={!canReimport}
                  title={
                    canReimport
                      ? "Xóa mục lục hiện tại và đọc lại chương từ EPUB gốc"
                      : "Sách chưa có EPUB gốc để nạp lại"
                  }
                  className="text-red-700 hover:bg-red-50 hover:text-red-800"
                >
                  <RotateCcw className="h-3.5 w-3.5" /> Xóa &amp; nạp lại mục lục
                </Button>
              </div>
            }
          />

          {report && (
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
              <Tile label="Chương" value={report.summary.chapters_total} />
              <Tile
                label="Lỗi"
                value={report.summary.chapters_error}
                tone={report.summary.chapters_error ? "danger" : "good"}
              />
              <Tile
                label="Cảnh báo"
                value={report.summary.chapters_warning}
                tone={report.summary.chapters_warning ? "warn" : undefined}
              />
              <Tile
                label="Sai định dạng"
                value={(titles?.fixable ?? 0) + (titles?.no_name ?? 0) + (titles?.unknown ?? 0)}
                tone={titles && titles.fixable + titles.no_name + titles.unknown ? "warn" : "good"}
              />
              <Tile
                label="Số thiếu"
                value={numbering?.missing_count ?? 0}
                tone={numbering?.missing_count ? "warn" : "good"}
              />
              <Tile
                label="Số trùng"
                value={numbering?.duplicate_count ?? 0}
                tone={numbering?.duplicate_count ? "danger" : "good"}
              />
            </div>
          )}

          {numbering && (
            <div
              className={cn(
                "flex flex-wrap items-center gap-2 rounded-md px-3 py-2 text-xs",
                numbering.is_continuous ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-800"
              )}
            >
              <ListChecks className="h-3.5 w-3.5 shrink-0" />
              {numbering.is_continuous ? (
                <span>
                  Đánh số liên tục: {numbering.first_number ?? "—"} → {numbering.last_number ?? "—"}.
                </span>
              ) : (
                <span>
                  Có bất thường trong đánh số ({numbering.missing_count} thiếu, {numbering.duplicate_count} trùng
                  {numbering.out_of_order_indices.length ? `, ${numbering.out_of_order_indices.length} sai thứ tự` : ""}
                  ).
                </span>
              )}
              {numbering.missing_count > 0 && (
                <div className="flex flex-wrap gap-1">
                  {numbering.missing_numbers.slice(0, 40).map((number) => (
                    <span key={number} className="rounded bg-white/60 px-1.5 py-0.5 font-mono text-[10px]">
                      {number}
                    </span>
                  ))}
                  {numbering.missing_count > 40 && (
                    <span className="text-[10px]">+{numbering.missing_count - 40} nữa</span>
                  )}
                </div>
              )}
            </div>
          )}

          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <div className="relative flex-1">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <input
                className={`${fieldClass} pl-9`}
                placeholder="Tìm theo tiêu đề hoặc số chương..."
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </div>
            <TabBar<ChapterFilter>
              value={filter}
              onChange={setFilter}
              className="bg-background"
              tabs={[
                { value: "all", label: "Tất cả", badge: counts.all },
                { value: "error", label: "Lỗi", badge: counts.error },
                { value: "warning", label: "Cảnh báo", badge: counts.warning },
                { value: "title", label: "Sai định dạng", badge: counts.title },
                { value: "excluded", label: "Bỏ qua", badge: counts.excluded },
              ]}
            />
          </div>
        </CardHeader>

        <CardContent className="p-0">
          {(selected.length > 0 || playingChapter !== undefined) && (
            <div className="space-y-2 border-b border-border bg-muted/20 px-4 py-2.5">
              {selected.length > 0 && (
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="font-medium">{selected.length} chương đã chọn</span>
                  <Button size="sm" variant="ghost" onClick={() => setSelected([])}>
                    Bỏ chọn
                  </Button>
                  <div className="ml-auto flex flex-wrap gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={audioBusy}
                      title="Reset audio/video các patch phủ chương rồi chạy TTS lại — chunk đã có được dùng lại"
                      onClick={() => void runAudio(() => rebuildChapterPatches(bookId, selected))}
                    >
                      <RefreshCw className="h-3.5 w-3.5" /> Ghép lại patch chứa chương
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      className="text-red-700 hover:bg-red-50 hover:text-red-800"
                      disabled={audioBusy || !selected.some((index) => audio[String(index)])}
                      onClick={deleteSelectedAudio}
                    >
                      <Trash2 className="h-3.5 w-3.5" /> Xoá audio
                    </Button>
                  </div>
                </div>
              )}
              {playingChapter !== undefined && (preview.state !== "idle" || preview.chunks.length > 0) && (
                <div className="text-xs">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium">Phát tạm chương {playingChapter + 1}</span>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="h-7"
                      disabled={preview.state === "loading"}
                      onClick={() => void preview.toggle()}
                    >
                      {preview.state === "playing" ? <Square className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
                      {preview.state === "playing" ? "Dừng" : "Phát tiếp"}
                    </Button>
                  </div>
                  <ChunkPreviewBar preview={preview} />
                </div>
              )}
            </div>
          )}
          {visible.length === 0 ? (
            <EmptyState text={chapters.length === 0 ? "Chưa trích xuất được chương" : "Không tìm thấy chương phù hợp"} />
          ) : (
            <div className="max-h-[32rem] overflow-auto">
              <Table>
                <TableHeader className="sticky top-0 z-10 bg-card">
                  <TableRow>
                    <TableHead className="w-8 pl-4">
                      <input
                        type="checkbox"
                        className={checkboxClass}
                        aria-label="Chọn tất cả chương đang hiển thị"
                        checked={visible.length > 0 && visible.every((chapter) => selected.includes(chapter.chapter_index))}
                        onChange={(event) =>
                          setSelected(event.target.checked ? visible.map((chapter) => chapter.chapter_index) : [])
                        }
                      />
                    </TableHead>
                    <TableHead className="w-16">Ch.</TableHead>
                    <TableHead className="w-16">Số</TableHead>
                    <TableHead>Tiêu đề</TableHead>
                    <TableHead>Vấn đề</TableHead>
                    <TableHead className="text-right">Ký tự</TableHead>
                    <TableHead title={audioChapters ? `${audioChapters} chương có chunk audio` : undefined}>Audio</TableHead>
                    <TableHead className="pr-4 text-right">Mức</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {visible.map((chapter) => {
                    const row = reportByIndex.get(chapter.chapter_index);
                    const gapBefore = row?.numbering_flag === "gap_before";
                    return (
                      <React.Fragment key={chapter.id}>
                        {gapBefore && (
                          <TableRow className="border-none bg-amber-50/60">
                            <TableCell colSpan={8} className="border-y border-dashed border-amber-300 py-1.5 text-center text-[10px] font-medium text-amber-800">
                              Thiếu chương giữa #{chapter.chapter_index} và #{chapter.chapter_index + 1}
                            </TableCell>
                          </TableRow>
                        )}
                        <TableRow className={chapter.is_excluded ? "opacity-50" : undefined}>
                          <TableCell className="py-2 pl-4">
                            <input
                              type="checkbox"
                              className={checkboxClass}
                              aria-label={`Chọn chương ${chapter.chapter_index + 1}`}
                              checked={selected.includes(chapter.chapter_index)}
                              onChange={() => toggleSelected(chapter.chapter_index)}
                            />
                          </TableCell>
                          <TableCell className="py-2 font-mono text-xs text-muted-foreground">
                            {chapter.chapter_index + 1}
                          </TableCell>
                          <TableCell
                            className={cn(
                              "py-2 font-mono text-xs",
                              chapter.chapter_no == null ? "text-red-600" : "text-muted-foreground"
                            )}
                          >
                            {chapter.chapter_no ?? "—"}
                          </TableCell>
                          <TableCell className="max-w-md py-2 text-xs">
                            <button
                              className={cn(
                                "flex max-w-full items-center gap-1 truncate text-left hover:underline",
                                row?.severity === "error"
                                  ? "text-red-700"
                                  : row?.severity === "warning"
                                  ? "text-amber-800"
                                  : "text-foreground"
                              )}
                              title={chapter.title}
                              onClick={() => onOpenChapter(chapter.chapter_index)}
                            >
                              {row && row.title_state !== "canonical" && <Wand2 className="h-3 w-3 shrink-0" />}
                              <span className="truncate">{chapter.title || "(không có tiêu đề)"}</span>
                            </button>
                          </TableCell>
                          <TableCell className="py-2 text-xs text-muted-foreground">
                            {row?.issues.length ? (
                              <div className="flex flex-wrap gap-1">
                                {row.issues.map((issue, index) => (
                                  <span
                                    key={`${issue.code}-${index}`}
                                    title={issue.message}
                                    className={cn(
                                      "rounded px-1.5 py-0.5 text-[10px]",
                                      issue.severity === "error"
                                        ? "bg-red-50 text-red-700"
                                        : issue.severity === "warning"
                                        ? "bg-amber-50 text-amber-800"
                                        : "bg-muted text-muted-foreground"
                                    )}
                                  >
                                    {issue.code}
                                  </span>
                                ))}
                              </div>
                            ) : (
                              "—"
                            )}
                          </TableCell>
                          <TableCell className="py-2 text-right font-mono text-xs">
                            {chapter.char_count.toLocaleString("vi-VN")}
                          </TableCell>
                          <TableCell className="py-2 text-xs">
                            <ChapterAudioCell
                              summary={audio[String(chapter.chapter_index)]}
                              playing={playingChapter === chapter.chapter_index && preview.state !== "idle"}
                              onPlay={() => playChapter(chapter.chapter_index)}
                              onOpen={() => onOpenChapter(chapter.chapter_index, "audio")}
                            />
                          </TableCell>
                          <TableCell className="py-2 pr-4 text-right">
                            <SeverityTag value={row?.severity || "ok"} />
                          </TableCell>
                        </TableRow>
                      </React.Fragment>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
