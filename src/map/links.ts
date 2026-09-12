import type { Config, Selector } from "../types";

export type MapLink =
  | { kind: "caller"; callerId: string; routeId: string }
  | {
      kind: "engine";
      routeId: string;
      engineId: string;
      tier: "primary" | "fallback";
    };

export function linkPolicy(config: Config, link: MapLink): Config {
  if (link.kind === "caller") {
    const route = config.routes.find((value) => value.id === link.routeId)!;
    return {
      ...config,
      clients: config.clients.map((client) =>
        client.id !== link.callerId
          ? client
          : {
              ...client,
              route_names: [...new Set([...client.route_names, route.name])],
            },
      ),
    };
  }
  const engine = config.engines.find((value) => value.id === link.engineId)!;
  return {
    ...config,
    routes: config.routes.map((route) => {
      if (route.id !== link.routeId) return route;
      const selector: Selector = route[link.tier] || {
        kind: engine.kind,
        engine_ids: [],
        model_patterns: ["*"],
        tags: [],
      };
      return {
        ...route,
        [link.tier]: {
          ...selector,
          kind: selector.kind === engine.kind ? selector.kind : "any",
          engine_ids: [...new Set([...selector.engine_ids, engine.id])],
        },
      };
    }),
  };
}
