export function mapToRecord<K extends string | number, V>(map: Map<K, V>): Record<K, V> {
  const record: Record<K, V> = {} as Record<K, V>;
  for (const k of map.keys()) {
    const value = map.get(k);
    record[k] = value as V;
  }
  return record;
}

export function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function stringToBuffer(array: string): ArrayBuffer {
  // array is a string of numbers split by commas
  const split = array.split(',');
  const buffer = new ArrayBuffer(split.length);
  const view = new Uint8Array(buffer);
  for (let i = 0; i < split.length; i++) {
    view[i] = parseInt(split[i]);
  }
  return buffer;
}


export function escapeString(str: string): string {
  return str.replace(/'/g, "''").replace(/"/g, '""');
}

export function typedArrayToBuffer(array: Uint8Array): ArrayBuffer {
  return array.buffer.slice(array.byteOffset, array.byteLength + array.byteOffset);
}