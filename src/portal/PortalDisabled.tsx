import { Network } from "lucide-react";

export function PortalDisabled() {
  return (
    <div className="unlock">
      <Network size={34} />
      <h1>Model Router · Portal</h1>
      <p>
        User accounts are not enabled on this router. Ask the administrator.
      </p>
    </div>
  );
}
