import { Check, ChevronDown, X } from "lucide-react";
import {
  useEffect,
  useId,
  useRef,
  useState,
  type InputHTMLAttributes,
  type ReactNode,
} from "react";

export function Switch({
  checked,
  onChange,
  label,
  disabled = false,
}: {
  checked: boolean;
  onChange: () => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      className="switch"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={onChange}
      disabled={disabled}
    >
      <span />
    </button>
  );
}
export function Dialog({
  title,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    ref.current?.showModal();
    return () => ref.current?.close();
  }, []);
  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      className={wide ? "dialog wide" : "dialog"}
      onCancel={onClose}
      onClick={(e) => {
        if (e.target === ref.current) onClose();
      }}
    >
      <div className="dialog-head">
        <h2 id={titleId}>{title}</h2>
        <button className="icon-button" onClick={onClose} aria-label="Close">
          <X size={19} />
        </button>
      </div>
      {children}
    </dialog>
  );
}
export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}
export function Choice({
  checked,
  onClick,
  children,
}: {
  checked: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      className={"choice " + (checked ? "chosen" : "")}
      aria-pressed={checked}
      onClick={onClick}
    >
      <span className="checkmark">{checked && <Check size={12} />}</span>
      {children}
    </button>
  );
}
export function Select({
  value,
  onChange,
  children,
  label,
}: {
  value: string;
  onChange: (v: string) => void;
  children: ReactNode;
  label?: string;
}) {
  return (
    <span className="select-wrap">
      <select
        aria-label={label}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {children}
      </select>
      <ChevronDown size={14} />
    </span>
  );
}
export const toggle = (values: string[], value: string) =>
  values.includes(value)
    ? values.filter((x) => x !== value)
    : [...values, value];
export const words = (value: string) =>
  value
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
export function timeLabel(ts: number) {
  return ts
    ? new Date(ts * 1000).toLocaleTimeString([], {
        hour: "numeric",
        minute: "2-digit",
        second: "2-digit",
      })
    : "Waiting for discovery";
}

export function ListInput({
  values,
  onValues,
  ...props
}: { values: (string | number)[]; onValues: (v: string[]) => void } & Omit<
  InputHTMLAttributes<HTMLInputElement>,
  "value" | "onChange"
>) {
  const [text, setText] = useState(values.join(", "));
  useEffect(() => {
    if (JSON.stringify(values.map(String)) !== JSON.stringify(words(text))) {
      setText(values.join(", "));
    }
  }, [values, text]);
  return (
    <input
      {...props}
      value={text}
      onChange={(e) => {
        setText(e.target.value);
        onValues(words(e.target.value));
      }}
    />
  );
}
