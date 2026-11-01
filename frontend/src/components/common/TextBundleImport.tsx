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

/** Tóm tắt kết quả import — dùng chung cho bước xem trước và import thật. */
export function TextBundleSummaryView({ summary }: { summary: TextBundleSummary }) {
  const changed = summary.text_changed.length;
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
          </p>
        </div>
      </div>

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
  }, [open, defaultMode]);

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
              onChange={(event) => {
                setFile(event.target.files?.[0] || null);
                setSummary(null);
                setError("");
              }}
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
