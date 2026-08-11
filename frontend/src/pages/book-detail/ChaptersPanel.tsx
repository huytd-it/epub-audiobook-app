import React, { useMemo, useState } from "react";
import { FileText, ListChecks, ScanText, Search, Wand2 } from "lucide-react";
import { Chapter } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { EmptyState } from "@/components/common/Header";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { ChapterReport, ChaptersValidation } from "./types";
import { SectionHead, SeverityTag, TabBar, Tile, fieldClass } from "./parts";

type ChapterFilter = "all" | "error" | "warning" | "title" | "excluded";

export function ChaptersPanel({
  chapters,
  report,
  loading,
  onAnalyze,
  onOpenChapter,
  onOpenNormalize,
}: {
  chapters: Chapter[];
  report?: ChaptersValidation;
  loading: boolean;
  onAnalyze: () => void;
  onOpenChapter: (chapterIndex: number) => void;
  onOpenNormalize: () => void;
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
          {visible.length === 0 ? (
            <EmptyState text={chapters.length === 0 ? "Chưa trích xuất được chương" : "Không tìm thấy chương phù hợp"} />
          ) : (
            <div className="max-h-[32rem] overflow-auto">
              <Table>
                <TableHeader className="sticky top-0 z-10 bg-card">
                  <TableRow>
                    <TableHead className="w-16 pl-4">Ch.</TableHead>
                    <TableHead className="w-16">Số</TableHead>
                    <TableHead>Tiêu đề</TableHead>
                    <TableHead>Vấn đề</TableHead>
                    <TableHead className="text-right">Ký tự</TableHead>
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
                            <TableCell colSpan={6} className="border-y border-dashed border-amber-300 py-1.5 text-center text-[10px] font-medium text-amber-800">
                              Thiếu chương giữa #{chapter.chapter_index} và #{chapter.chapter_index + 1}
                            </TableCell>
                          </TableRow>
                        )}
                        <TableRow className={chapter.is_excluded ? "opacity-50" : undefined}>
                          <TableCell className="py-2 pl-4 font-mono text-xs text-muted-foreground">
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
