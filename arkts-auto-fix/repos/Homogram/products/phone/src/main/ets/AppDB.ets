import sql from "@liushengyi/smartdb";
import { chatDao, messageDao, packedChatDao, rawMessageDao, seenChatDao } from "@ohos/home";
import relationalStore from "@ohos.data.relationalStore";
import { Logger } from '@ohos/base';

class AppDB {
  static DB_VERSION = 7;
  private static STORE_CONFIG: relationalStore.StoreConfig = {
    name: 'homo.db',
    securityLevel: relationalStore.SecurityLevel.S1,
    encrypt: false,
    isReadOnly: false
  };

  initDb(context: Context) {
    let appDbOpenHelper = new AppDbOpenHelper();
    sql.dbHelper.initDb(context, 'homo.db', AppDB.DB_VERSION, appDbOpenHelper)
      .catch((err: Error) => {
        Logger.error(`initDb failed due to ${err.name} - ${err.message}`);
      })
    // appDbOpenHelper.onUpgrade(sql.dbHelper.rdbStore, 0, 1); // force to delete and recreate tables, for debug purpose
  }
}

class AppDbOpenHelper extends sql.DbOpenHelper {
  constructor() {
    super()
  }

  async onCreate(db: relationalStore.RdbStore) {
    //用户表
    let promises = [
      chatDao.createChatsTable(),
      messageDao.createMessagesTable(),
      packedChatDao.createPackedChatsTable(),
      seenChatDao.createSeenChatsTable(),
      rawMessageDao.createRawMessagesTable()
    ];

    await Promise.all(promises);
    Logger.debug("onCreate all tables created")
  }

  async onUpgrade(db: relationalStore.RdbStore, oldVersion: number, newVersion: number) {
    if (oldVersion < newVersion) {
      Logger.debug("onUpgrade delete all tables");
      // delete the old db
      let promises = [
        seenChatDao.deleteSeenChatsTable(),
        packedChatDao.deletePackedChatsTable(),
        rawMessageDao.deleteRawMessagesTable(),
        messageDao.deleteMessagesTable(),
        chatDao.deleteChatsTable(),
      ];

      await Promise.all(promises);
      Logger.debug("onUpgrade all tables deleted");
      this.onCreate(db);
    }
  }
}

export default new AppDB()