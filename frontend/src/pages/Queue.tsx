import React, { useEffect, useMemo, useState } from "react";
import { Check, ChevronLeft, ChevronRight, Copy, FileText, Trash2, RotateCcw, StopCircle, RefreshCw, ListOrdered, Settings2 } from "lucide-react";
import { api, post, put, Job } from "@/api";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Progress } from "@/components/ui/progress";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Table, TableHeader, TableBody, TableHead, TableRow, TableCell } from "@/components/ui/table";
import { TabBar, checkboxClass } from "@/pages/book-detail/parts";
import { Input } from "@/components/ui/input";

type StatusFilter = "all" | "pending" | "running" | "failed" | "cancelled" | "done";

const PAGE_SIZE_OPTIONS = [10, 25, 50, 100];
const WORKER_TYPES = [
  ["audiobook_tts", "TTS audiobook"], ["light_tts", "TTS nhẹ"],
  ["flow_audio", "Flow audio"], ["video", "Video sách"],
  ["patch_video", "Video phân đoạn"], ["standalone_video", "Video độc lập"],
  ["flow_video", "Flow video"], ["youtube_upload", "YouTube upload"],
  ["flow_youtube", "Flow YouTube"], ["background_gen", "Tạo ảnh nền"],
] as const;
type WorkerType = typeof WORKER_TYPES[number][0];
type WorkerSettings = { concurrency: Record<WorkerType, number>; min: number; max: number; requires_restart: boolean };

function jobTypeLabel(jobType: string) {
  if (jobType.includes("tts") || jobType === "flow_audio") return "TTS";
  if (jobType.includes("youtube")) return "YouTube";
  if (jobType.includes("video")) return "Video";
  return jobType;
}

export function Queue() {
  const [jobs, setJobs] = useState<Job[]>();
  const [loading, setLoading] = useState(true);
  const [clearing, setClearing] = useState(false);
  const [retryingSelected, setRetryingSelected] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [pageSize, setPageSize] = useState(25);
  const [page, setPage] = useState(1);
  const [logJob, setLogJob] = useState<Job | null>(null);
  const [logText, setLogText] = useState("");
  const [logLoading, setLogLoading] = useState(false);
  const [logError, setLogError] = useState("");
  const [logCopied, setLogCopied] = useState(false);
  const [workerDialogOpen, setWorkerDialogOpen] = useState(false);
  const [workerSettings, setWorkerSettings] = useState<WorkerSettings>();
  const [workerDraft, setWorkerDraft] = useState<Record<WorkerType, string>>();
  const [workerLoading, setWorkerLoading] = useState(false);
  const [workerSaving, setWorkerSaving] = useState(false);
  const [workerError, setWorkerError] = useState("");

  const load = () => {
    return api<{ jobs: Job[] }>("/queue/jobs?limit=200")
      .then((x) => setJobs(x.jobs))
      .catch((err) => console.error(err))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    load();
    const timer = setInterval(load, 3000);
    return () => clearInterval(timer);
  }, []);

  async function act(id: number, action: string) {
    try {
      await post(`/queue/jobs/${id}/${action}`);
      await load();
    } catch (err) {
      console.error(err);
    }
  }

  async function clearOldJobs() {
    setClearing(true);
    try {
      await post("/queue/clear");
      await load();
    } catch (err) {
      console.error(err);
    } finally {
      setClearing(false);
    }
  }

  const statusCounts = useMemo(() => {
    const counts: Record<StatusFilter, number> = { all: jobs?.length || 0, pending: 0, running: 0, failed: 0, cancelled: 0, done: 0 };
    jobs?.forEach((job) => {
      if (job.status in counts && job.status !== "all") counts[job.status as Exclude<StatusFilter, "all">] += 1;
    });
    return counts;
  }, [jobs]);

  const filteredJobs = useMemo(
    () => jobs?.filter((job) => statusFilter === "all" || job.status === statusFilter) || [],
    [jobs, statusFilter]
  );
  const totalPages = Math.max(1, Math.ceil(filteredJobs.length / pageSize));
  const currentPage = Math.min(page, totalPages);
  const visibleJobs = filteredJobs.slice((currentPage - 1) * pageSize, currentPage * pageSize);
  const retryableSelectedIds = jobs
    ?.filter((job) => selectedIds.has(job.id) && ["failed", "cancelled"].includes(job.status))
    .map((job) => job.id) || [];
  const visibleSelectableIds = visibleJobs.filter((job) => ["failed", "cancelled"].includes(job.status)).map((job) => job.id);
  const allVisibleSelected = visibleSelectableIds.length > 0 && visibleSelectableIds.every((id) => selectedIds.has(id));

  useEffect(() => {
    setPage(1);
  }, [statusFilter, pageSize]);

  useEffect(() => {
    if (!jobs) return;
    const retryableIds = new Set(jobs.filter((job) => ["failed", "cancelled"].includes(job.status)).map((job) => job.id));
    setSelectedIds((previous) => new Set([...previous].filter((id) => retryableIds.has(id))));
  }, [jobs]);

  async function retrySelected() {
    const ids = [...retryableSelectedIds];
    if (!ids.length) return;
    setRetryingSelected(true);
    try {
      for (const id of ids) {
        try {
          await post(`/queue/jobs/${id}/retry`);
        } catch (err) {
          console.error(`Không thể thử lại job ${id}`, err);
        }
      }
      setSelectedIds(new Set());
      await load();
    } finally {
      setRetryingSelected(false);
    }
  }

  function toggleSelection(id: number, checked: boolean) {
    setSelectedIds((previous) => {
      const next = new Set(previous);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  }

  function toggleVisibleSelection(checked: boolean) {
    setSelectedIds((previous) => {
      const next = new Set(previous);
      visibleSelectableIds.forEach((id) => checked ? next.add(id) : next.delete(id));
      return next;
    });
  }

  async function openLog(job: Job) {
    setLogJob(job);
    setLogCopied(false);
    await loadLog(job.id);
  }

  async function loadLog(jobId: number) {
    setLogText("");
    setLogError("");
    setLogLoading(true);
    try {
      setLogText(await api<string>(`/queue/jobs/${jobId}/log?tail=1000`));
    } catch (err) {
      setLogError(err instanceof Error ? err.message : "Không tải được log tác vụ");
    } finally {
      setLogLoading(false);
    }
  }

  async function copyLog() {
    await navigator.clipboard.writeText(logText);
    setLogCopied(true);
    window.setTimeout(() => setLogCopied(false), 1500);
  }

  async function openWorkerSettings() {
    setWorkerDialogOpen(true);
    setWorkerLoading(true);
    setWorkerError("");
    try {
      const data = await api<WorkerSettings>("/queue/settings");
      setWorkerSettings(data);
      setWorkerDraft(Object.fromEntries(WORKER_TYPES.map(([type]) => [type, String(data.concurrency[type])])) as Record<WorkerType, string>);
    } catch (err) {
      setWorkerError(err instanceof Error ? err.message : "Không tải được cấu hình worker");
    } finally {
      setWorkerLoading(false);
    }
  }

  async function saveWorkerSettings() {
    if (!workerSettings || !workerDraft) return;
    const concurrency = Object.fromEntries(WORKER_TYPES.map(([type]) => [type, Number(workerDraft[type])])) as Record<WorkerType, number>;
    if (Object.values(concurrency).some((value) => !Number.isInteger(value) || value < workerSettings.min || value > workerSettings.max)) {
      setWorkerError(`Số worker phải là số nguyên từ ${workerSettings.min} đến ${workerSettings.max}.`);
      return;
    }
    setWorkerSaving(true);
    setWorkerError("");
    try {
      const data = await put<WorkerSettings>("/queue/settings", { concurrency });
      setWorkerSettings(data);
      setWorkerDialogOpen(false);
    } catch (err) {
      setWorkerError(err instanceof Error ? err.message : "Không lưu được cấu hình worker");
    } finally {
      setWorkerSaving(false);
    }
  }

  return (
    <div className="space-y-6">
      <Header
        title="Hàng đợi sản xuất"
        subtitle="Trung tâm điều phối các tác vụ tổng hợp giọng đọc, ghép media và xuất video theo thời gian thực."
        action={
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={openWorkerSettings} className="text-xs">
              <Settings2 className="h-3.5 w-3.5" /> Worker
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={retrySelected}
              disabled={retryingSelected || retryableSelectedIds.length === 0}
              className="text-xs text-emerald-700 hover:bg-emerald-50 disabled:text-muted-foreground"
            >
              <RotateCcw className={`h-3.5 w-3.5 ${retryingSelected ? "animate-spin" : ""}`} />
              {retryingSelected ? "Đang thử lại..." : `Thử lại đã chọn${retryableSelectedIds.length ? ` (${retryableSelectedIds.length})` : ""}`}
            </Button>
            <Button variant="outline" size="sm" onClick={load} title="Làm mới">
              <RefreshCw className="h-3.5 w-3.5" />
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={clearOldJobs}
              disabled={clearing}
              className="text-xs text-muted-foreground hover:text-foreground"
            >
              <Trash2 className="h-3.5 w-3.5 text-muted-foreground" />
              {clearing ? "Đang dọn..." : "Dọn tác vụ cũ"}
            </Button>
          </div>
        }
      />

      <Card className="border-border">
        <CardHeader className="pb-3 border-b border-border">
          <div className="flex items-center justify-between">
            <CardTitle className="text-sm font-bold uppercase tracking-wider font-mono flex items-center gap-2">
              <ListOrdered className="h-4 w-4 text-primary" />
              TÁC VỤ SẢN XUẤT ({jobs?.length || 0})
            </CardTitle>
            <span className="text-xs font-mono text-muted-foreground">Tự động làm mới mỗi 3s</span>
          </div>
        </CardHeader>

        <CardContent className="p-0">
          {!!jobs?.length && (
            <div className="border-b border-border px-4 py-3">
              <TabBar<StatusFilter>
                value={statusFilter}
                onChange={setStatusFilter}
                tabs={[
                  { value: "all", label: "Tất cả", badge: statusCounts.all },
                  { value: "pending", label: "Đang chờ", badge: statusCounts.pending },
                  { value: "running", label: "Đang chạy", badge: statusCounts.running },
                  { value: "failed", label: "Thất bại", badge: statusCounts.failed },
                  { value: "cancelled", label: "Đã dừng", badge: statusCounts.cancelled },
                  { value: "done", label: "Hoàn tất", badge: statusCounts.done },
                ]}
              />
            </div>
          )}
          {loading && !jobs ? (
            <LoadingState text="Đang kết nối danh sách tác vụ hàng đợi..." />
          ) : !jobs || jobs.length === 0 ? (
            <EmptyState text="Hàng đợi hiện không có tác vụ nào đang chờ." />
          ) : filteredJobs.length === 0 ? (
            <EmptyState text="Không có tác vụ nào ở trạng thái này." />
          ) : (
            <>
            <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-10 px-3">
                    <input
                      type="checkbox"
                      className={checkboxClass}
                      checked={allVisibleSelected}
                      disabled={visibleSelectableIds.length === 0 || retryingSelected}
                      onChange={(event) => toggleVisibleSelection(event.target.checked)}
                      aria-label="Chọn tất cả tác vụ có thể thử lại trên trang này"
                    />
                  </TableHead>
                  <TableHead className="w-16">MÃ JOB</TableHead>
                  <TableHead>LOẠI</TableHead>
                  <TableHead>GIAI ĐOẠN / SÁCH</TableHead>
                  <TableHead className="w-48">TIẾN ĐỘ</TableHead>
                  <TableHead className="text-center">TRẠNG THÁI</TableHead>
                  <TableHead className="text-right">THAO TÁC</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visibleJobs.map((job) => (
                  <TableRow
                    key={job.id}
                    tabIndex={0}
                    role="button"
                    aria-label={`Xem log job ${job.id}`}
                    onClick={(event) => {
                      if ((event.target as HTMLElement).closest("button, input, select, a")) return;
                      openLog(job);
                    }}
                    onKeyDown={(event) => {
                      if ((event.target as HTMLElement).closest("button, input, select, a")) return;
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        openLog(job);
                      }
                    }}
                    className="cursor-pointer transition-colors hover:bg-primary/[0.04] focus-visible:bg-primary/[0.06] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/40"
                  >
                    <TableCell className="px-3" onClick={(event) => event.stopPropagation()} onKeyDown={(event) => event.stopPropagation()}>
                      <input
                        type="checkbox"
                        className={checkboxClass}
                        checked={selectedIds.has(job.id)}
                        disabled={!['failed', 'cancelled'].includes(job.status) || retryingSelected}
                        onClick={(event) => event.stopPropagation()}
                        onKeyDown={(event) => event.stopPropagation()}
                        onChange={(event) => toggleSelection(job.id, event.target.checked)}
                        aria-label={`Chọn job ${job.id} để thử lại`}
                      />
                    </TableCell>
                    <TableCell className="font-mono text-xs font-bold text-primary">
                      #{job.id}
                    </TableCell>

                    <TableCell className="font-semibold text-xs text-foreground">
                      {jobTypeLabel(job.job_type)}
                    </TableCell>

                    <TableCell className="max-w-md text-xs text-muted-foreground font-mono">
                      <div className="flex min-w-0 items-center gap-1.5 whitespace-nowrap">
                        <span className="shrink-0">
                          {job.phase || "Đang chờ"} • {job.production_name || (job.book_id ? `Sách ${job.book_id}` : "—")}
                        </span>
                        {job.error_message && (
                          <span
                            className="min-w-0 truncate text-[10px] text-red-600"
                            title={job.error_message}
                          >
                            • {job.error_message}
                          </span>
                        )}
                        {job.status === "failed" && job.attempt_count != null && (
                          <span
                            className="shrink-0 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground"
                            title={`Số lần đã thử / tổng lần thử tối đa của job này`}
                          >
                            thử {job.attempt_count}/{job.max_attempts ?? job.attempt_count}
                          </span>
                        )}
                      </div>
                    </TableCell>

                    <TableCell>
                      <div className="space-y-1">
                        <div className="flex justify-between text-[10px] font-mono">
                          <span className="text-muted-foreground">Phần trăm</span>
                          <span className="font-bold text-foreground">{job.percent}%</span>
                        </div>
                        <Progress value={job.percent} className="h-1.5" />
                      </div>
                    </TableCell>

                    <TableCell className="text-center">
                      <StatusBadge value={job.status} />
                    </TableCell>

                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-1" onClick={(event) => event.stopPropagation()} onKeyDown={(event) => event.stopPropagation()}>
                        {["failed", "cancelled"].includes(job.status) && (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => act(job.id, "retry")}
                            className="h-7 text-[11px] px-2 gap-1 text-emerald-700 hover:bg-emerald-50"
                          >
                            <RotateCcw className="h-3 w-3" /> Thử lại
                          </Button>
                        )}
                        {["pending", "running"].includes(job.status) && (
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => act(job.id, "cancel")}
                            className="h-7 text-[11px] px-2 gap-1 text-red-700 hover:bg-red-50"
                          >
                            <StopCircle className="h-3 w-3" /> Dừng
                          </Button>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
            </div>
            <div className="flex flex-col gap-3 border-t border-border px-4 py-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <span>Hiển thị</span>
                <select
                  value={pageSize}
                  onChange={(event) => setPageSize(Number(event.target.value))}
                  className="h-8 rounded-md border border-input bg-background px-2 text-xs text-foreground outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/15"
                  aria-label="Số tác vụ mỗi trang"
                >
                  {PAGE_SIZE_OPTIONS.map((size) => <option key={size} value={size}>{size}</option>)}
                </select>
                <span>mục · {filteredJobs.length} tác vụ</span>
              </div>
              <div className="flex items-center justify-between gap-2 sm:justify-end">
                <span className="font-mono text-xs text-muted-foreground">Trang {currentPage} / {totalPages}</span>
                <div className="flex items-center gap-1">
                  <Button variant="outline" size="sm" className="h-8 w-8 p-0" disabled={currentPage <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))} title="Trang trước" aria-label="Trang trước">
                    <ChevronLeft className="h-4 w-4" />
                  </Button>
                  <Button variant="outline" size="sm" className="h-8 w-8 p-0" disabled={currentPage >= totalPages} onClick={() => setPage((value) => Math.min(totalPages, value + 1))} title="Trang sau" aria-label="Trang sau">
                    <ChevronRight className="h-4 w-4" />
                  </Button>
                </div>
              </div>
            </div>
            </>
          )}
        </CardContent>
      </Card>

      <Dialog open={workerDialogOpen} onOpenChange={setWorkerDialogOpen}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Cấu hình worker theo loại job</DialogTitle>
            <DialogDescription>Giới hạn số job được xử lý đồng thời. Đặt 0 để tắt worker của loại đó.</DialogDescription>
          </DialogHeader>
          {workerLoading ? (
            <LoadingState text="Đang tải cấu hình worker..." />
          ) : workerDraft && workerSettings ? (
            <div className="grid gap-3 py-2 sm:grid-cols-2">
              {WORKER_TYPES.map(([type, label]) => (
                <label key={type} className="flex items-center justify-between gap-4 rounded-md border border-border px-3 py-2">
                  <span className="min-w-0">
                    <span className="block text-sm font-medium">{label}</span>
                    <span className="block truncate font-mono text-[10px] text-muted-foreground">{type}</span>
                  </span>
                  <Input
                    type="number"
                    min={workerSettings.min}
                    max={workerSettings.max}
                    step={1}
                    value={workerDraft[type]}
                    onChange={(event) => setWorkerDraft({ ...workerDraft, [type]: event.target.value })}
                    className="w-20 text-center font-mono"
                    aria-label={`Số worker cho ${label}`}
                  />
                </label>
              ))}
            </div>
          ) : null}
          <div className="rounded-md border border-amber-300/60 bg-amber-50 px-3 py-2 text-xs text-amber-900">
            Cấu hình mới có hiệu lực sau khi khởi động lại backend. Các job đang chạy không bị ảnh hưởng.
          </div>
          {workerError && <p className="text-sm text-red-600">{workerError}</p>}
          <DialogFooter>
            <DialogClose asChild><Button variant="outline" disabled={workerSaving}>Hủy</Button></DialogClose>
            <Button onClick={saveWorkerSettings} disabled={workerLoading || workerSaving || !workerDraft}>
              {workerSaving ? "Đang lưu..." : "Lưu cấu hình"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={!!logJob} onOpenChange={(open) => !open && setLogJob(null)}>
        <DialogContent className="flex h-[min(760px,88vh)] w-[calc(100vw-2rem)] max-w-5xl flex-col gap-0 overflow-hidden p-0">
          <DialogHeader className="border-b border-border bg-muted/25 px-6 py-5 pr-14 text-left">
            <DialogTitle className="flex items-center gap-3 text-base">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-primary/20 bg-primary/10 text-primary">
                <FileText className="h-4 w-4" />
              </span>
              Nhật ký xử lý job #{logJob?.id}
            </DialogTitle>
            <DialogDescription className="pl-12">
              Trace thực thi và lỗi chi tiết để chẩn đoán tác vụ.
            </DialogDescription>
            <div className="flex flex-wrap items-center gap-2 pl-12 pt-2 font-mono text-[11px]">
              <span className="rounded border border-border bg-background px-2 py-1 text-foreground">
                {logJob ? jobTypeLabel(logJob.job_type) : ""}
              </span>
              {logJob?.production_name && (
                <span className="rounded border border-border bg-background px-2 py-1 text-muted-foreground">
                  {logJob.production_name}
                </span>
              )}
              {logJob && <StatusBadge value={logJob.status} />}
            </div>
          </DialogHeader>
          <div className="flex min-h-0 flex-1 flex-col bg-slate-950">
            <div className="flex h-11 shrink-0 items-center justify-between border-b border-slate-800 px-4">
              <span className="font-mono text-[10px] uppercase tracking-wider text-slate-500">1.000 dòng gần nhất</span>
              <div className="flex items-center gap-1">
                <Button variant="ghost" size="sm" onClick={() => logJob && loadLog(logJob.id)} disabled={logLoading} className="h-7 text-[11px] text-slate-300 hover:bg-slate-800 hover:text-white">
                  <RefreshCw className={`h-3 w-3 ${logLoading ? "animate-spin" : ""}`} /> Tải lại
                </Button>
                <Button variant="ghost" size="sm" onClick={copyLog} disabled={!logText} className="h-7 text-[11px] text-slate-300 hover:bg-slate-800 hover:text-white">
                  {logCopied ? <Check className="h-3 w-3 text-emerald-400" /> : <Copy className="h-3 w-3" />}
                  {logCopied ? "Đã chép" : "Sao chép"}
                </Button>
              </div>
            </div>
            <div className="min-h-0 flex-1 overflow-auto p-5">
              {logLoading ? (
                <div className="flex h-full items-center justify-center gap-2 font-mono text-xs text-slate-400">
                  <RefreshCw className="h-3.5 w-3.5 animate-spin" /> Đang tải log...
                </div>
              ) : logError ? (
                <div className="rounded-md border border-red-900/70 bg-red-950/40 p-4 font-mono text-xs text-red-300">{logError}</div>
              ) : (
                <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-5 text-slate-200">
                  {logText || "Job chưa có nội dung log."}
                </pre>
              )}
            </div>
          </div>
          <DialogFooter className="shrink-0 border-t border-border bg-card px-6 py-3">
            <DialogClose asChild>
              <Button variant="outline" size="sm">Đóng</Button>
            </DialogClose>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
