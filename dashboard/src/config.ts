// API configuration
// Uses VITE_API_URL environment variable, falls back to relative path for dev proxy
export const API_BASE_URL = import.meta.env.VITE_API_URL || '';
export const API_V1 = `${API_BASE_URL}/api/v1`;
