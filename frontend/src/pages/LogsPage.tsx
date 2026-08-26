import React, { useEffect, useState } from "react";
import { FileText, RefreshCw, Trash2, Download } from "lucide-react";
import { api, del } from "@/api";
import { Header, LoadingState } from "@/components/common/Header";
import { LogLines } from "@/components/common/LogLines";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export function LogsPage() {
  const [content, setContent] = useState(""); const [lines, setLines] = useState("500"); const [loading, setLoading] = useState(true);
  const load = () => { setLoading(true); api<string>(`/logs/raw?lines=${encodeURIComponent(lines)}`).then(setContent).catch((e) => setContent(`Không thể tải log: ${e.message}`)).finally(() => setLoading(false)); };
  useEffect(load, []);
  const clear = async () => { if (!confirm("Xóa toàn bộ log?")) return; try { await del("/logs"); load(); } catch (e: any) { alert(e.message); } };
  return <div className="space-y-6"><Header title="Nhật ký hệ thống" subtitle="Theo dõi hoạt động backend và các lỗi trong pipeline." action={<div className="flex gap-2"><Input className="w-20" type="number" min="1" value={lines} onChange={(e) => setLines(e.target.value)} aria-label="Số dòng log" /><Button variant="outline" size="sm" onClick={load}><RefreshCw className="h-4 w-4" /> Làm mới</Button><Button variant="destructive" size="sm" onClick={clear}><Trash2 className="h-4 w-4" /> Xóa log</Button></div>} />{loading ? <LoadingState text="Đang tải log..." /> : <div className="rounded-lg border border-border bg-zinc-950 text-zinc-200 p-4 min-h-[32rem] overflow-auto"><LogLines text={content} emptyText="Chưa có log." /></div>}<Button variant="outline" size="sm" onClick={() => { const blob = new Blob([content], { type: "text/plain" }); const url = URL.createObjectURL(blob); const a = document.createElement("a"); a.href = url; a.download = "xưởng-sách-nói.log"; a.click(); URL.revokeObjectURL(url); }}><Download className="h-4 w-4" /> Tải log</Button></div>;
}
