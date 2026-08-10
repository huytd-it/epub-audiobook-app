import React, { useMemo, useState } from "react";
import { FileText, Search } from "lucide-react";
import { Chapter } from "@/api";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { EmptyState } from "@/components/common/Header";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { SectionHead, fieldClass } from "./parts";

export function ChaptersPanel({ chapters }: { chapters: Chapter[] }) {
  const [query, setQuery] = useState("");

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return chapters;
    return chapters.filter(
      (chapter) =>
        chapter.title.toLowerCase().includes(needle) || String(chapter.chapter_index + 1).includes(needle)
    );
  }, [chapters, query]);

  const totalChars = useMemo(
    () => chapters.reduce((sum, chapter) => sum + chapter.char_count, 0),
    [chapters]
  );

  return (
    <Card>
      <CardHeader className="gap-4 border-b border-border bg-muted/20">
        <SectionHead
          icon={FileText}
          title={`Mục lục (${chapters.length})`}
          detail={`Chương trích xuất từ EPUB · ${totalChars.toLocaleString("vi-VN")} ký tự.`}
        />
        <div className="relative">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <input
            className={`${fieldClass} pl-9`}
            placeholder="Tìm theo tiêu đề hoặc số chương..."
            value={query}
            onChange={(event) => setQuery(event.target.value)}
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
                  <TableHead>Tiêu đề</TableHead>
                  <TableHead className="pr-4 text-right">Ký tự</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visible.map((chapter) => (
                  <TableRow key={chapter.id} className={chapter.is_excluded ? "opacity-50" : undefined}>
                    <TableCell className="py-2 pl-4 font-mono text-xs text-muted-foreground">
                      {chapter.chapter_index + 1}
                    </TableCell>
                    <TableCell className="max-w-md truncate py-2 text-xs" title={chapter.title}>
                      {chapter.title}
                    </TableCell>
                    <TableCell className="py-2 pr-4 text-right font-mono text-xs">
                      {chapter.char_count.toLocaleString("vi-VN")}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
