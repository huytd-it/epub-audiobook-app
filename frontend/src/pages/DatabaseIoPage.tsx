import React, { useEffect, useState } from "react";
import { Database, Download, Upload, AlertTriangle } from "lucide-react";
import { api, postForm } from "@/api";
import { Header } from "@/components/common/Header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export function DatabaseIoPage() {
  const [tables, setTables] = useState<string[]>([]);
  const [format, setFormat] = useState<"sql" | "json">("sql");
  const [mode, setMode] = useState<"overwrite" | "merge">("overwrite");
  const [file, setFile] = useState<File | null>(null);
  const [tableFilter, setTableFilter] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { api<any>("/database-io").catch(() => undefined); }, []);
  const exportDb = () => { const params = new URLSearchParams({ format }); if (tableFilter.trim()) params.set("tables", tableFilter.trim()); window.location.href = `/api/db/export?${params}`; };
  const importDb = async (e: React.FormEvent) => { e.preventDefault(); if (!file) return; setBusy(true); const form = new FormData(); form.append("file", file); form.append("format", format); form.append("mode", mode); if (tableFilter.trim()) form.append("tables", tableFilter.trim()); try { await postForm("/api/db/import", form); alert("Nhập dữ liệu thành công."); } catch (err: any) { alert(`Nhập dữ liệu thất bại: ${err.message}`); } finally { setBusy(false); } };
  return <div className="space-y-6"><Header title="Cơ sở dữ liệu · Import / Export" subtitle="Sao lưu và khôi phục dữ liệu SQLite bằng payload thực từ FastAPI." /><div className="grid grid-cols-1 lg:grid-cols-2 gap-6"><Card><CardHeader><CardTitle className="text-sm flex items-center gap-2"><Download className="h-4 w-4 text-primary" /> Xuất cơ sở dữ liệu</CardTitle></CardHeader><CardContent className="space-y-4"><label className="text-xs font-semibold">Định dạng<select className="mt-1 h-9 w-full rounded-md border border-input bg-background px-3 text-sm" value={format} onChange={(e) => setFormat(e.target.value as any)}><option value="sql">SQL</option><option value="json">JSON</option></select></label><Input placeholder="Bảng cần xuất, phân cách dấu phẩy (tùy chọn)" value={tableFilter} onChange={(e) => setTableFilter(e.target.value)} /><Button onClick={exportDb}><Download className="h-4 w-4" /> Tải file backup</Button></CardContent></Card><Card><CardHeader><CardTitle className="text-sm flex items-center gap-2"><Upload className="h-4 w-4 text-primary" /> Nhập cơ sở dữ liệu</CardTitle></CardHeader><CardContent><form onSubmit={importDb} className="space-y-4"><Input type="file" accept=".sql,.json" onChange={(e) => setFile(e.target.files?.[0] || null)} required /><div className="grid grid-cols-2 gap-3"><label className="text-xs font-semibold">Định dạng<select className="mt-1 h-9 w-full rounded-md border border-input bg-background px-3 text-sm" value={format} onChange={(e) => setFormat(e.target.value as any)}><option value="sql">SQL</option><option value="json">JSON</option></select></label><label className="text-xs font-semibold">Chế độ<select className="mt-1 h-9 w-full rounded-md border border-input bg-background px-3 text-sm" value={mode} onChange={(e) => setMode(e.target.value as any)}><option value="overwrite">Ghi đè</option><option value="merge">Gộp dữ liệu</option></select></label></div><Input placeholder="Chỉ nhập các bảng này (tùy chọn)" value={tableFilter} onChange={(e) => setTableFilter(e.target.value)} /><div className="flex gap-2 p-3 rounded border border-amber-500/30 bg-amber-500/5 text-xs text-amber-700"><AlertTriangle className="h-4 w-4 shrink-0" /> Ghi đè sẽ thay thế dữ liệu bảng được chọn.</div><Button type="submit" disabled={busy}><Database className="h-4 w-4" /> {busy ? "Đang nhập..." : "Nhập dữ liệu"}</Button></form></CardContent></Card></div></div>;
}
