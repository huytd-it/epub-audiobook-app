import React, { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  CheckCircle2,
  Eye,
  FolderOpen,
  ListPlus,
  Play,
  RefreshCw,
  RotateCcw,
  Upload,
  XCircle,
} from "lucide-react";
import { api, post, postForm, InboxProcessResult, InboxStatus } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { ExtendPlan, PlannedPatch, PlannedRangeCheck, ReimportPlan, errorText, PUBLISH_STATUS_LABEL } from "./types";
import { CheckField, Field, SectionHead, Tile, fieldClass } from "./parts";

/** Báo cáo từng patch sau khi xử lý inbox / upload kết quả: nhận audio, hoặc
 * bị từ chối kèm lý do, và trạng thái đưa vào dây chuyền YouTube. */
export function InboxReportList({ report }: { report?: InboxProcessResult }) {
  if (!report) return null;
  const { results, renamed, publish_warning } = report;
  const issues = results.filter((item) => item.status === "error" || item.status === "skipped");
  const okCount = results.filter((item) => item.status === "ok").length;

  return (
    <div className="space-y-3">
      {publish_warning && !report.publish_ready && (
        <div className="flex items-start gap-2 rounded-md bg-amber-50 px-3 py-2 text-[11px] text-amber-900">
          <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
          <span>{publish_warning}</span>
        </div>
      )}

      {results.length > 0 && (
        <div className="overflow-hidden rounded-md border border-border">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-muted/30 px-3 py-2">
            <span className="text-xs font-medium">
              Kết quả từng patch: {okCount} nhận · {issues.length} từ chối / bỏ qua
            </span>
            {report.publish_ready && (
              <span className="text-[10px] font-medium text-emerald-700">
                Auto-upload bật — patch đã nhận sẽ tự đăng lên YouTube
              </span>
            )}
          </div>
          <div className="max-h-72 overflow-auto">
            {results.map((item) => (
              <div
                key={item.filename}
                className="flex flex-col gap-1 border-b border-border px-3 py-2 last:border-0 sm:flex-row sm:items-start sm:gap-3"
              >
                <span className="inline-flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium sm:mt-0.5">
                  {item.status === "ok" ? (
                    <span className="inline-flex items-center gap-1 rounded bg-emerald-50 px-1.5 py-0.5 text-emerald-700">
                      <CheckCircle2 className="h-2.5 w-2.5" /> Đã nhận
                    </span>
                  ) : item.status === "skipped" ? (
                    <span className="inline-flex items-center gap-1 rounded bg-amber-50 px-1.5 py-0.5 text-amber-800">
                      <AlertTriangle className="h-2.5 w-2.5" /> Bỏ qua
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 rounded bg-red-50 px-1.5 py-0.5 text-red-700">
                      <XCircle className="h-2.5 w-2.5" /> Từ chối
                    </span>
                  )}
                </span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                    <span className="break-all font-mono text-[11px] font-medium">{item.filename}</span>
                    {item.patch_index != null && (
                      <span className="text-[10px] text-muted-foreground">
                        Patch #{item.patch_index + 1}
                        {item.patch_name ? ` · ${item.patch_name}` : ""}
                      </span>
                    )}
                  </div>
                  <div className="mt-0.5 text-[11px] text-muted-foreground">
                    {item.status !== "ok"
                      ? item.detail || "không rõ lý do"
                      : `Audio ${item.audio ? "đã cài" : "giữ nguyên"}${
                          item.timeline
                            ? ` · timeline ${item.timeline === "installed" ? "cài mới" : item.timeline === "rejected" ? "bị từ chối" : item.timeline}`
                            : ""
                        }`}
                  </div>
                  {item.publish_status && (
                    <div className="mt-1">
                      <span
                        className={cn(
                          "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium",
                          item.publish_status === "queued"
                            ? "bg-blue-50 text-blue-700"
                            : item.publish_status === "blocked_active_pipeline" || item.publish_status === "enqueue_failed"
                              ? "bg-red-50 text-red-700"
                              : item.publish_status.startsWith("skipped_")
                                ? "bg-muted text-muted-foreground"
                                : "bg-emerald-50 text-emerald-700"
                        )}
                      >
                        {item.publish_status === "queued" && item.job_id != null && (
                          <Link to="/queue" className="underline">#{item.job_id}</Link>
                        )}
                        {PUBLISH_STATUS_LABEL[item.publish_status] || item.publish_status}
                      </span>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {renamed.length > 0 && (
        <details className="text-[11px] text-muted-foreground">
          <summary className="cursor-pointer font-medium text-foreground">
            Đổi tên {renamed.length} file theo tên chuẩn
          </summary>
          <div className="mt-2 space-y-1 pl-1">
            {renamed.map((item) => (
              <div key={`${item.from}-${item.to}`} className="break-all font-mono">
                {item.from} → {item.to}
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}

export function BuildPanel({
  bookId,
  chapterMax,
  failedCount,
  onMessage,
  onRefresh,
  onBusyChange,
}: {
  bookId: string;
  chapterMax: number;
  failedCount: number;
  onMessage: (message: string) => void;
  onRefresh: () => Promise<void> | void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [startChapter, setStartChapter] = useState("0");
  const [endChapter, setEndChapter] = useState("");
  const [patchSize, setPatchSize] = useState("");
  const [forceRebuild, setForceRebuild] = useState(false);
  const [planned, setPlanned] = useState<PlannedPatch[]>([]);
  const [rangeCheck, setRangeCheck] = useState<PlannedRangeCheck>();
  const [building, setBuilding] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [inbox, setInbox] = useState<InboxStatus>();
  const [inboxReport, setInboxReport] = useState<InboxProcessResult>();
  const [processingInbox, setProcessingInbox] = useState(false);

  const [reimportPlan, setReimportPlan] = useState<ReimportPlan>();
  const [extendPlan, setExtendPlan] = useState<ExtendPlan>();
  const [updateChanged, setUpdateChanged] = useState(false);
  const [working, setWorking] = useState(false);

  const loadPlans = useCallback(async () => {
    try {
      const [reimport, extend] = await Promise.all([
        api<ReimportPlan>(`/books/${bookId}/reimport/preview`).catch(() => undefined),
        api<ExtendPlan>(`/books/${bookId}/patches/extend/preview`),
      ]);
      setReimportPlan(reimport);
      setExtendPlan(extend);
    } catch (error) {
      onMessage(errorText(error));
    }
  }, [bookId, onMessage]);

  const loadInbox = useCallback(async () => {
    try {
      setInbox(await api<InboxStatus>(`/books/${bookId}/patches/result-inbox`));
    } catch (error) {
      onMessage(errorText(error));
    }
  }, [bookId, onMessage]);

  useEffect(() => {
    loadPlans();
    loadInbox();
  }, [loadPlans, loadInbox]);

  const openInbox = async () => {
    try {
      const result = await post(`/books/${bookId}/patches/result-inbox/open`) as { path: string };
      setInbox((current) => ({ path: result.path, files: current?.files || [], count: current?.count || 0 }));
      onMessage(`Đã mở folder nhận kết quả: ${result.path}`);
    } catch (error) {
      onMessage(errorText(error));
    }
  };

  const processInbox = async () => {
    setProcessingInbox(true);
    onBusyChange(true);
    try {
      const report = await api<InboxProcessResult>(`/books/${bookId}/patches/result-inbox/process`, {
        method: "POST",
      });
      const problems = report.results.filter((item) => item.status === "error" || item.status === "skipped");
      const installedNote = report.publish_ready
        ? " Auto-upload đang bật — patch đã nhận sẽ chạy tiếp dây chuyền đăng."
        : report.auto_upload
          ? ` Auto-upload bật nhưng chưa sẵn sàng.${report.publish_warning ? ` ${report.publish_warning}` : ""}`
          : "";
      setInboxReport(report);
      onMessage(
        `Đã xử lý ${report.installed} audio, đổi tên ${report.renamed.length} file.${installedNote}${
          problems.length ? ` ${problems.length} mục bị từ chối — xem chi tiết bên dưới.` : ""
        }`
      );
      await Promise.all([loadInbox(), onRefresh()]);
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setProcessingInbox(false);
      onBusyChange(false);
    }
  };

  const runIncremental = async (action: () => Promise<string>) => {
    setWorking(true);
    onBusyChange(true);
    try {
      onMessage(await action());
      await Promise.all([loadPlans(), onRefresh()]);
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setWorking(false);
      onBusyChange(false);
    }
  };

  const reimportStored = () =>
    runIncremental(async () => {
      const form = new FormData();
      form.set("update_changed", updateChanged ? "true" : "false");
      const result = await postForm<{ inserted: number; updated: number; skipped_changed: number }>(
        `/books/${bookId}/reimport`,
        form
      );
      return `Đã thêm ${result.inserted} chương mới, cập nhật ${result.updated} chương${
        result.skipped_changed ? `, bỏ qua ${result.skipped_changed} chương đã có audio` : ""
      }.`;
    });

  const reimportUpload = (files: FileList | null) => {
    if (!files?.length) return;
    return runIncremental(async () => {
      const form = new FormData();
      form.set("epub_file", files[0]);
      form.set("update_changed", updateChanged ? "true" : "false");
      const result = await postForm<{ inserted: number; updated: number; skipped_changed: number }>(`/books/${bookId}/reimport`, form);
      return `Đã nạp EPUB mới: thêm ${result.inserted} chương, cập nhật ${result.updated} chương${
        result.skipped_changed ? `, bỏ qua ${result.skipped_changed} chương đã có audio` : ""
      }.`;
    });
  };

  const extendPatches = () =>
    runIncremental(async () => {
      const result = await post(`/books/${bookId}/patches/extend`);
      const created = (result as { created: number }).created;
      return created ? `Đã tạo thêm ${created} patch cho các chương mới.` : "Không có chương nào cần tạo patch.";
    });

  const params = () => {
    const query = new URLSearchParams({ start_chapter: startChapter });
    if (endChapter) query.set("end_chapter", endChapter);
    if (patchSize) query.set("patch_size", patchSize);
    return query;
  };

  const preview = async () => {
    try {
      // Xem trước và soát khoảng chương cùng lúc: cảnh báo phải hiện ngay lúc thêm mới,
      // chứ không phải đợi patch được tạo rồi mới biết là lệch.
      const [result, check] = await Promise.all([
        api<{ patches: PlannedPatch[] }>(`/books/${bookId}/patches/auto-build/preview?${params()}`),
        api<PlannedRangeCheck>(`/books/${bookId}/patches/auto-build/range-check?${params()}`).catch(
          () => undefined
        ),
      ]);
      setPlanned(result.patches);
      setRangeCheck(check);
      if (!result.patches.length) onMessage("Không có chương nào khớp khoảng đã chọn.");
    } catch (error) {
      onMessage(errorText(error));
      setPlanned([]);
      setRangeCheck(undefined);
    }
  };

  const build = async () => {
    setBuilding(true);
    onBusyChange(true);
    try {
      const form = new FormData();
      form.set("start_chapter", startChapter);
      if (endChapter) form.set("end_chapter", endChapter);
      if (patchSize) form.set("patch_size", patchSize);
      if (forceRebuild) form.set("force", "true");
      await post(`/books/${bookId}/patches/auto-build`, form);
      setPlanned([]);
      onMessage("Đã tạo patch.");
      await onRefresh();
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setBuilding(false);
      onBusyChange(false);
    }
  };

  const retryFailed = async () => {
    onBusyChange(true);
    try {
      await post(`/books/${bookId}/patches/retry-failed`);
      onMessage("Đã đưa các patch lỗi vào hàng đợi.");
      await onRefresh();
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      onBusyChange(false);
    }
  };

  const uploadResults = async (files: FileList | null) => {
    if (!files?.length) return;
    setUploading(true);
    onBusyChange(true);
    try {
      const form = new FormData();
      Array.from(files).forEach((file) => form.append("files", file));
      const report = await postForm<InboxProcessResult>(`/books/${bookId}/patches/upload-results`, form);
      const problems = report.results.filter((item) => item.status === "error" || item.status === "skipped");
      setInboxReport(report);
      onMessage(
        `Đã nhận ${report.installed} kết quả audio.${
          problems.length ? ` ${problems.length} mục bị từ chối — xem chi tiết bên dưới.` : ""
        }`
      );
      await onRefresh();
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setUploading(false);
      onBusyChange(false);
    }
  };

  return (
    <div className="space-y-5">
      <Card>
        <CardHeader className="border-b border-border bg-muted/20">
          <SectionHead
            icon={RefreshCw}
            title="Xây dựng patch"
            detail="Chia nội dung theo chương rồi đưa vào hàng đợi tổng hợp."
            action={
              <Button asChild variant="link" className="text-xs">
                <Link to="/queue">Hàng đợi →</Link>
              </Button>
            }
          />
        </CardHeader>

        <CardContent className="space-y-5 pt-5">
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
            <Field label="Chương bắt đầu" hint={`0–${chapterMax}`}>
              <input
                className={fieldClass}
                type="number"
                min="0"
                max={chapterMax}
                value={startChapter}
                onChange={(event) => setStartChapter(event.target.value)}
              />
            </Field>
            <Field label="Chương kết thúc" hint={`Mặc định ${chapterMax}`}>
              <input
                className={fieldClass}
                type="number"
                min="0"
                max={chapterMax}
                placeholder={String(chapterMax)}
                value={endChapter}
                onChange={(event) => setEndChapter(event.target.value)}
              />
            </Field>
            <Field label="Chương / patch" hint="Tự động">
              <input
                className={fieldClass}
                type="number"
                min="1"
                placeholder="Tự động"
                value={patchSize}
                onChange={(event) => setPatchSize(event.target.value)}
              />
            </Field>
          </div>

          <div className="flex flex-wrap gap-2">
            <Button variant="outline" onClick={preview}>
              <Eye className="h-4 w-4" /> Xem trước
            </Button>
            <Button onClick={build} disabled={building}>
              {building ? "Đang xây dựng..." : "Build patch"}
            </Button>
            <CheckField
              label="Force rebuild"
              checked={forceRebuild}
              onChange={setForceRebuild}
            />
            {failedCount > 0 && (
              <Button variant="outline" onClick={retryFailed}>
                <RotateCcw className="h-4 w-4" /> Retry lỗi ({failedCount})
              </Button>
            )}
          </div>

          {planned.length > 0 && (
            <div className="overflow-hidden rounded-md border border-border">
              <div className="flex items-center justify-between border-b border-border bg-muted/40 px-3 py-2">
                <span className="text-xs font-medium">Xem trước: {planned.length} patch</span>
                <button className="text-xs text-muted-foreground hover:text-foreground" onClick={() => setPlanned([])}>
                  Ẩn
                </button>
              </div>

              {rangeCheck && rangeCheck.issues.length > 0 && (
                <div
                  className={cn(
                    "space-y-1 border-b border-border px-3 py-2 text-xs",
                    rangeCheck.has_error ? "bg-red-50 text-red-800" : "bg-amber-50 text-amber-900"
                  )}
                >
                  {rangeCheck.issues.map((issue, index) => (
                    <div key={`${issue.code}-${index}`} className="flex items-start gap-1.5">
                      <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                      <span>{issue.message}</span>
                    </div>
                  ))}
                </div>
              )}

              <div className="max-h-64 overflow-auto">
                <Table>
                  <TableHeader className="sticky top-0 bg-card">
                    <TableRow>
                      <TableHead>#</TableHead>
                      <TableHead>Chương</TableHead>
                      <TableHead>Số chương</TableHead>
                      <TableHead>Tên</TableHead>
                      <TableHead className="text-right">Chunk</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {planned.map((item) => (
                      <TableRow key={item.patch_index}>
                        <TableCell className="py-2 font-mono text-xs">#{item.patch_index + 1}</TableCell>
                        <TableCell className="py-2 text-xs">
                          {item.chapter_start + 1}–{item.chapter_end + 1}
                        </TableCell>
                        <TableCell
                          className={cn(
                            "py-2 font-mono text-xs",
                            item.chapter_no_start == null ? "text-amber-700" : "text-muted-foreground"
                          )}
                        >
                          {item.chapter_no_start == null
                            ? "—"
                            : `${item.chapter_no_start}–${item.chapter_no_end}`}
                        </TableCell>
                        <TableCell className="py-2 text-xs">{item.name}</TableCell>
                        <TableCell className="py-2 text-right font-mono text-xs">{item.chunk_count}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="border-b border-border bg-muted/20">
          <SectionHead
            icon={FolderOpen}
            title="Nhận kết quả trong folder ebook"
            detail="Chép WAV/timeline vào data/books/{bookId}, sau đó nhấn Xử lý — không upload file lớn qua trình duyệt."
          />
        </CardHeader>
        <CardContent className="space-y-4 pt-5">
          <div className="rounded-md border border-border bg-muted/20 px-3 py-2.5">
            <div className="break-all font-mono text-[11px] text-foreground">{inbox?.path || "Đang tạo folder..."}</div>
            <div className="mt-1 text-[11px] text-muted-foreground">
              {inbox?.count || 0} file chờ · tên chuẩn: {bookId}_001.wav và {bookId}_001.timeline.json
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="outline" onClick={openInbox}>
              <FolderOpen className="h-4 w-4" /> Mở folder
            </Button>
            <Button size="sm" onClick={processInbox} disabled={processingInbox || !inbox?.count}>
              <Play className="h-4 w-4" /> {processingInbox ? "Đang xử lý..." : "Xử lý file trong folder"}
            </Button>
            <Button size="sm" variant="ghost" onClick={loadInbox} disabled={processingInbox}>
              <RefreshCw className="h-4 w-4" /> Quét lại
            </Button>
          </div>
          <InboxReportList report={inboxReport} />
          <details className="text-xs text-muted-foreground">
            <summary className="cursor-pointer font-medium text-foreground">Upload trực tiếp (dự phòng)</summary>
            <label className="mt-3 inline-flex">
              <input
                className="hidden"
                type="file"
                multiple
                accept=".wav,.json"
                onChange={(event) => {
                  uploadResults(event.target.files);
                  event.currentTarget.value = "";
                }}
              />
              <span className="inline-flex h-9 cursor-pointer items-center gap-2 rounded-md border border-input px-3 text-xs font-medium hover:bg-muted">
                <Upload className="h-4 w-4" />
                {uploading ? "Đang upload..." : "Chọn WAV / timeline"}
              </span>
            </label>
          </details>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="border-b border-border bg-muted/20">
          <SectionHead
            icon={ListPlus}
            title="Cập nhật sách (thêm chương mới)"
            detail="Chương đã có giữ nguyên chỉ số và audio; chỉ chương mới được nạp thêm."
          />
        </CardHeader>
        <CardContent className="space-y-4 pt-5 text-xs">
          {reimportPlan ? (
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <Tile label="Chương đang có" value={reimportPlan.existing_count} />
              <Tile label="Trong EPUB" value={reimportPlan.parsed_count} />
              <Tile label="Chương mới" value={reimportPlan.added.length} tone={reimportPlan.added.length ? "good" : undefined} />
              <Tile label="Nội dung đổi" value={reimportPlan.changed.length} tone={reimportPlan.changed.length ? "warn" : undefined} />
            </div>
          ) : (
            <div className="text-muted-foreground">Không đọc được EPUB gốc — hãy tải file EPUB mới lên.</div>
          )}

          <CheckField
            checked={updateChanged}
            onChange={setUpdateChanged}
            label="Ghi đè cả chương đã sửa nội dung (bỏ qua chương đã có audio hoàn thành)"
          />

          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" onClick={reimportStored} disabled={working || !reimportPlan}>
              <RefreshCw className={cn("h-3.5 w-3.5", working && "animate-spin")} /> Nạp từ EPUB gốc
            </Button>
            <label className="inline-flex">
              <input
                className="hidden"
                type="file"
                accept=".epub"
                onChange={(event) => {
                  reimportUpload(event.target.files);
                  event.currentTarget.value = "";
                }}
              />
              <span className="inline-flex h-8 cursor-pointer items-center gap-2 rounded-md border border-input px-3 text-xs font-medium hover:bg-muted">
                <Upload className="h-3.5 w-3.5" /> Tải EPUB mới
              </span>
            </label>
          </div>

          {reimportPlan && reimportPlan.added.length > 0 && (
            <div className="rounded-md border border-border">
              <div className="border-b border-border bg-muted/30 px-3 py-2 font-medium">
                {reimportPlan.added.length} chương sẽ được thêm
              </div>
              <div className="max-h-40 overflow-auto">
                {reimportPlan.added.slice(0, 50).map((chapter, index) => (
                  <div key={`${chapter.title}-${index}`} className="flex items-center gap-3 border-b border-border px-3 py-1.5 last:border-0">
                    <span className="font-mono text-[10px] text-muted-foreground">
                      {chapter.chapter_no ?? "—"}
                    </span>
                    <span className="min-w-0 flex-1 truncate">{chapter.title}</span>
                    <span className="font-mono text-[10px] text-muted-foreground">
                      {chapter.char_count.toLocaleString("vi-VN")}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="border-t border-border pt-4">
            <div className="mb-2 font-medium">
              Patch bổ sung
              {extendPlan ? ` · ${extendPlan.uncovered_chapters} chương chưa có patch` : ""}
            </div>
            {extendPlan && extendPlan.patches.length > 0 ? (
              <div className="space-y-2">
                <div className="flex flex-wrap gap-1">
                  {extendPlan.patches.map((patch) => (
                    <span key={patch.patch_index} className="rounded bg-muted px-2 py-1 font-mono text-[10px]">
                      #{patch.patch_index + 1} · Ch. {patch.chapter_start + 1}–{patch.chapter_end + 1} · {patch.chunk_count} chunk
                    </span>
                  ))}
                </div>
                <Button size="sm" onClick={extendPatches} disabled={working}>
                  <ListPlus className="h-3.5 w-3.5" /> Tạo {extendPlan.patches.length} patch mới
                </Button>
              </div>
            ) : (
              <div className="text-muted-foreground">
                Mọi chương đều đã nằm trong một patch — không cần tạo thêm.
              </div>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
