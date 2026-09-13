import { Check, Copy } from "lucide-react";
import { useState } from "react";
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
          <code>{link.url}</code>
          <button
            type="button"
            onClick={async () => {
              await navigator.clipboard?.writeText(link.url).catch(() => {});
              setCopied(true);
            }}
          >
            {copied ? <Check size={14} /> : <Copy size={14} />}{" "}
            {copied ? "Copied" : "Copy link"}
          </button>
        </div>
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
