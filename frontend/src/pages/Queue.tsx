import React, { useEffect, useState } from "react";
import { Trash2, RotateCcw, StopCircle, RefreshCw, ListOrdered } from "lucide-react";
import { api, post, Job } from "@/api";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Progress } from "@/components/ui/progress";
import { Table, TableHeader, TableBody, TableHead, TableRow, TableCell } from "@/components/ui/table";

export function Queue() {
  const [jobs, setJobs] = useState<Job[]>();
  const [loading, setLoading] = useState(true);
  const [clearing, setClearing] = useState(false);

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

  return (
    <div className="space-y-6">
      <Header
        title="Hàng đợi sản xuất"
        subtitle="Trung tâm điều phối các tác vụ tổng hợp giọng đọc, ghép media và xuất video theo thời gian thực."
        action={
          <div className="flex items-center gap-2">
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
                  <TableHead>LOẠI TÁC VỤ</TableHead>
                  <TableHead>GIAI ĐOẠN / SÁCH</TableHead>
                  <TableHead className="w-48">TIẾN ĐỘ</TableHead>
                  <TableHead className="text-center">TRẠNG THÁI</TableHead>
                  <TableHead className="text-right">THAO TÁC</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {jobs.map((job) => (
                  <TableRow key={job.id}>
                    <TableCell className="font-mono text-xs font-bold text-primary">
                      #{job.id}
                    </TableCell>

                    <TableCell className="font-semibold text-xs text-foreground">
                      {job.job_type}
                      {job.error_message && (
                        <div className="text-[10px] text-red-600 font-mono mt-0.5 max-w-sm truncate">
                          {job.error_message}
                        </div>
                      )}
                    </TableCell>

                    <TableCell className="text-xs text-muted-foreground font-mono">
                      {job.phase || "Đang chờ"} • Sách {job.book_id || "—"}
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
                      <div className="flex items-center justify-end gap-1">
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
    </div>
  );
}
