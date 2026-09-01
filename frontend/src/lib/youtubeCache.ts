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

const fresh = (entry: CacheEntry<unknown> | null | undefined, ttl: number) =>
  entry != null && Date.now() - entry.fetched_at < ttl;

/** Cached playlist list; fetches only when older than the TTL or not cached. */
export async function loadPlaylistsCached(force = false): Promise<PlaylistItem[]> {
  if (!force && fresh(playlistsEntry, PLAYLISTS_TTL_MS) && playlistsEntry) {
    return playlistsEntry.value;
  }
  // One request per burst even when several callers race the same cold cache.
  if (!playlistsInFlight) {
    playlistsInFlight = api<{ items: PlaylistItem[] }>("/youtube/api/playlists")
      .then((res) => {
        playlistsEntry = { value: res.items || [], fetched_at: Date.now() };
        return playlistsEntry.value;
      })
      .finally(() => {
        playlistsInFlight = null;
      });
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
    inFlight = api<{ items: PlaylistItemDetail[] }>(
      `/youtube/api/playlists/${playlistId}/items?fetch_all=true`
    )
      .then((res) => {
        const items = res.items || [];
        itemsEntries.set(playlistId, { value: items, fetched_at: Date.now() });
        return items;
      })
      .finally(() => {
        itemsInFlight.delete(playlistId);
      });
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
