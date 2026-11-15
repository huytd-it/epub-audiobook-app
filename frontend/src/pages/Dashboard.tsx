import React, { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { BookOpen, ListOrdered, ArrowRight, Plus, CheckCircle2, ChevronRight, HardDriveDownload, Layers, Cpu, Play, History, BookMarked, Clock3 } from "lucide-react";
import { api, Book, Job } from "@/api";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { Card, CardHeader, CardTitle, CardContent, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Progress } from "@/components/ui/progress";
import { getLastBookId, getRecentBooks, timeAgo } from "@/lib/recentBooks";

export function Dashboard() {
  const [data, setData] = useState<any>();
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api<any>("/api/ui/bootstrap")
      .then((res) => {
        setData(res);
      })
      .catch((err) => console.error(err))
      .finally(() => setLoading(false));
  }, []);

  // recently-worked helper — đọc từ localStorage (do BookDetail ghi mỗi lần mở sách)
  const [recentTick, setRecentTick] = useState(0);
  useEffect(() => {
    const onStorage = () => setRecentTick((n) => n + 1);
    const onFocus = () => setRecentTick((n) => n + 1);
    window.addEventListener("storage", onStorage);
    window.addEventListener("focus", onFocus);
    return () => {
      window.removeEventListener("storage", onStorage);
      window.removeEventListener("focus", onFocus);
    };
  }, []);

  // ── Hooks phải nằm TRƯỚC early return, nếu không sẽ lỗi React #310
  //    (Rendered more hooks than during the previous render) ──
  const recentLocal = useMemo(() => getRecentBooks(), [recentTick, data?.book_count]);

  const bootstrapById = useMemo(() => {
    const m = new Map<number, Book>();
    (data?.books as Book[] | undefined)?.forEach((b) => m.set(b.id, b));
    return m;
  }, [data?.books]);

  const lastId = useMemo(() => getLastBookId(), [recentTick, data?.book_count]);

  const resumeBook: (Book & { _localTitle?: string }) | null = useMemo(() => {
    // khi data chưa về vẫn trả về local để hook order ổn định (render loading sẽ không dùng giá trị này)
    if (!data?.books || !Array.isArray(data.books)) {
      const local = recentLocal[0];
      if (local) return { id: local.id, title: local.title, status: "ready", priority: 0, original_filename: local.filename || "", created_at: local.at } as Book;
      return null;
    }
    if (lastId && bootstrapById.has(lastId)) return bootstrapById.get(lastId)!;
    const local = recentLocal[0];
    if (local && bootstrapById.has(local.id)) return bootstrapById.get(local.id)!;
    if (local) return { id: local.id, title: local.title, status: "ready", priority: 0, original_filename: local.filename || "", created_at: local.at } as Book;
    const newest = (data.books as Book[] | undefined)?.[0];
    return newest || null;
  }, [lastId, bootstrapById, recentLocal, data?.books]);

  const otherRecents = useMemo(() => {
    const ids = new Set([resumeBook?.id]);
    return recentLocal.filter((r) => !ids.has(r.id)).slice(0, 4);
  }, [recentLocal, resumeBook]);

  const taskCount = useMemo(() => {
    if (!data?.queue?.jobs) return 0;
    return Object.values(data.queue.jobs as Record<string, unknown>).reduce<number>(
      (sum, counts) =>
        sum +
        Object.entries(counts as Record<string, unknown>).reduce<number>(
          (s, [status, n]) => s + (["pending", "running"].includes(status) ? Number(n) || 0 : 0),
          0
        ),
      0
    );
  }, [data?.queue]);

  const resumeProgress = useMemo(
    () => (resumeBook?.patches ? Math.round((resumeBook.patches.done / Math.max(resumeBook.patches.total, 1)) * 100) : null),
    [resumeBook]
  );

  if (loading || !data) return <LoadingState text="Đang kết nối trung tâm điều phối..." />;

  return (
    <div className="space-y-6">
      <Header
        title="Bàn làm việc trung tâm"
        subtitle="Tổng quan nhịp độ sản xuất sách nói, tiến độ hàng đợi và kho lưu trữ."
        action={
          <Button asChild variant="accent">
            <Link to="/upload">
              <Plus className="h-4 w-4" />
              Nhập sách mới
            </Link>
          </Button>
        }
      />

      {/* ── QUAY LẠI EBOOK ĐANG LÀM ── */}
      {resumeBook ? (
        <Card className="border-primary/30 overflow-hidden shadow-sm">
          <div className="flex flex-col lg:flex-row">
            {/* Left: hero */}
            <div className="flex-1 p-4 sm:p-5 flex gap-4 min-w-0">
              <div className="hidden sm:flex h-12 w-12 rounded-lg bg-primary text-primary-foreground items-center justify-center shrink-0">
                <BookMarked className="h-6 w-6" />
              </div>
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="inline-flex items-center gap-1.5 rounded-full bg-lime/20 border border-lime/40 px-2.5 py-0.5 text-[11px] font-mono font-semibold text-foreground">
                    <Clock3 className="h-3 w-3" /> TIẾP TỤC LÀM VIỆC
                  </span>
                  <span className="text-[11px] font-mono text-muted-foreground">#{resumeBook.id}</span>
                  <StatusBadge value={resumeBook.status} />
                </div>
                <h3 className="mt-2 text-sm sm:text-base font-bold leading-tight truncate pr-2">{resumeBook.title}</h3>
                <p className="text-[11px] font-mono text-muted-foreground truncate mt-0.5">{resumeBook.original_filename || "—"}</p>
                {resumeBook.patches && (
                  <div className="mt-3 space-y-1.5 max-w-md">
                    <div className="flex justify-between text-[11px] font-mono">
                      <span className="text-muted-foreground">Tiến độ patches</span>
                      <span className="font-semibold text-foreground">
                        {resumeBook.patches.done}/{resumeBook.patches.total} · {resumeProgress}%
                      </span>
                    </div>
                    <Progress value={resumeProgress ?? 0} className="h-2" />
                    <div className="flex gap-3 text-[11px] font-mono">
                      {(resumeBook.patches.active || 0) > 0 && <span className="text-amber-700">đang chạy {resumeBook.patches.active}</span>}
                      {(resumeBook.patches.failed || 0) > 0 && <span className="text-red-600">lỗi {resumeBook.patches.failed}</span>}
                      {(resumeBook.patches.active || 0) === 0 && (resumeBook.patches.failed || 0) === 0 && resumeBook.patches.total > 0 && resumeBook.patches.done < resumeBook.patches.total && (
                        <span className="text-muted-foreground">còn {resumeBook.patches.total - resumeBook.patches.done} patch chờ</span>
                      )}
                    </div>
                  </div>
                )}
              </div>
            </div>
            {/* Right: actions */}
            <div className="flex flex-row lg:flex-col items-center gap-2 p-4 sm:p-5 bg-muted/20 border-t lg:border-t-0 lg:border-l border-border lg:w-[220px] shrink-0">
              <Button asChild className="flex-1 lg:w-full justify-center">
                <Link to={`/books/${resumeBook.id}`}>
                  Quay lại ebook <ArrowRight className="h-4 w-4" />
                </Link>
              </Button>
              <Button asChild variant="outline" size="sm" className="lg:w-full justify-center">
                <Link to="/books">Thư viện</Link>
              </Button>
              <span className="hidden lg:block text-[10px] font-mono text-muted-foreground text-center mt-1">
                Phím tắt: nhấp để tiếp tục
              </span>
            </div>
          </div>

          {/* Recent strip — 1 hàng lịch sử nhanh */}
          {otherRecents.length > 0 && (
            <div className="border-t border-border bg-muted/10 px-4 py-2.5 flex flex-wrap items-center gap-2">
              <span className="inline-flex items-center gap-1 text-[11px] font-mono font-medium text-muted-foreground shrink-0">
                <History className="h-3.5 w-3.5" /> Gần đây:
              </span>
              {otherRecents.map((r) => (
                <Link
                  key={r.id}
                  to={`/books/${r.id}`}
                  className="inline-flex items-center gap-1.5 rounded-full border border-border bg-card px-2.5 py-1 text-xs hover:border-primary/40 hover:bg-accent/10 transition-colors max-w-[220px]"
                  title={r.title}
                >
                  <span className="font-mono text-[11px] text-muted-foreground">#{r.id}</span>
                  <span className="truncate font-medium">{r.title}</span>
                  <span className="text-[10px] font-mono text-muted-foreground shrink-0">{timeAgo(r.at)}</span>
                </Link>
              ))}
              <Link to="/books" className="ml-auto text-[11px] font-mono text-primary hover:underline shrink-0">
                Xem tất cả →
              </Link>
            </div>
          )}
        </Card>
      ) : (
        <Card className="border-dashed p-5 flex flex-col sm:flex-row items-center justify-between gap-3">
          <div className="flex items-center gap-3 min-w-0">
            <div className="h-10 w-10 rounded-lg bg-muted flex items-center justify-center shrink-0">
              <BookMarked className="h-5 w-5 text-muted-foreground" />
            </div>
            <div className="min-w-0">
              <p className="text-sm font-semibold">Chưa có ebook nào đang làm</p>
              <p className="text-xs text-muted-foreground">Nhập EPUB mới hoặc mở một sách trong thư viện để bắt đầu — sẽ hiện ở đây để quay lại nhanh.</p>
            </div>
          </div>
          <div className="flex gap-2 shrink-0">
            <Button asChild variant="accent" size="sm">
              <Link to="/upload"><Plus className="h-4 w-4" /> Nhập sách mới</Link>
            </Button>
            <Button asChild variant="outline" size="sm">
              <Link to="/books">Mở thư viện</Link>
            </Button>
          </div>
        </Card>
      )}

      {/* KPI Stats Grid */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <Card className="border-border">
          <CardHeader className="flex flex-row items-center justify-between pb-2 space-y-0">
            <CardTitle className="text-xs font-mono font-medium text-muted-foreground uppercase">
              Đầu sách trong xưởng
            </CardTitle>
            <BookOpen className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold font-mono">{data.book_count}</div>
            <CardDescription className="text-[11px] mt-1">Sách EPUB đã tải lên</CardDescription>
          </CardContent>
        </Card>

        <Card className="border-border">
          <CardHeader className="flex flex-row items-center justify-between pb-2 space-y-0">
            <CardTitle className="text-xs font-mono font-medium text-muted-foreground uppercase">
              Tác vụ hàng đợi
            </CardTitle>
            <ListOrdered className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold font-mono">{taskCount}</div>
            <CardDescription className="text-[11px] mt-1">Đang & chờ xử lý</CardDescription>
          </CardContent>
        </Card>

        <Card className="border-border">
          <CardHeader className="flex flex-row items-center justify-between pb-2 space-y-0">
            <CardTitle className="text-xs font-mono font-medium text-muted-foreground uppercase">
              Dây chuyền tự động
            </CardTitle>
            <Cpu className="h-4 w-4 text-emerald-600" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold text-emerald-600 flex items-center gap-1.5 font-mono">
              <span className="h-2 w-2 rounded-full bg-emerald-500 animate-pulse" />
              KÍCH HOẠT
            </div>
            <CardDescription className="text-[11px] mt-1">Tách chương & Synthesizer</CardDescription>
          </CardContent>
        </Card>

        <Card className="border-border">
          <CardHeader className="flex flex-row items-center justify-between pb-2 space-y-0">
            <CardTitle className="text-xs font-mono font-medium text-muted-foreground uppercase">
              Tác vụ vừa chạy
            </CardTitle>
            <Layers className="h-4 w-4 text-primary" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold font-mono">{data.jobs?.length || 0}</div>
            <CardDescription className="text-[11px] mt-1">Phiên làm việc gần nhất</CardDescription>
          </CardContent>
        </Card>
      </div>

      {/* Production Pipeline Rail */}
      <Card className="border-border bg-card">
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <div>
              <CardTitle className="text-sm font-bold uppercase tracking-wider font-mono">
                QUY TRÌNH SẢN XUẤT TỰ ĐỘNG
              </CardTitle>
              <CardDescription className="text-xs mt-0.5">
                Các giai đoạn từ văn bản EPUB thô đến video audiobook hoàn chỉnh
              </CardDescription>
            </div>
            <span className="text-[11px] font-mono px-2 py-0.5 rounded bg-lime/20 text-foreground border border-lime/40">
              PIPELINE ACTIVE
            </span>
          </div>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 md:grid-cols-5 gap-3 pt-2">
            {[
              { step: "01", name: "Nhập EPUB", desc: "Tách chương & lọc TOC", icon: HardDriveDownload },
              { step: "02", name: "Chia Patches", desc: "Phân cụm văn bản", icon: Layers },
              { step: "03", name: "Tổng hợp TTS", desc: "Tạo audio giọng đọc", icon: Cpu },
              { step: "04", name: "Ghép Media", desc: "Nhạc nền & hình ảnh", icon: Play },
              { step: "05", name: "Xuất Video", desc: "Đóng gói MP4 thành phẩm", icon: CheckCircle2 },
            ].map((p, idx) => {
              const Icon = p.icon;
              return (
                <div
                  key={p.step}
                  className="relative flex flex-col p-3 rounded-md border border-border bg-background/60 hover:border-primary/50 transition-colors"
                >
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-[10px] font-mono font-bold text-muted-foreground">{p.step}</span>
                    <Icon className="h-4 w-4 text-primary" />
                  </div>
                  <span className="text-xs font-semibold text-foreground">{p.name}</span>
                  <span className="text-[11px] text-muted-foreground mt-0.5">{p.desc}</span>
                  {idx < 4 && (
                    <ChevronRight className="hidden md:block absolute -right-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground/40 z-10" />
                  )}
                </div>
              );
            })}
          </div>
        </CardContent>
      </Card>

      {/* Main Grid: Recent Books & Active Job Stream */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Left Column: Sách gần đây */}
        <Card className="border-border">
          <CardHeader className="flex flex-row items-center justify-between pb-3">
            <div>
              <CardTitle className="text-sm font-bold uppercase tracking-wider font-mono">
                SÁCH GẦN ĐÂY
              </CardTitle>
              <CardDescription className="text-xs">Các bản thảo đang được xử lý trong xưởng</CardDescription>
            </div>
            <Button asChild variant="ghost" size="sm">
              <Link to="/books" className="gap-1 text-xs">
                Xem tất cả <ArrowRight className="h-3.5 w-3.5" />
              </Link>
            </Button>
          </CardHeader>
          <CardContent className="space-y-3">
            {data.books && data.books.length > 0 ? (
              data.books.map((book: Book) => {
                const patchRatio = book.patches
                  ? Math.round((book.patches.done / Math.max(book.patches.total, 1)) * 100)
                  : 0;

                return (
                  <Link
                    key={book.id}
                    to={`/books/${book.id}`}
                    className="flex flex-col p-3 rounded-md border border-border bg-background/50 hover:bg-muted/50 hover:border-primary/30 transition-all space-y-2 group"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <div className="flex items-center gap-2.5 min-w-0">
                        <span className="h-7 w-7 rounded bg-secondary text-secondary-foreground font-mono text-xs font-bold flex items-center justify-center shrink-0">
                          #{book.id}
                        </span>
                        <div className="min-w-0">
                          <h4 className="text-xs font-semibold text-foreground group-hover:text-primary transition-colors truncate">
                            {book.title}
                          </h4>
                          <p className="text-[11px] text-muted-foreground truncate">{book.original_filename}</p>
                        </div>
                      </div>
                      <StatusBadge value={book.status} />
                    </div>

                    {book.patches && (
                      <div className="space-y-1">
                        <div className="flex justify-between text-[10px] font-mono text-muted-foreground">
                          <span>Tiến độ Patches</span>
                          <span>
                            {book.patches.done}/{book.patches.total} ({patchRatio}%)
                          </span>
                        </div>
                        <Progress value={patchRatio} className="h-1.5" />
                      </div>
                    )}
                  </Link>
                );
              })
            ) : (
              <EmptyState text="Chưa có sách nào trong thư viện" />
            )}
          </CardContent>
        </Card>

        {/* Right Column: Nhịp xưởng (Job Stream) */}
        <Card className="border-border">
          <CardHeader className="flex flex-row items-center justify-between pb-3">
            <div>
              <CardTitle className="text-sm font-bold uppercase tracking-wider font-mono">
                NHỊP XƯỞNG SẢN XUẤT
              </CardTitle>
              <CardDescription className="text-xs">Tác vụ đang điều phối trong hệ thống</CardDescription>
            </div>
            <Button asChild variant="ghost" size="sm">
              <Link to="/queue" className="gap-1 text-xs">
                Chi tiết hàng đợi <ArrowRight className="h-3.5 w-3.5" />
              </Link>
            </Button>
          </CardHeader>
          <CardContent className="space-y-3">
            {data.jobs && data.jobs.length > 0 ? (
              data.jobs.map((job: Job) => (
                <div
                  key={job.id}
                  className="flex items-center justify-between p-3 rounded-md border border-border bg-background/50 space-y-1 text-xs"
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <span className="font-mono text-xs font-bold text-primary shrink-0">
                      {job.percent}%
                    </span>
                    <div className="min-w-0">
                      <div className="font-medium text-foreground truncate">{job.job_type}</div>
                      <div className="text-[11px] text-muted-foreground font-mono truncate">
                        Job #{job.id} • {job.phase || "chờ xử lý"}
                      </div>
                    </div>
                  </div>
                  <StatusBadge value={job.status} />
                </div>
              ))
            ) : (
              <EmptyState text="Hàng đợi hiện không có tác vụ nào" />
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
