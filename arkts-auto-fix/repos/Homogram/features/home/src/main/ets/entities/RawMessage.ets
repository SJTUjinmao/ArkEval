import sql from "@liushengyi/smartdb"
import { NativeRawMessage } from 'libhomogrape.so'

export class RawMessage implements NativeRawMessage {
  @sql.SqlColumn(sql.ColumnType.INTEGER)
  chatId: number;
  @sql.SqlColumn(sql.ColumnType.INTEGER)
  messageId: number;
  @sql.SqlColumn(sql.ColumnType.BLOB)
  rawMessage: Uint8Array;

  constructor(chatId: number, messageId: number, rawMessage: Uint8Array) {
    this.chatId = chatId;
    this.messageId = messageId;
    this.rawMessage = rawMessage;
  }

  public static fromNative(native: NativeRawMessage): RawMessage {
    return new RawMessage(native.chatId, native.messageId, new Uint8Array(native.rawMessage));
  }
}