import { z } from "zod";
import { getOmnigentServerIdentity } from "./host";
import { getCurrentUserId } from "./identity";

const MAX_AGE_MS = 24 * 60 * 60 * 1000;
const cacheSchema = z.object({ displayName: z.string(), savedAt: z.number() });

export interface SessionModelLabelScope {
  sessionId: string | null;
  hostId: string | null;
  agentId: string | null;
  harness: string | null;
}

export function getSessionModelLabelCacheKey(
  scope: SessionModelLabelScope,
  model: string | null,
): string | null {
  const server = getOmnigentServerIdentity();
  const user = getCurrentUserId();
  if (server === null || user === null || scope.sessionId === null || !model) return null;
  return `omnigent:session-model-label:v1:${JSON.stringify([
    server,
    user,
    scope.sessionId,
    scope.hostId,
    scope.agentId,
    scope.harness,
    model,
  ])}`;
}

export function readSessionModelLabelCache(key: string | null): string | null {
  if (key === null || typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === null) return null;
    let record: unknown = null;
    try {
      record = JSON.parse(raw);
    } catch {
      // Malformed JSON is dropped below like any other rejected record.
    }
    const parsed = cacheSchema.safeParse(record);
    if (!parsed.success) {
      // Unparseable records never become valid again; drop them so dead
      // entries don't accumulate toward the storage quota.
      window.localStorage.removeItem(key);
      return null;
    }
    const age = Date.now() - parsed.data.savedAt;
    if (age > MAX_AGE_MS) {
      window.localStorage.removeItem(key);
      return null;
    }
    // A future timestamp (clock skew) can become valid again; leave it.
    return age >= 0 ? parsed.data.displayName : null;
  } catch {
    return null;
  }
}

// Only advertised display names belong here, never model selections or inferred labels.
export function writeSessionModelLabelCache(key: string | null, displayName: string | null): void {
  if (key === null || typeof window === "undefined") return;
  try {
    if (displayName === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, JSON.stringify({ displayName, savedAt: Date.now() }));
  } catch {
    // Private browsing and storage quotas must not affect the live composer.
  }
}
