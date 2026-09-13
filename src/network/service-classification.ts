import type { NetworkService } from "./types";

export function isVerifiedCatalog(service: NetworkService): boolean {
  return service.status === "model_service" || service.status === "gateway";
}

export function isCompletionEndpoint(service: NetworkService): boolean {
  return (
    service.status === "model_surface" ||
    (service.status === "authentication_required" &&
      Boolean(service.compatible_base_url))
  );
}

export function isProtectedCompletionEndpoint(
  service: NetworkService,
): boolean {
  return (
    service.status === "authentication_required" &&
    Boolean(service.compatible_base_url)
  );
}

export function isNativeInventory(service: NetworkService): boolean {
  return service.status === "native_inventory";
}

/**
 * A native inventory or nonstandard data catalog can identify model names
 * without proving an OpenAI-compatible completion transport. Keep that
 * evidence visible, but require the operator to enter a documented URL.
 */
export function needsManualTransportEntry(service: NetworkService): boolean {
  return (
    isNativeInventory(service) ||
    (service.status === "http_service" &&
      service.models.length > 0 &&
      !service.compatible_base_url)
  );
}

/**
 * Discovery observed an API base but could not prove a complete completion
 * request and response contract. It stays visible for operator review and is
 * never auto-registered.
 */
export function needsManualEndpointReview(service: NetworkService): boolean {
  return (
    needsManualTransportEntry(service) ||
    // An authentication challenge may be observed before the inspector proves
    // that the same API base can complete an OpenAI request. Keep the observed
    // URL as evidence, but do not treat it as a routeable engine transport.
    (service.status === "authentication_required" &&
      !service.compatible_base_url) ||
    (service.status === "http_service" &&
      Boolean(service.observed_base_url || service.base_url))
  );
}

export function serviceKindLabel(service: NetworkService): string {
  if (service.status === "model_service") return "Verified model catalog";
  if (service.status === "model_surface")
    return "Completion endpoint, no model catalog";
  if (isProtectedCompletionEndpoint(service))
    return "Completion endpoint, access required";
  if (isNativeInventory(service))
    return "Native model inventory, OpenAI transport unverified";
  if (needsManualTransportEntry(service))
    return "Published model inventory, OpenAI transport unverified";
  if (service.status === "gateway") return "Routing service catalog";
  if (service.status === "http_service")
    return "HTTP service, no compatible completion transport";
  if (service.status === "authentication_required")
    return "Access required before inspection";
  if (service.status === "inspection_required") return "Needs endpoint review";
  return service.status.replaceAll("_", " ");
}

export function modelInventoryEvidenceLabel(service: NetworkService): string {
  return isNativeInventory(service)
    ? "Native inventory, OpenAI transport unverified"
    : "Published inventory, OpenAI transport unverified";
}
