import React from "react";

export function Header({
  title,
  subtitle,
  action,
}: {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col sm:flex-row sm:items-end justify-between gap-4 mb-6">
      <div>
        <h1 className="text-2xl sm:text-3xl font-extrabold tracking-tight text-foreground">
          {title}
        </h1>
        {subtitle && (
          <p className="text-xs sm:text-sm text-muted-foreground mt-1 max-w-2xl">
            {subtitle}
          </p>
        )}
      </div>
      {action && <div className="flex items-center gap-2 shrink-0">{action}</div>}
    </div>
  );
}

export function LoadingState({ text = "Đang tải dữ liệu..." }: { text?: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 px-4 border border-dashed border-border rounded-lg bg-card text-center">
      <div className="h-6 w-6 border-2 border-primary border-t-transparent rounded-full animate-spin mb-3" />
      <span className="text-xs font-mono text-muted-foreground">{text}</span>
    </div>
  );
}

export function EmptyState({ text = "Chưa có dữ liệu" }: { text?: string }) {
  return (
    <div className="py-12 px-4 border border-dashed border-border rounded-lg bg-card/50 text-center text-xs font-mono text-muted-foreground">
      {text}
    </div>
  );
}
