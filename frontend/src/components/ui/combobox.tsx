import React, { useEffect, useId, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, Search } from "lucide-react";
import { cn } from "@/lib/utils";

export type ComboboxOption = {
  value: string;
  label: string;
  description?: string;
};

type NativeInputProps = Omit<
  React.InputHTMLAttributes<HTMLInputElement>,
  "value" | "defaultValue" | "onChange" | "className"
>;

export type ComboboxProps = NativeInputProps & {
  options: ComboboxOption[];
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  emptyMessage?: string;
  className?: string;
  inputClassName?: string;
};

/**
 * Combobox nhỏ, không phụ thuộc thư viện ngoài: ô nhập vừa hiển thị lựa chọn
 * vừa lọc danh sách. Giữ trong bộ nhớ trạng thái mở/đóng để các form hiện tại
 * (native select) chuyển sang cùng một trải nghiệm khi danh sách voice dài.
 */
export function Combobox({
  options,
  value,
  onChange,
  placeholder = "Chọn...",
  emptyMessage = "Không có lựa chọn phù hợp",
  className,
  inputClassName,
  disabled,
  onFocus: onFocusProp,
  onBlur: onBlurProp,
  onClick: onClickProp,
  onKeyDown: onKeyDownProp,
  type = "text",
  ...inputProps
}: ComboboxProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(-1);
  const rootRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const listboxId = useId();

  const selected = options.find((option) => option.value === value);
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase("vi-VN");
    if (!needle) return options;
    return options.filter((option) =>
      `${option.label} ${option.value} ${option.description || ""}`.toLocaleLowerCase("vi-VN").includes(needle)
    );
  }, [options, query]);

  useEffect(() => {
    if (!open) return;
    setActiveIndex(filtered.length ? 0 : -1);
  }, [filtered.length, open]);

  useEffect(() => {
    if (!open) return;
    const handlePointerDown = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [open]);

  useEffect(() => {
    if (!open || activeIndex < 0) return;
    document.getElementById(`${listboxId}-${activeIndex}`)?.scrollIntoView?.({ block: "nearest" });
  }, [activeIndex, listboxId, open]);

  const close = () => {
    setOpen(false);
    setQuery("");
    setActiveIndex(-1);
  };

  const choose = (option: ComboboxOption) => {
    onChange(option.value);
    close();
    inputRef.current?.focus();
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    onKeyDownProp?.(event);
    if (event.defaultPrevented || disabled) return;

    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (!open) {
        setOpen(true);
        setActiveIndex(filtered.length ? 0 : -1);
      } else {
        setActiveIndex((current) => (filtered.length ? (current + 1) % filtered.length : -1));
      }
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      if (!open) {
        setOpen(true);
        setActiveIndex(filtered.length - 1);
      } else {
        setActiveIndex((current) => (filtered.length ? (current - 1 + filtered.length) % filtered.length : -1));
      }
      return;
    }
    if (event.key === "Enter" && open && activeIndex >= 0 && filtered[activeIndex]) {
      event.preventDefault();
      choose(filtered[activeIndex]);
      return;
    }
    if (event.key === "Escape" && open) {
      event.preventDefault();
      close();
    }
  };

  const displayValue = open ? query : selected?.label || value;

  return (
    <div ref={rootRef} className={cn("relative", className)}>
      <div className="relative">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
        <input
          {...inputProps}
          ref={inputRef}
          type={type}
          role="combobox"
          aria-autocomplete="list"
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-controls={listboxId}
          aria-activedescendant={open && activeIndex >= 0 ? `${listboxId}-${activeIndex}` : undefined}
          disabled={disabled}
          value={displayValue}
          placeholder={placeholder}
          onFocus={(event) => {
            onFocusProp?.(event);
            if (disabled) return;
            setQuery("");
            setOpen(true);
          }}
          onBlur={(event) => {
            onBlurProp?.(event);
            if (event.relatedTarget instanceof Node && rootRef.current?.contains(event.relatedTarget)) return;
            setOpen(false);
          }}
          onClick={(event) => {
            onClickProp?.(event);
            if (!disabled) setOpen(true);
          }}
          onChange={(event) => {
            setQuery(event.target.value);
            setOpen(true);
            setActiveIndex(0);
          }}
          onKeyDown={handleKeyDown}
          className={cn(
            "h-9 w-full rounded-md border border-input bg-background py-1 pl-9 pr-9 text-sm shadow-xs outline-none transition focus:border-primary focus:ring-2 focus:ring-primary/15 disabled:cursor-not-allowed disabled:opacity-50",
            inputClassName
          )}
        />
        <ChevronDown className="pointer-events-none absolute right-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
      </div>

      {open && !disabled && (
        <div
          id={listboxId}
          role="listbox"
          className="absolute left-0 right-0 top-[calc(100%+0.25rem)] z-50 max-h-60 overflow-auto rounded-md border border-border bg-card p-1 text-sm shadow-lg"
        >
          {filtered.length ? (
            filtered.map((option, index) => {
              const isSelected = option.value === value;
              const isActive = index === activeIndex;
              return (
                <div
                  key={`${option.value}-${index}`}
                  id={`${listboxId}-${index}`}
                  role="option"
                  aria-selected={isSelected}
                  className={cn(
                    "flex cursor-pointer items-center gap-2 rounded px-2.5 py-2",
                    isActive && "bg-muted",
                    !isActive && "hover:bg-muted/70"
                  )}
                  onMouseEnter={() => setActiveIndex(index)}
                  onPointerDown={(event) => event.preventDefault()}
                  onClick={() => choose(option)}
                >
                  <Check className={cn("h-3.5 w-3.5 shrink-0 text-primary", !isSelected && "invisible")} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate">{option.label}</span>
                    {option.description && (
                      <span className="block truncate font-mono text-[10px] text-muted-foreground">
                        {option.description}
                      </span>
                    )}
                  </span>
                </div>
              );
            })
          ) : (
            <div className="px-2.5 py-3 text-xs text-muted-foreground">{emptyMessage}</div>
          )}
        </div>
      )}
    </div>
  );
}
