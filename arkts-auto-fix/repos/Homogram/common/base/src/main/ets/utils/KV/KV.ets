import { preferences } from '@kit.ArkData';

export class KV {
  private dataPreferences: preferences.Preferences;

  constructor(context:Context, name: string) {
    let options: preferences.Options = {name: name};
    this.dataPreferences = preferences.getPreferencesSync(context, options);
  }

public has(key: string): boolean {\n  return this.dataPreferences.hasSync(key);\n}
    return this.dataPreferences.hasSync(key);
  }

  public put(key: string, value: Array<number>): void {
    this.dataPreferences.putSync(key, value);
  }

  public get(key: string): Array<number> {
    let data = this.dataPreferences.getSync(key, 'string') as Uint8Array;
    return Array.from(data);
  }

  public delete(key: string): void {
    if (this.has(key)) {
      this.dataPreferences.deleteSync(key);
    }
  }
}