/** Undo/redo of placement snapshots (plain JSON-able objects). */
export class History {
  constructor(limit = 50) {
    this.limit = limit;
    this.past = [];
    this.future = [];
  }

  /** Record the state *before* a change. */
  push(snapshot) {
    this.past.push(JSON.stringify(snapshot));
    if (this.past.length > this.limit) this.past.shift();
    this.future = [];
  }

  /** Drop the last record (a change that was cancelled). */
  discard() {
    this.past.pop();
  }

  undo(current) {
    if (!this.past.length) return null;
    this.future.push(JSON.stringify(current));
    return JSON.parse(this.past.pop());
  }

  redo(current) {
    if (!this.future.length) return null;
    this.past.push(JSON.stringify(current));
    return JSON.parse(this.future.pop());
  }

  clear() {
    this.past = [];
    this.future = [];
  }

  get canUndo() { return this.past.length > 0; }

  get canRedo() { return this.future.length > 0; }
}
