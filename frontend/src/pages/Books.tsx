import React, { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  Plus,
  Search,
  X,
  LayoutGrid,
  TableIcon,
  ChevronLeft,
  ChevronRight,
  Layers,
  ArrowRight,
  Trash2,
  Calendar,
  FileText,
  SlidersHorizontal,
} from "lucide-react";
import { api, Book } from "@/api";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Progress } from "@/components/ui/progress";
import { Input } from "@/components/ui/input";
import { Table, TableHeader, TableBody, TableHead, TableRow, TableCell } from "@/components/ui/table";

type ViewMode = "grid" | "table";
type SortKey = "newest" | "oldest" | "title_asc" | "title_desc" | "progress_desc" | "progress_asc";

const PAGE_SIZE_OPTIONS: Record<ViewMode, number[]> = {
  grid: [6, 12, 24, 48],
  table: [10, 25, 50, 100],
};

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: "all", label: "Tất cả trạng thái" },
  { value: "ready", label: "Sẵn sàng" },
  { value: "pending", label: "Chờ xử lý" },
  { value: "processing", label: "Đang xử lý" },
  { value: "done", label: "Hoàn thành" },
  { value: "failed", label: "Lỗi" },
];

const SORT_OPTIONS: { value: SortKey; label: string }[] = [
  { value: "newest", label: "Mới nhất" },
  { value: "oldest", label: "Cũ nhất" },
  { value: "title_asc", label: "Tiêu đề A → Z" },
  { value: "title_desc", label: "Tiêu đề Z → A" },
  { value: "progress_desc", label: "Tiến độ cao → thấp" },
  { value: "progress_asc", label: "Tiến độ thấp → cao" },
];

function getProgress(b: Book) {
  const total = b.patches?.total || 0;
  const done = b.patches?.done || 0;
  return total > 0 ? Math.round((done / total) * 100) : 0;
}

function formatDate(value: string) {
  if (!value) return "—";
  try {
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return value;
    return d.toLocaleDateString("vi-VN", { day: "2-digit", month: "2-digit", year: "numeric" });
  } catch {
    return value;
  }
}

function getPageNumbers(current: number, total: number): (number | "...")[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const pages: (number | "...")[] = [1];
  if (current > 3) pages.push("...");
  for (let p = Math.max(2, current - 1); p <= Math.min(total - 1, current + 1); p++) pages.push(p);
  if (current < total - 2) pages.push("...");
  pages.push(total);
  // de-dupe số trang trùng, nhưng giữ cả hai dấu "..." nếu có
  const seen = new Set<number>();
  const out: (number | "...")[] = [];
  for (const v of pages) {
    if (v === "...") out.push(v);
    else if (!seen.has(v)) { seen.add(v); out.push(v); }
  }
  return out;
}

export function Books() {
  const [data, setData] = useState<any>();
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState("");

  // controls
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("all");
  const [sort, setSort] = useState<SortKey>("newest");
  const [view, setView] = useState<ViewMode>(() => {
    try {
      const v = localStorage.getItem("books:view");
      return v === "table" || v === "grid" ? (v as ViewMode) : "grid";
    } catch {
      return "grid";
    }
  });
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState<number>(12);

  // keep pageSize in sync when view switches
  useEffect(() => {
    try {
      localStorage.setItem("books:view", view);
    } catch {}
    setPageSize((prev) => {
      const opts = PAGE_SIZE_OPTIONS[view];
      return opts.includes(prev) ? prev : opts[1] ?? opts[0];
    });
    setPage(1);
  }, [view]);

  useEffect(() => {
    setLoading(true);
    setFetchError("");
    // fetch a large page so client-side search/filter covers the whole library
    api<any>("/api/ui/books?per_page=100&page=1")
      .then(setData)
      .catch((err) => setFetchError(err instanceof Error ? err.message : "Không tải được danh sách"))
      .finally(() => setLoading(false));
  }, []);

  const books: Book[] = data?.items || [];
  const totalBooks = data?.total ?? books.length;

  const hasActiveFilter = q.trim() !== "" || status !== "all" || sort !== "newest";

  const filtered = useMemo(() => {
    let out = [...books];
    const needle = q.trim().toLowerCase();
    if (needle) {
      out = out.filter((b) => {
        const hay = `${b.title} ${b.original_filename} ${String(b.id)}`.toLowerCase();
        return hay.includes(needle);
      });
    }
    if (status !== "all") {
      out = out.filter((b) => (b.status || "").toLowerCase() === status.toLowerCase());
    }
    out.sort((a, b) => {
      switch (sort) {
        case "oldest":
          return new Date(a.created_at).getTime() - new Date(b.created_at).getTime();
        case "title_asc":
          return (a.title || "").localeCompare(b.title || "", "vi");
        case "title_desc":
          return (b.title || "").localeCompare(a.title || "", "vi");
        case "progress_desc":
          return getProgress(b) - getProgress(a);
        case "progress_asc":
          return getProgress(a) - getProgress(b);
        case "newest":
        default:
          return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
      }
    });
    return out;
  }, [books, q, status, sort]);

  // reset page when filters change
  useEffect(() => {
    setPage(1);
  }, [q, status, sort, pageSize]);

  const totalFiltered = filtered.length;
  const totalPages = Math.max(1, Math.ceil(totalFiltered / pageSize));
  const currentPage = Math.min(page, totalPages);
  const start = totalFiltered === 0 ? 0 : (currentPage - 1) * pageSize + 1;
  const end = Math.min(currentPage * pageSize, totalFiltered);
  const visible = filtered.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  const clearFilters = () => {
    setQ("");
    setStatus("all");
    setSort("newest");
  };

  const deleteBook = (e: React.MouseEvent, bookId: number) => {
    e.preventDefault();
    e.stopPropagation();
    if (!confirm("Bạn có chắc chắn muốn xóa sách này? Hành động này không thể hoàn tác.")) return;
    fetch(`/books/${bookId}/delete`, { method: "POST" })
      .then(() => window.location.reload())
      .catch((err) => console.error(err));
  };

  if (loading || !data) {
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
        <LoadingState text="Đang tải danh sách sách..." />
      </div>
    );
  }

  if (fetchError) {
    return (
      <div className="space-y-6">
        <Header title="Thư viện sách EPUB" subtitle="Quản lý bộ sưu tập bản thảo." />
        <Card className="border-destructive/30 bg-destructive/5 p-6 text-sm text-destructive">{fetchError}</Card>
      </div>
    );
  }

  const pageNumbers = getPageNumbers(currentPage, totalPages);

  return (
    <div className="space-y-5">
      <Header
        title="Thư viện sách EPUB"
        subtitle={`Quản lý ${totalBooks} bản thảo · ${totalFiltered !== totalBooks ? `đang hiển thị ${totalFiltered} kết quả` : "kiểm tra trạng thái chia patch và tiến độ tổng hợp."}`}
        action={
          <Button asChild variant="accent">
            <Link to="/upload">
              <Plus className="h-4 w-4" />
              Nhập sách mới
            </Link>
          </Button>
        }
      />

      {/* Controls */}
      <Card className="border-border overflow-hidden">
        <div className="flex flex-col gap-3 p-4 lg:flex-row lg:items-center lg:justify-between">
          {/* Search */}
          <div className="relative flex-1 max-w-xl">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Tìm theo tiêu đề, tên file, ID..."
              className="h-9 pl-9 pr-9 text-sm"
              aria-label="Tìm kiếm sách"
            />
            {q && (
              <button
                type="button"
                onClick={() => setQ("")}
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
                aria-label="Xóa tìm kiếm"
              >
                <X className="h-4 w-4" />
              </button>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {/* Status filter */}
            <div className="flex items-center gap-1.5">
              <SlidersHorizontal className="hidden h-3.5 w-3.5 text-muted-foreground sm:block" />
              <select
                value={status}
                onChange={(e) => setStatus(e.target.value)}
                className="h-9 rounded-md border border-input bg-background px-2.5 text-xs font-medium text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15"
                aria-label="Lọc theo trạng thái"
              >
                {STATUS_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>

            {/* Sort */}
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value as SortKey)}
              className="h-9 rounded-md border border-input bg-background px-2.5 text-xs font-medium text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15"
              aria-label="Sắp xếp"
            >
              {SORT_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>

            {hasActiveFilter && (
              <Button variant="ghost" size="sm" onClick={clearFilters} className="h-9 text-xs">
                <X className="h-3.5 w-3.5" />
                Xóa lọc
              </Button>
            )}

            <div className="ml-1 hidden h-6 w-px bg-border sm:block" />

            {/* View toggle */}
            <div className="flex overflow-hidden rounded-md border border-border p-0.5">
              <button
                type="button"
                onClick={() => setView("grid")}
                aria-pressed={view === "grid"}
                aria-label="Dạng lưới"
                className={`inline-flex h-7 items-center gap-1.5 rounded px-2.5 text-xs font-medium transition-colors ${view === "grid" ? "bg-primary text-primary-foreground shadow-xs" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}
              >
                <LayoutGrid className="h-3.5 w-3.5" />
                Lưới
              </button>
              <button
                type="button"
                onClick={() => setView("table")}
                aria-pressed={view === "table"}
                aria-label="Dạng bảng"
                className={`inline-flex h-7 items-center gap-1.5 rounded px-2.5 text-xs font-medium transition-colors ${view === "table" ? "bg-primary text-primary-foreground shadow-xs" : "text-muted-foreground hover:bg-muted hover:text-foreground"}`}
              >
                <TableIcon className="h-3.5 w-3.5" />
                Bảng
              </button>
            </div>
          </div>
        </div>

        {/* Meta bar */}
        <div className="flex flex-col gap-2 border-t border-border bg-muted/20 px-4 py-2.5 sm:flex-row sm:items-center sm:justify-between">
          <span className="text-xs font-mono text-muted-foreground">
            {totalFiltered === 0
              ? "Không có kết quả phù hợp"
              : `Hiển thị ${start}–${end} / ${totalFiltered} bản thảo${totalFiltered !== totalBooks ? ` (tổng ${totalBooks})` : ""}`}
            {hasActiveFilter && totalFiltered > 0 && (
              <span className="ml-2 hidden sm:inline">· đã lọc</span>
            )}
          </span>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <span className="hidden sm:inline">Hiển thị</span>
            <select
              value={pageSize}
              onChange={(e) => setPageSize(Number(e.target.value))}
              className="h-7 rounded-md border border-input bg-background px-2 text-xs font-medium text-foreground outline-none focus:border-primary focus:ring-2 focus:ring-primary/15"
              aria-label="Số mục mỗi trang"
            >
              {PAGE_SIZE_OPTIONS[view].map((n) => (
                <option key={n} value={n}>
                  {n} / trang
                </option>
              ))}
            </select>
          </div>
        </div>
      </Card>

      {/* Content */}
      {books.length === 0 ? (
        <EmptyState text="Chưa có bản thảo nào trong thư viện. Vui lòng bấm 'Nhập sách mới' để bắt đầu." />
      ) : totalFiltered === 0 ? (
        <Card className="border-dashed p-10 text-center">
          <p className="text-sm font-medium text-foreground">Không tìm thấy bản thảo nào</p>
          <p className="mx-auto mt-1 max-w-md text-xs text-muted-foreground">
            Thử đổi từ khóa, bộ lọc trạng thái hoặc xóa sắp xếp để xem lại toàn bộ thư viện.
          </p>
          <Button variant="outline" size="sm" onClick={clearFilters} className="mt-4">
            Xóa bộ lọc
          </Button>
        </Card>
      ) : view === "grid" ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {visible.map((b: Book) => {
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
                  <p className="text-xs text-muted-foreground font-mono truncate mt-1 flex items-center gap-1">
                    <FileText className="h-3 w-3 shrink-0" />
                    <span className="truncate">{b.original_filename}</span>
                  </p>
                  <p className="text-[11px] font-mono text-muted-foreground flex items-center gap-1 mt-1">
                    <Calendar className="h-3 w-3" />
                    {formatDate(b.created_at)}
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
                    {(b.patches?.active || 0) > 0 || (b.patches?.failed || 0) > 0 ? (
                      <div className="flex gap-2 text-[11px] font-mono">
                        {(b.patches?.active || 0) > 0 && <span className="text-amber-700">đang chạy {b.patches?.active}</span>}
                        {(b.patches?.failed || 0) > 0 && <span className="text-red-600">lỗi {b.patches?.failed}</span>}
                      </div>
                    ) : null}
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
      ) : (
        <Card className="overflow-hidden border-border">
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-20">MÃ</TableHead>
                  <TableHead className="min-w-[280px]">TIÊU ĐỀ / FILE GỐC</TableHead>
                  <TableHead className="w-32 text-center">TRẠNG THÁI</TableHead>
                  <TableHead className="w-48">TIẾN ĐỘ PATCHES</TableHead>
                  <TableHead className="w-28">NGÀY TẠO</TableHead>
                  <TableHead className="w-32 text-right">THAO TÁC</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visible.map((b: Book) => {
                  const patchTotal = b.patches?.total || 0;
                  const patchDone = b.patches?.done || 0;
                  const percent = patchTotal > 0 ? Math.round((patchDone / patchTotal) * 100) : 0;
                  return (
                    <TableRow key={b.id} className="group">
                      <TableCell className="font-mono text-xs font-bold text-muted-foreground">
                        #{String(b.id).padStart(2, "0")}
                      </TableCell>
                      <TableCell className="max-w-[420px]">
                        <Link to={`/books/${b.id}`} className="block min-w-0 hover:text-primary">
                          <span className="block truncate text-sm font-semibold text-foreground group-hover:text-primary">
                            {b.title}
                          </span>
                          <span className="block truncate font-mono text-[11px] text-muted-foreground">
                            {b.original_filename}
                          </span>
                        </Link>
                      </TableCell>
                      <TableCell className="text-center">
                        <StatusBadge value={b.status} />
                      </TableCell>
                      <TableCell>
                        <div className="space-y-1.5">
                          <div className="flex items-center justify-between font-mono text-[11px]">
                            <span className="text-muted-foreground">
                              {patchDone}/{patchTotal}
                            </span>
                            <span className="font-semibold text-foreground">{percent}%</span>
                          </div>
                          <Progress value={percent} className="h-1.5" />
                          <div className="flex gap-2 font-mono text-[10px]">
                            {(b.patches?.active || 0) > 0 && (
                              <span className="text-amber-700">{b.patches?.active} đang chạy</span>
                            )}
                            {(b.patches?.failed || 0) > 0 && (
                              <span className="text-red-600">{b.patches?.failed} lỗi</span>
                            )}
                          </div>
                        </div>
                      </TableCell>
                      <TableCell className="font-mono text-xs text-muted-foreground">{formatDate(b.created_at)}</TableCell>
                      <TableCell className="text-right">
                        <div className="flex items-center justify-end gap-1">
                          <Button asChild variant="ghost" size="sm" className="h-7 text-xs">
                            <Link to={`/books/${b.id}`}>Chi tiết</Link>
                          </Button>
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7 text-muted-foreground hover:bg-red-50 hover:text-red-600"
                            title="Xóa sách"
                            onClick={(e) => deleteBook(e, b.id)}
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        </Card>
      )}

      {/* Pagination */}
      {totalFiltered > 0 && totalPages > 1 && (
        <div className="flex flex-col gap-3 rounded-lg border border-border bg-card px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
          <span className="text-xs font-mono text-muted-foreground">
            Trang {currentPage} / {totalPages} · {totalFiltered} bản thảo
          </span>
          <div className="flex items-center gap-1">
            <Button
              variant="outline"
              size="sm"
              className="h-8 w-8 p-0"
              disabled={currentPage <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
              aria-label="Trang trước"
            >
              <ChevronLeft className="h-4 w-4" />
            </Button>
            {pageNumbers.map((p, idx) =>
              p === "..." ? (
                <span key={`e-${idx}`} className="px-1 text-xs text-muted-foreground">
                  …
                </span>
              ) : (
                <Button
                  key={p}
                  variant={p === currentPage ? "default" : "outline"}
                  size="sm"
                  className="h-8 min-w-8 px-2 text-xs"
                  onClick={() => setPage(p as number)}
                  aria-current={p === currentPage ? "page" : undefined}
                >
                  {p}
                </Button>
              )
            )}
            <Button
              variant="outline"
              size="sm"
              className="h-8 w-8 p-0"
              disabled={currentPage >= totalPages}
              onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              aria-label="Trang sau"
            >
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
