import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/**
 * Append the session token as a query param so browser-native elements
 * (<img src>, <a href download>, etc.) can load authenticated /api/media/*
 * resources — these cannot carry a custom X-Markbot-Session-Token header.
 */
export function authUrl(url: string): string {
  if (!url.startsWith("/api/")) return url;
  const w = window as any;
  const token: string = w.__MARKBOT_SESSION_TOKEN__ ?? "";
  if (!token) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}
