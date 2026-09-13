import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { PortalApp } from "./portal/PortalApp";
// Same build, chosen by path: /portal and /portal/* are the account holder's page.
const portal =
  location.pathname === "/portal" || location.pathname.startsWith("/portal/");
createRoot(document.getElementById("app")!).render(
  <StrictMode>{portal ? <PortalApp /> : <App />}</StrictMode>,
);
