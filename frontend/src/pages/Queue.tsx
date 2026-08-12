import React, { useEffect, useState } from "react";
import { Check, Copy, FileText, Trash2, RotateCcw, StopCircle, RefreshCw, ListOrdered } from "lucide-react";
import { api, post, Job } from "@/api";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Progress } from "@/components/ui/progress";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Table, TableHeader, TableBody, TableHead, TableRow, TableCell } from "@/components/ui/table";

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
  const [retryingAll, setRetryingAll] = useState(false);
  const [logJob, setLogJob] = useState<Job | null>(null);
  const [logText, setLogText] = useState("");
  const [logLoading, setLogLoading] = useState(false);
  const [logError, setLogError] = useState("");
  const [logCopied, setLogCopied] = useState(false);

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

  async function retryAllFailed() {
    setRetryingAll(true);
    try {
      await post("/queue/jobs/retry-all-failed");
      await load();
    } catch (err) {
      console.error(err);
    } finally {
      setRetryingAll(false);
    }
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

  return (
    <div className="space-y-6">
      <Header
        title="Hàng đợi sản xuất"
        subtitle="Trung tâm điều phối các tác vụ tổng hợp giọng đọc, ghép media và xuất video theo thời gian thực."
        action={
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={retryAllFailed}
              disabled={retryingAll || !jobs?.some((job) => job.status === "failed")}
              className="text-xs text-emerald-700 hover:bg-emerald-50"
            >
              <RotateCcw className="h-3.5 w-3.5" />
              {retryingAll ? "Đang thử lại..." : "Thử lại tất cả"}
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
          {loading && !jobs ? (
            <LoadingState text="Đang kết nối danh sách tác vụ hàng đợi..." />
          ) : !jobs || jobs.length === 0 ? (
            <EmptyState text="Hàng đợi hiện không có tác vụ nào đang chờ." />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-16">MÃ JOB</TableHead>
                  <TableHead>LOẠI</TableHead>
                  <TableHead>GIAI ĐOẠN / SÁCH</TableHead>
                  <TableHead className="w-48">TIẾN ĐỘ</TableHead>
                  <TableHead className="text-center">TRẠNG THÁI</TableHead>
                  <TableHead className="text-right">THAO TÁC</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {jobs.map((job) => (
                  <TableRow
                    key={job.id}
                    tabIndex={0}
                    role="button"
                    aria-label={`Xem log job ${job.id}`}
                    onClick={() => openLog(job)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault();
                        openLog(job);
                      }
                    }}
                    className="cursor-pointer transition-colors hover:bg-primary/[0.04] focus-visible:bg-primary/[0.06] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-primary/40"
                  >
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
          )}
        </CardContent>
      </Card>

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
