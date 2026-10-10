import React, { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { ArrowDown, ArrowUp, ArrowUpDown, ChevronLeft, ChevronRight, ExternalLink, RefreshCw, Search } from "lucide-react";
import { api } from "@/api";
import { Header } from "@/components/common/Header";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableHeader, TableBody, TableHead, TableRow, TableCell } from "@/components/ui/table";

type Package = {
  id: number; book_id: number; book_title: string; batch_id: string; model_id: string;
  status: string; remote_count: number | null; local_count: number; total: number;
  checked_at: string | null; updated_at: string; created_at: string; error_message: string | null;
  kernel_ref: string | null; drive_folder_id: string | null; last_job_id: number | null;
  can_resume: boolean; phase: string | null; next_retry_at: string | null;
};
type Result = {
  items: Package[]; total: number; page: number; total_pages: number;
  books: { id: number; title: string }[]; models: string[];
};
const statuses: Record<string, string> = {
  incomplete: "Chưa hoàn thành", pending: "Đang chờ", running: "Đang xử lý",
  cancelling: "Đang dừng", failed: "Lỗi", cancelled: "Đã hủy", done: "Hoàn thành",
};
const phases: Record<string, string> = {
  checking_drive: "Kiểm tra Drive", uploading: "Đang tải gói lên", pushing: "Tạo version Kaggle",
  running: "Kaggle đang chạy", done: "Đã đối chiếu kết quả",
};
const selectClass = "h-10 w-full rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const formatDate = (value: string | null) => value ? new Date(value).toLocaleString("vi-VN") : "Chưa kiểm tra";

export function KagglePackagesPage() {
  const [params, setParams] = useSearchParams();
  const [data, setData] = useState<Result>();
  const [search, setSearch] = useState(params.get("search") || "");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const sort = params.get("sort") || "updated_at";
  const direction = params.get("direction") || "desc";
  const query = params.toString();

  function change(key: string, value: string) {
    setParams(previous => {
      const next = new URLSearchParams(previous);
      if (value) next.set(key, value); else next.delete(key);
      if (key !== "page") next.set("page", "1");
      return next;
    }, { replace: true });
  }
  useEffect(() => { setSearch(params.get("search") || ""); }, [params.get("search")]);
  useEffect(() => {
    if (search === (params.get("search") || "")) return;
    const timer = setTimeout(() => change("search", search.trim()), 300);
    return () => clearTimeout(timer);
  }, [search, params.get("search")]);
  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    setLoading(true);
    async function load() {
      try {
        const result = await api<Result>(`/api/kaggle/packages?${query}`, { signal: controller.signal });
        if (!stopped) { setData(result); setError(""); }
      } catch (err) {
        if (!stopped) setError(err instanceof Error ? err.message : "Không tải được danh sách gói.");
      } finally {
        if (!stopped) { setLoading(false); timer = setTimeout(load, 5000); }
      }
    }
    void load();
    return () => { stopped = true; controller.abort(); clearTimeout(timer); };
  }, [query, refresh]);

  async function act(item: Package, action: "check" | "resume") {
    setBusy(item.id); setError(""); setNotice("");
    try {
      await api(`/api/kaggle/packages/${item.id}/${action}`, { method: "POST" });
      setNotice(action === "check"
        ? `Gói #${item.id} đã vào hàng đợi kiểm tra Drive và tải file local còn thiếu.`
        : `Gói #${item.id} đã vào hàng đợi. Drive sẽ được kiểm tra trước khi chạy tiếp.`);
      setRefresh(value => value + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Không thực hiện được thao tác.");
    } finally { setBusy(null); }
  }
  function sortBy(key: string) {
    setParams(previous => {
      const next = new URLSearchParams(previous);
      next.set("sort", key); next.set("direction", sort === key && direction === "asc" ? "desc" : "asc");
      next.set("page", "1"); return next;
    }, { replace: true });
  }
  function column(key: string, label: string) {
    const Icon = sort !== key ? ArrowUpDown : direction === "asc" ? ArrowUp : ArrowDown;
    return <TableHead aria-sort={sort === key ? direction === "asc" ? "ascending" : "descending" : "none"}>
      <button className="flex items-center gap-2 whitespace-nowrap rounded-sm py-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" onClick={() => sortBy(key)}>
        {label}<Icon className="h-3.5 w-3.5" aria-hidden="true" />
      </button>
    </TableHead>;
  }

  return <div>
    <Header title="Gói Kaggle CLI" subtitle="Theo dõi kết quả trên Drive, tải file còn thiếu và tiếp tục các gói chưa hoàn thành."
      action={<><Button variant="outline" onClick={() => setRefresh(value => value + 1)} disabled={loading}>
        <RefreshCw className={`mr-2 h-4 w-4 ${loading ? "animate-spin" : ""}`} />Làm mới
      </Button><Button variant="outline" asChild><Link to="/drive#kaggle">Tài khoản</Link></Button></>} />

    <div className="mb-5 grid gap-3 sm:grid-cols-2 xl:grid-cols-[minmax(16rem,2fr)_repeat(3,minmax(10rem,1fr))]">
      <label className="space-y-1.5 text-xs font-medium">Tìm kiếm
        <span className="relative block"><Search aria-hidden="true" className="absolute left-3 top-3 h-4 w-4 text-muted-foreground" />
          <Input className="pl-9" value={search} onChange={event => setSearch(event.target.value)} placeholder="Tên ebook, mã gói, notebook…" />
        </span>
      </label>
      <label className="space-y-1.5 text-xs font-medium">Ebook
        <select className={selectClass} value={params.get("book_id") || ""} onChange={e => change("book_id", e.target.value)}>
          <option value="">Tất cả ebook</option>{data?.books.map(book => <option key={book.id} value={book.id}>{book.title}</option>)}
        </select>
      </label>
      <label className="space-y-1.5 text-xs font-medium">Trạng thái
        <select className={selectClass} value={params.get("status") || ""} onChange={e => change("status", e.target.value)}>
          <option value="">Tất cả trạng thái</option>{Object.entries(statuses).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
      </label>
      <label className="space-y-1.5 text-xs font-medium">Model TTS
        <select className={selectClass} value={params.get("model") || ""} onChange={e => change("model", e.target.value)}>
          <option value="">Tất cả model</option>{data?.models.map(model => <option key={model}>{model}</option>)}
        </select>
      </label>
    </div>
    {error && <div role="alert" className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">{error}</div>}
    {notice && <p role="status" className="mb-4 text-sm">{notice} <Link className="underline underline-offset-4" to="/queue">Xem hàng đợi</Link></p>}
    <div className="overflow-hidden rounded-lg border border-border bg-card" aria-busy={loading}>
      <Table>
        <TableHeader><TableRow>
          {column("id", "Gói")}{column("book_title", "Ebook")}{column("model_id", "Model")}
          {column("status", "Trạng thái")}{column("remote_count", "Drive / result")}
          {column("local_count", "Local")}{column("updated_at", "Cập nhật")}
          <TableHead>Thao tác</TableHead>
        </TableRow></TableHeader>
        <TableBody>
          {data?.items.map(item => <TableRow key={item.id}>
            <TableCell className="min-w-40 align-top"><span className="font-semibold tabular-nums">#{item.id}</span>
              <p className="mt-1 max-w-48 break-all text-xs text-muted-foreground">{item.batch_id}</p>
              <div className="mt-2 flex flex-wrap gap-3 text-xs">
                {item.drive_folder_id && <a className="inline-flex items-center gap-1 underline underline-offset-4" href={`https://drive.google.com/drive/folders/${encodeURIComponent(item.drive_folder_id)}`} target="_blank" rel="noreferrer">Drive<ExternalLink className="h-3 w-3" /></a>}
                {item.kernel_ref && <a className="inline-flex items-center gap-1 underline underline-offset-4" href={`https://www.kaggle.com/code/${item.kernel_ref.split("/").map(encodeURIComponent).join("/")}`} target="_blank" rel="noreferrer">Kaggle<ExternalLink className="h-3 w-3" /></a>}
              </div>
            </TableCell>
            <TableCell className="min-w-44 max-w-64 align-top"><Link className="font-medium hover:underline" to={`/books/${item.book_id}`}>{item.book_title}</Link></TableCell>
            <TableCell className="align-top text-xs">{item.model_id}</TableCell>
            <TableCell className="min-w-44 max-w-72 align-top">
              <span className={`inline-flex rounded-md px-2 py-1 text-xs font-medium ${item.status === "done" ? "bg-primary/10 text-primary" : item.status === "failed" ? "bg-destructive/10 text-destructive" : "bg-muted text-foreground"}`}>{statuses[item.status] || item.status}</span>
              {!item.can_resume && item.phase && <p className="mt-2 text-xs text-muted-foreground">{phases[item.phase] || item.phase}</p>}
              {item.next_retry_at && <p className="mt-2 text-xs text-muted-foreground">Thử lại: {formatDate(item.next_retry_at)}</p>}
              {item.error_message && <details className="mt-2 text-xs text-destructive"><summary className="cursor-pointer">Chi tiết lỗi</summary><p className="mt-1 max-w-64 break-words">{item.error_message}</p></details>}
            </TableCell>
            <TableCell className="min-w-44 align-top"><span className="tabular-nums">{item.remote_count === null ? "Chưa kiểm tra" : `${item.remote_count} / ${item.total} WAV`}</span>
              <p className="mt-1 text-xs text-muted-foreground">{item.checked_at ? formatDate(item.checked_at) : "Kiểm tra để lấy tiến độ thực tế"}</p>
            </TableCell>
            <TableCell className="whitespace-nowrap align-top tabular-nums">{item.local_count} / {item.total} WAV</TableCell>
            <TableCell className="min-w-36 align-top text-xs tabular-nums">{formatDate(item.updated_at)}</TableCell>
            <TableCell className="min-w-48 align-top"><div className="flex flex-col items-stretch gap-2">
              <Button size="sm" variant="outline" disabled={!item.can_resume || busy !== null} onClick={() => act(item, "check")}>{busy === item.id ? "Đang gửi…" : "Kiểm tra & tải thiếu"}</Button>
              {item.status !== "done" && <Button size="sm" disabled={!item.can_resume || busy !== null} onClick={() => act(item, "resume")}>Chạy tiếp</Button>}
            </div></TableCell>
          </TableRow>)}
          {!data?.items.length && <TableRow><TableCell colSpan={8} className="py-14 text-center">
            {loading ? "Đang tải danh sách gói…" : error ? "Không thể tải dữ liệu. Bấm Làm mới để thử lại." : <>
              <p className="font-medium">{query ? "Không có gói phù hợp" : "Chưa có gói Kaggle CLI"}</p>
              <p className="mt-2 text-sm text-muted-foreground">{query ? "Thử thay đổi từ khóa hoặc bộ lọc." : "Chọn các phân đoạn trong ebook và xuất qua Kaggle để tạo gói."}</p>
              <Button variant="outline" className="mt-4" asChild={!query} onClick={query ? () => { setParams({}); setSearch(""); } : undefined}>
                {query ? "Xóa bộ lọc" : <Link to="/books">Mở thư viện ebook</Link>}
              </Button>
            </>}
          </TableCell></TableRow>}
        </TableBody>
      </Table>
      <div className="flex flex-wrap items-center justify-between gap-4 border-t border-border px-4 py-3 text-sm">
        <p className="tabular-nums">{data?.total || 0} gói · Trang {data?.page || 1} / {data?.total_pages || 1}</p>
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-xs">Số dòng<select className={`${selectClass} w-20`} value={params.get("per_page") || "25"} onChange={e => change("per_page", e.target.value)}>
            {[10, 25, 50, 100].map(size => <option key={size}>{size}</option>)}
          </select></label>
          <Button variant="outline" size="icon" aria-label="Trang trước" disabled={loading || !data || data.page <= 1} onClick={() => change("page", String((data?.page || 1) - 1))}><ChevronLeft className="h-4 w-4" /></Button>
          <Button variant="outline" size="icon" aria-label="Trang sau" disabled={loading || !data || data.page >= data.total_pages} onClick={() => change("page", String((data?.page || 1) + 1))}><ChevronRight className="h-4 w-4" /></Button>
        </div>
      </div>
    </div>
    <p className="mt-3 text-xs text-muted-foreground">Tiến độ Drive được tính theo file WAV kết quả. Timeline đi kèm được tải khi có. “Hoàn thành” yêu cầu đủ WAV trên Drive và local.</p>
  </div>;
}
