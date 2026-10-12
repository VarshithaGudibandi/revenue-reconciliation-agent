import { useCallback, useState, useSyncExternalStore } from "react";

function subscribe(callback: () => void): () => void {
  window.addEventListener("popstate", callback);
  return () => window.removeEventListener("popstate", callback);
}

/** One query-string parameter, kept in the address so views can be linked and survive reload. */
export function useSearchParam(name: string): [string | null, (value: string | null) => void] {
  const value = useSyncExternalStore(
    subscribe,
    () => new URLSearchParams(window.location.search).get(name),
    () => null,
  );
  const set = useCallback(
    (next: string | null) => {
      const url = new URL(window.location.href);
      if (next === null) url.searchParams.delete(name);
      else url.searchParams.set(name, next);
      window.history.pushState(null, "", url);
      window.dispatchEvent(new PopStateEvent("popstate"));
    },
    [name],
  );
  return [value, set];
}

export type Theme = "light" | "dark" | "system";

function readTheme(): Theme {
  try {
    const stored = window.localStorage.getItem("theme");
    if (stored === "light" || stored === "dark") return stored;
  } catch {
    /* storage can be blocked */
  }
  return "system";
}

export function useTheme(): [Theme, (theme: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(readTheme);
  const apply = useCallback((next: Theme) => {
    setTheme(next);
    const root = document.documentElement;
    if (next === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", next);
    try {
      if (next === "system") window.localStorage.removeItem("theme");
      else window.localStorage.setItem("theme", next);
    } catch {
      /* storage can be blocked */
    }
  }, []);
  return [theme, apply];
}

export function applyStoredTheme(): void {
  const theme = readTheme();
  if (theme !== "system") document.documentElement.setAttribute("data-theme", theme);
}
