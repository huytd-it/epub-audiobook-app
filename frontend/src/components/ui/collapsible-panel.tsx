import React, { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { cn } from "@/lib/utils";

interface CollapsiblePanelProps {
  title: string;
  icon?: React.ElementType;
  defaultOpen?: boolean;
  hint?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}

export function CollapsiblePanel({
  title,
  icon: Icon,
  defaultOpen = true,
  hint,
  action,
  children,
  className,
}: CollapsiblePanelProps) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <Card className={className}>
      <CardHeader
        className="cursor-pointer select-none border-b border-border bg-muted/20 py-3"
        onClick={() => setOpen(!open)}
      >
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            {open ? (
              <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground" />
            ) : (
              <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground" />
            )}
            {Icon && <Icon className="h-4 w-4 shrink-0 text-primary" />}
            <div className="min-w-0">
              <div className="text-sm font-semibold">{title}</div>
              {hint && (
                <div className="text-xs text-muted-foreground truncate">{hint}</div>
              )}
            </div>
          </div>
          {action && (
            <div onClick={(e) => e.stopPropagation()}>{action}</div>
          )}
        </div>
      </CardHeader>
      {open && <CardContent className="pt-5">{children}</CardContent>}
    </Card>
  );
}
