import type { Config } from "../types";
import { ArrowRight, Check, Copy } from "lucide-react";
import { useState } from "react";
import { Dialog } from "../components";
export function ConnectionDialog({
  baseUrl,
  config,
  onClose,
  onManage,
}: {
  baseUrl: string;
  config: Config;
  onClose: () => void;
  onManage: () => void;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <Dialog title="Connect a caller" onClose={onClose}>
      <div className="dialog-body">
        <p>
          Point your agent at this router. Keep the route name as your models
          change.
        </p>
        <label className="field">
          <span>Base URL</span>
          <div className="copy-field">
            <code>{baseUrl}</code>
            <button
              className="icon-button"
              aria-label="Copy base URL"
              onClick={() => {
                void navigator.clipboard.writeText(`${baseUrl}`);
                setCopied(true);
              }}
            >
              {copied ? <Check size={17} /> : <Copy size={17} />}
            </button>
          </div>
        </label>
        <div className="endpoint-route">
          <span>Default route</span>
          <strong>
            {config.routes.find((r) => r.name === "auto" && r.enabled)?.name ||
              config.routes.find((r) => r.enabled)?.name ||
              "Configure a route"}
          </strong>
        </div>
        <p className="hint">
          {config.security.client_auth_enabled
            ? "Use this caller's existing key. Manage its permissions in Callers."
            : "Caller keys are optional. Callers without a key use their matching source policy or the selected shared policy. An existing key keeps its own permissions."}
        </p>
      </div>
      <footer>
        <button
          className="primary"
          onClick={() => {
            onClose();
            onManage();
          }}
        >
          Manage callers
          <ArrowRight size={16} />
        </button>
      </footer>
    </Dialog>
  );
}
