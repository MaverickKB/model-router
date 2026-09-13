import { Check, Copy } from "lucide-react";
import { useRef, useState } from "react";
import { Dialog } from "../components";
import type { ActivationLink } from "../types";

// The link carries its token in the URL fragment and is shown exactly once;
// the router keeps only a digest, so nothing here can be recovered later.
export function ActivationDialog({
  link,
  purpose,
  accountName,
  accountsEnabled,
  onClose,
}: {
  link: ActivationLink;
  purpose: "activate" | "reset";
  accountName: string;
  accountsEnabled: boolean;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const code = useRef<HTMLElement>(null);
  // The Clipboard API exists only in secure contexts; a plain-http console on
  // a LAN address has none, and a rejected write must not read as success.
  async function copy() {
    try {
      if (!navigator.clipboard) throw new Error("Clipboard unavailable");
      await navigator.clipboard.writeText(link.url);
      setCopied(true);
      setCopyFailed(false);
    } catch {
      setCopied(false);
      setCopyFailed(true);
      if (code.current) window.getSelection()?.selectAllChildren(code.current);
    }
  }
  return (
    <Dialog
      title={purpose === "activate" ? "Activation link" : "Password-reset link"}
      onClose={onClose}
    >
      <div className="dialog-body">
        <p>
          Hand this link to {accountName}. Opening it in the portal
          {purpose === "activate"
            ? " sets their password and activates the account."
            : " lets them choose a new password; other portal sessions are signed out."}
        </p>
        <div className="key-result">
          <p>Shown once. Expires in 72 hours.</p>
          <code ref={code}>{link.url}</code>
          <button type="button" onClick={() => void copy()}>
            {copied ? <Check size={14} /> : <Copy size={14} />}{" "}
            {copied ? "Copied" : "Copy link"}
          </button>
        </div>
        {copyFailed && (
          <p className="error" role="alert">
            Copy is unavailable here — select the link and copy it manually
            before pressing Done.
          </p>
        )}
        {!accountsEnabled && (
          <p className="hint" role="status">
            Accounts are disabled — this link works once accounts are enabled.
          </p>
        )}
      </div>
      <footer>
        <button type="button" className="primary" onClick={onClose}>
          Done
        </button>
      </footer>
    </Dialog>
  );
}
