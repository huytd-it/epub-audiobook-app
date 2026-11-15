import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, CircleDashed, FolderUp, HardDrive, Info, Pause, RefreshCw, X } from "lucide-react";
import { Chapter, api, postForm, postJson } from "@/api";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import { CheckField } from "./parts";
import { ChapterAudioPatch, errorText } from "./types";

/** Cùng quy ước với app/chapter_chunk_upload.py */
const CHAPTER_DIR = /^(\d+)/;
const CHUNK_FILE = /^(?:chunk_)?(\d+)\.wav$/i;

/** Giới hạn an toàn cho folder 24GB+: chia nhỏ theo cả số file và tổng bytes. */
const BATCH_MAX_FILES = 48;
const BATCH_MAX_BYTES = 80 * 1024 * 1024;
const BATCH_RETRY = 3;
const BATCH_RETRY_BASE_MS = 1200;

type ParsedFile = { file: File; path: string; chapterIndex: number; chunkIndex: number; size: number };
type InvalidFile = { path: string; reason: string };

type ChapterGroup = { chapterIndex: number; files: ParsedFile[]; duplicates: number; bytes: number };

type ChapterUploadReport = {
  chapter_index: number;
  chapter_no: number;
  title?: string | null;
  expected?: number;
  received: number;
  installed: number;
  missing: number[];
  rejected: { filename: string; reason: string }[];
  complete: boolean;
  status: "ok" | "partial" | "error";
  detail?: string | null;
};

type UploadResponse = {
  max_chars: number;
  chapters: ChapterUploadReport[];
  rejected: { filename: string; reason: string }[];
  installed: number;
  affected_patches: ChapterAudioPatch[];
};

type MergeResponse = {
  merged: number;
  patches: (ChapterAudioPatch & { result: "merged" | "skipped"; detail?: string; publish_status?: string })[];
};

type IngestCandidate = { name: string; path: string; wav_count: number };
type IngestStatusResponse = { book_id: number; base: string; candidates: IngestCandidate[] };

function relativePath(file: File) {
  return (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name;
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function sleep(ms: number, signal?: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => {
      clearTimeout(t);
      reject(new DOMException("Aborted", "AbortError"));
    });
  });
}

function parseFiles(files: File[]) {
  const parsed: ParsedFile[] = [];
  const invalid: InvalidFile[] = [];
  let ignored = 0;
  for (const file of files) {
    const path = relativePath(file);
    const parts = path.split(/[\\/]/).filter(Boolean);
    const name = parts[parts.length - 1] || "";
    if (!name.toLowerCase().endsWith(".wav")) {
      ignored++;
      continue;
    }
    const fileMatch = CHUNK_FILE.exec(name);
    const dirMatch = parts.length >= 2 ? CHAPTER_DIR.exec(parts[parts.length - 2]) : null;
    if (!fileMatch) invalid.push({ path, reason: "tên file phải là chunk_NNN.wav" });
    else if (parts.length < 2) invalid.push({ path, reason: "phải nằm trong thư mục chương" });
    else if (!dirMatch) invalid.push({ path, reason: `thư mục “${parts[parts.length - 2]}” không bắt đầu bằng số chương` });
    else if (Number(dirMatch[1]) < 1) invalid.push({ path, reason: "số chương đánh từ 1" });
    else parsed.push({ file, path, chapterIndex: Number(dirMatch[1]) - 1, chunkIndex: Number(fileMatch[1]), size: file.size });
  }
  const map = new Map<number, ChapterGroup>();
  for (const item of parsed) {
    const group = map.get(item.chapterIndex) || { chapterIndex: item.chapterIndex, files: [], duplicates: 0, bytes: 0 };
    if (group.files.some((other) => other.chunkIndex === item.chunkIndex)) {
      group.duplicates++;
      invalid.push({ path: item.path, reason: `trùng chunk ${item.chunkIndex} của chương ${item.chapterIndex + 1}` });
    } else {
      group.files.push(item);
      group.bytes += item.size;
    }
    map.set(item.chapterIndex, group);
  }
  const groups = [...map.values()]
    .map((group) => ({ ...group, files: group.files.sort((a, b) => a.chunkIndex - b.chunkIndex) }))
    .sort((a, b) => a.chapterIndex - b.chapterIndex);
  return { groups, invalid, ignored };
}

function buildBatches(groups: ChapterGroup[]): ChapterGroup[][] {
  const slices: ChapterGroup[] = [];
  for (const group of groups) {
    if (group.files.length <= BATCH_MAX_FILES && group.bytes <= BATCH_MAX_BYTES) {
      slices.push(group);
      continue;
    }
    let offset = 0;
    while (offset < group.files.length) {
      let count = 0;
      let bytes = 0;
      while (offset + count < group.files.length) {
        const next = group.files[offset + count];
        const wouldExceedFiles = count + 1 > BATCH_MAX_FILES;
        const wouldExceedBytes = bytes + next.size > BATCH_MAX_BYTES && count > 0;
        if (wouldExceedFiles || wouldExceedBytes) break;
        bytes += next.size;
        count++;
      }
      if (count === 0) count = 1;
      const chunk = group.files.slice(offset, offset + count);
      slices.push({
        chapterIndex: group.chapterIndex,
        files: chunk,
        duplicates: 0,
        bytes: chunk.reduce((s, f) => s + f.size, 0),
      });
      offset += count;
    }
  }
  const batches: ChapterGroup[][] = [];
  let current: ChapterGroup[] = [];
  let currentFiles = 0;
  let currentBytes = 0;
  for (const slice of slices) {
    const fitsFiles = currentFiles + slice.files.length <= BATCH_MAX_FILES;
    const fitsBytes = currentBytes + slice.bytes <= BATCH_MAX_BYTES;
    if (current.length && (!fitsFiles || !fitsBytes)) {
      batches.push(current);
      current = [];
      currentFiles = 0;
      currentBytes = 0;
    }
    current.push(slice);
    currentFiles += slice.files.length;
    currentBytes += slice.bytes;
  }
  if (current.length) batches.push(current);
  return batches;
}

function FormatGuide({ maxChars }: { maxChars?: string }) {
  return (
    <div className="space-y-2.5 rounded-md border border-primary/30 bg-primary/5 p-3 text-xs">
      <div className="flex items-center gap-1.5 font-semibold">
        <Info className="h-3.5 w-3.5 text-primary" /> Định dạng thư mục
      </div>
      <pre className="overflow-x-auto rounded bg-background/80 px-3 py-2 font-mono text-[11px] leading-relaxed">
{`thư-mục-bất-kỳ/
├── 0001/              ← chương 1 (số ở cột "Ch.")
│   ├── chunk_000.wav  ← chunk đầu tiên, đánh từ 0
│   ├── chunk_001.wav
│   └── ...
├── 0002/
│   └── ...
└── 0015 - Chương 15/  ← chỉ cần bắt đầu bằng số chương`}
      </pre>
      <ul className="list-disc space-y-1 pl-4 text-[11px] text-muted-foreground">
        <li>
          <span className="font-medium text-foreground">Thư mục chương</span>: tên bắt đầu bằng số chương, đánh từ 1
          giống cột “Ch.” (<code>0001</code>, <code>12</code>, <code>0012 - Tên</code> đều được).
        </li>
        <li>
          <span className="font-medium text-foreground">File chunk</span>: <code>chunk_NNN.wav</code> (hoặc{" "}
          <code>NNN.wav</code>), đánh từ <span className="font-semibold">0</span> trong từng chương.
        </li>
        <li>
          Chunk N phải khớp đoạn văn N theo cách tách hiện tại (max_chars{maxChars ? ` = ${maxChars}` : ""}) — xem tab Audio của từng chương.
        </li>
        <li>
          WAV PCM mono/stereo (tự trộn mono). Mọi chunk cùng chương phải cùng sample rate (vd. 24000 Hz).
        </li>
        <li className="font-medium text-foreground">
          Folder lớn 24 GB: chép sẵn vào <code>data/books/{"{id}"}/_staging</code> trên server rồi bấm “Nạp qua queue” — server quét đĩa từng batch ~80 MB, ghi vào kho và tự ghép patch. Lỗi ở một file/batch chỉ bỏ qua, không xoá nguồn.
        </li>
      </ul>
    </div>
  );
}

const STATUS_META = {
  ok: { icon: CheckCircle2, className: "text-emerald-600", label: "Đủ chunk" },
  partial: { icon: CircleDashed, className: "text-amber-600", label: "Chưa đủ" },
  error: { icon: AlertTriangle, className: "text-red-600", label: "Lỗi" },
} as const;

export function ChunkFolderUploadDialog({
  bookId,
  chapters,
  maxChars,
  open,
  onOpenChange,
  onMessage,
  onChanged,
}: {
  bookId: string;
  chapters: Chapter[];
  maxChars?: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onMessage: (message: string) => void;
  onChanged: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const pausedRef = useRef(false);
  const [files, setFiles] = useState<File[]>([]);
  const [replace, setReplace] = useState(false);
  const [mergePatches, setMergePatches] = useState(true);
  const [busy, setBusy] = useState(false);
  const [paused, setPaused] = useState(false);
  const [progress, setProgress] = useState<{ doneBatches: number; totalBatches: number; doneBytes: number; totalBytes: number; doneChapters: Set<number> }>();
  const [reports, setReports] = useState<ChapterUploadReport[]>();
  const [serverRejected, setServerRejected] = useState<{ filename: string; reason: string }[]>([]);
  const [merge, setMerge] = useState<MergeResponse>();
  const [usedMaxChars, setUsedMaxChars] = useState<number>();
  const [batchErrors, setBatchErrors] = useState<string[]>([]);

  // Server-side ingest (folder đã chép vào data/books/{id})
  const [staging, setStaging] = useState<IngestCandidate[] | null>(null);
  const [stagingBase, setStagingBase] = useState<string>("");
  const [stagingLoading, setStagingLoading] = useState(false);
  const [ingestJobId, setIngestJobId] = useState<number | null>(null);
  const [ingestJob, setIngestJob] = useState<any>(null);
  const [ingestError, setIngestError] = useState<string>("");

  const chapterByIndex = useMemo(() => new Map(chapters.map((chapter) => [chapter.chapter_index, chapter])), [chapters]);
  const { groups, invalid, ignored } = useMemo(() => parseFiles(files), [files]);
  const unknownChapters = groups.filter((group) => !chapterByIndex.has(group.chapterIndex));
  const uploadGroups = groups.filter((group) => chapterByIndex.has(group.chapterIndex));
  const uploadFileCount = uploadGroups.reduce((sum, group) => sum + group.files.length, 0);
  const uploadTotalBytes = uploadGroups.reduce((sum, group) => sum + group.bytes, 0);
  const batches = useMemo(() => buildBatches(uploadGroups), [uploadGroups]);
  const isLarge = uploadTotalBytes > 2 * 1024 * 1024 * 1024 || uploadFileCount > 2000;

  const busyAny = busy || ingestJobId !== null;

  const reset = useCallback(() => {
    pausedRef.current = false;
    setFiles([]);
    setReports(undefined);
    setServerRejected([]);
    setMerge(undefined);
    setProgress(undefined);
    setUsedMaxChars(undefined);
    setBatchErrors([]);
    setPaused(false);
    setIngestError("");
    if (inputRef.current) inputRef.current.value = "";
  }, []);

  const close = (next: boolean) => {
    if (busyAny) return;
    if (!next) reset();
    onOpenChange(next);
  };

  const togglePaused = useCallback(() => {
    pausedRef.current = !pausedRef.current;
    setPaused(pausedRef.current);
  }, []);

  const cancel = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  const fetchStaging = useCallback(async () => {
    if (!open) return;
    setStagingLoading(true);
    try {
      const res = await api<IngestStatusResponse>(`/books/${bookId}/chapter-audio/ingest-status`);
      setStaging(res.candidates);
      setStagingBase(res.base);
    } catch {
      setStaging([]);
    } finally {
      setStagingLoading(false);
    }
  }, [bookId, open]);

  useEffect(() => {
    if (open) {
      fetchStaging();
      // Hủy poll cũ khi mở lại
      setIngestJobId(null);
      setIngestJob(null);
      setIngestError("");
    }
  }, [open, fetchStaging]);

  // Poll ingest job
  useEffect(() => {
    if (ingestJobId == null) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const job = await api<any>(`/queue/jobs/${ingestJobId}`);
        if (cancelled) return;
        setIngestJob(job);
        if (job.status === "done" || job.status === "failed" || job.status === "cancelled") {
          // Kết thúc
          if (job.status === "done") {
            const result = job.result || {};
            const installed = result.installed ?? 0;
            const mergedCount = result.merged ?? 0;
            onMessage(`Ingest qua queue xong: ${installed} chunk đã ghi · ghép ${mergedCount} patch.`);
            if (installed) onChanged();
            // Refresh staging và báo cáo
            fetchStaging();
            // Hiển thị reports từ result nếu có
            if (result.reports) setReports(result.reports);
          } else if (job.status === "failed") {
            setIngestError(job.error_message || "job thất bại");
            onMessage(`Ingest lỗi: ${job.error_message || "unknown"}`);
          }
          setIngestJobId(null);
        } else {
          setTimeout(poll, 2000);
        }
      } catch (err) {
        if (!cancelled) setIngestError(errorText(err));
      }
    };
    poll();
    return () => {
      cancelled = true;
    };
  }, [ingestJobId, fetchStaging, onChanged, onMessage]);

  const startServerIngest = async (sourceSubdir: string) => {
    if (ingestJobId != null) return;
    setIngestError("");
    setReports(undefined);
    setMerge(undefined);
    try {
      const res = await api<{ job_id: number }>(`/books/${bookId}/chapter-audio/ingest`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_subdir: sourceSubdir, replace, merge_patches: mergePatches }),
      });
      setIngestJobId(res.job_id);
      setIngestJob({ id: res.job_id, status: "pending", percent: 0, phase: "queued" });
      onMessage(`Đã đưa folder "${sourceSubdir || "_auto"}" vào queue #${res.job_id} — xử lý từng batch, lỗi bỏ qua không xoá nguồn.`);
    } catch (err) {
      setIngestError(errorText(err));
      onMessage(errorText(err));
    }
  };

  const runBrowserUpload = async () => {
    if (!uploadGroups.length || busy) return;
    const controller = new AbortController();
    abortRef.current = controller;
    pausedRef.current = false;
    setBusy(true);
    setPaused(false);
    setReports(undefined);
    setServerRejected([]);
    setMerge(undefined);
    setBatchErrors([]);
    const totalBytes = uploadTotalBytes;
    const totalBatches = batches.length;
    setProgress({ doneBatches: 0, totalBatches, doneBytes: 0, totalBytes, doneChapters: new Set() });

    const collected: ChapterUploadReport[] = [];
    const rejected: { filename: string; reason: string }[] = [];
    const errors: string[] = [];
    let failure: unknown;
    const reportByChapter = new Map<number, ChapterUploadReport>();

    const upsertReport = (r: ChapterUploadReport) => {
      const prev = reportByChapter.get(r.chapter_index);
      if (!prev) {
        reportByChapter.set(r.chapter_index, r);
      } else {
        reportByChapter.set(r.chapter_index, {
          ...r,
          received: prev.received + r.received,
          installed: prev.installed + r.installed,
          rejected: [...prev.rejected, ...r.rejected],
          missing: r.missing,
          complete: r.complete,
          status: r.status,
          detail: r.detail,
          expected: r.expected,
        });
      }
      collected.length = 0;
      for (const v of reportByChapter.values()) collected.push(v);
    };

    try {
      for (let bi = 0; bi < batches.length; bi++) {
        if (controller.signal.aborted) break;
        while (pausedRef.current && !controller.signal.aborted) {
          await sleep(300, controller.signal);
        }
        const batch = batches[bi];
        const batchBytes = batch.reduce((s, g) => s + g.bytes, 0);
        let attempt = 0;
        let succeeded = false;
        let lastError: unknown;

        while (attempt < BATCH_RETRY && !succeeded && !controller.signal.aborted) {
          try {
            const form = new FormData();
            form.append("replace", replace ? "true" : "false");
            for (const group of batch) {
              for (const item of group.files) {
                form.append("files", item.file, item.path);
              }
            }
            const result = await postForm<UploadResponse>(`/books/${bookId}/chapter-audio/upload`, form);
            if (controller.signal.aborted) break;
            for (const r of result.chapters) upsertReport(r);
            rejected.push(...result.rejected);
            setUsedMaxChars(result.max_chars);
            succeeded = true;
          } catch (err) {
            lastError = err;
            attempt++;
            if (attempt < BATCH_RETRY && !controller.signal.aborted) {
              const backoff = BATCH_RETRY_BASE_MS * Math.pow(2, attempt - 1);
              await sleep(backoff, controller.signal).catch(() => {});
            }
          }
        }

        if (!succeeded) {
          failure = failure || lastError;
          const msg = errorText(lastError);
          errors.push(`Batch ${bi + 1}/${totalBatches}: ${msg}`);
          for (const group of batch) {
            upsertReport({
              chapter_index: group.chapterIndex,
              chapter_no: group.chapterIndex + 1,
              received: group.files.length,
              installed: 0,
              missing: [],
              rejected: [],
              complete: false,
              status: "error",
              detail: msg,
            });
          }
        }

        setReports([...collected]);
        setProgress((cur) =>
          cur
            ? {
                ...cur,
                doneBatches: cur.doneBatches + 1,
                doneBytes: cur.doneBytes + (succeeded ? batchBytes : 0),
                doneChapters: new Set(collected.filter((r) => r.installed > 0).map((r) => r.chapter_index)),
              }
            : cur
        );
      }

      if (controller.signal.aborted) {
        onMessage("Đã hủy upload — những batch đã xong vẫn được giữ lại.");
        return;
      }

      setServerRejected(rejected);
      setBatchErrors(errors);

      const installed = [...reportByChapter.values()].reduce((sum, r) => sum + r.installed, 0);
      const completeChapters = [...reportByChapter.values()].filter((r) => r.complete).map((r) => r.chapter_index);
      let mergeText = "";
      if (mergePatches && completeChapters.length) {
        try {
          const merged = await postJson<MergeResponse>(`/books/${bookId}/chapter-audio/merge-patches`, {
            chapter_indexes: completeChapters,
          });
          setMerge(merged);
          mergeText = merged.patches.length ? ` Đã ghép ${merged.merged}/${merged.patches.length} patch.` : "";
        } catch (err) {
          mergeText = ` Ghép patch lỗi: ${errorText(err)}`;
        }
      }
      const okChapters = [...reportByChapter.values()].filter((r) => r.installed).length;
      onMessage(
        `Đã nạp ${installed} chunk vào ${okChapters} chương` +
          ` (${completeChapters.length} chương đủ chunk).${mergeText}${failure ? ` ${errors.length} batch lỗi: ${errorText(failure)}` : ""}`
      );
      if (installed) onChanged();
    } finally {
      setBusy(false);
      setPaused(false);
      abortRef.current = null;
    }
  };

  const directoryProps = { webkitdirectory: "", directory: "" } as Record<string, string>;
  const progressPct = progress && progress.totalBytes > 0 ? Math.round((progress.doneBytes / progress.totalBytes) * 100) : 0;
  const ingestPct = ingestJob ? (ingestJob.percent ?? 0) : 0;

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-h-[90vh] max-w-3xl overflow-auto">
        <DialogHeader>
          <DialogTitle>Upload thư mục chunk theo chương</DialogTitle>
          <DialogDescription>
            Nạp chunk WAV làm sẵn vào kho chunk từng chương. Folder lớn nên chép sẵn lên server rồi nạp qua queue.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <FormatGuide maxChars={usedMaxChars ? String(usedMaxChars) : maxChars} />

          {/* Khu vực folder đã chép sẵn trên server */}
          <div className="space-y-2 rounded-md border border-border p-3">
            <div className="flex items-center justify-between gap-2">
              <div className="flex items-center gap-1.5 text-xs font-semibold">
                <HardDrive className="h-3.5 w-3.5" /> Folder đã chép lên server
              </div>
              <Button type="button" variant="ghost" size="sm" className="h-7 px-2" onClick={fetchStaging} disabled={stagingLoading || ingestJobId != null}>
                <RefreshCw className={cn("h-3.5 w-3.5", stagingLoading && "animate-spin")} /> Làm mới
              </Button>
            </div>
            <div className="text-[11px] text-muted-foreground">
              Chép thư mục chunk vào <code className="rounded bg-muted px-1 py-0.5">{stagingBase || `data/books/${bookId}/_staging`}</code> trên máy chạy server (vd. qua SMB/USB), rồi bấm “Nạp qua queue”. Server quét đĩa từng batch ~80 MB/48 file, ghi vào <code>chapters/NNNN</code>, lỗi bỏ qua không xoá nguồn.
            </div>

            {staging == null ? (
              <div className="py-2 text-center text-xs text-muted-foreground">Đang quét...</div>
            ) : staging.length === 0 ? (
              <div className="rounded-md bg-muted/40 px-3 py-2.5 text-xs text-muted-foreground">
                Chưa thấy folder chứa WAV nào trong <code>{stagingBase}</code>. Tạo <code>_staging/0001/chunk_000.wav</code> rồi bấm Làm mới.
              </div>
            ) : (
              <div className="space-y-1.5">
                {staging.map((cand) => (
                  <div key={cand.name} className="flex items-center justify-between gap-3 rounded-md border border-border bg-card px-3 py-2">
                    <div className="min-w-0">
                      <div className="truncate font-mono text-xs font-medium">{cand.name}/</div>
                      <div className="truncate text-[11px] text-muted-foreground">{cand.wav_count} WAV · {cand.path}</div>
                    </div>
                    <Button
                      type="button"
                      size="sm"
                      disabled={ingestJobId != null || busy}
                      onClick={() => startServerIngest(cand.name)}
                    >
                      <HardDrive className="h-3.5 w-3.5" /> Nạp qua queue
                    </Button>
                  </div>
                ))}
              </div>
            )}

            {ingestJobId != null && ingestJob && (
              <div className="space-y-2 rounded-md border border-primary/30 bg-primary/5 px-3 py-3">
                <div className="flex items-center justify-between text-xs">
                  <span className="font-medium">
                    Job #{ingestJob.id} · {ingestJob.status} · {ingestJob.phase || "ingesting"} · {ingestPct}%
                  </span>
                  <span className="font-mono text-[11px] text-muted-foreground">
                    {ingestJob.progress_current ?? 0}/{ingestJob.progress_total ?? "?"} batch
                  </span>
                </div>
                <div className="h-2 overflow-hidden rounded-full bg-muted">
                  <div className="h-full bg-primary transition-all" style={{ width: `${ingestPct}%` }} />
                </div>
                <div className="flex gap-2">
                  <Button type="button" variant="outline" size="sm" onClick={async () => { try { await api(`/queue/jobs/${ingestJobId}/cancel`, { method: "POST" }); } catch {} }}>
                    <X className="h-3.5 w-3.5" /> Hủy job
                  </Button>
                </div>
                {ingestJob.error_message && (
                  <div className="rounded bg-red-50 px-2 py-1 font-mono text-[11px] text-red-700">{ingestJob.error_message}</div>
                )}
              </div>
            )}
            {ingestError && (
              <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-[11px] text-red-800">{ingestError}</div>
            )}
          </div>

          <div className="relative py-1">
            <div className="absolute inset-0 flex items-center">
              <div className="w-full border-t border-border" />
            </div>
            <div className="relative flex justify-center">
              <span className="bg-background px-3 text-[11px] text-muted-foreground">hoặc chọn folder từ máy (upload qua trình duyệt)</span>
            </div>
          </div>

          <label
            className={cn(
              "flex cursor-pointer flex-col items-center gap-2 rounded-md border-2 border-dashed border-border bg-muted/20 px-5 py-6 text-center transition-colors hover:border-primary/60",
              (busy || ingestJobId != null) && "pointer-events-none opacity-60"
            )}
          >
            <FolderUp className="h-7 w-7 text-primary" />
            <span className="text-sm font-semibold">
              {files.length ? `${files.length} file đã chọn · ${formatBytes(uploadTotalBytes)}` : "Chọn thư mục chứa các thư mục chương"}
            </span>
            <span className="text-[11px] text-muted-foreground">
              {files.length
                ? `${uploadGroups.length} chương · ${uploadFileCount} chunk hợp lệ${ignored ? ` · bỏ qua ${ignored} file không phải WAV` : ""} · ${batches.length} batch (~${formatBytes(BATCH_MAX_BYTES)}/batch)`
                : "Trình duyệt sẽ hỏi xác nhận tải lên toàn bộ thư mục"}
            </span>
            <input
              ref={inputRef}
              type="file"
              multiple
              className="hidden"
              disabled={busy || ingestJobId != null}
              {...directoryProps}
              onChange={(event) => {
                setFiles(Array.from(event.target.files || []));
                setReports(undefined);
                setServerRejected([]);
                setMerge(undefined);
                setBatchErrors([]);
              }}
            />
          </label>

          {isLarge && !busy && !reports && (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2.5 text-xs text-amber-900">
              <div className="font-semibold">Folder lớn {formatBytes(uploadTotalBytes)} · {uploadFileCount} file</div>
              <div className="mt-1">
                Với folder &gt;2 GB nên dùng cách chép lên server rồi “Nạp qua queue” ở trên để tránh tràn RAM/timeout trình duyệt. Upload qua trình duyệt cũng được chia ~{formatBytes(BATCH_MAX_BYTES)}/~{BATCH_MAX_FILES} file mỗi batch nhưng vẫn tốn băng thông.
              </div>
            </div>
          )}

          {busy && progress && (
            <div className="space-y-2 rounded-md border border-border bg-muted/20 px-3 py-3">
              <div className="flex items-center justify-between text-xs">
                <span className="font-medium">
                  {progress.doneBatches}/{progress.totalBatches} batch · {formatBytes(progress.doneBytes)}/{formatBytes(progress.totalBytes)} {progressPct}%
                </span>
                <span className="font-mono text-[11px] text-muted-foreground">
                  {progress.doneChapters.size} chương đã ghi
                </span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-muted">
                <div className="h-full bg-primary transition-all" style={{ width: `${progressPct}%` }} />
              </div>
              <div className="flex gap-2">
                <Button type="button" variant="outline" size="sm" onClick={togglePaused}>
                  <Pause className="h-3.5 w-3.5" /> {paused ? "Tiếp tục" : "Tạm dừng"}
                </Button>
                <Button type="button" variant="ghost" size="sm" onClick={cancel}>
                  <X className="h-3.5 w-3.5" /> Hủy
                </Button>
              </div>
            </div>
          )}

          {(invalid.length > 0 || unknownChapters.length > 0) && (
            <div className="space-y-1 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-900">
              {unknownChapters.length > 0 && (
                <div>
                  Sách không có chương {unknownChapters.map((group) => group.chapterIndex + 1).join(", ")} — bỏ qua{" "}
                  {unknownChapters.reduce((sum, group) => sum + group.files.length, 0)} file.
                </div>
              )}
              {invalid.length > 0 && (
                <>
                  <div className="font-semibold">{invalid.length} file sai định dạng sẽ bị bỏ qua:</div>
                  <ul className="max-h-24 overflow-auto font-mono">
                    {invalid.slice(0, 50).map((item) => (
                      <li key={item.path} className="truncate">
                        {item.path} — {item.reason}
                      </li>
                    ))}
                    {invalid.length > 50 && <li>… và {invalid.length - 50} file khác</li>}
                  </ul>
                </>
              )}
            </div>
          )}

          {batchErrors.length > 0 && (
            <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-[11px] text-red-800">
              <div className="font-semibold">{batchErrors.length} batch lỗi (đã thử lại {BATCH_RETRY} lần):</div>
              <ul className="max-h-24 overflow-auto font-mono">
                {batchErrors.slice(0, 10).map((e, i) => (
                  <li key={i} className="truncate">{e}</li>
                ))}
              </ul>
            </div>
          )}

          {uploadGroups.length > 0 && (
            <div className="max-h-64 overflow-auto rounded-md border border-border">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-muted/60 text-left text-[11px] text-muted-foreground">
                  <tr>
                    <th className="px-3 py-1.5 font-medium">Ch.</th>
                    <th className="px-3 py-1.5 font-medium">Tiêu đề</th>
                    <th className="px-3 py-1.5 text-right font-medium">Chunk</th>
                    <th className="px-3 py-1.5 text-right font-medium">Dung lượng</th>
                    <th className="px-3 py-1.5 font-medium">Kết quả</th>
                  </tr>
                </thead>
                <tbody>
                  {uploadGroups.map((group) => {
                    const report = reports?.find((item) => item.chapter_index === group.chapterIndex);
                    const meta = report ? STATUS_META[report.status] : undefined;
                    const indexes = group.files.map((item) => item.chunkIndex);
                    return (
                      <tr key={group.chapterIndex} className="border-t border-border align-top">
                        <td className="px-3 py-1.5 font-mono">{group.chapterIndex + 1}</td>
                        <td className="max-w-[14rem] truncate px-3 py-1.5">
                          {chapterByIndex.get(group.chapterIndex)?.title}
                        </td>
                        <td className="whitespace-nowrap px-3 py-1.5 text-right font-mono">
                          {report?.expected !== undefined
                            ? `${report.installed}/${report.expected}`
                            : `${group.files.length} (${indexes[0]}–${indexes[indexes.length - 1]})`}
                        </td>
                        <td className="whitespace-nowrap px-3 py-1.5 text-right font-mono text-muted-foreground">
                          {formatBytes(group.bytes)}
                        </td>
                        <td className="px-3 py-1.5">
                          {meta ? (
                            <div className={cn("flex items-start gap-1", meta.className)}>
                              <meta.icon className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                              <div className="min-w-0">
                                <div>{report?.detail || meta.label}</div>
                                {report && report.missing.length > 0 && (
                                  <div className="truncate font-mono text-[10px] text-muted-foreground">
                                    thiếu: {report.missing.slice(0, 12).join(", ")}
                                    {report.missing.length > 12 ? "…" : ""}
                                  </div>
                                )}
                                {report?.rejected.map((item) => (
                                  <div key={item.filename} className="truncate text-[10px] text-red-700">
                                    {item.filename.split(/[\\/]/).pop()}: {item.reason}
                                  </div>
                                ))}
                              </div>
                            </div>
                          ) : (
                            <span className="text-muted-foreground">{busy ? "Đang chờ…" : "Sẵn sàng"}</span>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {serverRejected.length > 0 && (
            <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-[11px] text-red-800">
              {serverRejected.length} file bị máy chủ từ chối:{" "}
              {serverRejected.slice(0, 5).map((item) => `${item.filename} (${item.reason})`).join("; ")}
            </div>
          )}

          {merge && merge.patches.length > 0 && (
            <div className="space-y-1 rounded-md border border-border bg-muted/20 px-3 py-2 text-[11px]">
              <div className="font-semibold">
                Ghép patch: {merge.merged}/{merge.patches.length}
              </div>
              {merge.patches.map((patch) => (
                <div
                  key={patch.patch_id}
                  className={patch.result === "merged" ? "text-emerald-700" : "text-muted-foreground"}
                >
                  #{patch.patch_index + 1} {patch.name}:{" "}
                  {patch.result === "merged" ? "đã ghép audio" : `bỏ qua — ${patch.detail}`}
                </div>
              ))}
            </div>
          )}

          <div className="space-y-2 rounded-md border border-border p-3">
            <CheckField
              checked={replace}
              disabled={busyAny}
              onChange={setReplace}
              label="Thay toàn bộ: xoá chunk cũ của các chương được nạp trước khi ghi"
            />
            <CheckField
              checked={mergePatches}
              disabled={busyAny}
              onChange={setMergePatches}
              label="Ghép ngay các patch phủ chương đã đủ chunk (thay audio hiện tại của patch)"
            />
          </div>
        </div>

        <DialogFooter>
          {progress && busy && (
            <span className="mr-auto self-center font-mono text-[11px] text-muted-foreground">
              Batch {progress.doneBatches}/{progress.totalBatches} · {progressPct}%
            </span>
          )}
          {ingestJob && ingestJobId != null && (
            <span className="mr-auto self-center font-mono text-[11px] text-muted-foreground">
              Queue #{ingestJob.id} · {ingestJob.status}
            </span>
          )}
          <Button type="button" variant="outline" disabled={busyAny && !paused} onClick={() => (busy ? cancel() : close(false))}>
            {busy || ingestJobId != null ? "Đang chạy..." : "Đóng"}
          </Button>
          <Button type="button" disabled={busy || ingestJobId != null || !uploadGroups.length} onClick={runBrowserUpload}>
            <FolderUp className="h-3.5 w-3.5" />{" "}
            {busy ? "Đang nạp..." : `Upload trình duyệt · ${uploadFileCount} chunk`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
