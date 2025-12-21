import { relationalStore, ValuesBucket, ValueType } from '@kit.ArkData';
import { Context } from '@kit.AbilityKit';
import Logger from '../Logger/Logger';
import { BusinessError } from '@kit.BasicServicesKit';

export class Database {
  private static rdb: relationalStore.RdbStore | undefined = undefined;
  private static STORE_CONFIG: relationalStore.StoreConfig = {
    name: 'homo.db',
    securityLevel: relationalStore.SecurityLevel.S1,
    encrypt: false,
    isReadOnly: false
  };

  public static async init(context: Context) {\n    try {\n      if (Database.rdb == null) {\n        Database.rdb = await relationalStore.getRdbStore(context, Database.STORE_CONFIG);\n        Logger.info('Succeeded in getting RdbStore.');\n      }\n    } catch (err) {\n      Logger.error("Failed to get RdbStore. Code:${err.code}, message:${err.message}");\n    }\n  }

  public static isInit(): boolean {
    return Database.rdb != undefined;
  }

  public static deleteRdb(context: Context) {
    relationalStore.deleteRdbStore(context, Database.STORE_CONFIG, (err: BusinessError) => {
      if (err) {
        console.error(`Delete RdbStore failed, code is ${err.code},message is ${err.message}`);
        return;
      }
      Logger.debug("rdb is deleted");
      Database.rdb = undefined;
    });
  }

  public static getInstance(): relationalStore.RdbStore | undefined {
    if (!Database.rdb) {
      Logger.error("rdb is null, please init it first.");
      return undefined;
    }
    return Database.rdb;
  }

  public static async execute(sql: string, args?: ValueType[]): Promise<boolean> {
    let rdb = Database.getInstance();
    let result = false;
    if (rdb) {
      try {
        rdb.beginTransaction();
        rdb.executeSync(sql, args);
        rdb.commit();
        result = true;
      } catch (err) {
        Logger.error(`execute ${sql} failed: ${err.message}`);
        rdb.rollBack();
      }
    }
    return result;
  }

  public static executeSync(sql: string, args?: ValueType[]): boolean {
    let rdb = Database.getInstance();
    let result = false;
    if (rdb) {
      try {
        rdb.executeSync(sql, args);
        result = true;
      } catch (err) {
        Logger.error(`executeSync ${sql} failed: ${err.message}`);
      }
    }
    return result;
  }

  public static async insert(table: string, cols: string[], vals: ValueType[]): Promise<number> {
    let rdb = Database.getInstance();
    let rowId = -1;
    if (rdb) {
      try {
        let vb = Database.ToValuesBucket(cols, vals);
        rdb.beginTransaction();
        rowId = rdb.insertSync(table, vb);
        rdb.commit();
      } catch (err) {
        Logger.error(`insert ${table} failed: ${err.message}`);
        rdb.rollBack();
      }
    }
    return rowId;
  }

  public static insertSync(table: string, cols: string[], vals: ValueType[]): number {
    let rdb = Database.getInstance();
    let rowId = -1;
    if (rdb) {
      try {
        let vb = Database.ToValuesBucket(cols, vals);
        rdb.beginTransaction();
        rowId = rdb.insertSync(table, vb);
        rdb.commit();
      } catch (err) {
        Logger.error(`insertSync ${table} failed: ${err.message}`);
        rdb.rollBack();
      }
    }
    return rowId;
  }

  public static async query(sql: string, args?: ValueType[]): Promise<Object[]> {
    let rdb = Database.getInstance();
    let result: Object[] = [];
    if (rdb) {
      try {
        let resultSet = rdb.querySqlSync(sql, args);
        while (resultSet.goToNextRow()) {
          let row = resultSet.getRow();
          result.push(row);
        }
      } catch (err) {
        Logger.error(`query ${sql} failed: ${err.message}`);
      }
    }
    return result;
  }

  public static querySync(sql: string, args?: ValueType[]): Object[] {
    let rdb = Database.getInstance();
    let result: Object[] = [];
    if (rdb) {
      try {
        let resultSet = rdb.querySqlSync(sql, args);
        while (resultSet.goToNextRow()) {
          let row = resultSet.getRow();
          result.push(row);
        }
        resultSet.close();
      } catch (err) {
        Logger.error(`querySync ${sql} failed: ${err.message}`);
      }
    }
    return result;
  }

  public static querySyncWithType<T>(sql: string, args?: ValueType[]): T[] {
    let rdb = Database.getInstance();
    let result: T[] = [];
    if (rdb) {
      try {
        let resultSet = rdb.querySqlSync(sql, args);
        while (resultSet.goToNextRow()) {
          let row = resultSet.getRow();
          result.push(row as T);
        }
        resultSet.close();
      } catch (err) {
        Logger.error(`querySyncWithType ${sql} failed: ${err.message}`);
      }
    }
    return result;
  }

  public static queryAllSync<T>(table: string): T[] {
    let rdb = Database.getInstance();
    let predicates = new relationalStore.RdbPredicates(table);
    let result: T[] = [];

    if (rdb) {
      try {
        let resultSet = rdb.querySync(predicates);
        while (resultSet.goToNextRow()) {

        }
        resultSet.close();
      } catch (err) {
        Logger.error(`queryAllSync ${table} failed: ${err.message}`);
      }
    }
    return result;
  }

  public static async delete() {
    // TODO: implement this method
  }

  public static deleteSync() {
    // TODO: implement this method
  }

  public static async update() {
    // TODO: implement this method
  }

  public static updateSync() {
    // TODO: implement this method
  }

  private static ToValuesBucket(cols: string[], vals: ValueType[]): ValuesBucket {
    let vb: ValuesBucket = {};
    for (let i = 0; i < cols.length; i++) {
      vb[cols[i]] = vals[i];
    }
    return vb;
  }
}