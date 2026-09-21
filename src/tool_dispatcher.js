import { ToolSlugs } from './constants.js';
import { ExternalSearchProbeHandler } from './tools/external_search_probe.js';

export class ToolDispatcher {
  constructor(registry = defaultRegistry()) {
    this.registry = registry;
  }

  async execute(execution) {
    const slug = execution?.tool_definition?.slug;
    const handler = this.registry[slug];
    if (!handler) {
      const error = new Error(`Unsupported tool: ${slug || 'unknown'}`);
      error.code = 'unsupported_tool';
      throw error;
    }
    return handler.handle(execution.request_payload || {}, execution);
  }
}

export function defaultRegistry() {
  return {
    [ToolSlugs.EXTERNAL_SEARCH_PROBE]: new ExternalSearchProbeHandler(),
  };
}
