import sql from "@liushengyi/smartdb"
import { Chat } from "../entities/Chat"
import { DatabaseConstants } from "../constants/DatabaseConstants"

class ChatDao {
  constructor() {
  }

  @sql.Sql(`CREATE TABLE IF NOT EXISTS ${DatabaseConstants.CHATS_TABLE_NAME} (
    chatId INTEGER,
    chatType INTEGER NOT NULL,
    name TEXT NOT NULL,
    pinned BOOLEAN DEFAULT FALSE,
    lastMessageId INTEGER,
    lastMessageSenderName TEXT,
    lastMessageText TEXT,
    lastMessageTimestamp INTEGER,
    megagroup BOOLEAN DEFAULT FALSE,
    forum BOOLEAN DEFAULT FALSE,
  	PRIMARY KEY (chatId)
)`)
  createChatsTable(): Promise<void> {
    return sql.PromiseNull();
  }

  @sql.Sql(`DROP TABLE IF EXISTS ${DatabaseConstants.CHATS_TABLE_NAME}`)
  deleteChatsTable(): Promise<void> {
    return sql.PromiseNull();
  }

  @sql.SqlInsert("REPLACE INTO chats (chatId, chatType, name, pinned, lastMessageId, lastMessageSenderName, lastMessageText, lastMessageTimestamp, megagroup, forum) " +
    "VALUES (#{chatId}, #{chatType}, #{name}, #{pinned}, #{lastMessageId}, #{lastMessageSenderName}, #{lastMessageText}, #{lastMessageTimestamp}, #{megagroup}, #{forum})")
  insert(@sql.Param("chatId") chatId: number, @sql.Param("chatType") chatType: number, @sql.Param("name") name: string,
    @sql.Param("pinned") pinned: boolean, @sql.Param("lastMessageId") lastMessageId: number,
    @sql.Param("lastMessageSenderName") lastMessageSenderName: string,
    @sql.Param("lastMessageText") lastMessageText: string,
    @sql.Param("lastMessageTimestamp") lastMessageTimestamp: number,
    @sql.Param("megagroup") megagroup: boolean, @sql.Param("forum") forum: boolean): Promise<void> {
    return sql.PromiseNull();
  }

  @sql.SqlInsert("REPLACE INTO chats (chatId, chatType, name, pinned, lastMessageId, lastMessageSenderName, lastMessageText, lastMessageTimestamp, megagroup, forum) " +
    "VALUES (#{chat.chatId}, #{chat.chatType}, #{chat.name}, #{chat.pinned}, #{chat.lastMessageId}, #{chat.lastMessageSenderName}, #{chat.lastMessageText}, " +
    "#{chat.lastMessageTimestamp}, #{chat.megagroup}, #{chat.forum})")
  insertChat(@sql.Param("chat") chat: Chat): Promise<void> {
    return sql.PromiseNull();
  }

  @sql.SqlInsert("REPLACE INTO chats (chatId, chatType, name, pinned, lastMessageId, lastMessageSenderName, lastMessageText, lastMessageTimestamp, megagroup, forum) VALUES #{chatsSql}")
  _insertChats(@sql.Param("chatsSql", true) chatsSql: string): Promise<void> {
    return sql.PromiseNull();
  }

  async insertChats(chats: Array<Chat>): Promise<void> {
    let chatsSql = chats.map((chat) => {
      return `(${chat.chatId}, ${chat.chatType}, "${chat.name}", ${chat.pinned}, ${chat.lastMessageId}, "${chat.lastMessageSenderName}",
      "${chat.lastMessageText}", ${chat.lastMessageTimestamp}, ${chat.megagroup}, ${chat.forum})`;
    }).join(", ");
    return this._insertChats(chatsSql);
  }

  @sql.SqlQuery("SELECT * FROM chats WHERE chatId = #{chatId}")
  @sql.ReturnType(Chat)
  getChatById(@sql.Param("chatId") chatId: number): Promise<Chat> {
    return sql.PromiseNull();
  }

  @sql.SqlQuery(`
    SELECT * FROM chats
    WHERE lastMessageTimestamp = (
      SELECT MAX(lastMessageTimestamp)
      FROM chats
      WHERE lastMessageTimestamp <= #{timestamp}
    )
    ORDER BY lastMessageTimestamp DESC
  `)
  @sql.ReturnType([Chat])
  getAllChatsBeforeTimestamp(@sql.Param("timestamp") timestamp: number): Promise<Array<Chat>> {
    return sql.PromiseNull();
  }

  @sql.SqlQuery("SELECT * FROM chats WHERE pinned = 1")
  @sql.ReturnType([Chat])
  getAllPinnedChats(): Promise<Array<Chat>> {
    return sql.PromiseNull();
  }

  @sql.SqlQuery("SELECT * FROM chats WHERE pinned = 0 ORDER BY lastMessageTimestamp DESC")
  @sql.ReturnType([Chat])
  getAllUnpinnedChats(): Promise<Array<Chat>> {
    return sql.PromiseNull();
  }

  @sql.SqlQuery("SELECT * FROM chats")
  @sql.ReturnType([Chat])
  getAllChats(): Promise<Array<Chat>> {
    return sql.PromiseNull();
  }

  @sql.SqlQuery("SELECT COUNT(1) FROM chats WHERE chatId = #{chatId}")
  _checkIfChatExistsById(chatId: number): Promise<number> {
    return sql.PromiseNull();
  }

  async checkIfChatExistsById(chatId: number): Promise<boolean> {
    return (await this._checkIfChatExistsById(chatId)) == 1;
  }
}

export const chatDao = new ChatDao();