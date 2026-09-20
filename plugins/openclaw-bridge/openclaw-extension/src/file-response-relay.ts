// File contents stay in memory only, for the bounded request/reconnect window.
export class FileResponseRelay {
 private senders = new Map<string, (message: any) => void>();
 private replies = new Map<string, { scope: string; message: any; expires: number; bytes: number }>();
 constructor(private now = Date.now) {}
 private prune() {
  for (const [key, entry] of this.replies) if (entry.expires <= this.now()) this.replies.delete(key);
 }
 connect(scope: string, send: (message: any) => void): () => void {
  this.prune(); this.senders.set(scope, send);
  for (const entry of this.replies.values()) if (entry.scope === scope) send(entry.message);
  return () => { if (this.senders.get(scope) === send) this.senders.delete(scope); };
 }
 deliver(scope: string, message: any) {
  this.prune();
  const key = scope + '\n' + message.data.requestId;
  const bytes = Buffer.byteLength(JSON.stringify(message));
  this.replies.set(key, { scope, message, expires: this.now() + 30_000, bytes });
  while (this.replies.size > 16 || [...this.replies.values()].reduce((sum, item) => sum + item.bytes, 0) > 5_000_000) {
   this.replies.delete(this.replies.keys().next().value!);
  }
  // Keep the exact reply briefly even after sending: credential rotation can
  // invalidate an apparently open socket before its close event arrives.
  this.senders.get(scope)?.(message);
  const timer = setTimeout(() => this.prune(), 30_100); timer.unref();
 }
}
export const fileResponses = new FileResponseRelay();
