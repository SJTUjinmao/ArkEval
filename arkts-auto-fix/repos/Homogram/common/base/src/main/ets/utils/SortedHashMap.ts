import { HashMap } from '@kit.ArkTS';

export interface IComparable<T> {
  compareTo(other: T): number;
}

export class SortedHashMap<K, V extends IComparable<V>> {
  private map: HashMap<K, V>;
  private sortedValues: V[];

  constructor() {
    this.map = new HashMap<K, V>();
    this.sortedValues = [];
  }

  set(key: K, value: V): number {
    if (this.map.hasKey(key)) {
      const oldValue = this.map.get(key);
      this.removeSortedValue(oldValue);
    }
    this.map.set(key, value);
    return this.insertSortedValue(value);
  }

  setAll(map: HashMap<K, V>): void {
    this.map.setAll(map);
  }

  get(key: K): V | undefined {
    return this.map.get(key);
  }

  has(key: K): boolean {
    return this.map.hasKey(key);
  }

  delete(key: K): number {
    if (this.map.hasKey(key)) {
      const value = this.map.get(key);
      const index = this.removeSortedValue(value);
      this.map.remove(key);
      return index;
    }
    return -1;
  }

  clear(): void {
    this.map.clear();
    this.sortedValues = [];
  }

  getSortedValues(): V[] {
    return this.sortedValues;
  }

  length(): number {
    return this.sortedValues.length;
  }

  indexOf(key: K): number {
    // TODO: this is not efficient, we should have a map from value to index
    if (!this.map.hasKey(key)) {
      return -1;
    }
    const value = this.map.get(key);
    return this.sortedValues.indexOf(value);
  }

  getValueAt(index: number): V | undefined {
    if (index < 0 || index >= this.sortedValues.length) {
      return undefined;
    }
    return this.sortedValues[index];
  }

  hasValueAt(index: number): boolean {
    return index >= 0 && index < this.sortedValues.length;
  }

  // TODO: for those two methods returning IterableIterator, we should pay attention if new values are added during iteration

  valuesIterator(): IterableIterator<V> {
    return this.map.values();
  }

  sortedValuesIterator(): IterableIterator<V> {
    return this.sortedValues.values();
  }

  private binarySearchInsertIndex(value: V): number {
    let low = 0;
    let high = this.sortedValues.length - 1;

    while (low <= high) {
      const mid = Math.floor((low + high) / 2);
      if (this.sortedValues[mid].compareTo(value) < 0) {
        high = mid - 1;
      } else {
        low = mid + 1;
      }
    }

    return low;
  }

  private insertSortedValue(value: V): number {
    const index = this.binarySearchInsertIndex(value);
    this.sortedValues.splice(index, 0, value);
    return index;
  }

  private removeSortedValue(value: V): number {
    const index = this.sortedValues.indexOf(value);
    if (index !== -1) {
      this.sortedValues.splice(index, 1);
    }
    return index;
  }
}
