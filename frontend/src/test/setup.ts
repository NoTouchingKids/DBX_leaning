// jsdom has no IndexedDB and no SharedWorker. The transport spine is built on
// both, so tests get a real fake rather than a stub that agrees with whatever
// the implementation happens to do.
import "fake-indexeddb/auto";
import "@testing-library/jest-dom/vitest";
