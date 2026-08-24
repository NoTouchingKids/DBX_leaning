/**
 * Placeholder entry. M2 replaces this with the real shell (router, layout,
 * React Query provider); until then it renders the transport probe, which is
 * what M1 is judged on.
 */
import StreamProbe from "./dev/StreamProbe";

export default function App() {
  return <StreamProbe />;
}
