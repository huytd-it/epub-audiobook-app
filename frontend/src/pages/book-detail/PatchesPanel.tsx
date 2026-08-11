import React, { useCallback, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, CloudDownload, FileAudio2, FileSearch, Film, Layers, Upload, Video } from "lucide-react";
import { api, Patch, post, postForm } from "@/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { EmptyState } from "@/components/common/Header";
import { Progress } from "@/components/ui/progress";
import { StatusBadge } from "@/components/common/StatusBadge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";
import { PatchReport, PipelineInfo, errorText } from "./types";
import { SectionHead, TabBar, checkboxClass } from "./parts";

type Filter = "all" | "processing" | "done" | "failed";

type RowProps = {
  patch: Patch;
  pipeline?: PipelineInfo;
  chunkReport?: PatchReport;
  selected: boolean;
  busy: boolean;
  onToggle: (patchId: number) => void;
  onOpen: (patch: Patch) => void;
  onImportDrive: (patch: Patch) => void;
  onImportFiles: (patch: Patch, files: FileList | null) => void;
};

/** Memo hoá theo từng dòng: nhịp polling chỉ vẽ lại patch thực sự đổi. */
const PatchRow = React.memo(function PatchRow({
  patch,
  pipeline,
  chunkReport,
  selected,
  busy,
  onToggle,
  onOpen,
  onImportDrive,
  onImportFiles,
}: RowProps) {
  const percent = patch.chunk_count ? (patch.next_chunk_index * 100) / patch.chunk_count : 0;

  return (
    <TableRow className={cn(selected && "bg-primary/5")}>
      <TableCell className="w-8 py-2.5 pl-4 pr-0">
        <input
          type="checkbox"
          className={checkboxClass}
          checked={selected}
          onChange={() => onToggle(patch.id)}
          aria-label={`Chọn patch ${patch.patch_index + 1}`}
        />
      </TableCell>

      <TableCell className="min-w-56 py-2.5">
        <button className="block text-left" onClick={() => onOpen(patch)}>
          <span className="text-xs font-semibold hover:text-primary">
            #{patch.patch_index + 1} · {patch.name || `Patch ${patch.patch_index + 1}`}
          </span>
          <span className="mt-0.5 block font-mono text-[10px] text-muted-foreground">
            Chương {patch.chapter_start + 1}–{patch.chapter_end + 1}
          </span>
        </button>
        {patch.error_message && (
          <div className="mt-1 flex max-w-xs items-start gap-1 text-[10px] text-red-600">
            <AlertTriangle className="mt-px h-3 w-3 shrink-0" />
            <span className="truncate font-mono" title={patch.error_message}>
              {patch.error_message}
            </span>
          </div>
        )}
      </TableCell>

      <TableCell className="min-w-36 py-2.5">
        <div className="mb-1 flex justify-between font-mono text-[10px] text-muted-foreground">
          <span>
            {patch.next_chunk_index}/{patch.chunk_count}
          </span>
          <span className="font-semibold text-foreground">{Math.round(percent)}%</span>
        </div>
        <Progress value={percent} className="h-1.5" />
      </TableCell>

      <TableCell className="min-w-28 py-2.5">
        <div className="flex flex-wrap gap-1">
          {patch.status === "done" && (
            <span className="inline-flex items-center gap-1 rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700">
              <FileAudio2 className="h-2.5 w-2.5" /> Audio
            </span>
          )}
          {pipeline?.video_status === "done" && (
            <span className="inline-flex items-center gap-1 rounded bg-blue-50 px-1.5 py-0.5 text-[10px] font-medium text-blue-700">
              <Film className="h-2.5 w-2.5" /> Video
            </span>
          )}
          {pipeline?.upload_state === "published" && (
            <span className="inline-flex items-center gap-1 rounded bg-red-50 px-1.5 py-0.5 text-[10px] font-medium text-red-700">
              <Video className="h-2.5 w-2.5" /> YouTube
            </span>
          )}
          {pipeline?.last_error && (
            <span
              className="inline-flex items-center rounded bg-red-50 px-1.5 py-0.5 text-[10px] text-red-600"
              title={pipeline.last_error}
            >
              Lỗi pipeline
            </span>
          )}
          {!pipeline && patch.status !== "done" && <span className="text-[10px] text-muted-foreground">—</span>}
          {chunkReport && chunkReport.severity !== "ok" && (
            <span
              className={cn(
                "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium",
                chunkReport.severity === "error" ? "bg-red-50 text-red-700" : "bg-amber-50 text-amber-800"
              )}
              title={`${chunkReport.chunk_count} chunk · ${chunkReport.total_chars} ký tự · quá dài ${chunkReport.oversized_chunks} · rỗng ${chunkReport.empty_chunks} · không đọc được ${chunkReport.unspeakable_chunks}`}
            >
              <FileSearch className="h-2.5 w-2.5" /> {chunkReport.severity === "error" ? "Lỗi chunk" : "Cảnh báo chunk"}
            </span>
          )}
        </div>
      </TableCell>

      <TableCell className="py-2.5 text-right">
        <StatusBadge value={patch.status} />
      </TableCell>

      <TableCell className="py-2.5 pr-4 text-right">
        <div className="flex items-center justify-end gap-1">
          <Button
            size="sm"
            variant="ghost"
            className="h-7 px-2 text-[11px]"
            disabled={busy || patch.status === "processing"}
            onClick={() => onImportDrive(patch)}
            title="Quét kết quả từ Drive Desktop"
          >
            <CloudDownload className="h-3 w-3" />
            <span className="hidden lg:inline">Drive</span>
          </Button>
          <label className="inline-flex">
            <input
              className="hidden"
              type="file"
              multiple
              accept=".wav,.zip"
              onChange={(event) => {
                onImportFiles(patch, event.target.files);
                event.currentTarget.value = "";
              }}
            />
            <span
              className="inline-flex h-7 cursor-pointer items-center gap-1 rounded-md px-2 text-[11px] font-medium text-muted-foreground hover:bg-muted hover:text-foreground"
              title="Tải chunk WAV lên"
            >
              <Upload className="h-3 w-3" />
              <span className="hidden lg:inline">Upload</span>
            </span>
          </label>
        </div>
      </TableCell>
    </TableRow>
  );
});

export function PatchesPanel({
  bookId,
  patches,
  pipelines,
  selectedIds,
  onSelectionChange,
  onOpenPatch,
  onMessage,
  onRefresh,
  onBusyChange,
}: {
  bookId: string;
  patches: Patch[];
  pipelines: Record<string, PipelineInfo>;
  selectedIds: number[];
  onSelectionChange: (ids: number[]) => void;
  onOpenPatch: (patch: Patch) => void;
  onMessage: (message: string) => void;
  onRefresh: () => Promise<void> | void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [importingId, setImportingId] = useState<number>();
  const [chunkReports, setChunkReports] = useState<Record<number, PatchReport>>();
  const [checkingChunks, setCheckingChunks] = useState(false);

  const checkChunks = useCallback(async () => {
    setCheckingChunks(true);
    try {
      const result = await api<{ patches: PatchReport[] }>(`/books/${bookId}/validation`);
      const byId: Record<number, PatchReport> = {};
      for (const item of result.patches) byId[item.patch_id] = item;
      setChunkReports(byId);
    } catch (error) {
      onMessage(errorText(error));
    } finally {
      setCheckingChunks(false);
    }
  }, [bookId, onMessage]);

  const counts = useMemo(
    () => ({
      all: patches.length,
      processing: patches.filter((patch) => patch.status === "processing").length,
      done: patches.filter((patch) => patch.status === "done").length,
      failed: patches.filter((patch) => patch.status === "failed").length,
    }),
    [patches]
  );

  const visible = useMemo(
    () => (filter === "all" ? patches : patches.filter((patch) => patch.status === filter)),
    [patches, filter]
  );

  const allVisibleSelected = visible.length > 0 && visible.every((patch) => selectedIds.includes(patch.id));

  const toggle = useCallback(
    (patchId: number) => {
      onSelectionChange(
        selectedIds.includes(patchId) ? selectedIds.filter((id) => id !== patchId) : [...selectedIds, patchId]
      );
    },
    [selectedIds, onSelectionChange]
  );

  const toggleAll = useCallback(() => {
    const visibleIds = visible.map((patch) => patch.id);
    onSelectionChange(
      allVisibleSelected
        ? selectedIds.filter((id) => !visibleIds.includes(id))
        : Array.from(new Set([...selectedIds, ...visibleIds]))
    );
  }, [visible, allVisibleSelected, selectedIds, onSelectionChange]);

  const runImport = useCallback(
    async (patch: Patch, action: () => Promise<unknown>, done: string) => {
      setImportingId(patch.id);
      onBusyChange(true);
      try {
        await action();
        onMessage(done);
        await onRefresh();
      } catch (error) {
        onMessage(errorText(error));
      } finally {
        setImportingId(undefined);
        onBusyChange(false);
      }
    },
    [onBusyChange, onMessage, onRefresh]
  );

  const importDrive = useCallback(
    (patch: Patch) =>
      runImport(
        patch,
        () => post(`/books/${bookId}/patches/${patch.id}/import`),
        `Đã quét Drive Desktop cho patch ${patch.patch_index + 1}.`
      ),
    [bookId, runImport]
  );

  const importFiles = useCallback(
    (patch: Patch, files: FileList | null) => {
      if (!files?.length) return;
      const form = new FormData();
      Array.from(files).forEach((file) => form.append("files", file));
      return runImport(
        patch,
        () => postForm(`/books/${bookId}/patches/${patch.id}/import-local`, form),
        `Đã upload ${files.length} chunk cho patch ${patch.patch_index + 1}.`
      );
    },
    [bookId, runImport]
  );

  return (
    <Card>
      <CardHeader className="gap-4 border-b border-border bg-muted/20">
        <SectionHead
          icon={Layers}
          title={`Patches (${patches.length})`}
          detail="Chọn patch để chạy hành động hàng loạt ở thanh dưới màn hình."
          action={
            <div className="flex shrink-0 items-center gap-3">
              <Button size="sm" variant="outline" onClick={checkChunks} disabled={checkingChunks}>
                <FileSearch className={cn("h-3.5 w-3.5", checkingChunks && "animate-pulse")} /> Soát chunk
              </Button>
              <Link to="/queue" className="text-xs text-primary hover:underline">
                Hàng đợi →
              </Link>
            </div>
          }
        />
        <TabBar<Filter>
          value={filter}
          onChange={setFilter}
          className="bg-background"
          tabs={[
            { value: "all", label: "Tất cả", badge: counts.all },
            { value: "processing", label: "Đang chạy", badge: counts.processing },
            { value: "done", label: "Hoàn thành", badge: counts.done },
            { value: "failed", label: "Lỗi", badge: counts.failed },
          ]}
        />
      </CardHeader>

      <CardContent className="p-0">
        {visible.length === 0 ? (
          <EmptyState text={patches.length === 0 ? "Chưa khởi tạo patch nào" : "Không có patch khớp bộ lọc"} />
        ) : (
          <div className="max-h-[32rem] overflow-auto">
            <Table>
              <TableHeader className="sticky top-0 z-10 bg-card">
                <TableRow>
                  <TableHead className="w-8 pl-4 pr-0">
                    <input
                      type="checkbox"
                      className={checkboxClass}
                      checked={allVisibleSelected}
                      onChange={toggleAll}
                      aria-label="Chọn tất cả patch đang hiển thị"
                    />
                  </TableHead>
                  <TableHead>Patch</TableHead>
                  <TableHead>Tiến độ</TableHead>
                  <TableHead>Pipeline</TableHead>
                  <TableHead className="text-right">Trạng thái</TableHead>
                  <TableHead className="pr-4 text-right">Thao tác</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visible.map((patch) => (
                  <PatchRow
                    key={patch.id}
                    patch={patch}
                    pipeline={pipelines[String(patch.id)]}
                    chunkReport={chunkReports?.[patch.id]}
                    selected={selectedIds.includes(patch.id)}
                    busy={importingId === patch.id}
                    onToggle={toggle}
                    onOpen={onOpenPatch}
                    onImportDrive={importDrive}
                    onImportFiles={importFiles}
                  />
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
