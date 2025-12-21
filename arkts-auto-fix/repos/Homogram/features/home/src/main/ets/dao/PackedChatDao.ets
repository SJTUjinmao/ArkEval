import sql from "@liushengyi/smartdb"
import { PackedChat } from "../entities/PackedChat"
import { DatabaseConstants } from "../constants/DatabaseConstants"

class PackedChatDao {
  constructor() {
  }

  @sql.Sql(`CREATE TABLE IF NOT EXISTS ${DatabaseConstants.PACKED_CHATS_TABLE_NAME} (
    chatId INTEGER NOT NULL,
    packedChat BLOB NOT NULL,
  	PRIMARY KEY (chatId),
  	FOREIGN KEY (chatId) REFERENCES ${DatabaseConstants.CHATS_TABLE_NAME}(chatId) DEFERRABLE INITIALLY DEFERRED
)`)
  createPackedChatsTable(): Promise<void> {
    return sql.PromiseNull();
  }

  @sql.Sql(`DROP TABLE IF EXISTS ${DatabaseConstants.PACKED_CHATS_TABLE_NAME}`)
  deletePackedChatsTable(): Promise<void> {
    return sql.PromiseNull();
  }

  @sql.SqlInsert("REPLACE INTO packed_chats (chatId, packedChat) " +
    "VALUES (#{chatId}, #{packedChat})")
  insert(@sql.Param("chatId") chatId: number, @sql.Param("packedChat") packedChat: Uint8Array): Promise<void> {
    // Base64.encode(Uint8Array.from(packedChat));
    return sql.PromiseNull();
  }

  @sql.SqlInsert(`REPLACE INTO packed_chats (chatId, packedChat) VALUES (#{packedChat.chatId}, #{packedChat.packedChat})`)
  insertPackedChat(@sql.Param("packedChat") packedChat: PackedChat): Promise<void> {
    return sql.PromiseNull();
  }

  @sql.SqlInsert("REPLACE INTO packed_chats (chatId, packedChat) VALUES #{packedChatsSql}")
  _insertPackedChats(@sql.Param("packedChatsSql", true) packedChatsSql: string): Promise<void> {
    return sql.PromiseNull();
  }

  async insertPackedChats(packedChats: Array<PackedChat>): Promise<void> {
    let packedChatsSql = packedChats.map((packedChat) => {
      return `(${packedChat.chatId}, ${packedChat.packedChat})`;
    }).join(", ");
    return this._insertPackedChats(packedChatsSql);
  }

  @sql.SqlQuery("SELECT * FROM packed_chats WHERE chatId = #{chatId}")
  @sql.ReturnType(PackedChat)
  getPackedChatById(@sql.Param("chatId") chatId: number): Promise<PackedChat> {
    return sql.PromiseNull();
  }
}

export const packedChatDao = new PackedChatDao();