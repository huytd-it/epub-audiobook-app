import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  ChevronRight,
  Folder,
  FolderOpen,
  FileImage,
  FileVideo,
  FileAudio,
  File,
  Search,
  X,
  Grid3X3,
  List,
  Check,
} from "lucide-react";
import { api } from "@/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { LoadingState, EmptyState } from "@/components/common/Header";

// ------------------------------------------------------------------ Types -- //

export type MediaKind = "image" | "video" | "audio" | "file" | "directory";

export interface MediaEntry {
  name: string;
  path: string;
  is_dir: boolean;
  size: number;
  modified: number;
  ext: string;
  mime: string;
  kind: MediaKind;
}

export interface BrowseResult {
  path: string;
  entries: MediaEntry[];
  roots: string[];
}

export type ViewMode = "grid" | "list";

export interface MediaBrowserProps {
  /** Pre-selected file path (controlled). */
  selectedPath?: string | null;
  /** Callback when user selects a file. Omit to disable selection mode. */
  onSelect?: (entry: MediaEntry) => void;
  /** Initial root path to open (e.g. "_Nền"). */
  initialPath?: string;
  /** Category filter sent to the backend. */
  category?: string;
  /** Extra CSS class on the outer container. */
  className?: string;
  /** Height of the outer container (default: 600px). */
  height?: number | string;
}

// ------------------------------------------------------------- Utilities -- //

function formatSize(bytes: number): string {
  if (bytes === 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function KindIcon({ kind, className }: { kind: MediaKind; className?: string }) {
  const cls = className ?? "h-4 w-4";
  switch (kind) {
    case "image":
      return <FileImage className={cls} />;
    case "video":
      return <FileVideo className={cls} />;
    case "audio":
      return <FileAudio className={cls} />;
    case "directory":
      return <Folder className={cls} />;
    default:
      return <File className={cls} />;
  }
}

function kindColor(kind: MediaKind): string {
  switch (kind) {
    case "image":
      return "text-emerald-500";
    case "video":
      return "text-cobalt";
    case "audio":
      return "text-primary";
    default:
      return "text-muted-foreground";
  }
}

const IMAGE_EXTS = new Set([".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".svg"]);

function isImageEntry(entry: MediaEntry): boolean {
  return entry.kind === "image" && IMAGE_EXTS.has(entry.ext);
}

// -------------------------------------------------------- Breadcrumbs -- //

function Breadcrumbs({
  path,
  onNavigate,
}: {
  path: string;
  onNavigate: (p: string) => void;
}) {
  const segments = path ? path.split("/").filter(Boolean) : [];
  return (
    <nav className="flex items-center gap-1 text-xs font-mono flex-wrap min-h-[28px]" aria-label="Đường dẫn">
      <button
        onClick={() => onNavigate("")}
        className="px-1.5 py-0.5 rounded hover:bg-muted transition-colors text-muted-foreground hover:text-foreground"
      >
        Tất cả thư mục
      </button>
      {segments.map((seg, i) => {
        const partial = segments.slice(0, i + 1).join("/");
        const isLast = i === segments.length - 1;
        return (
          <React.Fragment key={partial}>
            <ChevronRight className="h-3 w-3 text-muted-foreground/50 shrink-0" />
            <button
              onClick={() => onNavigate(partial)}
              className={`px-1.5 py-0.5 rounded transition-colors truncate max-w-[160px] ${
                isLast
                  ? "font-semibold text-foreground bg-muted"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground"
              }`}
              title={partial}
            >
              {seg}
            </button>
          </React.Fragment>
        );
      })}
    </nav>
  );
}

// -------------------------------------------------------- Preview -- //

function PreviewPane({
  entry,
  previewBase,
}: {
  entry: MediaEntry;
  previewBase: string;
}) {
  const url = `${previewBase}?path=${encodeURIComponent(entry.path)}`;

  if (entry.kind === "image") {
    return (
      <div className="flex items-center justify-center bg-black/80 rounded border border-border overflow-hidden min-h-[200px] max-h-[60vh]">
        <img
          src={url}
          alt={entry.name}
          className="max-h-full max-w-full object-contain"
          loading="lazy"
        />
      </div>
    );
  }
  if (entry.kind === "video") {
    return (
      <div className="bg-black rounded border border-border overflow-hidden min-h-[200px]">
        <video
          src={url}
          controls
          className="w-full object-contain max-h-[60vh]"
          preload="metadata"
        />
      </div>
    );
  }
  if (entry.kind === "audio") {
    return (
      <div className="flex flex-col items-center justify-center bg-muted/50 rounded border border-border p-6 min-h-[200px] gap-4">
        <FileAudio className="h-12 w-12 text-primary/60" />
        <audio src={url} controls className="w-full max-w-sm" preload="metadata" />
      </div>
    );
  }
  return (
    <div className="flex flex-col items-center justify-center bg-muted/30 rounded border border-border p-6 min-h-[200px] gap-2">
      <File className="h-10 w-10 text-muted-foreground/50" />
      <span className="text-xs text-muted-foreground font-mono">{entry.ext || "N/A"}</span>
    </div>
  );
}

// ----------------------------------------------- Grid thumbnail -- //

function GridThumb({ entry, previewBase }: { entry: MediaEntry; previewBase: string }) {
  if (!isImageEntry(entry)) return null;
  const url = `${previewBase}?path=${encodeURIComponent(entry.path)}`;
  return (
    <img
      src={url}
      alt={entry.name}
      className="w-full h-20 object-cover rounded border border-border"
      loading="lazy"
    />
  );
}

// ------------------------------------------------- Main Component -- //

export function MediaBrowser({
  selectedPath,
  onSelect,
  initialPath = "",
  category = "",
  className,
  height = 600,
}: MediaBrowserProps) {
  const [currentPath, setCurrentPath] = useState(initialPath);
  const [data, setData] = useState<BrowseResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [viewMode, setViewMode] = useState<ViewMode>("grid");
  const [previewEntry, setPreviewEntry] = useState<MediaEntry | null>(null);

  const load = useCallback(
    async (p: string) => {
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams();
        if (p) params.set("path", p);
        if (category) params.set("category", category);
        const res = await api<BrowseResult>(`/api/ui/media-browser/browse?${params}`);
        setData(res);
      } catch (err: any) {
        setError(err?.message || "Lỗi tải dữ liệu");
      } finally {
        setLoading(false);
      }
    },
    [category],
  );

  useEffect(() => {
    load(currentPath);
  }, [load, currentPath]);

  // Reset preview when path changes
  useEffect(() => {
    setPreviewEntry(null);
  }, [currentPath]);

  const navigate = useCallback((p: string) => {
    setCurrentPath(p);
    setSearch("");
    setPreviewEntry(null);
  }, []);

  const filteredEntries = useMemo(() => {
    if (!data) return [];
    if (!search) return data.entries;
    const q = search.toLowerCase();
    return data.entries.filter((e) => e.name.toLowerCase().includes(q));
  }, [data, search]);

  const dirs = useMemo(() => filteredEntries.filter((e) => e.is_dir), [filteredEntries]);
  const files = useMemo(() => filteredEntries.filter((e) => !e.is_dir), [filteredEntries]);

  const handleEntryClick = useCallback(
    (entry: MediaEntry) => {
      if (entry.is_dir) {
        navigate(entry.path);
      } else {
        setPreviewEntry(entry);
      }
    },
    [navigate],
  );

  const handleKeyDown = useCallback(
    (entry: MediaEntry, e: React.KeyboardEvent) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        handleEntryClick(entry);
      }
    },
    [handleEntryClick],
  );

  return (
    <div
      className={`flex flex-col border border-border rounded-lg bg-card overflow-hidden ${className ?? ""}`}
      style={{ height }}
    >
      {/* Toolbar */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border bg-muted/30 shrink-0">
        <div className="flex-1 min-w-0">
          <Breadcrumbs path={currentPath} onNavigate={navigate} />
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <Button
            variant={viewMode === "grid" ? "default" : "ghost"}
            size="icon"
            className="h-7 w-7"
            onClick={() => setViewMode("grid")}
            aria-label="Hiển thị lưới"
          >
            <Grid3X3 className="h-3.5 w-3.5" />
          </Button>
          <Button
            variant={viewMode === "list" ? "default" : "ghost"}
            size="icon"
            className="h-7 w-7"
            onClick={() => setViewMode("list")}
            aria-label="Hiển thị danh sách"
          >
            <List className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      {/* Search + count */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border shrink-0">
        <div className="relative flex-1 max-w-xs">
          <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
          <Input
            placeholder="Tìm file..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="h-7 pl-7 text-xs"
          />
          {search && (
            <button
              onClick={() => setSearch("")}
              className="absolute right-1.5 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
            >
              <X className="h-3 w-3" />
            </button>
          )}
        </div>
        <span className="text-[10px] font-mono text-muted-foreground shrink-0">
          {dirs.length} thư mục, {files.length} file
        </span>
      </div>

      {/* Body */}
      <div className="flex flex-col lg:flex-row flex-1 min-h-0 overflow-hidden">
        {/* File list */}
        <div className="flex-1 overflow-y-auto min-w-0">
          {loading ? (
            <LoadingState text="Đang tải..." />
          ) : error ? (
            <div className="flex flex-col items-center justify-center py-12 px-4 text-center">
              <span className="text-xs text-destructive font-mono mb-2">{error}</span>
              <Button variant="outline" size="sm" onClick={() => load(currentPath)}>
                Thử lại
              </Button>
            </div>
          ) : filteredEntries.length === 0 ? (
            <EmptyState text={search ? "Không tìm thấy file phù hợp" : "Thư mục trống"} />
          ) : viewMode === "grid" ? (
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2 p-3">
              {dirs.map((entry) => (
                <button
                  key={entry.path}
                  onClick={() => handleEntryClick(entry)}
                  onKeyDown={(e) => handleKeyDown(entry, e)}
                  className="flex flex-col items-center gap-1.5 p-3 rounded-md border border-transparent hover:border-primary/40 hover:bg-muted/50 transition-colors text-center group focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                >
                  <FolderOpen className="h-8 w-8 text-yellow-500/80 group-hover:text-yellow-500 shrink-0" />
                  <span className="text-xs font-medium text-foreground truncate w-full" title={entry.name}>
                    {entry.name}
                  </span>
                </button>
              ))}
              {files.map((entry) => {
                const isSelected = selectedPath === entry.path;
                return (
                  <button
                    key={entry.path}
                    onClick={() => handleEntryClick(entry)}
                    onKeyDown={(e) => handleKeyDown(entry, e)}
                    className={`flex flex-col items-center gap-1.5 p-3 rounded-md border transition-colors text-center group
                      ${isSelected ? "border-primary bg-primary/10 ring-1 ring-primary/30" : "border-transparent hover:border-primary/40 hover:bg-muted/50"}
                      focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary`}
                  >
                    <div className="relative w-full">
                      <GridThumb entry={entry} previewBase="/api/ui/media-browser/preview" />
                      {!isImageEntry(entry) && (
                        <div className="flex justify-center py-1">
                          <KindIcon kind={entry.kind} className={`h-8 w-8 shrink-0 ${kindColor(entry.kind)}`} />
                        </div>
                      )}
                      {isSelected && (
                        <span className="absolute -top-1 -right-1 h-4 w-4 rounded-full bg-primary flex items-center justify-center">
                          <Check className="h-2.5 w-2.5 text-primary-foreground" />
                        </span>
                      )}
                    </div>
                    <span className="text-xs font-medium text-foreground truncate w-full" title={entry.name}>
                      {entry.name}
                    </span>
                    <span className="text-[10px] font-mono text-muted-foreground">{formatSize(entry.size)}</span>
                  </button>
                );
              })}
            </div>
          ) : (
            <div className="divide-y divide-border">
              {dirs.map((entry) => (
                <button
                  key={entry.path}
                  onClick={() => handleEntryClick(entry)}
                  onKeyDown={(e) => handleKeyDown(entry, e)}
                  className="w-full flex items-center gap-3 px-3 py-2 text-xs hover:bg-muted/50 transition-colors text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                >
                  <FolderOpen className="h-4 w-4 text-yellow-500/80 shrink-0" />
                  <span className="font-medium text-foreground truncate flex-1">{entry.name}</span>
                  <ChevronRight className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                </button>
              ))}
              {files.map((entry) => {
                const isSelected = selectedPath === entry.path;
                return (
                  <button
                    key={entry.path}
                    onClick={() => handleEntryClick(entry)}
                    onKeyDown={(e) => handleKeyDown(entry, e)}
                    className={`w-full flex items-center gap-3 px-3 py-2 text-xs transition-colors text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary ${
                      isSelected
                        ? "bg-primary/10"
                        : "hover:bg-muted/50"
                    }`}
                  >
                    <KindIcon kind={entry.kind} className={`h-4 w-4 shrink-0 ${kindColor(entry.kind)}`} />
                    <span className="font-medium text-foreground truncate flex-1">{entry.name}</span>
                    <span className="text-[10px] font-mono text-muted-foreground shrink-0">{formatSize(entry.size)}</span>
                    {isSelected && <Check className="h-3.5 w-3.5 text-primary shrink-0" />}
                  </button>
                );
              })}
            </div>
          )}
        </div>

        {/* Preview sidebar – hidden on small screens, overlay on mobile */}
        {previewEntry && (
          <>
            {/* Mobile overlay */}
            <div className="lg:hidden fixed inset-0 z-50 bg-background/80 backdrop-blur-sm" onClick={() => setPreviewEntry(null)} />
            <div className="lg:hidden fixed bottom-0 left-0 right-0 z-50 bg-card border-t border-border rounded-t-xl max-h-[70vh] overflow-auto p-4">
              <div className="flex items-center justify-between mb-3">
                <span className="text-xs font-semibold text-foreground truncate" title={previewEntry.name}>
                  {previewEntry.name}
                </span>
                <div className="flex items-center gap-1 shrink-0">
                  {onSelect && (
                    <Button size="sm" className="h-6 px-2 text-[10px]" onClick={() => onSelect(previewEntry)}>
                      Chọn
                    </Button>
                  )}
                  <button
                    onClick={() => setPreviewEntry(null)}
                    className="text-muted-foreground hover:text-foreground p-0.5"
                    aria-label="Đóng preview"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
              <PreviewPane entry={previewEntry} previewBase="/api/ui/media-browser/preview" />
              <div className="mt-3 space-y-1 text-[10px] font-mono text-muted-foreground">
                <div>Kích thước: {formatSize(previewEntry.size)}</div>
                {previewEntry.ext && <div>Loại: {previewEntry.ext}</div>}
                {previewEntry.mime && <div>MIME: {previewEntry.mime}</div>}
              </div>
            </div>

            {/* Desktop sidebar */}
            <div className="hidden lg:flex w-80 border-l border-border flex-col shrink-0 bg-muted/10">
              <div className="flex items-center justify-between px-3 py-2 border-b border-border shrink-0">
                <span className="text-xs font-semibold text-foreground truncate" title={previewEntry.name}>
                  {previewEntry.name}
                </span>
                <div className="flex items-center gap-1 shrink-0">
                  {onSelect && (
                    <Button size="sm" className="h-6 px-2 text-[10px]" onClick={() => onSelect(previewEntry)}>
                      Chọn
                    </Button>
                  )}
                  <button
                    onClick={() => setPreviewEntry(null)}
                    className="text-muted-foreground hover:text-foreground p-0.5"
                    aria-label="Đóng preview"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
              <div className="flex-1 p-3 overflow-auto">
                <PreviewPane entry={previewEntry} previewBase="/api/ui/media-browser/preview" />
                <div className="mt-3 space-y-1 text-[10px] font-mono text-muted-foreground">
                  <div>Kích thước: {formatSize(previewEntry.size)}</div>
                  {previewEntry.ext && <div>Loại: {previewEntry.ext}</div>}
                  {previewEntry.mime && <div>MIME: {previewEntry.mime}</div>}
                </div>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
