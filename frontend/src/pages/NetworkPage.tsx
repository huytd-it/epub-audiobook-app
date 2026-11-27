import React, { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, Globe, Pencil, Plus, Power, RefreshCw, Save, Server, Trash2, Wifi } from "lucide-react";
import { api, del, patchJson, postJson, put, EgressEndpoint, EgressRule, EgressState } from "@/api";
import { Header, LoadingState, EmptyState } from "@/components/common/Header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter } from "@/components/ui/dialog";
import { showToast } from "@/components/ui/toast";
import { confirmDialog } from "@/components/ui/confirm";
import { selectClass } from "@/pages/socials/platforms";

type TestResult = { ok: boolean; ip?: string; elapsed_ms?: number; error?: string };

const SCOPE_INFO: Record<string, { label: string; description: string; limits?: string }> = {
  ai: { label: "AI", description: "Gọi API AI (sinh nội dung, metadata). Dùng được relay." },
  youtube: {
    label: "YouTube",
    description: "Upload video và mọi lời gọi YouTube API. Gợi ý: proxy, chọn cả endpoint LAN và Tailscale của server 2, xoay vòng.",
    limits: "Upload đi máy này → server 2 → YouTube, nên tốc độ theo link chậm hơn. Upload rớt giữa chừng thì job được hoãn và lần sau làm lại từ đầu.",
  },
  facebook: { label: "Facebook", description: "Graph API của các trang Facebook đã kết nối." },
  tiktok: { label: "TikTok", description: "API của các tài khoản TikTok đã kết nối." },
  kaggle: {
    label: "Kaggle API",
    description: "Push kernel, đọc trạng thái, tải output, quota GPU — cả luồng ra một IP.",
    limits: "Kernel chạy trên Kaggle tự đồng bộ Google Drive từ IP của Kaggle, không đi qua server 2.",
  },
  drive: {
    label: "Google Drive API",
    description: "Upload batch, tải kết quả, refresh token, đổi code OAuth của app.",
    limits: "Bước đăng nhập Google trong trình duyệt diễn ra trên máy này. Chế độ thư mục local của Google Drive for desktop do hệ điều hành xử lý, app không định tuyến được.",
  },
};

const fmt = (value: string | null) => (value ? new Date(value).toLocaleString("vi-VN") : "—");

type EndpointForm = { id: number | null; kind: "proxy" | "relay"; label: string; url: string; secret: string; region: string; enabled: boolean };
const emptyForm: EndpointForm = { id: null, kind: "proxy", label: "", url: "", secret: "", region: "", enabled: true };

export function NetworkPage() {
  const [state, setState] = useState<EgressState | null>(null);
  const [loading, setLoading] = useState(true);
  const [form, setForm] = useState<EndpointForm | null>(null);
  const [tests, setTests] = useState<Record<number, TestResult | "running">>({});
  const [drafts, setDrafts] = useState<Record<string, EgressRule>>({});

  const load = () => {
    setLoading(true);
    api<EgressState>("/network/api")
      .then((next) => {
        setState(next);
        setDrafts(next.policy);
      })
      .catch((err) => showToast(`Không tải được cấu hình mạng: ${err.message}`, "error"))
      .finally(() => setLoading(false));
  };
  useEffect(load, []);

  const endpointsById = useMemo(
    () => Object.fromEntries((state?.endpoints ?? []).map((endpoint) => [endpoint.id, endpoint])),
    [state],
  );

  const saveEndpoint = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form) return;
    try {
      if (form.id === null) {
        await postJson("/network/api/endpoints", {
          kind: form.kind, label: form.label, url: form.url, secret: form.secret, region: form.region, enabled: form.enabled,
        });
      } else {
        // URL/secret để trống = giữ giá trị đang lưu (UI chỉ thấy bản đã che).
        await patchJson(`/network/api/endpoints/${form.id}`, {
          label: form.label, region: form.region, enabled: form.enabled,
          url: form.url || null, secret: form.secret || null,
        });
      }
      setForm(null);
      showToast("Đã lưu endpoint", "success");
      load();
    } catch (err: any) {
      showToast(err.message, "error");
    }
  };

  const toggle = async (endpoint: EgressEndpoint) => {
    try {
      await patchJson(`/network/api/endpoints/${endpoint.id}`, { enabled: !endpoint.enabled });
      load();
    } catch (err: any) {
      showToast(err.message, "error");
    }
  };

  const remove = async (endpoint: EgressEndpoint) => {
    const ok = await confirmDialog({
      title: `Xoá endpoint "${endpoint.label || endpoint.url}"?`,
      message: "Endpoint bị gỡ khỏi mọi luồng đang dùng nó và khỏi các tài khoản đang ghim.",
      kind: "warning", confirmLabel: "Xoá",
    });
    if (!ok) return;
    try {
      await del(`/network/api/endpoints/${endpoint.id}`);
      load();
    } catch (err: any) {
      showToast(err.message, "error");
    }
  };

  const runTest = async (endpoint: EgressEndpoint) => {
    setTests((current) => ({ ...current, [endpoint.id]: "running" }));
    try {
      const result = await postJson<TestResult>(`/network/api/endpoints/${endpoint.id}/test`, {});
      setTests((current) => ({ ...current, [endpoint.id]: result }));
    } catch (err: any) {
      setTests((current) => ({ ...current, [endpoint.id]: { ok: false, error: err.message } }));
    }
    load();
  };

  const saveRule = async (scope: string) => {
    const rule = drafts[scope];
    try {
      const result = await put<{ policy: Record<string, EgressRule> }>("/network/api/policy", { [scope]: rule });
      setState((current) => (current ? { ...current, policy: result.policy } : current));
      setDrafts(result.policy);
      showToast(`Đã lưu định tuyến ${SCOPE_INFO[scope]?.label ?? scope}`, "success");
      load();
    } catch (err: any) {
      showToast(err.message, "error");
    }
  };

  const setDraft = (scope: string, patch: Partial<EgressRule>) =>
    setDrafts((current) => {
      const next = { ...current[scope], ...patch };
      if (patch.mode) {
        // Đổi loại thì bỏ các endpoint không còn khớp loại mới.
        next.endpoint_ids = next.endpoint_ids.filter((id) => endpointsById[id]?.kind === patch.mode);
      }
      return { ...current, [scope]: next };
    });

  if (loading && !state) return <LoadingState />;
  if (!state) return <EmptyState text="Không tải được cấu hình mạng." />;

  return (
    <div className="space-y-6">
      <Header
        title="Mạng & Server"
        subtitle="Điểm thoát mạng (proxy/relay, ví dụ server 2 qua LAN và Tailscale) và luồng nào đi qua đâu. Khi server không vào được, job bị hoãn rồi tự thử lại, không bao giờ tự đi thẳng."
        action={<Button variant="outline" size="sm" onClick={load}><RefreshCw className="h-4 w-4" /> Tải lại</Button>}
      />

      {state.pinned_conflicts.length > 0 && (
        <Card className="border-amber-300">
          <CardHeader>
            <CardTitle className="text-sm flex items-center gap-2 text-amber-700">
              <AlertTriangle className="h-4 w-4" /> Kênh đang ghim proxy lệch với định tuyến
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-xs">
            {state.pinned_conflicts.map((conflict) => (
              <div key={conflict.account_id} className="flex flex-wrap items-center justify-between gap-2">
                <span>
                  <b>{conflict.label || `#${conflict.account_id}`}</b> ({conflict.platform}) ghim{" "}
                  <b>{conflict.endpoint_label || `endpoint #${conflict.endpoint_id}`}</b>: {conflict.reason}.
                </span>
                <Link className="text-primary underline" to={`/socials/${conflict.platform}`}>Mở Socials để bỏ ghim</Link>
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-sm flex items-center justify-between gap-2">
            <span className="flex items-center gap-2"><Server className="h-4 w-4" /> Endpoint</span>
            <Button size="sm" onClick={() => setForm({ ...emptyForm })}><Plus className="h-4 w-4" /> Thêm endpoint</Button>
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {state.endpoints.length === 0 ? (
            <div className="p-4"><EmptyState text="Chưa có endpoint. Với server 2: thêm 2 proxy socks5h:// — một qua IP LAN, một qua IP Tailscale 100.x." /></div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="text-left text-muted-foreground border-b border-border">
                  <tr>
                    <th className="p-3">Endpoint</th><th className="p-3">Trạng thái</th><th className="p-3">Dùng gần nhất</th>
                    <th className="p-3">Kiểm tra</th><th className="p-3 text-right">Thao tác</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {state.endpoints.map((endpoint) => {
                    const test = tests[endpoint.id];
                    return (
                      <tr key={endpoint.id} className="align-top">
                        <td className="p-3">
                          <div className="font-semibold">{endpoint.label || `#${endpoint.id}`} <Badge variant="outline">{endpoint.kind}</Badge></div>
                          <div className="font-mono text-muted-foreground break-all">{endpoint.url}</div>
                          {endpoint.region && <div className="text-muted-foreground">Vùng: {endpoint.region}</div>}
                        </td>
                        <td className="p-3 space-y-1">
                          {endpoint.enabled ? <Badge variant="success">Bật</Badge> : <Badge variant="secondary">Tắt</Badge>}
                          {endpoint.fail_count > 0 && <div>Lỗi liên tiếp: {endpoint.fail_count}</div>}
                          {endpoint.cooldown_until && <div className="text-amber-700">Nghỉ tới {fmt(endpoint.cooldown_until)}</div>}
                          {endpoint.last_error && <div className="text-red-600 break-words max-w-xs">{endpoint.last_error}</div>}
                        </td>
                        <td className="p-3 whitespace-nowrap">{fmt(endpoint.last_used_at)}</td>
                        <td className="p-3">
                          {test === "running" ? <span className="text-muted-foreground">Đang kiểm tra…</span>
                            : test?.ok ? <span className="text-emerald-700">IP thoát: <b className="font-mono">{test.ip}</b> ({test.elapsed_ms} ms)</span>
                            : test ? <span className="text-red-600 break-words">{test.error}</span>
                            : <span className="text-muted-foreground">—</span>}
                        </td>
                        <td className="p-3">
                          <div className="flex justify-end gap-1.5">
                            <Button variant="outline" size="sm" disabled={test === "running"} onClick={() => runTest(endpoint)}><Wifi className="h-3.5 w-3.5" /> Kiểm tra</Button>
                            <Button variant="outline" size="sm" onClick={() => toggle(endpoint)}><Power className="h-3.5 w-3.5" /> {endpoint.enabled ? "Tắt" : "Bật"}</Button>
                            <Button variant="outline" size="sm" onClick={() => setForm({ id: endpoint.id, kind: endpoint.kind, label: endpoint.label, url: "", secret: "", region: endpoint.region, enabled: endpoint.enabled })}><Pencil className="h-3.5 w-3.5" /> Sửa</Button>
                            <Button variant="ghost" size="icon" className="text-destructive" onClick={() => remove(endpoint)}><Trash2 className="h-4 w-4" /></Button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader><CardTitle className="text-sm flex items-center gap-2"><Globe className="h-4 w-4" /> Định tuyến theo luồng</CardTitle></CardHeader>
        <CardContent className="divide-y divide-border p-0">
          {state.scopes.map((scope) => {
            const info = SCOPE_INFO[scope] ?? { label: scope, description: "" };
            const rule = drafts[scope] ?? state.policy[scope];
            const saved = state.policy[scope];
            const dirty = JSON.stringify(rule) !== JSON.stringify(saved);
            const relayAllowed = state.relay_scopes.includes(scope);
            const pool = state.endpoints.filter((endpoint) => endpoint.kind === rule.mode);
            return (
              <div key={scope} className="p-4 space-y-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <div className="font-semibold text-sm">{info.label} <span className="font-mono text-[11px] text-muted-foreground">{scope}</span></div>
                    <div className="text-xs text-muted-foreground">{info.description}</div>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <select className={selectClass} value={rule.mode} onChange={(e) => setDraft(scope, { mode: e.target.value as EgressRule["mode"] })}>
                      <option value="direct">Đi thẳng</option>
                      <option value="proxy">Qua proxy</option>
                      {relayAllowed && <option value="relay">Qua relay</option>}
                    </select>
                    {rule.mode !== "direct" && (
                      <select className={selectClass} value={rule.strategy} onChange={(e) => setDraft(scope, { strategy: e.target.value as EgressRule["strategy"] })}>
                        <option value="round_robin">Xoay vòng</option>
                        <option value="sticky">Cố định theo tài khoản</option>
                      </select>
                    )}
                    <Button size="sm" disabled={!dirty} onClick={() => saveRule(scope)}><Save className="h-3.5 w-3.5" /> Lưu</Button>
                  </div>
                </div>
                {rule.mode !== "direct" && (
                  <div className="flex flex-wrap gap-3 text-xs">
                    {pool.length === 0 ? (
                      <span className="text-red-600">Chưa có endpoint loại {rule.mode} — luồng này sẽ báo lỗi (không tự đi thẳng).</span>
                    ) : pool.map((endpoint) => (
                      <label key={endpoint.id} className="flex items-center gap-1.5">
                        <input
                          type="checkbox"
                          checked={rule.endpoint_ids.includes(endpoint.id)}
                          onChange={(e) => setDraft(scope, {
                            endpoint_ids: e.target.checked
                              ? [...rule.endpoint_ids, endpoint.id]
                              : rule.endpoint_ids.filter((id) => id !== endpoint.id),
                          })}
                        />
                        {endpoint.label || endpoint.url}{!endpoint.enabled && " (đang tắt)"}
                      </label>
                    ))}
                    {pool.length > 0 && <span className="text-muted-foreground">Không chọn cái nào = dùng mọi endpoint {rule.mode} đang bật.</span>}
                  </div>
                )}
                {info.limits && <p className="text-[11px] text-muted-foreground">Giới hạn: {info.limits}</p>}
              </div>
            );
          })}
        </CardContent>
      </Card>

      <Dialog open={form !== null} onOpenChange={(open) => !open && setForm(null)}>
        <DialogContent>
          <DialogHeader><DialogTitle>{form?.id == null ? "Thêm endpoint" : "Sửa endpoint"}</DialogTitle></DialogHeader>
          {form && (
            <form onSubmit={saveEndpoint} className="space-y-3">
              {form.id == null && (
                <select className={`${selectClass} w-full`} value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value as EndpointForm["kind"] })}>
                  <option value="proxy">Outbound proxy (http/https/socks5/socks5h)</option>
                  <option value="relay">Relay (Vercel, chỉ cho luồng AI)</option>
                </select>
              )}
              <Input placeholder="Tên gợi nhớ (vd: Server 2 – LAN)" value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} />
              <Input
                placeholder={form.id == null
                  ? (form.kind === "proxy" ? "socks5h://user:pass@192.168.1.20:1080" : "https://relay.example.vercel.app")
                  : "URL mới (để trống nếu giữ nguyên)"}
                value={form.url}
                onChange={(e) => setForm({ ...form, url: e.target.value })}
                required={form.id == null}
              />
              {form.kind === "relay" && (
                <Input type="password" placeholder={form.id == null ? "RELAY_SECRET" : "Secret mới (để trống nếu giữ nguyên)"} value={form.secret}
                  onChange={(e) => setForm({ ...form, secret: e.target.value })} required={form.id == null} />
              )}
              <Input placeholder="Vùng (tuỳ chọn)" value={form.region} onChange={(e) => setForm({ ...form, region: e.target.value })} />
              <label className="flex items-center gap-2 text-xs">
                <input type="checkbox" checked={form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} /> Bật
              </label>
              <p className="text-[11px] text-muted-foreground">Dùng socks5h:// để DNS được phân giải ở server 2.</p>
              <DialogFooter>
                <Button type="button" variant="outline" onClick={() => setForm(null)}>Huỷ</Button>
                <Button type="submit">Lưu</Button>
              </DialogFooter>
            </form>
          )}
        </DialogContent>
      </Dialog>
    </div>
  );
}
