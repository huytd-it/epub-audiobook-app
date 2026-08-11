import React, { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Eye, ListPlus, RefreshCw, RotateCcw, Upload } from "lucide-react";
import { api, post, postForm } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { ExtendPlan, PlannedPatch, ReimportPlan, UploadResult, errorText } from "./types";
import { CheckField, Field, SectionHead, Tile, fieldClass } from "./parts";

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

  useEffect(() => {
    loadPlans();
  }, [loadPlans]);

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
      const result = await postForm<{ inserted: number; updated: number }>(`/books/${bookId}/reimport`, form);
      return `Đã nạp EPUB mới: thêm ${result.inserted} chương, cập nhật ${result.updated} chương.`;
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
