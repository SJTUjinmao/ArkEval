import { Database } from '@ohos/base';

export class DatabaseConstants {
  static readonly CHATS_TABLE_NAME = 'chats';
  static readonly MESSAGES_TABLE_NAME = 'messages';
  static readonly PACKED_CHATS_TABLE_NAME = 'packed_chats';
  static readonly RAW_MESSAGES_TABLE_NAME = 'raw_messages';
  static readonly SEEN_CHATS_TABLE_NAME = 'seen_chats';
  static readonly CREATE_CHATS_TABLE = `CREATE TABLE IF NOT EXISTS ${DatabaseConstants.CHATS_TABLE_NAME} (
    chatId INTEGER,
    chatType INTEGER NOT NULL,
    name TEXT NOT NULL,
    pinned BOOLEAN DEFAULT FALSE,
    lastMessageId INTEGER,
    lastMessageText TEXT,
    lastMessageTimestamp INTEGER,
  	PRIMARY KEY (chatId)
);`;
  static readonly CREATE_RAW_CHATS_TABLE = `CREATE TABLE IF NOT EXISTS ${DatabaseConstants.PACKED_CHATS_TABLE_NAME} (
    chatId INTEGER NOT NULL,
    rawChat BLOB NOT NULL,
  	PRIMARY KEY (chatId)
);`;
  static readonly CREATE_MESSAGES_TABLE = `CREATE TABLE IF NOT EXISTS ${DatabaseConstants.MESSAGES_TABLE_NAME} (
    chatId INTEGER NOT NULL,
    messageId INTEGER NOT NULL,
    outgoing BOOLEAN NOT NULL,
    pinned BOOLEAN DEFAULT FALSE,
    senderId INTEGER NOT NULL,
    senderName TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    text TEXT NOT NULL,
  	PRIMARY KEY (chatId, messageId),
    FOREIGN KEY (chatId) REFERENCES chats(chatId) DEFERRABLE INITIALLY DEFERRED
);`;
  static readonly CREATE_RAW_MESSAGES_TABLE = `CREATE TABLE IF NOT EXISTS ${DatabaseConstants.RAW_MESSAGES_TABLE_NAME} (
    chatId INTEGER NOT NULL,
    messageId INTEGER NOT NULL,
    rawMessage BLOB NOT NULL,
  	PRIMARY KEY (chatId, messageId),
    FOREIGN KEY (chatId) REFERENCES chats(chatId) DEFERRABLE INITIALLY DEFERRED
);`;
  static readonly CREATE_INDEXES = [
    `CREATE INDEX IF NOT EXISTS idx_chats_chatId ON ${DatabaseConstants.CHATS_TABLE_NAME} (chatId);`,
    `CREATE INDEX IF NOT EXISTS idx_name ON ${DatabaseConstants.CHATS_TABLE_NAME} (name);`,
    `CREATE INDEX IF NOT EXISTS idx_messages_messageId ON ${DatabaseConstants.MESSAGES_TABLE_NAME} (messageId);`,
    `CREATE INDEX IF NOT EXISTS idx_messages_chatId ON ${DatabaseConstants.MESSAGES_TABLE_NAME} (chatId);`,
    `CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON ${DatabaseConstants.MESSAGES_TABLE_NAME} (timestamp);`
  ];
  static readonly DROP_CHAT_TABLE = `DROP TABLE IF EXISTS ${DatabaseConstants.CHATS_TABLE_NAME};`;
  static readonly DROP_MESSAGES_TABLE = `DROP TABLE IF EXISTS ${DatabaseConstants.MESSAGES_TABLE_NAME};`;
  static readonly DROP_PACKED_CHATS_TABLE = `DROP TABLE IF EXISTS ${DatabaseConstants.PACKED_CHATS_TABLE_NAME};`;
  static readonly DROP_RAW_MESSAGES_TABLE = `DROP TABLE IF EXISTS ${DatabaseConstants.RAW_MESSAGES_TABLE_NAME};`;
  // static readonly INSERT_CHATS_FORMAT =
  //   "INSERT INTO chats(chatId, chatType, name, pinned, lastMessageId, lastMessageText, lastMessageTimestamp) VALUES {};";
  // static readonly INSERT_CHATS_OR_UPDATE_FORMAT =
  //   "INSERT INTO chats(chatId, chatType, name, pinned, lastMessageId, lastMessageText, lastMessageTimestamp) VALUES {} ON CONFLICT(chatId) DO UPDATE SET lastMessageId = excluded.lastMessageId, lastMessageText = excluded.lastMessageText, lastMessageTimestamp = excluded.lastMessageTimestamp;";
  // static readonly INSERT_MESSAGES_FORMAT =
  //   "INSERT INTO messages(chatId, messageId, outgoing, pinned, senderId, senderName, timestamp, text) VALUES {};";
  // static readonly INSERT_MESSAGES_OR_UPDATE_FORMAT =
  //   "INSERT INTO messages(chatId, messageId, outgoing, pinned, senderId, senderName, timestamp, text) VALUES {} ON CONFLICT(chatId, messageId) DO UPDATE SET outgoing = excluded.outgoing, pinned = excluded.pinned, senderId = excluded.senderId, senderName = excluded.senderName, timestamp = excluded.timestamp, text = excluded.text;";
  // static readonly INSERT_PACKED_CHAT_OR_UPDATE_FORMAT =
  //   "INSERT INTO raw_chats(chatId, rawChat) VALUES {} ON CONFLICT(chatId) DO UPDATE SET rawChat = excluded.rawChat;";
  // static readonly INSERT_RAW_MESSAGE_OR_UPDATE_FORMAT =
  //   "INSERT INTO raw_messages(chatId, messageId, rawMessage) VALUES {} ON CONFLICT(chatId, messageId) DO UPDATE SET rawMessage = excluded.rawMessage;";
  // static readonly GET_CACHED_CHATS =
  //   "SELECT chatId, chatType, name, pinned, lastMessageId, lastMessageText, lastMessageTimestamp FROM chats;";
  // static readonly GET_CACHED_CHAT_BY_ID =
  //   "SELECT chatId, chatType, name, pinned, lastMessageId, lastMessageText, lastMessageTimestamp FROM chats WHERE chatId = ?;";
  // static readonly GET_CACHED_PACKED_CHATS =
  //   "SELECT rawChat FROM raw_chats;";
  // static readonly GET_CACHED_PACKED_CHAT_BY_ID =
  //   "SELECT rawChat FROM raw_chats where chatId = ?;";
  // static readonly GET_CACHED_RAW_MESSAGES_FOR_CHAT =
  //   "SELECT rawMessage FROM raw_messages WHERE chatId = ?;";
  // static readonly CHECK_IF_CHAT_EXISTS_BY_ID = "SELECT 1 FROM chats WHERE chatId = ?;";
  // static readonly GET_CACHED_MESSAGES_FOR_CHAT =
  //   "SELECT * FROM messages WHERE chatId = ? ORDER BY timestamp ASC;";
  // static readonly GET_CACHED_MESSAGE_BY_ID =
  //   "SELECT * FROM messages WHERE messageId = ?;"; // TODO: sort by timestamp here?
  // static readonly UPDATE_CHAT_LAST_MESSAGE =
  //   "UPDATE chats SET lastMessageId = ?, lastMessageText = ?, lastMessageTimestamp = ? WHERE chatId = ?;";
  //
  // public static getInsertPackedChatOrUpdateSql(chatId: number, packedChat: Uint8Array): string {
  //   let values = `(${chatId}, '${packedChat}')`;
  //   return DatabaseConstants.INSERT_PACKED_CHAT_OR_UPDATE_FORMAT.replace('{}', values);
  // }
  //
  // public static getInsertRawMessagesSql(chatId: number, messageId: number[], rawMessages: Uint8Array[]): string {
  //   let values = rawMessages.map((rawMessage, index) => `(${chatId}, ${messageId[index]}, '${rawMessage}')`).join(',');
  //   return DatabaseConstants.INSERT_RAW_MESSAGE_OR_UPDATE_FORMAT.replace('{}', values);
  // }
  //
  // public static getInsertChatsOrUpdateSql(chats: Chat[]): string {
  //   let values =
  //     chats.map((chat) => `(${chat.chatId}, ${chat.chatType}, '${Database.escapeString(chat.name)}', ${chat.pinned}, ${chat.lastMessageId},
  //      '${Database.escapeString(chat.lastMessageText)}', ${chat.lastMessageTimestamp})`).join(',');
  //   return DatabaseConstants.INSERT_CHATS_OR_UPDATE_FORMAT.replace('{}', values);
  // }
  //
  // public static getInsertMessagesOrUpdateSql(messages: Message[]): string {
  //   let values =
  //     messages.map((message) => `(${message.chatId}, ${message.messageId}, ${message.outgoing}, ${message.pinned}, ${message.senderId}, '${Database.escapeString(message.senderName)}', ${message.timestamp}, '${Database.escapeString(message.text)}')`)
  //       .join(',');
  //   return DatabaseConstants.INSERT_MESSAGES_OR_UPDATE_FORMAT.replace('{}', values);
  // }
}