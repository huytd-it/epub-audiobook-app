import React from "react";
import { Pause, Play, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { Severity } from "./types";

export const fieldClass =
  "h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/15";
export const selectClass =
  "h-9 w-full rounded-md border border-input bg-background px-3 text-sm outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/15";
export const checkboxClass = "h-4 w-4 shrink-0 cursor-pointer rounded border-input accent-primary";

export function SectionHead({
  icon: Icon,
  title,
  detail,
  action,
}: {
  icon: React.ElementType;
  title: string;
  detail: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="flex items-start gap-3">
        <div className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
          <Icon className="h-4 w-4" />
        </div>
        <div>
          <CardTitle className="text-sm font-bold tracking-tight">{title}</CardTitle>
          <p className="mt-1 text-xs font-normal text-muted-foreground">{detail}</p>
        </div>
      </div>
      {action}
    </div>
  );
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block text-xs font-medium">
      <span className="flex items-baseline justify-between gap-2">
        {label}
        {hint && <span className="font-normal text-muted-foreground">{hint}</span>}
      </span>
      <span className="mt-1.5 block">{children}</span>
    </label>
  );
}

export function CheckField({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <label className="flex cursor-pointer items-center gap-2 text-xs font-medium">
      <input
        type="checkbox"
        className={checkboxClass}
        checked={checked}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className={disabled ? "text-muted-foreground" : undefined}>{label}</span>
    </label>
  );
}

export type TabItem<T extends string> = { value: T; label: string; badge?: number | string };

export function TabBar<T extends string>({
  tabs,
  value,
  onChange,
  className,
}: {
  tabs: TabItem<T>[];
  value: T;
  onChange: (next: T) => void;
  className?: string;
}) {
  return (
    <div
      role="tablist"
      className={cn("flex gap-1 overflow-x-auto rounded-md border border-border bg-card p-1", className)}
    >
      {tabs.map((tab) => {
        const active = tab.value === value;
        return (
          <button
            key={tab.value}
            role="tab"
            aria-selected={active}
            onClick={() => onChange(tab.value)}
            className={cn(
              "flex shrink-0 items-center gap-1.5 rounded px-3 py-1.5 text-xs font-medium transition-colors",
              active ? "bg-primary text-primary-foreground shadow-xs" : "text-muted-foreground hover:bg-muted hover:text-foreground"
            )}
          >
            {tab.label}
            {tab.badge !== undefined && (
              <span
                className={cn(
                  "rounded px-1 font-mono text-[10px]",
                  active ? "bg-primary-foreground/20" : "bg-muted-foreground/10"
                )}
              >
                {tab.badge}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

export const SEVERITY_STYLE: Record<Severity, string> = {
  error: "bg-red-50 text-red-700",
  warning: "bg-amber-50 text-amber-800",
  info: "bg-muted text-muted-foreground",
  ok: "bg-emerald-50 text-emerald-700",
};

export const SEVERITY_LABEL: Record<Severity, string> = {
  error: "Lỗi",
  warning: "Cảnh báo",
  info: "Ghi chú",
  ok: "Đạt",
};

export function SeverityTag({ value }: { value: Severity }) {
  return (
    <span className={cn("inline-flex rounded px-1.5 py-0.5 text-[10px] font-medium", SEVERITY_STYLE[value])}>
      {SEVERITY_LABEL[value]}
    </span>
  );
}

export function Tile({ label, value, tone }: { label: string; value: number | string; tone?: "danger" | "warn" | "good" }) {
  return (
    <div className="rounded-md border border-border bg-card p-3">
      <div
        className={cn(
          "font-mono text-lg font-bold leading-none",
          tone === "danger" && "text-red-600",
          tone === "warn" && "text-amber-600",
          tone === "good" && "text-emerald-600"
        )}
      >
        {value}
      </div>
      <div className="mt-1 text-[11px] text-muted-foreground">{label}</div>
    </div>
  );
}

/** Trạng thái tự động làm mới: hiện thời điểm cập nhật và cho phép tạm dừng. */
export function LiveIndicator({
  live,
  refreshing,
  updatedAt,
  onToggle,
  onRefresh,
}: {
  live: boolean;
  refreshing: boolean;
  updatedAt?: number;
  onToggle: () => void;
  onRefresh: () => void;
}) {
  const time = updatedAt ? new Date(updatedAt).toLocaleTimeString("vi-VN") : "—";

  return (
    <div className="flex items-center gap-1 rounded-md border border-border bg-card px-2 py-1">
      <span
        className={cn("h-1.5 w-1.5 rounded-full", live ? "bg-emerald-500" : "bg-muted-foreground/50", live && !refreshing && "animate-pulse")}
      />
      <span className="hidden font-mono text-[10px] text-muted-foreground sm:inline">
        {live ? `Cập nhật ${time}` : "Đã tạm dừng"}
      </span>
      <Button
        variant="ghost"
        size="icon"
        className="h-6 w-6"
        onClick={onRefresh}
        title="Làm mới ngay"
        aria-label="Làm mới ngay"
      >
        <RefreshCw className={cn("h-3 w-3", refreshing && "animate-spin")} />
      </Button>
      <Button
        variant="ghost"
        size="icon"
        className="h-6 w-6"
        onClick={onToggle}
        title={live ? "Tạm dừng tự động làm mới" : "Bật tự động làm mới"}
        aria-label={live ? "Tạm dừng tự động làm mới" : "Bật tự động làm mới"}
      >
        {live ? <Pause className="h-3 w-3" /> : <Play className="h-3 w-3" />}
      </Button>
    </div>
  );
}
