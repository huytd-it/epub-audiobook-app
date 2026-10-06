import { api, PlaylistItem, PlaylistItemDetail } from "@/api";

/**
 * Client-side cache for the two YouTube listings the manager re-fetches on every
 * tab switch and click: the channel's playlists and one playlist's items.
 *
 * Both go straight through to the YouTube Data API on the backend (1 quota unit
 * per page of 50), so re-listing is never free - it just doesn't look slow enough
 * to notice. A TTL keeps ordinary browsing (open tab, look around, come back)
 * from re-listing at all, while every mutation invalidates its exact key right
 * away, so the cache can never show a playlist state the user just changed.
 * "Reload" buttons force a fresh fetch for when the channel was edited elsewhere.
 */

const PLAYLISTS_TTL_MS = 60_000;
const PLAYLIST_ITEMS_TTL_MS = 60_000;

type CacheEntry<T> = { value: T; fetched_at: number };

let playlistsEntry: CacheEntry<PlaylistItem[]> | null = null;
let playlistsInFlight: Promise<PlaylistItem[]> | null = null;

const itemsEntries = new Map<string, CacheEntry<PlaylistItemDetail[]>>();
const itemsInFlight = new Map<string, Promise<PlaylistItemDetail[]>>();

/**
 * Bumped by clearYouTubeCache(). The cache holds one account's listings at a time;
 * a response that was requested before the switch must not land in the new
 * account's cache, so every fetch remembers the epoch it started in.
 */
let epoch = 0;

/** Drop everything, including in-flight requests (the active YouTube account changed). */
export function clearYouTubeCache(): void {
  epoch += 1;
  playlistsEntry = null;
  playlistsInFlight = null;
  itemsEntries.clear();
  itemsInFlight.clear();
}

const fresh = (entry: CacheEntry<unknown> | null | undefined, ttl: number) =>
  entry != null && Date.now() - entry.fetched_at < ttl;

/** Cached playlist list; fetches only when older than the TTL or not cached. */
export async function loadPlaylistsCached(force = false): Promise<PlaylistItem[]> {
  if (!force && fresh(playlistsEntry, PLAYLISTS_TTL_MS) && playlistsEntry) {
    return playlistsEntry.value;
  }
  // One request per burst even when several callers race the same cold cache.
  if (!playlistsInFlight) {
    const startedIn = epoch;
    const request = api<{ items: PlaylistItem[] }>("/youtube/api/playlists")
      .then((res) => {
        const items = res.items || [];
        if (startedIn === epoch) playlistsEntry = { value: items, fetched_at: Date.now() };
        return items;
      })
      .finally(() => {
        if (playlistsInFlight === request) playlistsInFlight = null;
      });
    playlistsInFlight = request;
  }
  return playlistsInFlight;
}

/** Drop the cached playlist list (after any playlist create/delete/rename). */
export function invalidatePlaylists(): void {
  playlistsEntry = null;
}

/**
 * Cached full item list for one playlist. `force` bypasses the TTL (a mutation
 * elsewhere or the user asking for a reload); the shared in-flight promise keeps
 * the refetch to one request per burst too.
 */
export async function loadPlaylistItemsCached(
  playlistId: string,
  force = false
): Promise<PlaylistItemDetail[]> {
  const entry = itemsEntries.get(playlistId);
  if (!force && fresh(entry, PLAYLIST_ITEMS_TTL_MS) && entry) {
    return entry.value;
  }
  let inFlight = itemsInFlight.get(playlistId);
  if (!inFlight) {
    const startedIn = epoch;
    const request = api<{ items: PlaylistItemDetail[] }>(
      `/youtube/api/playlists/${playlistId}/items?fetch_all=true`
    )
      .then((res) => {
        const items = res.items || [];
        if (startedIn === epoch) {
          itemsEntries.set(playlistId, { value: items, fetched_at: Date.now() });
        }
        return items;
      })
      .finally(() => {
        if (itemsInFlight.get(playlistId) === request) itemsInFlight.delete(playlistId);
      });
    inFlight = request;
    itemsInFlight.set(playlistId, inFlight);
  }
  return inFlight;
}

/** Drop one playlist's cached items (after add/remove/reorder of that playlist). */
export function invalidatePlaylistItems(playlistId: string): void {
  itemsEntries.delete(playlistId);
}

/** Drop every playlist's cached items (after an edit that touched many playlists). */
export function invalidateAllPlaylistItems(): void {
  itemsEntries.clear();
}
