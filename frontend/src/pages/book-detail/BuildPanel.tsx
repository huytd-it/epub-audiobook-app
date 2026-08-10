import React, { useState } from "react";
import { Link } from "react-router-dom";
import { Eye, RefreshCw, RotateCcw, Upload } from "lucide-react";
import { api, post, postForm } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { PlannedPatch, UploadResult, errorText } from "./types";
import { Field, SectionHead, fieldClass } from "./parts";

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
  const [planned, setPlanned] = useState<PlannedPatch[]>([]);
  const [building, setBuilding] = useState(false);
  const [uploading, setUploading] = useState(false);

  const params = () => {
    const query = new URLSearchParams({ start_chapter: startChapter });
    if (endChapter) query.set("end_chapter", endChapter);
    if (patchSize) query.set("patch_size", patchSize);
    return query;
  };

  const preview = async () => {
    try {
      const result = await api<{ patches: PlannedPatch[] }>(
        `/books/${bookId}/patches/auto-build/preview?${params()}`
      );
      setPlanned(result.patches);
      if (!result.patches.length) onMessage("Không có chương nào khớp khoảng đã chọn.");
    } catch (error) {
      onMessage(errorText(error));
      setPlanned([]);
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
      await post(`/books/${bookId}/patches/auto-build`, form);
      setPlanned([]);
      onMessage("Đã tạo patch và đưa vào hàng đợi.");
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
      const result = await postForm<{ installed: number; results: UploadResult[] }>(
        `/books/${bookId}/patches/upload-results`,
        form
      );
      const problems = result.results
        .filter((item) => item.status === "error" || item.status === "skipped")
        .map((item) => `${item.filename}: ${item.detail || item.status}`);
      onMessage(`Đã nhận ${result.installed} kết quả audio.${problems.length ? ` ${problems.join("; ")}` : ""}`);
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
              <div className="max-h-64 overflow-auto">
                <Table>
                  <TableHeader className="sticky top-0 bg-card">
                    <TableRow>
                      <TableHead>#</TableHead>
                      <TableHead>Chương</TableHead>
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
            icon={Upload}
            title="Nhận kết quả từ bên ngoài"
            detail="Tải lên WAV hoặc file timeline.json đã tổng hợp ở Colab / Kaggle."
          />
        </CardHeader>
        <CardContent className="pt-5">
          <label className="inline-flex">
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
        </CardContent>
      </Card>
    </div>
  );
}
