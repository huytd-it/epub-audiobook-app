import React, { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, FileJson, Upload } from "lucide-react";
import { postForm, TextBundleSummary } from "@/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { showToast } from "@/components/ui/toast";
import { cn } from "@/lib/utils";

type ImportMode = "merge" | "new";

function errorText(error: unknown) {
  return error instanceof Error ? error.message : "Đã xảy ra lỗi không xác định.";
}

/** Một dòng số liệu trong bảng tóm tắt kết quả nhập. */
function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-md border border-border bg-muted/20 px-3 py-2">
      <div className="font-mono text-sm font-semibold leading-none">{value}</div>
      <div className="mt-1 text-[11px] text-muted-foreground">{label}</div>
    </div>
  );
}

const MATCHED_BY_LABEL: Record<string, string> = {
  target: "sách đang mở (chọn tay)",
  book_id: "id sách ghi trong file",
  bundle_id: "bundle id",
  origin: "sách gốc",
  title: "tên + ngày tạo",
};

/** Định danh đọc thẳng từ file .book-text.json (trước khi gửi lên server). */
type BundleFileInfo = {
  title: string;
  bookId: number | null;
  machineId: string | null;
  bundleId: string | null;
  bookIds: Record<string, number>;
  chapterCount: number;
  exportedAt: string | null;
};

function readBundleInfo(data: unknown): BundleFileInfo | null {
  if (!data || typeof data !== "object") return null;
  const bundle = data as Record<string, any>;
  const source = bundle.source && typeof bundle.source === "object" ? bundle.source : {};
  const book = bundle.book && typeof bundle.book === "object" ? bundle.book : {};
  const rawId = source.book_id ?? book.id;
  return {
    title: String(source.title || book.title || ""),
    bookId: Number.isFinite(Number(rawId)) && rawId !== null && rawId !== undefined ? Number(rawId) : null,
    machineId: source.machine_id ? String(source.machine_id) : null,
    bundleId: bundle.bundle_id || bundle.origin?.bundle_id || null,
    bookIds: bundle.book_ids && typeof bundle.book_ids === "object" ? bundle.book_ids : {},
    chapterCount: Array.isArray(bundle.chapters) ? bundle.chapters.length : 0,
    exportedAt: bundle.exported_at || null,
  };
}

function IdChip({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <span className="inline-flex items-center gap-1 rounded bg-muted px-1.5 py-0.5 font-mono text-[11px]">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-semibold">{value}</span>
    </span>
  );
}

/** Hiển thị id sách theo từng máy ghi trong file. */
function BookIdList({ ids, localMachine }: { ids: Record<string, number>; localMachine?: string }) {
  const entries = Object.entries(ids);
  if (!entries.length) return null;
  return (
    <div className="flex flex-wrap gap-1.5">
      {entries.map(([machine, id]) => (
        <IdChip
          key={machine}
          label={machine === localMachine ? `máy này (${machine})` : `máy ${machine}`}
          value={`#${id}`}
        />
      ))}
    </div>
  );
}

/** Tóm tắt kết quả import — dùng chung cho bước xem trước và import thật. */
export function TextBundleSummaryView({ summary }: { summary: TextBundleSummary }) {
  const changed = summary.text_changed.length;
  const identity = summary.identity;
  return (
    <div className="space-y-3 rounded-md border border-border bg-muted/10 p-3">
      <div className="flex items-start gap-2">
        {summary.dry_run ? (
          <FileJson className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
        ) : (
          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
        )}
        <div className="min-w-0 flex-1">
          <p className="text-xs font-semibold">
            {summary.dry_run
              ? "Xem trước — chưa ghi vào thư viện"
              : summary.created
                ? "Đã tạo sách mới từ file text"
                : "Đã gộp text vào sách có sẵn"}
          </p>
          <p className="mt-0.5 truncate text-xs text-muted-foreground">
            {summary.title || "(không có tiêu đề)"} · sách #{summary.book_id}
            {summary.matched_by && ` · khớp theo ${MATCHED_BY_LABEL[summary.matched_by] || summary.matched_by}`}
          </p>
        </div>
      </div>

      {identity && (
        <div className="space-y-1.5 rounded-md border border-border bg-background px-3 py-2">
          <div className="flex flex-wrap gap-1.5">
            {identity.source_book_id != null && (
              <IdChip
                label={`nguồn${identity.source_machine_id ? ` (máy ${identity.source_machine_id})` : ""}`}
                value={`#${identity.source_book_id}`}
              />
            )}
            <IdChip label="máy này" value={`#${summary.book_id}`} />
            {identity.bundle_id && <IdChip label="bundle" value={identity.bundle_id.slice(0, 12)} />}
          </div>
          <BookIdList ids={identity.book_ids} localMachine={identity.machine_id} />
        </div>
      )}

      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Metric label="Chương mới" value={summary.chapters.created} />
        <Metric label="Chương cập nhật" value={summary.chapters.updated} />
        <Metric label="Patch mới" value={summary.patches.created} />
        <Metric label="Patch cập nhật" value={summary.patches.updated} />
        <Metric label="Patch bỏ qua" value={summary.patches.skipped} />
        <Metric label="Luật thay thế" value={summary.replace_rules} />
        <Metric
          label="Patch cần tạo lại audio"
          value={<span className={changed ? "text-amber-600" : undefined}>{changed}</span>}
        />
        <Metric label="Chế độ" value={summary.mode === "new" ? "Bản sao" : "Gộp"} />
      </div>

      {changed > 0 && (
        <div className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-800">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>
            {changed} patch đổi nội dung đã được đưa về trạng thái chờ. Audio cũ vẫn giữ trên đĩa — cần tạo lại
            audio cho các patch này.
          </span>
        </div>
      )}

      {summary.warnings.map((warning) => (
        <p key={warning} className="text-[11px] text-muted-foreground">
          {warning}
        </p>
      ))}
    </div>
  );
}

/**
 * Nhập file text sách (.book-text.json) do nút xuất ở trang sách tạo ra.
 *
 * `targetBookId` (trang chi tiết sách) ghim việc gộp vào đúng sách đang mở;
 * bỏ trống (thư viện) thì backend tự dò sách gốc theo bundle id, và chỉ tạo
 * sách mới khi file đến từ máy khác.
 */
export function TextBundleImportDialog({
  open,
  onOpenChange,
  targetBookId,
  targetBookTitle,
  defaultMode = "merge",
  onImported,
  onMessage,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  targetBookId?: number;
  targetBookTitle?: string;
  defaultMode?: ImportMode;
  onImported?: (summary: TextBundleSummary) => void;
  onMessage?: (message: string, kind?: "info" | "success" | "error") => void;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState<ImportMode>(defaultMode);
  const [useTargetBook, setUseTargetBook] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [summary, setSummary] = useState<TextBundleSummary | null>(null);
  const [fileInfo, setFileInfo] = useState<BundleFileInfo | null>(null);

  const notify =
    onMessage ??
    ((message: string, kind?: "info" | "success" | "error") => showToast(message, kind));
  const canPinToBook = Boolean(targetBookId) && mode === "merge";

  useEffect(() => {
    if (!open) return;
    setFile(null);
    setMode(defaultMode);
    setUseTargetBook(true);
    setBusy(false);
    setError("");
    setSummary(null);
    setFileInfo(null);
  }, [open, defaultMode]);

  const pickFile = (next: File | null) => {
    setFile(next);
    setSummary(null);
    setError("");
    setFileInfo(null);
    if (!next) return;
    next
      .text()
      .then((text) => {
        const info = readBundleInfo(JSON.parse(text.replace(/^﻿/, "")));
        setFileInfo(info);
        if (!info) setError("File không phải file text sách hợp lệ.");
      })
      .catch(() => setError("Không đọc được file JSON."));
  };

  const submit = async (dryRun: boolean) => {
    if (!file) {
      setError("Chọn file .book-text.json trước khi nhập.");
      return;
    }
    setBusy(true);
    setError("");
    const form = new FormData();
    form.append("file", file);
    form.append("mode", mode);
    form.append("dry_run", dryRun ? "1" : "0");
    if (canPinToBook && useTargetBook && targetBookId) {
      form.append("target_book_id", String(targetBookId));
    }
    try {
      const result = await postForm<TextBundleSummary>("/text-bundles/import", form);
      setSummary(result);
      if (!dryRun) {
        notify(
          result.created
            ? `Đã tạo sách "${result.title}" từ file text (#${result.book_id}).`
            : `Đã gộp text vào sách "${result.title}" (#${result.book_id}).`,
          "success"
        );
        onImported?.(result);
      }
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !busy && onOpenChange(next)}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Upload className="h-4 w-4 text-primary" /> Nhập text sách
          </DialogTitle>
          <DialogDescription>
            File chỉ chứa text: thông tin sách, mục lục đầy đủ (kể cả chương bị loại/rỗng), nội dung chương, patch,
            luật thay thế và cấu hình đọc. Không có audio, video, ảnh hay EPUB. File xuất từ máy này sẽ được gộp lại
            đúng sách, không tạo bản trùng.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <label className="block space-y-1.5">
            <span className="text-xs font-medium">File text sách (.json)</span>
            <input
              type="file"
              accept=".json,application/json"
              disabled={busy}
              onChange={(event) => pickFile(event.target.files?.[0] || null)}
              className={cn(
                "block w-full cursor-pointer rounded-md border border-input bg-background px-3 py-2 text-xs file:mr-3 file:rounded file:border-0 file:bg-muted file:px-2.5 file:py-1 file:text-xs file:font-medium",
                busy && "cursor-not-allowed opacity-60"
              )}
            />
            {file && (
              <span className="block font-mono text-[11px] text-muted-foreground">
                {file.name} · {(file.size / 1024).toFixed(0)} KB
              </span>
            )}
          </label>

          {fileInfo && (
            <div className="space-y-1.5 rounded-md border border-border bg-muted/10 px-3 py-2">
              <p className="truncate text-xs font-medium">
                {fileInfo.title || "(không có tiêu đề)"}
                <span className="font-normal text-muted-foreground">
                  {" "}
                  · {fileInfo.chapterCount} chương
                  {fileInfo.exportedAt && ` · xuất lúc ${new Date(fileInfo.exportedAt).toLocaleString("vi-VN")}`}
                </span>
              </p>
              <div className="flex flex-wrap gap-1.5">
                {fileInfo.bookId != null ? (
                  <IdChip
                    label={`sách nguồn${fileInfo.machineId ? ` (máy ${fileInfo.machineId})` : ""}`}
                    value={`#${fileInfo.bookId}`}
                  />
                ) : (
                  <span className="text-[11px] text-muted-foreground">
                    File bản cũ chưa ghi id sách — sẽ dò theo bundle id.
                  </span>
                )}
                {fileInfo.bundleId && <IdChip label="bundle" value={fileInfo.bundleId.slice(0, 12)} />}
              </div>
              <BookIdList ids={fileInfo.bookIds} />
              {canPinToBook && useTargetBook && targetBookId && fileInfo.bookIds && (
                Object.keys(fileInfo.bookIds).length > 0 &&
                !Object.values(fileInfo.bookIds).includes(targetBookId) && (
                  <p className="text-[11px] text-amber-700">
                    Id sách đang mở (#{targetBookId}) không có trong file — kiểm tra lại có đúng sách không, hoặc bỏ
                    chọn "Gộp vào sách đang mở" để tự dò.
                  </p>
                )
              )}
            </div>
          )}

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="block space-y-1.5">
              <span className="text-xs font-medium">Cách nhập</span>
              <select
                value={mode}
                disabled={busy}
                onChange={(event) => setMode(event.target.value as ImportMode)}
                className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/15"
              >
                <option value="merge">Gộp vào sách có sẵn (khuyến nghị)</option>
                <option value="new">Tạo bản sao mới</option>
              </select>
            </label>

            <div className="flex items-end pb-1">
              {canPinToBook ? (
                <label className="flex cursor-pointer items-center gap-2 text-xs font-medium">
                  <input
                    type="checkbox"
                    checked={useTargetBook}
                    disabled={busy}
                    onChange={(event) => setUseTargetBook(event.target.checked)}
                    className="h-4 w-4 rounded border-input accent-primary"
                  />
                  <span>
                    Gộp vào sách đang mở
                    {targetBookTitle && (
                      <span className="font-normal text-muted-foreground"> · {targetBookTitle}</span>
                    )}
                  </span>
                </label>
              ) : (
                <p className="text-[11px] text-muted-foreground">
                  {mode === "new"
                    ? "File sẽ thành một sách riêng, không ảnh hưởng sách đang có."
                    : "Backend tự dò sách gốc theo bundle id; tạo sách mới nếu chưa có."}
                </p>
              )}
            </div>
          </div>

          {canPinToBook && useTargetBook && (
            <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-800">
              File sẽ ghi đè phần text (chương, patch, luật thay thế) của sách đang mở. Audio/video hiện có được giữ
              nguyên. Bỏ chọn nếu file thuộc sách khác.
            </p>
          )}

          {error && (
            <p className="flex items-start gap-2 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              {error}
            </p>
          )}

          {summary && <TextBundleSummaryView summary={summary} />}
        </div>

        <DialogFooter className="gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>
            Đóng
          </Button>
          <Button
            variant="outline"
            onClick={() => void submit(true)}
            disabled={busy || !file}
            title="Chạy thử và xem kết quả, không ghi vào thư viện"
          >
            {busy ? "Đang kiểm tra..." : "Xem trước"}
          </Button>
          <Button onClick={() => void submit(false)} disabled={busy || !file}>
            {busy ? "Đang nhập..." : summary?.dry_run ? "Nhập ngay" : "Nhập file"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
