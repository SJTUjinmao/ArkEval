import sql from "@liushengyi/smartdb"
import { Message } from "../entities/Message"
import { DatabaseConstants } from "../constants/DatabaseConstants"
import { escapeString } from "@ohos/base"

class MessageDao {
  constructor() {
  }

  @sql.Sql(`CREATE TABLE IF NOT EXISTS ${DatabaseConstants.MESSAGES_TABLE_NAME} (
    chatId INTEGER NOT NULL,
    messageId INTEGER NOT NULL,
    outgoing BOOLEAN NOT NULL,
    pinned BOOLEAN DEFAULT FALSE,
    senderId INTEGER NOT NULL,
    senderName TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    text TEXT NOT NULL,
    mediaType INTEGER,
    editTimestamp INTEGER,
    groupedId INTEGER,
    replyToMessageId INTEGER,
  	PRIMARY KEY (chatId, messageId),
    FOREIGN KEY (chatId) REFERENCES chats(chatId) DEFERRABLE INITIALLY DEFERRED
    FOREIGN KEY (senderId) REFERENCES seen_chats(chatId) DEFERRABLE INITIALLY DEFERRED
    FOREIGN KEY (replyToMessageId) REFERENCES messages(messageId) DEFERRABLE INITIALLY DEFERRED
)`)
  createMessagesTable(): Promise<void> {
    return sql.PromiseNull();
  }

  @sql.Sql(`DROP TABLE IF EXISTS ${DatabaseConstants.MESSAGES_TABLE_NAME}`)
  deleteMessagesTable(): Promise<void> {
    return sql.PromiseNull();
  }

  @sql.SqlInsert("REPLACE INTO messages (chatId, messageId, outgoing, pinned, senderId, senderName, timestamp, text, mediaType) " +
    "VALUES (#{chatId}, #{messageId}, #{outgoing}, #{pinned}, #{senderId}, #{senderName}, #{timestamp}, #{text}, #{mediaType}," +
    " #{editTimestamp}, #{groupedId}, #{replyToMessageId})")
  insert(@sql.Param("chatId") chatId: number, @sql.Param("messageId") messageId: number,
    @sql.Param("outgoing") outgoing: boolean,
    @sql.Param("pinned") pinned: boolean, @sql.Param("senderId") senderId: number,
    @sql.Param("senderName") senderName: string,
    @sql.Param("timestamp") timestamp: number, @sql.Param("text") text: string,
    @sql.Param("mediaType") mediaType: number,
    @sql.Param("editTimestamp") editTimestamp: number,
    @sql.Param("groupedId") groupedId: number,
    @sql.Param("replyToMessageId") replyToMessageId: number): Promise<void> {
    return sql.PromiseNull();
  }

  @sql.SqlInsert("REPLACE INTO messages (chatId, messageId, outgoing, pinned, senderId, senderName, timestamp, text, " +
    "mediaType, editTimestamp, groupedId, replyToMessageId) " +
    "VALUES (#{message.chatId}, #{message.messageId}, #{message.outgoing}, #{message.pinned}, #{message.senderId}," +
    "#{message.senderName}, #{message.timestamp}, #{message.text}, #{message.mediaType}, " +
    "#{message.editTimestamp}, #{message.groupedId}, #{message.replyToMessageId})")
  insertMessage(@sql.Param("message") message: Message): Promise<void> {
    return sql.PromiseNull();
  }

  @sql.SqlInsert("REPLACE INTO messages (chatId, messageId, outgoing, pinned, senderId, senderName, timestamp, text, " +
    "mediaType, editTimestamp, groupedId, replyToMessageId) VALUES #{messagesSql}")
  _insertMessages(@sql.Param("messagesSql", true) messagesSql: string): Promise<void> {
    return sql.PromiseNull();
  }

  async insertMessages(messages: Array<Message>): Promise<void> {
    let messagesSql = messages.map((message) => {
      return `(${message.chatId}, ${message.messageId}, ${message.outgoing}, ${message.pinned}, ${message.senderId},
      "${escapeString(message.senderName)}", ${message.timestamp}, "${escapeString(message.text)}", ${message.mediaType},
      ${message.editTimestamp || 0}, ${message.groupedId || 0}, ${message.replyToMessageId || 0})`;
    }).join(", ");
    return this._insertMessages(messagesSql);
  }

  @sql.SqlQuery("SELECT * FROM messages WHERE chatId = #{chatId} AND messageId = #{messageId}")
  @sql.ReturnType(Message)
  getMessageById(@sql.Param("chatId") chatId: number, @sql.Param("messageId") messageId: number): Promise<Message> {
    return sql.PromiseNull();
  }

  @sql.SqlQuery("SELECT * FROM messages WHERE chatId = #{chatId}")
  @sql.ReturnType([Message])
  getMessagesByChatId(@sql.Param("chatId") chatId: number): Promise<Array<Message>> {
    return sql.PromiseNull();
  }
}

export const messageDao = new MessageDao();