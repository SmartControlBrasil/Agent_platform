export class GoogleMapsToolError extends Error {
  constructor(code, message = code, diagnostics = null) {
    super(message);
    this.name = 'GoogleMapsToolError';
    this.code = code;
    if (diagnostics) this.diagnostics = diagnostics;
  }
}

export function fail(code, message = code) {
  throw new GoogleMapsToolError(code, message);
}
