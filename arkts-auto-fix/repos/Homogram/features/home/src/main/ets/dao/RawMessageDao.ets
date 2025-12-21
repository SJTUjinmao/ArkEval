import sql from "@liushengyi/smartdb"
import { RawMessage } from "../entities/RawMessage"
import { DatabaseConstants } from "../constants/DatabaseConstants"
import { Base64 } from "@ohos/base64";
import { relationalStore } from '@kit.ArkData';

class RawMessageDao {
  constructor() {
  }

  @sql.Sql(`CREATE TABLE IF NOT EXISTS ${DatabaseConstants.RAW_MESSAGES_TABLE_NAME} (
    chatId INTEGER NOT NULL,
    messageId INTEGER NOT NULL,
    rawMessage BLOB NOT NULL,
  	PRIMARY KEY (chatId, messageId),
    FOREIGN KEY (chatId) REFERENCES ${DatabaseConstants.CHATS_TABLE_NAME}(chatId) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (messageId) REFERENCES ${DatabaseConstants.MESSAGES_TABLE_NAME}(messageId) DEFERRABLE INITIALLY DEFERRED
)`)
  createRawMessagesTable(): Promise<void> {
    return sql.PromiseNull();
  }

  @sql.Sql(`DROP TABLE IF EXISTS ${DatabaseConstants.RAW_MESSAGES_TABLE_NAME}`)
  deleteRawMessagesTable(): Promise<void> {
    return sql.PromiseNull();
  }

  // TODO: using Base64
  @sql.SqlInsert("REPLACE INTO raw_messages (chatId, messageId, rawMessage) " +
    "VALUES (#{chatId}, #{messageId}, #{rawMessage})")
  insert(@sql.Param("chatId") chatId: number, @sql.Param("messageId") messageId: number,
    @sql.Param("rawMessage") rawMessage: Uint8Array): Promise<void> {
    return sql.PromiseNull();
  }

  // TODO: using Base64
  @sql.SqlInsert("REPLACE INTO raw_messages (chatId, messageId, rawMessage) " +
    "VALUES (#{rawMessage.chatId}, #{rawMessage.messageId}, #{rawMessage.rawMessage})")
  insertRawMessage(@sql.Param("rawMessage") rawMessage: RawMessage): Promise<void> {
    return sql.PromiseNull();
  }

  @sql.SqlInsert("REPLACE INTO raw_messages (chatId, messageId, rawMessage) VALUES #{rawMessagesSql}")
  _insertRawMessages(@sql.Param("rawMessagesSql", true) rawMessagesSql: string): Promise<void> {
    return sql.PromiseNull();
  }

  async insertRawMessages(rawMessages: Array<RawMessage>): Promise<void> {
    let rdb = sql.dbHelper.rdbStore;
    rdb.beginTransaction();
    const valueBuckets = await Promise.all(rawMessages.map(async (rawMessage) => {
      let valueBucket: relationalStore.ValuesBucket = {
        chatId: rawMessage.chatId,
        messageId: rawMessage.messageId,
        rawMessage: Base64.encodeToString(new Uint8Array(rawMessage.rawMessage))
      };
      return valueBucket;
    }));
    await rdb.batchInsert(DatabaseConstants.RAW_MESSAGES_TABLE_NAME, valueBuckets);
    rdb.commit();
    // let rawMessagesSql = rawMessages.map((rawMessage) => {
    //   return `(${rawMessage.chatId}, ${rawMessage.messageId}, ${Base64.encodeToString(new Uint8Array(rawMessage.rawMessage))})`;
    // }).join(", ");
    // return this._insertRawMessages(rawMessagesSql);
  }

  @sql.SqlQuery("SELECT * FROM raw_messages WHERE chatId = #{chatId} AND messageId = #{messageId}")
  @sql.ReturnType(RawMessage)
  getRawMessageById(@sql.Param("chatId") chatId: number,
    @sql.Param("messageId") messageId: number): Promise<RawMessage> {
    return sql.PromiseNull();
  }

  @sql.SqlQuery("SELECT * FROM raw_messages WHERE chatId = #{chatId}")
  @sql.ReturnType([RawMessage])
  getRawMessagesByChatId(@sql.Param("chatId") chatId: number): Promise<Array<RawMessage>> {
    return sql.PromiseNull();
  }
}

export const rawMessageDao = new RawMessageDao();