/**
 * IndexedDB, hand-rolled.
 *
 * No wrapper library: the surface used here is four operations, and the
 * worker is the only writer. A dependency would be larger than the code.
 *
 * Why persist at all — a run is minutes to hours long, the app itself stops
 * after ~8h, and Databricks Apps ingress cuts long connections. Reopening a
 * tab should not mean refetching from the SQL warehouse, because warehouse
 * *uptime* is what costs money on Free Edition. Cached messages are free.
 *
 * `messages` is keyed `[run_id, seq]`, which does the dedupe for us: a live
 * message and its backfilled twin have the same seq and `put` collapses them.
 * That is the whole reason `seq` is assigned by the job rather than by a UC
 * identity column.
 */

import type { Message, RunStatus } from "@/lib/envelope";

const DB_NAME = "dbx-leaning";
const DB_VERSION = 1;
const MESSAGES = "messages";
const RUNS = "runs";

/** What we remember about a run between visits. Not authoritative — the
 *  server's `run_status` row is. This is a cache for a first paint. */
export interface CachedRun {
  run_id: string;
  model: string | null;
  status: RunStatus | null;
  terminal: boolean;
  last_seq: number;
  updated_ts: number;
}

function promisify<T>(req: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

function done(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error ?? new Error("transaction aborted"));
  });
}

let dbPromise: Promise<IDBDatabase> | null = null;

export function openDb(): Promise<IDBDatabase> {
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(MESSAGES)) {
        const store = db.createObjectStore(MESSAGES, { keyPath: ["run_id", "seq"] });
        // Range-scanning the compound key would work, but an explicit index
        // makes "everything for this run, in seq order" the obvious read.
        store.createIndex("by_run", "run_id", { unique: false });
      }
      if (!db.objectStoreNames.contains(RUNS)) {
        db.createObjectStore(RUNS, { keyPath: "run_id" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
    // Another tab is trying to upgrade past us. Nothing to do at v1, but
    // silence here would be a mystery hang at v2.
    req.onblocked = () => reject(new Error("indexedDB upgrade blocked by another tab"));
  });
  return dbPromise;
}

/**
 * Close the handle and forget it.
 *
 * Nothing in the app calls this — the worker holds the database open for its
 * whole life — but an open handle BLOCKS `deleteDatabase` and any version
 * upgrade, so a test that swaps databases without closing hangs rather than
 * failing. Dropping the memoised promise alone is not enough.
 */
export async function closeDb(): Promise<void> {
  const pending = dbPromise;
  dbPromise = null;
  if (!pending) return;
  try {
    (await pending).close();
  } catch {
    // Never opened successfully; nothing to close.
  }
}

export async function putMessages(messages: readonly Message[]): Promise<void> {
  if (messages.length === 0) return;
  const db = await openDb();
  const tx = db.transaction(MESSAGES, "readwrite");
  const store = tx.objectStore(MESSAGES);
  // One transaction for the whole batch: a per-message transaction on an
  // MCMC run's progress stream is the difference between fine and janky.
  for (const msg of messages) store.put(msg);
  await done(tx);
}

/** Everything cached for a run, ascending by seq. Holes are normal. */
export async function readMessages(runId: string): Promise<Message[]> {
  const db = await openDb();
  const tx = db.transaction(MESSAGES, "readonly");
  const index = tx.objectStore(MESSAGES).index("by_run");
  const rows = await promisify(index.getAll(IDBKeyRange.only(runId)));
  // getAll on an index yields primary-key order within the index key, which
  // for `[run_id, seq]` is already seq-ascending — sorted anyway rather than
  // relying on that.
  return (rows as Message[]).sort((a, b) => a.seq - b.seq);
}

export async function putRun(run: CachedRun): Promise<void> {
  const db = await openDb();
  const tx = db.transaction(RUNS, "readwrite");
  tx.objectStore(RUNS).put(run);
  await done(tx);
}

export async function getRun(runId: string): Promise<CachedRun | undefined> {
  const db = await openDb();
  const tx = db.transaction(RUNS, "readonly");
  return promisify(tx.objectStore(RUNS).get(runId)) as Promise<CachedRun | undefined>;
}

/** Drop a run's cache. Used by the "refetch from source" escape hatch — a
 *  cached run that went weird should be fixable without clearing site data. */
export async function forgetRun(runId: string): Promise<void> {
  const db = await openDb();

  // Two transactions on purpose. An IndexedDB transaction auto-commits as
  // soon as the microtask queue drains with no pending request, so awaiting
  // the key lookup and THEN issuing deletes on the same transaction throws
  // TransactionInactiveError — reliably, and only at runtime.
  const read = db.transaction(MESSAGES, "readonly");
  const keys = await promisify(
    read.objectStore(MESSAGES).index("by_run").getAllKeys(IDBKeyRange.only(runId)),
  );

  const write = db.transaction([MESSAGES, RUNS], "readwrite");
  const store = write.objectStore(MESSAGES);
  for (const key of keys) store.delete(key);
  write.objectStore(RUNS).delete(runId);
  await done(write);
}
