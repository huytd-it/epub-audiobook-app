import React, { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Plus, BookOpen, Layers, ArrowRight, Trash2 } from "lucide-react";
import { api, Book } from "@/api";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Progress } from "@/components/ui/progress";

export function Books() {
  const [data, setData] = useState<any>();
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api<any>("/api/ui/books")
      .then(setData)
      .catch((err) => console.error(err))
      .finally(() => setLoading(false));
  }, []);

  if (loading || !data) return <LoadingState text="Đang tải danh sách sách..." />;

  const deleteBook = (e: React.MouseEvent, bookId: number) => {
    e.preventDefault();
    if (!confirm("Bạn có chắc chắn muốn xóa sách này? Hành động này không thể hoàn tác.")) return;

    fetch(`/books/${bookId}/delete`, { method: "POST" })
      .then(() => window.location.reload())
      .catch((err) => console.error(err));
  };

  return (
    <div className="space-y-6">
      <Header
        title="Thư viện sách EPUB"
        subtitle="Quản lý bộ sưu tập bản thảo, kiểm tra trạng thái chia patch và tiến độ tổng hợp."
        action={
          <Button asChild variant="accent">
            <Link to="/upload">
              <Plus className="h-4 w-4" />
              Nhập sách mới
            </Link>
          </Button>
        }
      />

      {books.length === 0 ? (
        <EmptyState text="Chưa có bản thảo nào trong thư viện. Vui lòng bấm 'Nhập sách mới' để bắt đầu." />
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {books.map((b: Book) => {
            const patchTotal = b.patches?.total || 0;
            const patchDone = b.patches?.done || 0;
            const percent = patchTotal > 0 ? Math.round((patchDone / patchTotal) * 100) : 0;

            return (
              <Card
                key={b.id}
                className="border-border hover:border-primary/40 transition-all flex flex-col justify-between group"
              >
                <CardHeader className="p-5 pb-3">
                  <div className="flex items-center justify-between gap-2 mb-2">
                    <span className="font-mono text-xs font-bold text-muted-foreground px-2 py-0.5 rounded bg-muted">
                      ID #{String(b.id).padStart(2, "0")}
                    </span>
                    <StatusBadge value={b.status} />
                  </div>
                  <div className="flex items-start justify-between gap-2">
                    <CardTitle className="min-w-0 flex-1">
                      <Link
                        to={`/books/${b.id}`}
                        className="text-base font-bold text-foreground hover:text-primary transition-colors line-clamp-2"
                      >
                        {b.title}
                      </Link>
                    </CardTitle>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="h-8 w-8 shrink-0 text-muted-foreground hover:bg-red-50 hover:text-red-600"
                      title="Xóa sách"
                      onClick={(e) => deleteBook(e, b.id)}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                  <p className="text-xs text-muted-foreground font-mono truncate mt-1">
                    {b.original_filename}
                  </p>
                </CardHeader>

                <CardContent className="p-5 pt-0 space-y-4">
                  <div className="p-3 rounded-md bg-muted/30 border border-border space-y-2">
                    <div className="flex items-center justify-between text-xs font-mono">
                      <span className="text-muted-foreground flex items-center gap-1">
                        <Layers className="h-3.5 w-3.5 text-primary" />
                        Tiến độ Patches
                      </span>
                      <span className="font-semibold text-foreground">
                        {patchDone}/{patchTotal} ({percent}%)
                      </span>
                    </div>
                    <Progress value={percent} className="h-1.5" />
                  </div>

                  <Button asChild variant="outline" className="w-full justify-between text-xs group-hover:border-primary/50">
                    <Link to={`/books/${b.id}`}>
                      <span>Chi tiết bản thảo</span>
                      <ArrowRight className="h-3.5 w-3.5 text-primary" />
                    </Link>
                  </Button>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
