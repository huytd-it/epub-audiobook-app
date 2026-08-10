import React, { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { ArrowLeft, BookOpen, Layers, CheckCircle2, AlertTriangle, FileText } from "lucide-react";
import { api, Book, Patch, Chapter } from "@/api";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Table, TableHeader, TableBody, TableHead, TableRow, TableCell } from "@/components/ui/table";

export function BookDetail() {
  const { id } = useParams();
  const [data, setData] = useState<{
    book: Book;
    patches: Patch[];
    chapters: Chapter[];
    last_error: any;
  }>();
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!id) return;
    api<{ book: Book; patches: Patch[]; chapters: Chapter[]; last_error: any }>(`/api/ui/books/${id}`)
      .then(setData)
      .catch((err) => console.error(err))
      .finally(() => setLoading(false));
  }, [id]);

  if (loading || !data) return <LoadingState text={`Đang mở hồ sơ sách #${id}...`} />;

  const completedPatches = data.patches.filter((p) => p.status === "done").length;

  return (
    <div className="space-y-6">
      {/* Back Button */}
      <Button asChild variant="ghost" size="sm" className="mb-2">
        <Link to="/books" className="gap-1.5 text-xs text-muted-foreground hover:text-foreground">
          <ArrowLeft className="h-3.5 w-3.5" /> Trở lại Thư viện
        </Link>
      </Button>

      <Header
        title={data.book.title}
        subtitle={`Mã sách #${id} • File gốc: ${data.book.original_filename} • Khởi tạo: ${new Date(data.book.created_at).toLocaleString("vi-VN")}`}
        action={<StatusBadge value={data.book.status} />}
      />

      {/* Error alert if any */}
      {data.last_error && (
        <div className="p-4 rounded-lg bg-red-50 border border-red-200 text-red-800 text-xs font-mono flex items-start gap-3">
          <AlertTriangle className="h-4 w-4 shrink-0 text-red-600 mt-0.5" />
          <div>
            <div className="font-bold">LỖI XỬ LÝ GẦN NHẤT:</div>
            <div>{String(data.last_error.detail || data.last_error.message || JSON.stringify(data.last_error))}</div>
          </div>
        </div>
      )}

      {/* Stat Metrics Grid */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <Card className="border-border">
          <CardContent className="p-4 flex items-center gap-4">
            <div className="h-10 w-10 rounded bg-primary/10 text-primary flex items-center justify-center shrink-0">
              <FileText className="h-5 w-5" />
            </div>
            <div>
              <div className="text-xl font-bold font-mono">{data.chapters.length}</div>
              <div className="text-xs text-muted-foreground">Tổng số chương</div>
            </div>
          </CardContent>
        </Card>

        <Card className="border-border">
          <CardContent className="p-4 flex items-center gap-4">
            <div className="h-10 w-10 rounded bg-primary/10 text-primary flex items-center justify-center shrink-0">
              <Layers className="h-5 w-5" />
            </div>
            <div>
              <div className="text-xl font-bold font-mono">{data.patches.length}</div>
              <div className="text-xs text-muted-foreground">Tổng số Patches</div>
            </div>
          </CardContent>
        </Card>

        <Card className="border-border">
          <CardContent className="p-4 flex items-center gap-4">
            <div className="h-10 w-10 rounded bg-emerald-100 text-emerald-700 flex items-center justify-center shrink-0">
              <CheckCircle2 className="h-5 w-5" />
            </div>
            <div>
              <div className="text-xl font-bold font-mono text-emerald-700">{completedPatches}</div>
              <div className="text-xs text-muted-foreground">Patches hoàn tất</div>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Two Column Section: Patches & Table of Contents */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Patches Panel */}
        <Card className="border-border">
          <CardHeader className="pb-3 border-b border-border">
            <div className="flex items-center justify-between">
              <CardTitle className="text-sm font-bold uppercase tracking-wider font-mono">
                DANH SÁCH PATCHES ({data.patches.length})
              </CardTitle>
              <Button asChild variant="outline" size="sm">
                <Link to="/queue" className="text-xs">
                  Xem hàng đợi →
                </Link>
              </Button>
            </div>
          </CardHeader>
          <CardContent className="p-0">
            {data.patches.length === 0 ? (
              <EmptyState text="Chưa khởi tạo patch nào" />
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-12">STT</TableHead>
                    <TableHead>TÊN PATCH</TableHead>
                    <TableHead>ĐOẠN CHUNK</TableHead>
                    <TableHead className="text-right">TRẠNG THÁI</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.patches.map((p) => (
                    <TableRow key={p.id}>
                      <TableCell className="font-mono text-xs font-bold text-muted-foreground">
                        #{p.patch_index + 1}
                      </TableCell>
                      <TableCell className="font-medium text-xs">
                        {p.name || `Patch ${p.patch_index + 1}`}
                        {p.error_message && (
                          <div className="text-[10px] text-red-600 font-mono mt-0.5 truncate max-w-xs">
                            {p.error_message}
                          </div>
                        )}
                      </TableCell>
                      <TableCell className="font-mono text-xs text-muted-foreground">
                        {p.next_chunk_index}/{p.chunk_count}
                      </TableCell>
                      <TableCell className="text-right">
                        <StatusBadge value={p.status} />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>

        {/* Table of Contents (Mục lục) */}
        <Card className="border-border">
          <CardHeader className="pb-3 border-b border-border">
            <CardTitle className="text-sm font-bold uppercase tracking-wider font-mono">
              MỤC LỤC & CHƯƠNG ({data.chapters.length})
            </CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            {data.chapters.length === 0 ? (
              <EmptyState text="Chưa trích xuất được chương" />
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-12">CHƯƠNG</TableHead>
                    <TableHead>TIÊU ĐỀ</TableHead>
                    <TableHead className="text-right">KÝ TỰ</TableHead>
                    <TableHead className="text-right">GHI CHÚ</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.chapters.map((c) => (
                    <TableRow key={c.id}>
                      <TableCell className="font-mono text-xs font-bold text-muted-foreground">
                        {c.chapter_index + 1}
                      </TableCell>
                      <TableCell className="font-medium text-xs text-foreground max-w-xs truncate">
                        {c.title}
                      </TableCell>
                      <TableCell className="font-mono text-xs text-right text-muted-foreground">
                        {c.char_count.toLocaleString("vi-VN")}
                      </TableCell>
                      <TableCell className="text-right">
                        {c.is_excluded ? <StatusBadge value="excluded" /> : <span className="text-[11px] text-muted-foreground font-mono">Bình thường</span>}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
