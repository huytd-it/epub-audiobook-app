import React, { useEffect, useState } from "react";
import { HardDrive, Plus, RefreshCw, Trash2, ExternalLink, Settings2, Copy, Check, FolderOpen, Cloud, KeyRound } from "lucide-react";
import { api, postForm, postJson, DriveTarget, DriveAccount, DriveClient, PatchExport } from "@/api";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";

export function DrivePage() {
  const [targets, setTargets] = useState<DriveTarget[]>([]);
  const [accounts, setAccounts] = useState<DriveAccount[]>([]);
  const [clients, setClients] = useState<DriveClient[]>([]);
  const [exports, setExports] = useState<PatchExport[]>([]);
  const [loading, setLoading] = useState(true);
  const [activeSection, setActiveSection] = useState<"targets" | "accounts" | "exports" | "clients">("targets");
  const [showTargetForm, setShowTargetForm] = useState(false);
  const [showClientForm, setShowClientForm] = useState(false);
  const [syncingId, setSyncingId] = useState<number | null>(null);
  const [kaggleCreds, setKaggleCreds] = useState("");
  const [copied, setCopied] = useState(false);

  const [targetName, setTargetName] = useState("");
  const [targetEmail, setTargetEmail] = useState("");
  const [folderPath, setFolderPath] = useState("");
  const [rcloneRemote, setRcloneRemote] = useState("");
  const [clientName, setClientName] = useState("");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [rcloneClientId, setRcloneClientId] = useState("");
  const [rcloneClientSecret, setRcloneClientSecret] = useState("");

  const loadData = () => {
    setLoading(true);
    api<any>("/api/ui/drive")
      .then((data) => {
        setTargets(data.targets || []);
        setAccounts(data.accounts || []);
        setClients(data.clients || []);
        setExports(data.exports || []);
        setRcloneClientId(data.rclone_client_id || "");
        setRcloneClientSecret(data.rclone_client_secret || "");
      })
      .catch((err) => console.error("Lỗi tải dữ liệu Google Drive:", err))
      .finally(() => setLoading(false));
  };

  useEffect(() => {
    loadData();
  }, []);

  const handleCreateTarget = async (e: React.FormEvent) => {
    e.preventDefault();
    const form = new FormData();
    form.append("name", targetName);
    form.append("account_email", targetEmail);
    form.append("folder_path", folderPath);
    form.append("rclone_remote", rcloneRemote);
    try {
      await postForm("/drive/targets", form);
      setShowTargetForm(false);
      setTargetName(""); setTargetEmail(""); setFolderPath(""); setRcloneRemote("");
      loadData();
    } catch (err: any) { alert(`Tạo target thất bại: ${err.message}`); }
  };

  const handleDeleteTarget = async (id: number) => {
    if (!confirm("Xóa target đồng bộ này? Các file export sẽ được giữ nguyên.")) return;
    const form = new FormData();
    try { await postForm(`/drive/targets/${id}/delete`, form); loadData(); } catch (err: any) { alert(err.message); }
  };

  const handleSyncTarget = async (id: number) => {
    setSyncingId(id);
    try {
      const result = await postJson<any>(`/drive/targets/${id}/sync`, {});
      alert(result.status === "ok" ? "Đồng bộ thành công!" : `Đồng bộ lỗi: ${result.output || "Không rõ lỗi"}`);
    } catch (err: any) { alert(`Đồng bộ thất bại: ${err.message}`); }
    finally { setSyncingId(null); }
  };

  const handleCreateClient = async (e: React.FormEvent) => {
    e.preventDefault();
    const form = new FormData();
    form.append("name", clientName); form.append("client_id", clientId); form.append("client_secret", clientSecret);
    try { await postForm("/drive/clients", form); setShowClientForm(false); setClientName(""); setClientId(""); setClientSecret(""); loadData(); }
    catch (err: any) { alert(`Tạo OAuth client thất bại: ${err.message}`); }
  };

  const handleDisconnect = async (id: number) => {
    if (!confirm("Ngắt kết nối tài khoản Google Drive này?")) return;
    const form = new FormData(); form.append("account_id", String(id));
    try { await postForm("/drive/disconnect", form); loadData(); } catch (err: any) { alert(err.message); }
  };

  const handleKaggleCreds = async (accountId: number) => {
    try { setKaggleCreds(JSON.stringify(await api<any>(`/drive/kaggle-credentials?account_id=${accountId}`), null, 2)); setCopied(false); }
    catch (err: any) { alert(`Không thể lấy credentials: ${err.message}`); }
  };

  const saveRcloneConfig = async () => {
    const form = new FormData(); form.append("client_id", rcloneClientId); form.append("client_secret", rcloneClientSecret);
    try { await postForm("/drive/rclone-config", form); alert("Đã lưu cấu hình rclone"); } catch (err: any) { alert(err.message); }
  };

  if (loading) return <LoadingState text="Đang nạp cấu hình Google Drive..." />;

  return (
    <div className="space-y-6">
      <Header title="Google Drive & Đồng bộ dữ liệu" subtitle="Quản lý các điểm đồng bộ Drive Desktop, OAuth account và bản export audiobook." action={<Button variant="outline" size="sm" onClick={loadData}><RefreshCw className="h-4 w-4" /> Làm mới</Button>} />
      <div className="flex flex-wrap items-center gap-2 border-b border-border pb-2">
        <Button variant={activeSection === "targets" ? "default" : "ghost"} size="sm" onClick={() => setActiveSection("targets")}><FolderOpen className="h-4 w-4" /> Sync targets</Button>
        <Button variant={activeSection === "accounts" ? "default" : "ghost"} size="sm" onClick={() => setActiveSection("accounts")}><Cloud className="h-4 w-4" /> Tài khoản ({accounts.length})</Button>
        <Button variant={activeSection === "exports" ? "default" : "ghost"} size="sm" onClick={() => setActiveSection("exports")}><HardDrive className="h-4 w-4" /> Bản export ({exports.length})</Button>
        <Button variant={activeSection === "clients" ? "default" : "ghost"} size="sm" onClick={() => setActiveSection("clients")}><KeyRound className="h-4 w-4" /> OAuth clients</Button>
      </div>

      {activeSection === "targets" && <div className="space-y-4">
        <div className="flex justify-end"><Button size="sm" onClick={() => setShowTargetForm(true)}><Plus className="h-4 w-4" /> Thêm sync target</Button></div>
        {targets.length === 0 ? <EmptyState text="Chưa có thư mục đồng bộ nào." /> : <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">{targets.map((target) => <Card key={target.id}><CardHeader className="p-4 pb-2"><div className="flex justify-between gap-3"><CardTitle className="text-sm flex items-center gap-2"><FolderOpen className="h-4 w-4 text-primary" />{target.name}</CardTitle><span className="text-[10px] font-mono text-muted-foreground">{target.account_email}</span></div></CardHeader><CardContent className="p-4 pt-2 space-y-3"><div className="rounded bg-muted/40 border border-border p-2 text-xs font-mono break-all">{target.folder_path}</div>{target.rclone_remote && <div className="text-[11px] text-muted-foreground font-mono">rclone: {target.rclone_remote}</div>}<div className="flex justify-end gap-2 border-t border-border pt-3"><Button variant="outline" size="sm" disabled={!target.rclone_remote || syncingId === target.id} onClick={() => handleSyncTarget(target.id)}><RefreshCw className={`h-3.5 w-3.5 ${syncingId === target.id ? "animate-spin" : ""}`} /> Đồng bộ</Button><Button variant="ghost" size="icon" className="text-destructive" onClick={() => handleDeleteTarget(target.id)}><Trash2 className="h-4 w-4" /></Button></div></CardContent></Card>)}</div>}
      </div>}

      {activeSection === "accounts" && <div className="space-y-4"><Card><CardHeader><CardTitle className="text-sm">Tài khoản Google Drive đã kết nối</CardTitle></CardHeader><CardContent className="p-0 divide-y divide-border">{accounts.length === 0 ? <EmptyState text="Chưa kết nối tài khoản Google Drive." /> : accounts.map((account) => <div key={account.id} className="p-4 flex items-center justify-between gap-3"><div><div className="font-semibold text-sm">{account.account_email}</div><div className="text-[11px] font-mono text-muted-foreground">Kết nối: {new Date(account.created_at).toLocaleString("vi-VN")}</div></div><div className="flex gap-2"><Button variant="outline" size="sm" onClick={() => handleKaggleCreds(account.id)}><KeyRound className="h-3.5 w-3.5" /> Credentials</Button><Button variant="ghost" size="icon" className="text-destructive" onClick={() => handleDisconnect(account.id)}><Trash2 className="h-4 w-4" /></Button></div></div>)}</CardContent></Card><Button onClick={() => window.open("/drive/connect", "_blank")}><ExternalLink className="h-4 w-4" /> Kết nối tài khoản mới</Button>{kaggleCreds && <Card><CardHeader><CardTitle className="text-sm flex justify-between">GDRIVE_CREDS <Button size="sm" variant="outline" onClick={() => { navigator.clipboard.writeText(kaggleCreds); setCopied(true); }} >{copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />} Sao chép</Button></CardTitle></CardHeader><CardContent><pre className="p-3 bg-zinc-950 text-lime rounded text-xs overflow-auto">{kaggleCreds}</pre></CardContent></Card>}</div>}

      {activeSection === "exports" && <Card><CardHeader><CardTitle className="text-sm">Lịch sử bản export</CardTitle></CardHeader><CardContent className="p-0">{exports.length === 0 ? <EmptyState text="Chưa có bản export nào." /> : <div className="divide-y divide-border">{exports.map((item) => <div key={item.id} className="p-4 flex justify-between gap-3 text-xs"><div><div className="font-semibold">Export #{item.id} · Patch #{item.patch_id}</div><div className="font-mono text-muted-foreground break-all">{item.local_folder_path || item.drive_folder_link || item.drive_folder_id}</div><div className="text-[10px] text-muted-foreground">{new Date(item.created_at).toLocaleString("vi-VN")}</div></div><Button variant="outline" size="sm" asChild><a href={item.local_folder_path || item.drive_folder_link || item.drive_folder_id}><ExternalLink className="h-3.5 w-3.5" /> Mở</a></Button></div>)}</div>}</CardContent></Card>}

      {activeSection === "clients" && <div className="space-y-4"><div className="flex justify-end"><Button size="sm" onClick={() => setShowClientForm(true)}><Plus className="h-4 w-4" /> Thêm OAuth client</Button></div><Card><CardContent className="p-0 divide-y divide-border">{clients.length === 0 ? <EmptyState text="Chưa có OAuth client tùy chỉnh." /> : clients.map((client) => <div key={client.id} className="p-4 flex justify-between"><div><div className="font-semibold text-sm">{client.name}</div><div className="text-xs font-mono text-muted-foreground">{client.client_id}</div></div><Button variant="ghost" size="icon" className="text-destructive" onClick={async () => { if (confirm("Xóa OAuth client này?")) { try { await postForm(`/drive/clients/${client.id}/delete`, new FormData()); loadData(); } catch (e: any) { alert(e.message); } } }}><Trash2 className="h-4 w-4" /></Button></div>)}</CardContent></Card><Card><CardHeader><CardTitle className="text-sm">Rclone OAuth client</CardTitle></CardHeader><CardContent className="grid grid-cols-1 md:grid-cols-2 gap-3"><Input placeholder="Google client ID" value={rcloneClientId} onChange={(e) => setRcloneClientId(e.target.value)} /><Input type="password" placeholder="Google client secret" value={rcloneClientSecret} onChange={(e) => setRcloneClientSecret(e.target.value)} /><Button className="md:col-span-2" onClick={saveRcloneConfig}>Lưu cấu hình rclone</Button></CardContent></Card></div>}

      <Dialog open={showTargetForm} onOpenChange={setShowTargetForm}><DialogContent><DialogHeader><DialogTitle>Thêm điểm đồng bộ Drive</DialogTitle></DialogHeader><form onSubmit={handleCreateTarget} className="space-y-4"><Input placeholder="Tên target" value={targetName} onChange={(e) => setTargetName(e.target.value)} required /><Input type="email" placeholder="Email tài khoản Google" value={targetEmail} onChange={(e) => setTargetEmail(e.target.value)} required /><Input placeholder="Đường dẫn thư mục local" value={folderPath} onChange={(e) => setFolderPath(e.target.value)} required /><Input placeholder="rclone remote:path (không bắt buộc)" value={rcloneRemote} onChange={(e) => setRcloneRemote(e.target.value)} /><DialogFooter><Button type="button" variant="outline" onClick={() => setShowTargetForm(false)}>Hủy</Button><Button type="submit">Tạo target</Button></DialogFooter></form></DialogContent></Dialog>
      <Dialog open={showClientForm} onOpenChange={setShowClientForm}><DialogContent><DialogHeader><DialogTitle>Thêm Google OAuth client</DialogTitle></DialogHeader><form onSubmit={handleCreateClient} className="space-y-4"><Input placeholder="Tên client" value={clientName} onChange={(e) => setClientName(e.target.value)} required /><Input placeholder="Client ID" value={clientId} onChange={(e) => setClientId(e.target.value)} required /><Input type="password" placeholder="Client secret" value={clientSecret} onChange={(e) => setClientSecret(e.target.value)} /><DialogFooter><Button type="button" variant="outline" onClick={() => setShowClientForm(false)}>Hủy</Button><Button type="submit">Lưu client</Button></DialogFooter></form></DialogContent></Dialog>
    </div>
  );
}
