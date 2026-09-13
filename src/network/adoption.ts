import { newEngine } from "../editors/defaults";
import {
  OPENAI_COMPLETION_PATHS,
  type CompletionPath,
  type Engine,
} from "../types";
import {
  isProtectedCompletionEndpoint,
  needsManualEndpointReview,
} from "./service-classification";
import type { NetworkService } from "./types";

function exactPublishedModelIds(service: NetworkService): string[] {
  if (service.needs_model_identity !== false) return [];
  return [
    ...new Set(service.models.map((model) => model.id.trim()).filter(Boolean)),
  ];
}

function endpointHostname(origin: string): string {
  try {
    return new URL(origin).hostname;
  } catch {
    return origin;
  }
}

function observedEndpointUrl(service: NetworkService): string {
  return (
    service.observed_base_url ||
    service.compatible_base_url ||
    service.base_url ||
    service.origin
  );
}

function suggestedEngineName(
  service: NetworkService,
  hostName?: string,
): string {
  return hostName?.trim() || endpointHostname(observedEndpointUrl(service));
}

function provenCompletionPaths(service: NetworkService): CompletionPath[] {
  const observed = new Set(service.completion_paths || []);
  return OPENAI_COMPLETION_PATHS.filter((path) => observed.has(path));
}

function blankDiscoveryDraft(engine: Engine, name: string): Engine {
  return {
    ...engine,
    name,
    name_source: "discovered",
    // `newEngine` is a manual starting point and defaults to both operations.
    // A discovered surface must never inherit that assumption when inspection
    // did not prove an exact completion request path.
    completion_paths: [],
  };
}

/**
 * Turn an observed endpoint into an editable engine draft. A completion
 * surface gets an operator-declared inventory so the router does not present
 * a nonexistent catalog or infer a wildcard model selection.
 */
export function engineDraftFromNetworkService(
  service: NetworkService,
  hostName?: string,
): Engine {
  const engine = newEngine();
  const name = suggestedEngineName(service, hostName);
  const completion_paths = provenCompletionPaths(service);
  const cataloglessCompletionEndpoint = service.status === "model_surface";
  // A discovered surface needs both a compatible base and at least one exact
  // completion operation. Absent proof stays a blank manual draft, where the
  // operator must select an operation deliberately before binding a URL.
  if (
    !completion_paths.length ||
    needsManualEndpointReview(service) ||
    (cataloglessCompletionEndpoint && !service.compatible_base_url)
  ) {
    return blankDiscoveryDraft(engine, name);
  }
  const declaredModels = exactPublishedModelIds(service);
  return {
    ...engine,
    name,
    // A discovered compatible API is saved with only the operations it proved.
    completion_paths,
    // A path is usable only when inspection derived it. Do not turn an origin
    // into an assumed OpenAI endpoint by appending /v1.
    base_url:
      cataloglessCompletionEndpoint || isProtectedCompletionEndpoint(service)
        ? service.compatible_base_url || ""
        : service.observed_base_url ||
          service.base_url ||
          service.compatible_base_url ||
          "",
    ...(cataloglessCompletionEndpoint
      ? {
          source: "discovery",
          name_source: "discovered" as const,
          capabilities: service.capabilities,
          model_inventory_source: "declared" as const,
          declared_models: declaredModels,
          // These are the exact routable identities. An endpoint without a
          // catalog must never inherit the general local-engine wildcard.
          model_patterns: declaredModels,
        }
      : {
          source: "manual",
          name_source: "discovered" as const,
        }),
  };
}
