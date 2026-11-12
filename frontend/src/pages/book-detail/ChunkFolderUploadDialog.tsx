import React, { useMemo, useRef, useState } from "react";
import { AlertTriangle, CheckCircle2, CircleDashed, FolderUp, Info } from "lucide-react";
import { Chapter, postForm, postJson } from "@/api";
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

/** Cùng quy ước với app/chapter_chunk_upload.py: thư mục chương bắt đầu bằng số chương
 * (đánh từ 1), file chunk_NNN.wav hoặc NNN.wav (đánh từ 0). */
const CHAPTER_DIR = /^(\d+)/;
const CHUNK_FILE = /^(?:chunk_)?(\d+)\.wav$/i;
/** Số file tối đa trong một request — chương không bao giờ bị tách qua hai request. */
const BATCH_FILES = 300;

type ParsedFile = { file: File; path: string; chapterIndex: number; chunkIndex: number };
type InvalidFile = { path: string; reason: string };

type ChapterGroup = { chapterIndex: number; files: ParsedFile[]; duplicates: number };

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

function relativePath(file: File) {
  return (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name;
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
      // chunks.json, ảnh, file ẩn của hệ điều hành... không phải chunk — bỏ qua im lặng.
      ignored++;
      continue;
    }
    const fileMatch = CHUNK_FILE.exec(name);
    const dirMatch = parts.length >= 2 ? CHAPTER_DIR.exec(parts[parts.length - 2]) : null;
    if (!fileMatch) invalid.push({ path, reason: "tên file phải là chunk_NNN.wav" });
    else if (parts.length < 2) invalid.push({ path, reason: "phải nằm trong thư mục chương" });
    else if (!dirMatch) invalid.push({ path, reason: `thư mục “${parts[parts.length - 2]}” không bắt đầu bằng số chương` });
    else if (Number(dirMatch[1]) < 1) invalid.push({ path, reason: "số chương đánh từ 1" });
    else parsed.push({ file, path, chapterIndex: Number(dirMatch[1]) - 1, chunkIndex: Number(fileMatch[1]) });
  }
  const map = new Map<number, ChapterGroup>();
  for (const item of parsed) {
    const group = map.get(item.chapterIndex) || { chapterIndex: item.chapterIndex, files: [], duplicates: 0 };
    if (group.files.some((other) => other.chunkIndex === item.chunkIndex)) {
      group.duplicates++;
      invalid.push({ path: item.path, reason: `trùng chunk ${item.chunkIndex} của chương ${item.chapterIndex + 1}` });
    } else {
      group.files.push(item);
    }
    map.set(item.chapterIndex, group);
  }
  const groups = [...map.values()]
    .map((group) => ({ ...group, files: group.files.sort((a, b) => a.chunkIndex - b.chunkIndex) }))
    .sort((a, b) => a.chapterIndex - b.chapterIndex);
  return { groups, invalid, ignored };
}

/** Chia các chương thành lô ≤ BATCH_FILES file, giữ trọn từng chương trong một lô. */
function batchGroups(groups: ChapterGroup[]) {
  const batches: ChapterGroup[][] = [];
  let current: ChapterGroup[] = [];
  let size = 0;
  for (const group of groups) {
    if (current.length && size + group.files.length > BATCH_FILES) {
      batches.push(current);
      current = [];
      size = 0;
    }
    current.push(group);
    size += group.files.length;
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
          giống cột “Ch.” (<code>0001</code>, <code>12</code>, <code>0012 - Tên</code> đều được). Có thể chọn thư mục
          cha chứa nhiều chương, hoặc chọn thẳng một thư mục chương.
        </li>
        <li>
          <span className="font-medium text-foreground">File chunk</span>: <code>chunk_NNN.wav</code> (hoặc{" "}
          <code>NNN.wav</code>), đánh từ <span className="font-semibold">0</span> trong từng chương. File khác (vd.{" "}
          <code>chunks.json</code>) được bỏ qua.
        </li>
        <li>
          <span className="font-medium text-foreground">Chunk N phải đọc đúng đoạn văn N</span> theo cách tách hiện
          tại của sách (chuẩn hoá + max_chars{maxChars ? ` = ${maxChars}` : ""}) — xem danh sách chunk ở tab Audio
          của từng chương. Đổi chuẩn hoá/max_chars sau khi nạp sẽ làm chunk thành “cũ”.
        </li>
        <li>
          <span className="font-medium text-foreground">Âm thanh</span>: WAV PCM, mono hoặc stereo (tự trộn về mono).
          Mọi chunk của cùng một chương — và nên là cả sách — phải <span className="font-semibold">cùng sample rate</span>{" "}
          (vd. 24000 Hz), nếu không patch không ghép được. Âm lượng từng chunk được chuẩn hoá khi nạp.
        </li>
        <li>
          Chunk có sẵn của chương được giữ lại trừ khi bật “Thay toàn bộ”; chunk trùng số thứ tự bị ghi đè. Thư mục
          kho chunk của chính app (<code>chapters/0001/…</code>) cũng đúng định dạng này.
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

/** Hộp thoại nạp thư mục chunk WAV làm sẵn vào kho chunk theo chương, rồi (tuỳ chọn)
 * ghép trực tiếp các patch phủ những chương đã đủ chunk. */
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
  /** max_chars hiện tại trong cấu hình audio — chỉ để hiển thị trong hướng dẫn. */
  maxChars?: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onMessage: (message: string) => void;
  /** Kho chunk / patch đã đổi: làm mới cột Audio và danh sách patch. */
  onChanged: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [replace, setReplace] = useState(false);
  const [mergePatches, setMergePatches] = useState(true);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<{ done: number; total: number }>();
  const [reports, setReports] = useState<ChapterUploadReport[]>();
  const [serverRejected, setServerRejected] = useState<{ filename: string; reason: string }[]>([]);
  const [merge, setMerge] = useState<MergeResponse>();
  const [usedMaxChars, setUsedMaxChars] = useState<number>();

  const chapterByIndex = useMemo(() => new Map(chapters.map((chapter) => [chapter.chapter_index, chapter])), [chapters]);
  const { groups, invalid, ignored } = useMemo(() => parseFiles(files), [files]);
  const unknownChapters = groups.filter((group) => !chapterByIndex.has(group.chapterIndex));
  const uploadGroups = groups.filter((group) => chapterByIndex.has(group.chapterIndex));
  const uploadFileCount = uploadGroups.reduce((sum, group) => sum + group.files.length, 0);

  const reset = () => {
    setFiles([]);
    setReports(undefined);
    setServerRejected([]);
    setMerge(undefined);
    setProgress(undefined);
    setUsedMaxChars(undefined);
    if (inputRef.current) inputRef.current.value = "";
  };

  const close = (next: boolean) => {
    if (busy) return;
    if (!next) reset();
    onOpenChange(next);
  };

  const run = async () => {
    if (!uploadGroups.length || busy) return;
    setBusy(true);
    setReports(undefined);
    setServerRejected([]);
    setMerge(undefined);
    const batches = batchGroups(uploadGroups);
    const collected: ChapterUploadReport[] = [];
    const rejected: { filename: string; reason: string }[] = [];
    let failure: unknown;
    setProgress({ done: 0, total: uploadGroups.length });
    try {
      for (const batch of batches) {
        const form = new FormData();
        form.append("replace", replace ? "true" : "false");
        for (const group of batch) {
          for (const item of group.files) form.append("files", item.file, item.path);
        }
        try {
          const result = await postForm<UploadResponse>(`/books/${bookId}/chapter-audio/upload`, form);
          collected.push(...result.chapters);
          rejected.push(...result.rejected);
          setUsedMaxChars(result.max_chars);
        } catch (err) {
          failure = failure || err;
          for (const group of batch) {
            collected.push({
              chapter_index: group.chapterIndex,
              chapter_no: group.chapterIndex + 1,
              received: group.files.length,
              installed: 0,
              missing: [],
              rejected: [],
              complete: false,
              status: "error",
              detail: errorText(err),
            });
          }
        }
        setReports([...collected]);
        setProgress((current) => current && { ...current, done: current.done + batch.length });
      }
      setServerRejected(rejected);

      const installed = collected.reduce((sum, report) => sum + report.installed, 0);
      const complete = collected.filter((report) => report.complete).map((report) => report.chapter_index);
      let mergeText = "";
      if (mergePatches && complete.length) {
        try {
          const merged = await postJson<MergeResponse>(`/books/${bookId}/chapter-audio/merge-patches`, {
            chapter_indexes: complete,
          });
          setMerge(merged);
          mergeText = merged.patches.length ? ` Đã ghép ${merged.merged}/${merged.patches.length} patch.` : "";
        } catch (err) {
          mergeText = ` Ghép patch lỗi: ${errorText(err)}`;
        }
      }
      onMessage(
        `Đã nạp ${installed} chunk vào ${collected.filter((report) => report.installed).length} chương` +
          ` (${complete.length} chương đủ chunk).${mergeText}${failure ? ` ${errorText(failure)}` : ""}`
      );
      if (installed) onChanged();
    } finally {
      setBusy(false);
    }
  };

  const directoryProps = { webkitdirectory: "", directory: "" } as Record<string, string>;

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-h-[90vh] max-w-3xl overflow-auto">
        <DialogHeader>
          <DialogTitle>Upload thư mục chunk theo chương</DialogTitle>
          <DialogDescription>
            Nạp chunk WAV đã tổng hợp ở nơi khác (TTS ngoài, máy khác, bản sao lưu) vào kho chunk của từng chương. Patch
            phủ các chương đủ chunk có thể được ghép ngay, không phải tạo lại audio.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <FormatGuide maxChars={usedMaxChars ? String(usedMaxChars) : maxChars} />

          <label
            className={cn(
              "flex cursor-pointer flex-col items-center gap-2 rounded-md border-2 border-dashed border-border bg-muted/20 px-5 py-6 text-center transition-colors hover:border-primary/60",
              busy && "pointer-events-none opacity-60"
            )}
          >
            <FolderUp className="h-7 w-7 text-primary" />
            <span className="text-sm font-semibold">
              {files.length ? `${files.length} file đã chọn` : "Chọn thư mục chứa các thư mục chương"}
            </span>
            <span className="text-[11px] text-muted-foreground">
              {files.length
                ? `${uploadGroups.length} chương · ${uploadFileCount} chunk hợp lệ${ignored ? ` · bỏ qua ${ignored} file không phải WAV` : ""}`
                : "Trình duyệt sẽ hỏi xác nhận tải lên toàn bộ thư mục"}
            </span>
            <input
              ref={inputRef}
              type="file"
              multiple
              className="hidden"
              disabled={busy}
              {...directoryProps}
              onChange={(event) => {
                setFiles(Array.from(event.target.files || []));
                setReports(undefined);
                setServerRejected([]);
                setMerge(undefined);
              }}
            />
          </label>

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

          {uploadGroups.length > 0 && (
            <div className="max-h-64 overflow-auto rounded-md border border-border">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-muted/60 text-left text-[11px] text-muted-foreground">
                  <tr>
                    <th className="px-3 py-1.5 font-medium">Ch.</th>
                    <th className="px-3 py-1.5 font-medium">Tiêu đề</th>
                    <th className="px-3 py-1.5 text-right font-medium">Chunk</th>
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
                        <td className="max-w-[16rem] truncate px-3 py-1.5">
                          {chapterByIndex.get(group.chapterIndex)?.title}
                        </td>
                        <td className="whitespace-nowrap px-3 py-1.5 text-right font-mono">
                          {report?.expected !== undefined
                            ? `${report.installed}/${report.expected}`
                            : `${group.files.length} (${indexes[0]}–${indexes[indexes.length - 1]})`}
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
              disabled={busy}
              onChange={setReplace}
              label="Thay toàn bộ: xoá chunk cũ của các chương được nạp trước khi ghi"
            />
            <CheckField
              checked={mergePatches}
              disabled={busy}
              onChange={setMergePatches}
              label="Ghép ngay các patch phủ chương đã đủ chunk (thay audio hiện tại của patch)"
            />
          </div>
        </div>

        <DialogFooter>
          {progress && busy && (
            <span className="mr-auto self-center font-mono text-[11px] text-muted-foreground">
              Đang nạp {progress.done}/{progress.total} chương…
            </span>
          )}
          <Button type="button" variant="outline" disabled={busy} onClick={() => close(false)}>
            Đóng
          </Button>
          <Button type="button" disabled={busy || !uploadGroups.length} onClick={run}>
            <FolderUp className="h-3.5 w-3.5" />{" "}
            {busy ? "Đang nạp..." : `Nạp ${uploadFileCount} chunk / ${uploadGroups.length} chương`}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
