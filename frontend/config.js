// Automatically uses local dev API on port 5173, and same-origin (/api/*) when served behind a proxy (e.g. Docker / HF Spaces).
export const API_BASE_URL =
  window.location.port === '5173' ? 'http://127.0.0.1:8765' : '';
