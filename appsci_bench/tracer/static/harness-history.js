/* Display-only storage and variable-height windowing for native harness history.
 * Keep the complete (server-bounded) history outside Alpine's reactive DOM state.
 * Only the current window is copied into the component for rendering.
 */
class HarnessHistoryWindow {
    constructor() {
        this.threshold = 200;
        this.overscan = 6;
        this.estimatedHeight = 160;
        this.reset();
    }

    reset() {
        this.entries = [];
        this.indices = new Map();
        this.calls = new Map();
        this.expanded = new Set();
        this.heights = [];
        this.offsets = [0];
        this.dirty = 0;
        this.messageCount = 0;
    }

    append(entries) {
        for (const entry of entries) {
            if (this.indices.has(entry.id)) continue;
            this.indices.set(entry.id, this.entries.length);
            this.entries.push({...entry});
            this.heights.push(this.estimatedHeight);
            if (entry.kind === 'assistant') this.messageCount++;
            if (entry.kind === 'tool_call' && entry.call_id) this.calls.set(entry.call_id, entry.id);
            if (entry.kind === 'tool_result' && entry.call_id) {
                const index = this.indices.get(this.calls.get(entry.call_id));
                if (index !== undefined) this.entries[index].status = entry.status || 'completed';
            }
        }
    }

    layout() {
        for (let i = this.dirty; i < this.entries.length; i++) {
            this.offsets[i + 1] = this.offsets[i] + this.heights[i];
        }
        this.dirty = this.entries.length;
    }

    invalidateHeights() {
        this.heights.fill(this.estimatedHeight);
        this.dirty = 0;
    }

    measure(rows) {
        let changed = false;
        for (const [id, height] of rows) {
            const index = this.indices.get(id);
            if (index === undefined || !Number.isFinite(height) || height <= 0) continue;
            if (Math.abs(this.heights[index] - height) < 1) continue;
            this.heights[index] = height;
            this.dirty = Math.min(this.dirty, index);
            changed = true;
        }
        return changed;
    }

    indexAt(position) {
        this.layout();
        let low = 0, high = this.entries.length;
        while (low < high) {
            const mid = (low + high) >>> 1;
            if (this.offsets[mid + 1] <= position) low = mid + 1;
            else high = mid;
        }
        return Math.min(low, Math.max(0, this.entries.length - 1));
    }

    range(scrollTop, viewportHeight) {
        this.layout();
        const count = this.entries.length;
        const virtual = count > this.threshold;
        const start = virtual ? Math.max(0, this.indexAt(scrollTop) - this.overscan) : 0;
        const end = virtual
            ? Math.min(count, this.indexAt(scrollTop + viewportHeight) + this.overscan + 1) : count;
        return {
            start, end,
            top: this.offsets[start],
            bottom: this.offsets[count] - this.offsets[end],
            entries: this.entries.slice(start, end).map(entry => ({
                ...entry, expanded: this.expanded.has(entry.id),
            })),
        };
    }

    topFor(id) {
        this.layout();
        const index = this.indices.get(id);
        return index === undefined ? null : this.offsets[index];
    }
}
