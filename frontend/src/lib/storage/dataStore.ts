// Data Store abstraction layer
// This allows easy switching between localStorage, Firestore, or API backends

export interface DataStore<T> {
  getAll(): Promise<T[]>;
  getById(id: string): Promise<T | null>;
  save(item: T): Promise<void>;
  delete(id: string): Promise<void>;
  clear(): Promise<void>;
}

// Generate unique IDs
export function generateId(): string {
  return `${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
}
