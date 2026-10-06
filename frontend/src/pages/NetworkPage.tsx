import React, { useEffect, useState } from "react";
import { Globe, Plus, Power, Radar, Trash2 } from "lucide-react";
import { api, del, patchJson, postJson, put, EgressEndpoint, EgressRule, EgressState } from "@/api";
import { EmptyState, Header, LoadingState } from "@/components/common/Header";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { confirmDialog } from "@/components/ui/confirm";
import { showToast } from "@/components/ui/toast";
import { selectClass } from "./socials/platforms";

const SCOPE_LABEL: Record<string, string> = {
  ai: "AI (sinh nội dung, ảnh bìa)",
  youtube: "YouTube",
  facebook: "Facebook",
  tiktok: "TikTok",
};
const MODE_LABEL: Record<EgressRule["mode"], string> = {
  direct: "Đi thẳng",
  proxy: "Outbound proxy",
  relay: "Vercel relay",
};

type TestResult = { ok: boolean; ip?: string; elapsed_ms?: number; error?: string };

function AddEndpointForm({ onAdded }: { onAdded: () => void }) {
  const [kind, setKind] = useState<"proxy" | "relay">("proxy");
  const [label, setLabel] = useState("");
  const [url, setUrl] = useState("");
  const [secret, setSecret] = useState("");
  const [region, setRegion] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    try {
      await postJson("/socials/api/network/endpoints", {
        kind,
        label: label.trim(),
        url: url.trim(),
        secret: kind === "relay" ? secret.trim() : "",
        region: region.trim(),
      });
      setLabel("");
      setUrl("");
      setSecret("");
      setRegion("");
      showToast("Đã thêm endpoint", "success");
      onAdded();
    } catch (error: any) {
      showToast(error.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const fieldLabel = "flex flex-col gap-1 text-[11px] font-mono uppercase tracking-wide text-muted-foreground";
  return (
    <form onSubmit={submit} className="grid gap-3 sm:grid-cols-2 lg:grid-cols-[auto_1fr_2fr_1fr_auto_auto] lg:items-end">
      <label className={fieldLabel}>
        Loại
        <select className={selectClass} value={kind} onChange={(event) => setKind(event.target.value as "proxy" | "relay")}>
          <option value="proxy">Outbound proxy</option>
          <option value="relay">Vercel relay</option>
        </select>
      </label>
      <label className={fieldLabel}>
        Nhãn
        <Input value={label} onChange={(event) => setLabel(event.target.value)} className="text-xs" />
      </label>
      <label className={fieldLabel}>
        URL
        <Input
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          required
          autoComplete="off"
          placeholder={kind === "proxy" ? "http://user:pass@host:8080 hoặc socks5://host:1080" : "https://ten-project.vercel.app"}
          className="text-xs"
        />
      </label>
      {kind === "relay" ? (
        <label className={fieldLabel}>
          Secret (RELAY_SECRET)
          <Input
            type="password"
            autoComplete="off"
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
            required
            className="text-xs"
          />
        </label>
      ) : (
        <span className="hidden lg:block" />
      )}
      <label className={fieldLabel}>
        Vùng
        <Input
          value={region}
          onChange={(event) => setRegion(event.target.value)}
          placeholder="sg, us..."
          className="w-24 text-xs"
        />
      </label>
      <Button type="submit" size="sm" disabled={busy}>
        <Plus className="h-4 w-4" />
        Thêm
      </Button>
    </form>
  );
}

function EndpointRow({ endpoint, onChanged }: { endpoint: EgressEndpoint; onChanged: () => void }) {
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<TestResult | null>(null);

  const test = async () => {
    setTesting(true);
    setResult(null);
    try {
      setResult(await postJson<TestResult>(`/socials/api/network/endpoints/${endpoint.id}/test`, {}));
      onChanged();
    } catch (error: any) {
      setResult({ ok: false, error: error.message });
    } finally {
      setTesting(false);
    }
  };

  const toggle = async () => {
    try {
      await patchJson(`/socials/api/network/endpoints/${endpoint.id}`, { enabled: !endpoint.enabled });
      onChanged();
    } catch (error: any) {
      showToast(error.message, "error");
    }
  };

  const remove = async () => {
    const confirmed = await confirmDialog({
      title: `Xoá endpoint "${endpoint.label || endpoint.url}"?`,
      message: "Endpoint bị gỡ khỏi mọi policy và mọi tài khoản đang ghim nó.",
      confirmLabel: "Xoá",
    });
    if (!confirmed) return;
    try {
      await del(`/socials/api/network/endpoints/${endpoint.id}`);
      onChanged();
    } catch (error: any) {
      showToast(error.message, "error");
    }
  };

  return (
    <tr className="border-b border-border/60 align-top">
      <td className="p-2">
        <Badge variant={endpoint.kind === "relay" ? "lime" : "outline"}>{endpoint.kind}</Badge>
      </td>
      <td className="p-2">
        <p className="font-medium text-foreground">{endpoint.label || "—"}</p>
        <p className="break-all font-mono text-[11px] text-muted-foreground">{endpoint.url}</p>
      </td>
      <td className="p-2 font-mono">{endpoint.region || "—"}</td>
      <td className="p-2">
        {!endpoint.enabled ? (
          <Badge variant="outline" className="text-muted-foreground">Tắt</Badge>
        ) : endpoint.cooldown_until ? (
          <Badge variant="warning">Tạm nghỉ</Badge>
        ) : (
          <Badge variant="success">Sẵn sàng</Badge>
        )}
        {endpoint.last_error && <p className="mt-1 max-w-xs text-[11px] text-red-600">{endpoint.last_error}</p>}
        {result && (
          <p className={`mt-1 font-mono text-[11px] ${result.ok ? "text-emerald-600" : "text-red-600"}`}>
            {result.ok ? `IP thoát ${result.ip} · ${result.elapsed_ms} ms` : result.error}
          </p>
        )}
      </td>
      <td className="p-2">
        <div className="flex justify-end gap-1">
          <Button size="sm" variant="outline" disabled={testing} onClick={test}>
            <Radar className="h-3.5 w-3.5" />
            {testing ? "Đang thử..." : "Kiểm tra"}
          </Button>
          <Button size="sm" variant="ghost" onClick={toggle} title={endpoint.enabled ? "Tắt" : "Bật lại (xoá bộ đếm lỗi)"}>
            <Power className="h-3.5 w-3.5" />
          </Button>
          <Button size="sm" variant="ghost" onClick={remove} title="Xoá">
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      </td>
    </tr>
  );
}

function PolicyRow({
  scope,
  rule,
  endpoints,
  relayAllowed,
  onSave,
}: {
  scope: string;
  rule: EgressRule;
  endpoints: EgressEndpoint[];
  relayAllowed: boolean;
  onSave: (scope: string, rule: EgressRule) => void;
}) {
  const candidates = endpoints.filter((endpoint) => endpoint.kind === rule.mode);
  const change = (patch: Partial<EgressRule>) => {
    const next = { ...rule, ...patch };
    // Đổi mode thì danh sách endpoint cũ (khác loại) không còn nghĩa.
    if (patch.mode && patch.mode !== rule.mode) next.endpoint_ids = [];
    onSave(scope, next);
  };

  return (
    <tr className="border-b border-border/60 align-top">
      <td className="p-2 font-medium text-foreground">{SCOPE_LABEL[scope] || scope}</td>
      <td className="p-2">
        <select
          className={selectClass}
          value={rule.mode}
          onChange={(event) => change({ mode: event.target.value as EgressRule["mode"] })}
        >
          <option value="direct">{MODE_LABEL.direct}</option>
          <option value="proxy">{MODE_LABEL.proxy}</option>
          {relayAllowed && <option value="relay">{MODE_LABEL.relay}</option>}
        </select>
      </td>
      <td className="p-2">
        <select
          className={selectClass}
          value={rule.strategy}
          disabled={rule.mode === "direct"}
          onChange={(event) => change({ strategy: event.target.value as EgressRule["strategy"] })}
        >
          <option value="round_robin">Xoay vòng mỗi request</option>
          <option value="sticky">Cố định theo tài khoản</option>
        </select>
      </td>
      <td className="p-2">
        {rule.mode === "direct" ? (
          <span className="text-muted-foreground">—</span>
        ) : candidates.length === 0 ? (
          <span className="text-red-600">Chưa có endpoint loại {rule.mode}: mọi request của scope này sẽ báo lỗi.</span>
        ) : (
          <div className="flex flex-wrap gap-x-3 gap-y-1">
            {candidates.map((endpoint) => (
              <label key={endpoint.id} className="flex items-center gap-1.5">
                <input
                  type="checkbox"
                  checked={rule.endpoint_ids.includes(endpoint.id)}
                  onChange={(event) =>
                    change({
                      endpoint_ids: event.target.checked
                        ? [...rule.endpoint_ids, endpoint.id]
                        : rule.endpoint_ids.filter((id) => id !== endpoint.id),
                    })
                  }
                />
                {endpoint.label || endpoint.url}
              </label>
            ))}
            <span className="w-full text-[11px] text-muted-foreground">
              {rule.endpoint_ids.length === 0 ? "Không chọn = dùng tất cả endpoint đang bật." : ""}
            </span>
          </div>
        )}
      </td>
    </tr>
  );
}

/** Trang "Mạng & Proxy": điểm thoát (outbound proxy / Vercel relay) và policy theo scope. */
export function NetworkPage() {
  const [state, setState] = useState<EgressState | null>(null);

  const load = () => {
    api<EgressState>("/socials/api/network")
      .then(setState)
      .catch((error) => showToast(error.message, "error"));
  };
  useEffect(load, []);

  const savePolicy = async (scope: string, rule: EgressRule) => {
    try {
      const response = await put<{ policy: EgressState["policy"] }>("/socials/api/network/policy", { [scope]: rule });
      setState((current) => (current ? { ...current, policy: response.policy } : current));
    } catch (error: any) {
      showToast(error.message, "error");
      load();
    }
  };

  if (!state) return <LoadingState />;

  return (
    <div className="space-y-4">
      <Header
        title="Mạng & Proxy"
        subtitle="Điểm thoát cho request ra ngoài (outbound proxy, Vercel relay) và định tuyến theo từng luồng: AI, YouTube, Facebook, TikTok."
      />
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Globe className="h-4 w-4" />
            Điểm thoát
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <AddEndpointForm onAdded={load} />
          {state.endpoints.length === 0 ? (
            <EmptyState text="Chưa có endpoint nào — mọi request đang đi thẳng từ máy này." />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs">
                <thead className="border-b border-border text-[11px] font-mono uppercase text-muted-foreground">
                  <tr>
                    <th className="p-2">Loại</th>
                    <th className="p-2">Endpoint</th>
                    <th className="p-2">Vùng</th>
                    <th className="p-2">Trạng thái</th>
                    <th className="p-2" />
                  </tr>
                </thead>
                <tbody>
                  {state.endpoints.map((endpoint) => (
                    <EndpointRow key={endpoint.id} endpoint={endpoint} onChanged={load} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="text-[11px] text-muted-foreground">
            Relay là Edge Function trong thư mục <code>relay/</code> của repo (xem <code>relay/README.md</code> để
            deploy). Mật khẩu proxy và secret của relay không bao giờ được trả lại giao diện.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Định tuyến theo luồng</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead className="border-b border-border text-[11px] font-mono uppercase text-muted-foreground">
                <tr>
                  <th className="p-2">Luồng</th>
                  <th className="p-2">Đi qua</th>
                  <th className="p-2">Chọn endpoint</th>
                  <th className="p-2">Giới hạn trong</th>
                </tr>
              </thead>
              <tbody>
                {state.scopes.map((scope) => (
                  <PolicyRow
                    key={scope}
                    scope={scope}
                    rule={state.policy[scope]}
                    endpoints={state.endpoints}
                    relayAllowed={state.relay_scopes.includes(scope)}
                    onSave={savePolicy}
                  />
                ))}
              </tbody>
            </table>
          </div>
          <ul className="list-disc space-y-1 pl-4 text-[11px] text-muted-foreground">
            <li>
              Mạng xã hội không dùng được relay: Vercel giới hạn body khoảng 4,5 MB nên upload video chỉ đi thẳng
              hoặc qua outbound proxy.
            </li>
            <li>
              Với mạng xã hội nên để "Cố định theo tài khoản": mỗi tài khoản luôn ra cùng một IP. Muốn chỉ định
              chính xác thì ghim proxy ngay trên tài khoản ở trang Socials.
            </li>
            <li>
              Scope đã đặt proxy/relay mà không còn endpoint nào dùng được sẽ báo lỗi chứ không tự đi thẳng.
            </li>
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}
